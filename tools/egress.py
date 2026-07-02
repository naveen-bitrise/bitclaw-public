"""Public-repo GitHub-write egress approval registry.

The orchestrator holds internal context (private Slack threads, Jira tickets,
internal repos) and can write to GitHub. On a PUBLIC repo, a GitHub write is
the one channel an outside attacker can read — so it is the egress boundary
where an injected instruction (turn it into a harmful public action) or an
accidental insider leak (an internal detail pasted into a public PR comment)
actually materializes. Input redaction (the webhook gate + the PostToolUse
trust filter) can never be 100%; this gate moves the guarantee to the OUTPUT
side, where the blast radius is.

A PreToolUse hook (`tools.hooks.make_public_egress_gate`) parks every public-repo
GitHub write until a human approves it via a Slack button. This module is the
shared state that lets two parties cooperate without watching each other:

  * the hook (running inside an AgentLoop turn) PARKS a request and, on a later
    turn, checks whether the human approved it — both are pure registry ops;
  * the gateway (handling the Slack button click, out-of-band of the inbox, the
    same shape as the user-stop handler) flips a request to approved/rejected
    and enqueues a cue back into the owning session.

The registry is process-wide (one instance on the gateway) so a button click —
which carries only the request id — can resolve to its session without a
per-session lookup. State is in-memory/volatile: a restart just means the agent
re-issues the write and re-parks it (safe). Approval is bound to a CONTENT HASH,
not to "post to PR #42", so a human who approved text A can never have text B go
out under that approval — B has a different hash and re-parks.
"""
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Request lifecycle:
#   pending  — parked, waiting on a human; the hook denied the tool call
#   approved — a human clicked Approve; the next re-issue with a matching hash
#              is allowed (and the entry consumed/removed) by take_approved
#   rejected — a human clicked Reject; the gateway enqueues a rejection cue and
#              drops the entry
# consumed/rejected entries are removed, so only pending+approved ever linger.
_PENDING = "pending"
_APPROVED = "approved"

# Hard cap so a pathological run (approvals minted but never consumed, e.g. the
# agent died before re-issuing) can't grow the registry unbounded. Public writes
# are human-gated and rare, so this is never hit in practice; it's a backstop.
_MAX_ENTRIES = 1024


@dataclass
class EgressRequest:
    req_id: str
    session_id: int
    content_hash: str
    tool_name: str
    summary: str  # human-readable preview shown on the Slack approval message
    target: str  # e.g. "owner/repo#42" — for the approval message + logs
    status: str = _PENDING
    approver: str | None = None
    reason: str | None = None
    created_at: float = field(default_factory=time.monotonic)


class EgressApprovalRegistry:
    """In-memory store of pending/approved public-repo GitHub-write requests.

    All methods are synchronous and yield-free, so a check-and-consume
    (`take_approved`) is atomic under the single-threaded event loop — no two
    coroutines can interleave inside one call.
    """

    def __init__(self) -> None:
        self._by_id: "OrderedDict[str, EgressRequest]" = OrderedDict()

    def park(
        self, *, session_id: int, content_hash: str, tool_name: str,
        summary: str, target: str,
    ) -> tuple[EgressRequest, bool]:
        """Record a pending request, returning (request, created).

        Idempotent on (session_id, content_hash): if the agent re-issues the
        same denied call before the human acts, the existing pending request is
        returned with created=False, so the caller skips posting a second
        approval message. A new request returns created=True.
        """
        for r in self._by_id.values():
            if (
                r.session_id == session_id
                and r.content_hash == content_hash
                and r.status == _PENDING
            ):
                return r, False
        self._prune()
        req_id = "egr_" + secrets.token_urlsafe(9)
        req = EgressRequest(
            req_id=req_id, session_id=session_id, content_hash=content_hash,
            tool_name=tool_name, summary=summary, target=target,
        )
        self._by_id[req_id] = req
        return req, True

    def take_approved(self, *, session_id: int, content_hash: str) -> bool:
        """If an APPROVED request for this session+hash exists, consume (remove)
        it and return True; else False. This is the gate the hook applies on a
        re-issued call — a match means a human signed off on these exact bytes.

        Iterates over a snapshot (`list(...)`) so the in-loop `del` can never
        raise "OrderedDict mutated during iteration" — robust even if the
        early-return is ever refactored away."""
        for req_id, r in list(self._by_id.items()):
            if (
                r.session_id == session_id
                and r.content_hash == content_hash
                and r.status == _APPROVED
            ):
                del self._by_id[req_id]
                return True
        return False

    def forget(self, req_id: str) -> None:
        """Drop a request unconditionally (idempotent). Used when posting its
        Slack approval message failed: without this, the idempotent re-park
        (created=False on the same hash) would leave a buttonless, approval-less
        request stuck forever — fail-closed but a dead legitimate write. Dropping
        it lets the next re-issue re-park and re-post the buttons."""
        self._by_id.pop(req_id, None)

    def approve(self, req_id: str, approver: str) -> EgressRequest | None:
        """Flip a pending request to approved. Returns the request, or None if
        the id is unknown or already handled (double-click / expired)."""
        r = self._by_id.get(req_id)
        if r is None or r.status != _PENDING:
            return None
        r.status = _APPROVED
        r.approver = approver
        return r

    def reject(self, req_id: str, approver: str, reason: str | None = None) -> EgressRequest | None:
        """Reject and remove a pending request. Returns a snapshot (so the caller
        can build the rejection cue), or None if unknown/already handled."""
        r = self._by_id.get(req_id)
        if r is None or r.status != _PENDING:
            return None
        # No status flip: the entry is deleted immediately and the caller reads
        # only target/req_id off the returned snapshot, so a "rejected" status
        # would be dead state. approver/reason are kept on the snapshot for
        # logging fidelity.
        r.approver = approver
        r.reason = reason
        del self._by_id[req_id]
        return r

    def get(self, req_id: str) -> EgressRequest | None:
        return self._by_id.get(req_id)

    def _prune(self) -> None:
        """Drop the oldest entries if we've blown past the cap (backstop only)."""
        while len(self._by_id) >= _MAX_ENTRIES:
            old_id, old = self._by_id.popitem(last=False)
            logger.warning(
                "egress registry over %d entries — dropping oldest req %s "
                "(session %d, status=%s)",
                _MAX_ENTRIES, old_id, old.session_id, old.status,
            )

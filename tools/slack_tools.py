"""
Slack-facing tools for a single session.

post_message is lazy: a session may exist with no Slack thread yet (e.g.
bootstrapped silently from a Jira webhook). The first post_message call
creates a top-level message in the configured channel, captures the new
ts as the session's primary thread, registers the slack_thread anchor,
and updates the session row. Subsequent calls post in that thread.
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Annotated

from claude_agent_sdk import tool, create_sdk_mcp_server

from state import (
    attach_anchor,
    close_session as _close_session,
    list_anchors_for_session,
    slack_anchor_key,
    update_session,
)

logger = logging.getLogger(__name__)


# Max chars of proposed public-write content shown in an egress approval
# message (the full bytes are still what gets hashed/approved; this only bounds
# the Slack preview).
_APPROVAL_PREVIEW_MAX = 1500


def _parse_github_pr_key(key) -> tuple[str, str, int] | None:
    """Parse a `github_pr` anchor key 'owner/repo#42' → (owner, repo, 42).

    Returns None on any malformed key (the caller fails CLOSED — refuses to
    anchor what it can't parse). `rpartition('#')` so a '#' only ever splits the
    PR number off the end; GitHub owner/repo names contain no '#' or '/'."""
    if not isinstance(key, str) or "#" not in key:
        return None
    repo_full, _, num = key.rpartition("#")
    if not num.isdigit() or "/" not in repo_full:
        return None
    owner, _, repo = repo_full.partition("/")
    if not owner or not repo or "/" in repo:
        return None
    return owner, repo, int(num)


def _ok(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _err(message: str) -> dict:
    """A tool error the model sees and retries on (is_error → CallToolResult.isError)."""
    return {"content": [{"type": "text", "text": message}], "is_error": True}


@dataclass
class ThreadAnchor:
    """Per-session output-thread state — where this session posts in Slack.

    The single source of truth for "where do we post". The first post is
    serialised under `lock`, so even concurrent posters (an ordinary post
    racing an egress approval request) create exactly one top-level message.
    """
    channel: str
    ts: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Status text the model set before the Slack thread existed. The next
    # post_message that materializes the thread flushes this via
    # assistant.threads.setStatus, so the very first status the agent picks
    # (e.g. "reading the ticket") is actually shown instead of silently lost.
    pending_status: str | None = None
    # The most recent live status applied to the thread (by a post_message's
    # required `status`, the set_status tool, or a programmatic event status).
    # Remembered so the gateway can re-apply a default when an inbound event
    # lands with the indicator blank.
    last_status: str | None = None

    @property
    def is_set(self) -> bool:
        return self.ts is not None


class SlackTools:
    def __init__(
        self,
        slack_client,
        source_channel: str,
        source_thread_ts: str | None,
        session_id: int,
        anchor: ThreadAnchor,
        pr_trust_check=None,
    ):
        self.slack_client = slack_client
        # source_channel / source_thread_ts identify the *triggering* message
        # (used for reactions on it). The output channel + thread live on the
        # anchor, which is the single source of truth for where posts go.
        self.channel = source_channel
        self.thread_ts = source_thread_ts
        self.session_id = session_id
        self._anchor = anchor
        # Async `(owner, repo, number) -> bool|None` PR-author trust gate
        # (github_trust.default_pr_trust_check). Used by register_anchor to
        # HARD-REFUSE anchoring an external/untrusted-author github_pr. None (not
        # wired, e.g. in tests) → no gate. The gateway always wires it.
        self._pr_trust_check = pr_trust_check
        # Set once close_session runs, so a repeat call in the same session
        # instance neither re-posts the final message nor re-closes.
        self._closed = False

    @property
    def closed(self) -> bool:
        """True once close_session has run — no further Slack output may happen."""
        return self._closed

    def mark_reopened(self) -> None:
        """Reset the close latch — the gateway reopened this soft-closed session.

        The latch gates close_session's idempotency (left stale after a reopen,
        the NEXT legitimate close would no-op as 'already_closed': final message
        never posted, DB row left active) and agent_loop's end-of-turn
        live-status clear (a stale latch would wipe the status every turn).
        """
        self._closed = False

    @property
    def out_thread_ts(self) -> str | None:
        return self._anchor.ts if self._anchor.is_set else None

    def _has_thread(self) -> bool:
        # True once the Slack thread is materialized.
        return self.out_thread_ts is not None

    async def post_message(self, text: str, thread_ts: str | None = None) -> str:
        """Post to the session's Slack thread, lazily creating it if needed.

        The first post is serialised under `anchor.lock`, so exactly one
        top-level message is created even under concurrent posters.
        """
        # Fast path: anchor already materialized OR explicit thread_ts override.
        if thread_ts is not None or self._anchor.is_set:
            result = await self.slack_client.chat_postMessage(
                channel=self._anchor.channel,
                thread_ts=thread_ts or self._anchor.ts,
                text=text,
            )
            return result["ts"]

        # Slow path: exactly one caller bootstraps under the lock.
        async with self._anchor.lock:
            if self._anchor.is_set:
                # Lost the race — post into the now-bootstrapped thread.
                result = await self.slack_client.chat_postMessage(
                    channel=self._anchor.channel,
                    thread_ts=self._anchor.ts,
                    text=text,
                )
                return result["ts"]

            result = await self.slack_client.chat_postMessage(
                channel=self._anchor.channel,
                thread_ts=None,
                text=text,
            )
            ts = result["ts"]
            self._anchor.ts = ts
            await attach_anchor(
                self.session_id,
                "slack_thread",
                slack_anchor_key(self._anchor.channel, ts),
            )
            await update_session(
                self.session_id,
                slack_channel=self._anchor.channel,
                slack_thread_ts=ts,
            )
            logger.info(
                "Session %d bootstrapped Slack thread %s in %s",
                self.session_id, ts, self._anchor.channel,
            )
            # Flush a status the agent set before the thread existed. The
            # post itself auto-clears any existing status, so applying here
            # (after the post) is what makes it visible until the next post.
            pending = self._anchor.pending_status
            if pending is not None:
                self._anchor.pending_status = None
                try:
                    await self.slack_client.assistant_threads_setStatus(
                        channel_id=self._anchor.channel,
                        thread_ts=ts,
                        status=pending,
                    )
                except Exception:
                    logger.warning(
                        "Failed to flush pending status %r on first post", pending,
                        exc_info=True,
                    )
            return ts

    async def post_approval_request(
        self, *, req_id: str, action_label: str, target_label: str,
        body_preview: str, header: str | None = None, fallback: str | None = None,
        approve_label: str = "Approve & post",
    ) -> str | None:
        """Post a public-repo egress approval prompt with Approve/Reject buttons.

        The Block Kit buttons carry `value=req_id` verbatim, so the gateway's
        `@app.action` handler resolves the click straight to the parked request
        — no prose to interpret, and the human reviews the EXACT text. The
        proposed content is shown in a `rich_text_preformatted` block (NOT a
        mrkdwn ```code fence```), so a body that itself contains ``` can't
        terminate the block early and hide what's really being approved. Posts
        into the session's output thread, materializing it via post_message in
        the rare case the agent writes to GitHub before any Slack post (so the
        thread's first-post side-effects — anchor registration, pending-status
        flush — all run). Best-effort: returns the message ts, or None on
        failure (the egress gate keeps the write blocked and re-posts on the
        next re-issue). Requires the Slack app's Interactivity feature so
        clicks are delivered (Socket Mode — no Request URL needed)."""
        preview = (body_preview or "").strip()
        if len(preview) > _APPROVAL_PREVIEW_MAX:
            preview = preview[:_APPROVAL_PREVIEW_MAX] + "\n…(truncated)"
        header = header or (
            f":lock: *Approval needed* — I want to {action_label} on "
            f"*{target_label}* (a PUBLIC repo). Approve to publish it, or reject."
        )
        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        ]
        # Show the literal proposed content in a rich_text_preformatted element —
        # it renders monospace and does NOT parse mrkdwn, so backticks/``` in the
        # body are shown verbatim (mrkdwn ```fences``` would let a body's own ```
        # close the block early). Shown whenever non-empty; the caller passes ""
        # (not a "(...)" placeholder) when there's no free text, so a real body
        # that starts with "(" — e.g. "(WIP) …" — is never hidden from review.
        if preview:
            blocks.append({
                "type": "rich_text",
                "elements": [{
                    "type": "rich_text_preformatted",
                    "elements": [{"type": "text", "text": preview}],
                }],
            })
        blocks.append({
            "type": "actions",
            "block_id": f"egress::{req_id}",
            "elements": [
                {"type": "button", "action_id": "egress_approve", "style": "primary",
                 "text": {"type": "plain_text", "text": approve_label},
                 "value": req_id},
                {"type": "button", "action_id": "egress_reject", "style": "danger",
                 "text": {"type": "plain_text", "text": "Reject"}, "value": req_id},
            ],
        })
        fallback = fallback or (
            f"Approval needed to {action_label} on {target_label} (public repo)."
        )
        try:
            # Materialize the thread via the normal post path if it doesn't exist
            # yet, so post_message's first-post side-effects run. Rare: the agent
            # almost always posts a plan/PR milestone before any write.
            if not self._has_thread():
                await self.post_message(fallback)
            result = await self.slack_client.chat_postMessage(
                channel=self._anchor.channel, thread_ts=self._anchor.ts,
                text=fallback, blocks=blocks,
            )
            return result["ts"]
        except Exception:
            logger.warning(
                "Failed to post egress approval request %s", req_id, exc_info=True,
            )
            return None

    async def set_live_status(self, status_text: str) -> None:
        """Apply a live assistant-thread status and remember it as `last_status`.

        The single funnel for setting status — used by the `set_status` tool,
        by `post_message` right after a post, and by the gateway when an event
        arrives. If the Slack thread isn't materialized yet, the status is
        stashed on the anchor (`pending_status`) and flushed by the first
        `post_message`. Best-effort: a Slack failure is logged, never raised,
        so it can't abort a turn.
        """
        # Record intent regardless of thread state. Empty string means "clear".
        self._anchor.last_status = status_text or None
        if not self._has_thread():
            self._anchor.pending_status = status_text
            return
        try:
            await self.slack_client.assistant_threads_setStatus(
                channel_id=self._anchor.channel,
                thread_ts=self._anchor.ts,
                status=status_text,
            )
        except Exception:
            logger.warning("Failed to set live status %r", status_text, exc_info=True)

    async def has_delivering_anchors(self) -> bool:
        """True if this session has an anchor that can deliver async external
        events — a GitHub PR, a Jira issue, or a live RDE.

        Drives the end-of-turn decision to KEEP the live status across a wait
        when the bot is parked on webhook-covered events (an open PR, a ticket),
        so the thread shows "awaiting review" instead of going blank. The
        `slack_thread` anchor (the output thread itself) doesn't count.
        Best-effort: on a DB error returns False, so end_turn falls back to
        clearing rather than wedging on the lookup.
        """
        try:
            anchors = await list_anchors_for_session(self.session_id)
        except Exception:
            logger.debug("has_delivering_anchors: anchor lookup failed", exc_info=True)
            return False
        return any(kind != "slack_thread" for kind, _ in anchors)

    async def clear_live_status(self) -> None:
        """Clear the assistant-thread status indicator if the thread exists.

        Called by the AgentLoop at end_turn so a status like "closing
        session" doesn't hang after the bot has actually finished working
        — only `post_message` auto-clears, so any non-post final action
        (e.g. `close_session`, the last `register_anchor`) would otherwise
        leave the cue stuck on the thread until Slack times it out. Also
        forgets `last_status`/`pending_status` so a stale status can't be
        resurrected later.
        """
        # Forget the remembered status even if there's no thread, so a session
        # that never materialized one can't carry a stale status into a rebuild.
        self._anchor.last_status = None
        self._anchor.pending_status = None
        if not self._has_thread():
            return
        try:
            await self.slack_client.assistant_threads_setStatus(
                channel_id=self._anchor.channel,
                thread_ts=self._anchor.ts,
                status="",
            )
        except Exception:
            logger.warning("Failed to clear live status at end_turn", exc_info=True)

    async def _github_pr_anchor_denial(self, key) -> str | None:
        """Return a denial message if this github_pr anchor must be REFUSED, else
        None (allowed).

        Refuses to anchor a PR in a repo NOT owned by the trusted org, a fork /
        cross-repo PR, OR a PR whose author can't be confirmed as a trusted org
        member — an external / fork / untrusted-author PR. This is a hard rule
        with NO opt-in (a human asking does not override it): bitclaw never
        takes an outside contribution into its autonomous workflow. bitclaw's
        own PRs (trusted-org repo, same-repo branch, author = GITHUB_BOT_LOGIN)
        and trusted org members'/app-bots' same-repo PRs in trusted-org repos
        pass. Fails CLOSED — an unparseable key, an outside-org repo, a fork,
        or an unconfirmable author is refused. If the trust check isn't wired
        (no checker, e.g. in tests) the gate is a no-op; the gateway always
        wires it in production."""
        if self._pr_trust_check is None:
            return None
        parsed = _parse_github_pr_key(key)
        if parsed is None:
            return (
                f"register_anchor refused: '{key}' is not a valid github_pr key "
                "(expected 'owner/repo#42'). Re-check the PR reference."
            )
        owner, repo, number = parsed
        try:
            trusted = await self._pr_trust_check(owner, repo, number)
        except Exception:
            logger.warning(
                "PR-author trust check raised for %s — refusing anchor (fail "
                "closed)", key, exc_info=True,
            )
            trusted = None
        if trusted is True:
            return None
        if trusted is False:
            logger.warning(
                "register_anchor DENIED for %s: external/untrusted PR (a fork, or "
                "an author who is not a trusted org member) — session %d",
                key, self.session_id,
            )
            return (
                f"register_anchor DENIED for PR `{key}`: it is an external / "
                "untrusted pull request — its repo is not owned by the trusted "
                "org, OR it comes from a fork (its head branch lives in a "
                "different repo), OR its author is NOT a confirmed member of the "
                "trusted org. bitclaw only takes on PRs in the org's own repos, "
                "pushed to the upstream repo (not a fork), by a trusted member. "
                "It does not anchor, work on, or act on external contributions: "
                "this is a hard security rule and it holds EVEN IF a human asks. "
                "Do NOT retry and do NOT try to work the PR another way. Tell the "
                "human that you can't take on an external/untrusted PR — they "
                "need to handle it themselves."
            )
        # None — couldn't determine (transient API error / fetch failed).
        logger.warning(
            "register_anchor refused for %s: PR-author trust unconfirmed "
            "(fail closed) — session %d", key, self.session_id,
        )
        return (
            f"register_anchor refused for PR `{key}`: could not verify the PR "
            "author's org membership (a transient GitHub API issue). The anchor "
            "is refused fail-closed. Retry ONCE; if it still fails, tell the "
            "human the trust check is failing — do not loop."
        )

    def build_mcp_server(self):
        @tool(
            "post_message",
            (
                "Post a message to the Slack thread. Always use this to communicate "
                "with the user — only when you have something useful to say (a "
                "question, a blocker, a milestone, completion). This is also the "
                "PRIMARY way your live status gets set — `status` is MANDATORY and "
                "must be non-empty: a short 1–4 word label for what you're doing NEXT "
                "(e.g. 'spinning up RDE', 'watching CI', 'reading the diff', 'wrapping "
                "up'). The post clears the indicator Slack-side; we re-apply `status` "
                "right after, so it stays visible through the slow work that follows, "
                "until your next post — you don't manage that ordering. There is no "
                "empty/blank option: a post with an empty status is rejected, so "
                "always carry a real status. You never need to clear it yourself — "
                "the indicator clears automatically when your turn ends or the session "
                "closes. If a human asks you to set or update your status, do it via "
                "the `status` of your next post here — don't call set_status and then "
                "post."
            ),
            {
                "text": Annotated[str, "The message to post to the Slack thread."],
                "status": Annotated[
                    str,
                    "MANDATORY, non-empty. Short 1–4 word live-status label for "
                    "what you're doing NEXT (e.g. 'watching CI', 'reading the "
                    "diff', 'wrapping up'). A blank status is rejected — always "
                    "pass a real one; you never clear it yourself.",
                ],
            },
        )
        async def post_message(args):
            status = (args.get("status") or "").strip()
            if not status:
                # `status` is MANDATORY — there must always be a live status
                # while the bot is working. Reject (don't post) so the agent
                # re-calls with a real label instead of blanking the indicator;
                # the common slip is set_status("X") then a post with an empty
                # status, which used to wipe "X". The indicator is cleared for
                # real only at end_turn / close_session, never by a post — so
                # the agent never needs an empty one.
                return _err(
                    "post_message requires a non-empty `status` (1–4 words for "
                    "what you're doing NEXT, e.g. 'watching CI', 'reading the "
                    "diff', 'wrapping up'). Re-call post_message with the same "
                    "text plus a status — you never need an empty one; the "
                    "indicator clears on its own when your turn ends or the "
                    "session closes."
                )
            ts = await self.post_message(args["text"])
            # Re-apply the agent's chosen status AFTER the post (the post itself
            # auto-cleared it Slack-side). This is what makes a live status
            # reliably present without the agent juggling set_status ordering.
            await self.set_live_status(status)
            return _ok({"ts": ts})

        @tool(
            "update_message",
            "Edit an existing Slack message by its ts. Use post_message first to get a ts, then update_message to stream progress without spamming new messages.",
            {"ts": str, "text": str},
        )
        async def update_message(args):
            await self.slack_client.chat_update(
                channel=self._anchor.channel,
                ts=args["ts"],
                text=args["text"],
            )
            return _ok({"status": "updated"})

        @tool(
            "register_anchor",
            (
                "Bind this session to an external entity so future events for that "
                "entity (webhooks, mentions) route back to this thread. Call this "
                "immediately after creating the entity. "
                "Supported kinds: 'github_pr' (key='owner/repo#42'), 'jira_issue' "
                "(key='PROJ-123'), 'rde_session' (key='<RDE session id>')."
            ),
            {"kind": str, "key": str},
        )
        async def register_anchor(args):
            kind = args["kind"]
            key = args["key"]
            # HARD gate: never anchor an external / untrusted-author github_pr.
            # No opt-in — not even on a human's request (bitclaw holds internal
            # context + privileged MCP access; it must not pull an outside
            # contribution into its autonomous workflow). See
            # _github_pr_anchor_denial.
            if kind == "github_pr":
                denial = await self._github_pr_anchor_denial(key)
                if denial is not None:
                    return _err(denial)
            await attach_anchor(self.session_id, kind, key)
            logger.info(
                "Registered anchor: %s:%s → session %d", kind, key, self.session_id
            )
            return _ok({"status": "registered", "kind": kind, "key": key})

        @tool(
            "set_status",
            (
                "Keep the live status indicator fresh BETWEEN posts. Call this "
                "ONLY when your activity changes but you're not posting a message — "
                "e.g. moving through 'reading the diff' → 'running tests' → "
                "'checking logs' across a stretch of tool calls. Doing so often is "
                "GOOD: it shows the user steady progress so a long silent stretch "
                "never reads as stuck. If you ARE about to post a message, do NOT "
                "call this first — put the status directly in that post_message's "
                "`status` field instead. (Calling set_status and then posting is the "
                "common mistake: the post_message is the authoritative status-setter, "
                "so route the status through it.) If the Slack thread doesn't exist "
                "yet, the status is stashed and applied when the first post_message "
                "creates it. Like post_message's `status`, this must be non-empty — "
                "a blank status is rejected; you never clear it yourself."
            ),
            {
                "status": Annotated[
                    str,
                    "MANDATORY, non-empty. Short 1–4 word label for your current "
                    "activity (e.g. 'running tests', 'checking logs'). A blank "
                    "status is rejected.",
                ],
            },
        )
        async def set_status(args):
            status_text = (args.get("status") or "").strip()
            if not status_text:
                return _err(
                    "set_status requires a non-empty status (1–4 words for your "
                    "current activity, e.g. 'running tests'). You never clear it "
                    "yourself — the indicator is cleared automatically when the "
                    "session closes."
                )
            # Routed through set_live_status so it records last_status and
            # handles the no-thread stash.
            await self.set_live_status(status_text)
            if not self._has_thread():
                return _ok({
                    "status": "pending",
                    "reason": "no_thread_yet",
                    "hint": "Stashed — will be applied when post_message creates the thread.",
                })
            return _ok({"status": "set"})

        @tool(
            "list_anchors",
            (
                "List the external-entity anchors registered against this Slack "
                "session. Returns [(kind, key), ...]. Use this to discover RDE "
                "session ids you created earlier in this thread (kind='rde_session') "
                "so you can clean them up at end of work."
            ),
            {},
        )
        async def list_anchors(args):
            anchors = await list_anchors_for_session(self.session_id)
            return _ok({"anchors": [{"kind": k, "key": v} for k, v in anchors]})

        @tool(
            "close_session",
            (
                "Finish and close this Slack session after the work is done "
                "(PR merged or abandoned, RDEs destroyed). TERMINAL — this is "
                "ALWAYS your LAST action. Pass your closing message as "
                "`final_message`: close_session posts it to the thread and marks "
                "the session done in ONE atomic step, so your last word always "
                "lands before the session goes away. Do NOT post the final "
                "message separately and then close — pass it here. After this "
                "returns, treat the thread's Slack connection as gone: never "
                "call post_message, update_message, or set_status again. Sets "
                "stage='done' so the bot won't auto-restore the session on "
                "restart; the Slack thread itself stays, and if a human later "
                "returns to it, THIS session is reopened — you'll get a "
                "message carrying a '[bitclaw session reopened: …]' cue (near "
                "the start, possibly after a refresh note or preamble), which "
                "means all Slack tools (this one included) work normally again "
                "in this same conversation. Idempotent: calling twice is a "
                "no-op (the message is posted only on the first call)."
            ),
            {
                "type": "object",
                "properties": {
                    "final_message": {
                        "type": "string",
                        "description": (
                            "Your final user-facing message, posted to the "
                            "Slack thread immediately before the session is "
                            "closed (e.g. 'Done — merged. RDE abc123 cleaned "
                            "up.'). Optional: omit only if you have already "
                            "said everything and have nothing left to add."
                        ),
                    },
                },
            },
        )
        async def close_session(args):
            if self._closed:
                return _ok({"status": "already_closed"})
            final_message = (args.get("final_message") or "").strip()
            if final_message:
                await self.post_message(final_message)
            await _close_session(self.session_id)
            self._closed = True
            logger.info(
                "Session %d marked done via close_session tool%s",
                self.session_id,
                " (posted final message)" if final_message else "",
            )
            return _ok({"status": "closed", "posted_final_message": bool(final_message)})

        return create_sdk_mcp_server(
            name="slack",
            version="1.0.0",
            tools=[
                post_message, update_message, register_anchor, set_status,
                list_anchors, close_session,
            ],
        )

    @staticmethod
    def tool_names() -> list[str]:
        return [
            "mcp__slack__post_message",
            "mcp__slack__update_message",
            "mcp__slack__register_anchor",
            "mcp__slack__set_status",
            "mcp__slack__list_anchors",
            "mcp__slack__close_session",
        ]

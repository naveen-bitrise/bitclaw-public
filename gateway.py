"""
SlackGateway maps external anchors (Slack threads, GitHub PRs, Jira issues)
to persistent Sessions and routes every inbound event into the owning
session's inbox.

Each Session owns one AgentLoop (a persistent Claude Agent SDK conversation)
and one asyncio worker task draining its inbox sequentially — sessions run in
parallel across the process; messages within a session are strictly ordered.
Every delivery edge converges on the _enqueue chokepoint, which owns the
done-session backstop gate, the inactivity clock, and the eviction-race retry.
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from agent_loop import AgentLoop
from github_trust import default_pr_trust_check
from mcp_manager import load_external_mcp_servers
from state import (
    claim_or_create_session,
    clear_sdk_session_id,
    close_session,
    get_session,
    get_session_stage,
    load_stale_active_sessions,
    lookup_anchor,
    reopen_session,
    slack_anchor_key,
    touch_session,
    update_session,
)
from tools.egress import EgressApprovalRegistry
from tools.scheduling import SchedulingTools
from tools.slack_tools import SlackTools, ThreadAnchor

logger = logging.getLogger(__name__)

# Coarse live status applied at turn start when an inbound event carries no
# event-specific cue and nothing is remembered — so a turn is never worked on
# with a blank indicator. The agent's first post/set_status refines it.
DEFAULT_LIVE_STATUS = "working"

# Idle TTL before in-memory eviction; the DB row stays 'active', so the next
# inbound event rebuilds the session (reversible, unlike auto-close below).
SESSION_IDLE_TTL_SECS = int(os.environ.get("SESSION_IDLE_TTL_SECS", "3600"))
SESSION_EVICTION_CHECK_SECS = int(os.environ.get("SESSION_EVICTION_CHECK_SECS", "300"))

# User stop: after the graceful SDK interrupt is sent, how long
# stop_session_work waits for the in-flight turn to actually end before
# escalating to eviction (worker cancel + client disconnect). Long enough to
# cover a client mid-cold-boot, short enough that "stop" stays prompt even
# against a wedged CLI.
STOP_GRACE_SECS = float(os.environ.get("STOP_GRACE_SECS", "15"))

# Inactivity-based auto-close: a session with no inbound event (human message,
# webhook/anchor delivery, or scheduler fire — anything routed through
# _enqueue, which bumps updated_at) for this many days is handed a cleanup cue
# so the agent tears down its dev environments and marks the DB row done.
# Unlike in-memory eviction this is durable: without it, sessions the agent
# never explicitly closes stay 'active' forever and get restored on every
# restart — an unbounded-row leak.
SESSION_AUTOCLOSE_IDLE_DAYS = int(os.environ.get("SESSION_AUTOCLOSE_IDLE_DAYS", "7"))
SESSION_AUTOCLOSE_CHECK_SECS = int(os.environ.get("SESSION_AUTOCLOSE_CHECK_SECS", "3600"))

# Pre-mention thread history: when the bot is first @mentioned as a REPLY in an
# already-existing thread, the agent would otherwise see only the mention line —
# none of the conversation that led up to it. conversations.replies returns
# messages OLDEST-FIRST, so the fetch must paginate to the end of the thread and
# keep the TAIL — the newest messages are the ones that led to the mention.
_THREAD_HISTORY_MAX_MESSAGES = 50
_THREAD_HISTORY_MAX_CHARS = 8000
_THREAD_HISTORY_MAX_PAGES = 5  # 5 × 200 = up to 1000 messages scanned


def _inactivity_autoclose_msg(days: int) -> str:
    """Synthetic cue enqueued into a stale session's inbox.

    Self-contained (full tool names, exact order) so it works even if the SDK
    conversation file was lost and the agent has no history to lean on.
    """
    return (
        f"[bitclaw inactivity cleanup cue: this session has had no human "
        f"message or anchor/webhook event for {days} days and is being "
        f"auto-closed to reclaim resources. Do this now, in order: "
        f"(1) post a brief, friendly Slack note to this thread saying the "
        f"session is being closed due to inactivity and any dev environments "
        f"are being cleaned up; (2) call mcp__slack__list_anchors() and for "
        f"every rde_session anchor: "
        f"mcp__infra__bitrise_devenv_terminate(session_id=<key>), then poll "
        f"mcp__infra__bitrise_devenv_get(session_id=<key>) until status is "
        f"SESSION_STATUS_TERMINATED (terminate is async — delete fails until "
        f"it finishes), then mcp__infra__bitrise_devenv_delete(session_id="
        f"<key>) (treat 'already terminated' / 404 / 'not found' as "
        f"success); (3) call "
        f"mcp__slack__close_session(final_message='Done — closed (inactive "
        f"{days}d)') — it posts that final milestone AND closes in one "
        f"atomic step (do not post it separately first, and never post after "
        f"close). If there are no rde_session anchors, skip step 2.]"
    )


async def _maybe_notify_start_failure(session: "Session", slack_client) -> None:
    """Best-effort 'session failed to start' post; silent if no thread exists yet."""
    if not session.slack_thread_ts:
        return
    try:
        await slack_client.chat_postMessage(
            channel=session.slack_channel,
            thread_ts=session.slack_thread_ts,
            text=(
                "Couldn't start the session — please retry by sending "
                "another message. (Check the bot logs for details.)"
            ),
        )
    except Exception:
        logger.debug("Could not post start-failure notice", exc_info=True)


def resolve_jira_channel() -> str | None:
    """Return the Slack channel that hosts Jira-bootstrapped sessions."""
    return os.environ.get("JIRA_DEFAULT_CHANNEL") or None


# Marker shared by every reopen edge (the delivery gates, _enqueue's cold path,
# and the worker's drain-time gate) to detect a message that already carries
# the cue — a message racing through two gates must never collect two cues.
_REOPEN_CUE_PREFIX = "[bitclaw session reopened:"


def _reopen_cue(how: str) -> str:
    """Prefix for the message that reopens a soft-closed session.

    The resumed conversation's history ends with the agent's own close_session
    call, and its instructions declare a closed thread severed — without an
    explicit counter-signal the model refuses to act on the delivered message
    (it has no way to know the gateway flipped stage back to 'active'; this
    shows up as "my session is already closed, I can't post" — posted *to the
    thread*). The system prompt teaches that this cue, and only this cue,
    un-severs the thread. It also carries the re-close contract so a reopened
    session doesn't linger open until the inactivity sweeper.
    """
    return (
        f"{_REOPEN_CUE_PREFIX} {how} after you closed this session, so "
        "the close has been undone — the session is ACTIVE again and every "
        "Slack tool (post_message, update_message, set_status, close_session) "
        "works normally. This is the SAME conversation continuing, not a fresh "
        "session. Handle the message below, then RE-CLOSE — a reopened session "
        "must not linger open: if the message needs no real work (a thank-you, "
        "an acknowledgement, an FYI), put your brief reply in close_session's "
        "final_message and close again in this SAME turn; if it asks for real "
        "work, do it and close again at the terminal milestone as usual; if "
        "you cannot tell whether more is coming, ask, and schedule a self-check "
        "(mcp__sched__schedule_message, a few hours out) whose message tells "
        "you to close the session if no reply has arrived — cancelling it via "
        "mcp__sched__cancel_scheduled_task if the human does reply first.]"
    )


@dataclass
class Session:
    id: int
    slack_channel: str
    slack_thread_ts: str | None  # may be None until the agent's first post
    loop: AgentLoop
    scheduling_tools: SchedulingTools | None = None
    # Shared output-thread state (lazy thread ts, live-status memory).
    anchor: ThreadAnchor | None = None
    # The SlackTools instance — exposed so the gateway can read the close
    # latch (drain-time gate, evict-on-close) and reset it on reopen.
    slack_tools: SlackTools | None = None
    inbox: asyncio.Queue = field(default_factory=asyncio.Queue)  # items: (text, is_human)
    worker_task: asyncio.Task | None = None
    # Heartbeat + active-turn flag drive the idle-TTL eviction sweeper.
    last_activity: float = field(default_factory=time.monotonic)
    turn_in_progress: bool = False
    # Monotonic turn counter, bumped alongside turn_in_progress=True. A user
    # stop snapshots it to identify WHICH turn it is killing: turn_in_progress
    # alone is ambiguous — its False→True transition between turns has zero
    # event-loop yields when the inbox is non-empty, so a grace-wait polling
    # the boolean can mistake a follow-up's fresh turn for the stopped one
    # (and escalate into it, destroying work the user did NOT target).
    turn_generation: int = 0


class SlackGateway:
    def __init__(self):
        self.sessions: dict[int, Session] = {}
        self._external_mcp_servers = load_external_mcp_servers()
        # Process-wide registry of public-repo GitHub-write approval requests.
        # The per-session egress hook (in each AgentLoop) parks/consumes here;
        # the Slack-button handler (approve_egress/reject_egress) resolves a
        # click's request id to its owning session and enqueues the cue.
        self._egress = EgressApprovalRegistry()

    # ------------------------------------------------------------------
    # Public delivery API
    # ------------------------------------------------------------------

    async def start_or_resume_slack_thread(
        self,
        channel: str,
        thread_ts: str,
        text: str,
        slack_client,
        *,
        trigger_ts: str | None = None,
    ) -> int:
        """Slack-mention entry point: ensure a session exists for this thread, then enqueue.

        `trigger_ts` is the @mention message's own ts, passed ONLY when the
        mention arrived as a reply inside an already-existing thread. On the
        very first mention of such a thread (created=True) we pull the messages
        that preceded the mention and prepend them as context, so the agent
        starts with the conversation that led up to it rather than just the
        mention line. Threads we already own (created=False) have that history
        from prior turns, and a top-level mention starting a fresh thread has
        nothing before it — both skip the fetch.
        """
        key = slack_anchor_key(channel, thread_ts)
        session_id, created = await claim_or_create_session(
            "slack_thread", key,
            slack_channel=channel, slack_thread_ts=thread_ts,
        )
        if created:
            logger.info("Created session %d for slack thread %s", session_id, key)
            if trigger_ts is not None:
                history = await self._fetch_thread_history(
                    slack_client, channel, thread_ts, exclude_ts=trigger_ts,
                )
                if history:
                    text = (
                        f"[Existing Slack thread history — oldest first]\n"
                        f"{history}\n"
                        f"[End of prior thread history. The mention follows.]\n\n"
                        f"{text}"
                    )
        elif await reopen_session(session_id):
            # An @mention in a thread whose session was soft-closed is a human
            # asking to continue — claim_or_create_session hands back the done
            # row (the slack_thread anchor outlives the close), so flip it back
            # to active before enqueueing. The WHERE stage='done' guard in
            # reopen_session makes this a no-op for already-active sessions;
            # --resume on the kept sdk_session_id rehydrates the conversation.
            logger.info("Reopened done session %d on @mention in %s", session_id, key)
            self._mark_reopened_in_memory(session_id)
            text = _reopen_cue("a human @-mentioned you in this thread") + "\n\n" + text

        await self._enqueue(session_id, text, slack_client, is_human=True)
        return session_id

    async def _fetch_thread_history(
        self,
        slack_client,
        channel: str,
        thread_ts: str,
        *,
        exclude_ts: str,
    ) -> str | None:
        """Return a formatted transcript of a thread's earlier messages, or None.

        Best-effort: any failure (missing history scope, bot not in channel,
        API error) degrades to None so the turn proceeds without history rather
        than blocking the mention. Slack pages the thread oldest-first, so this
        walks the cursor to the end (bounded by _THREAD_HISTORY_MAX_PAGES) and
        the tail-keeping below then really keeps the NEWEST messages; a thread
        longer than the page budget loses its oldest messages, which is the
        right end to lose.
        """
        messages: list[dict] = []
        try:
            cursor = None
            for _page in range(_THREAD_HISTORY_MAX_PAGES):
                kwargs = {"channel": channel, "ts": thread_ts, "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = await slack_client.conversations_replies(**kwargs)
                messages.extend(resp.get("messages") or [])
                cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
            else:
                logger.warning(
                    "Thread %s:%s longer than the history page budget — "
                    "keeping the newest %d pages' worth",
                    channel, thread_ts, _THREAD_HISTORY_MAX_PAGES,
                )
        except Exception:
            logger.warning(
                "Could not fetch thread history for %s:%s — proceeding without it",
                channel, thread_ts, exc_info=True,
            )
            return None

        lines: list[str] = []
        for m in messages:
            if m.get("ts") == exclude_ts:
                continue  # the @mention itself — already delivered as the user message
            # Skip Slack-internal subtypes (joins, edits, ...). bot_message is
            # the exception: its text is real content.
            if m.get("subtype") and m.get("subtype") != "bot_message":
                continue
            body = (m.get("text") or "").strip()
            if not body:
                continue
            if m.get("user"):
                # <@U…> is unambiguous and renders as the name if ever echoed.
                lines.append(f"<@{m['user']}>: {body}")
            else:
                lines.append(f"{m.get('username') or m.get('bot_id') or 'unknown'}: {body}")

        if not lines:
            return None
        # Newest messages are the most relevant — keep the tail, capped by
        # message count first, then by characters.
        trimmed = len(lines) > _THREAD_HISTORY_MAX_MESSAGES
        lines = lines[-_THREAD_HISTORY_MAX_MESSAGES:]
        transcript = "\n".join(lines)
        if len(transcript) > _THREAD_HISTORY_MAX_CHARS:
            trimmed = True
            transcript = transcript[-_THREAD_HISTORY_MAX_CHARS:]
        if trimmed:
            transcript = "…(earlier messages trimmed)…\n" + transcript
        return transcript

    async def deliver_to_anchor(
        self,
        kind: str,
        key: str,
        text: str,
        slack_client,
        *,
        is_human: bool = False,
        status_hint: str | None = None,
    ) -> bool:
        """Deliver to the session bound to (kind, key). Returns False if unrouted.

        close_session is a SOFT close (stage='done', row kept), so anchors keep
        resolving forever. is_human is the resurrection discriminator: automated
        events on a done session (a post-merge PR comment, CI on a merged PR, a
        transition on a Done ticket) are noise — dropping them avoids a full
        SDK/MCP cold-start plus a paid LLM turn posting into a thread the agent
        declared severed. A human reply is genuine intent to continue, so it
        reopens the session instead; sdk_session_id survives the soft close, so
        --resume rehydrates the same conversation with full history.

        The gate must live HERE, not only in _enqueue's rebuild branch: a done
        session still in memory (inside the eviction window) skips the rebuild
        and would deliver straight to the live worker.
        """
        session_id = await lookup_anchor(kind, key)
        if session_id is None:
            logger.info("No session bound to %s:%s — dropping", kind, key)
            return False
        stage = await get_session_stage(session_id)
        if stage is None:
            logger.error(
                "Session %d bound to %s:%s has no DB row — dropping",
                session_id, kind, key,
            )
            return False
        if stage == "done":
            if not is_human:
                logger.info(
                    "Dropping %s:%s — owning session %d is done",
                    kind, key, session_id,
                )
                return False
            if await reopen_session(session_id):
                logger.info(
                    "Reopened done session %d on human reply (%s:%s)",
                    session_id, kind, key,
                )
                self._mark_reopened_in_memory(session_id)
                text = _reopen_cue("a human returned to this conversation") + "\n\n" + text
            # else: a racing human reply won the flip and already carried the
            # cue + reset the latch — deliver this one bare, no second cue.
        await self._enqueue(
            session_id, text, slack_client,
            is_human=is_human, status_hint=status_hint,
        )
        return True

    def _mark_reopened_in_memory(self, session_id: int) -> None:
        """Reset the in-memory close latch when a done session is reopened.

        A session reopened minutes after closing is usually still inside the
        eviction window, so the SAME SlackTools instance — close latch set —
        keeps serving it. The latch gates close_session's idempotency and the
        end-of-turn live-status clear: left stale, the NEXT legitimate
        close_session would no-op as 'already_closed' (final message never
        posted, DB row left active) and every turn-end would wipe live status.
        The cold path (session evicted) needs nothing: the rebuild constructs
        a fresh SlackTools with the latch unset.
        """
        session = self.sessions.get(session_id)
        if session is not None and session.slack_tools is not None:
            session.slack_tools.mark_reopened()

    async def deliver_to_session(
        self,
        session_id: int,
        text: str,
        slack_client,
        *,
        is_human: bool = False,
    ) -> None:
        """Deliver a message into a session's inbox by session id.

        The entry point for callers that already hold a session id (startup
        recovery firing past-due scheduled rows); anchor-keyed events should
        use deliver_to_anchor. The done-session gate is the CALLER's job here —
        recovery re-checks stage right before calling; _enqueue's cold-path
        backstop covers misses.
        """
        await self._enqueue(session_id, text, slack_client, is_human=is_human)

    # ------------------------------------------------------------------
    # Out-of-band user stop
    # ------------------------------------------------------------------

    async def stop_session_work(self, session_id: int) -> dict:
        """Halt this session's in-flight work NOW, bypassing the inbox.

        A "stop" delivered through the inbox would wait behind the very turn it
        is meant to kill (the worker drains sequentially), so this bypasses the
        queue entirely: discard pending inbox items, cancel scheduled
        self-wake-ups (an armed poll timer would otherwise restart the work
        minutes later), gracefully interrupt the in-flight SDK turn, and — if
        the turn doesn't end within STOP_GRACE_SECS — escalate to eviction
        (worker cancel + client disconnect; the DB row stays active, so the
        next message rebuilds via --resume with history intact). The session is
        NOT closed: anchors keep delivering and the human continues by replying.

        The stop targets exactly ONE turn — the one running when it was issued,
        identified by a turn_generation snapshot taken before the first await.
        Work that starts afterwards (the user's own follow-up message is the
        canonical case) is never interrupted, escalated into, or status-wiped.

        Two guards close the dying turn's re-arm window (the sched MCP server
        is in-process, so the turn can still call schedule_message until it
        actually dies): schedule_message is latched (rejected) for the duration
        of the stop, and a second cancel sweep runs once the turn has ended.

        Returns {"disposition": "idle" | "interrupted" | "forced",
                 "flushed": n, "cancelled_wakeups": n} for the caller's ack.
        """
        session = self.sessions.get(session_id)
        if session is None:
            # Not in memory ⇒ nothing queued and nothing running. Don't
            # rebuild a full SDK stack just to report that.
            return {"disposition": "idle", "flushed": 0, "cancelled_wakeups": 0}

        # --- Synchronous snapshot: no awaits from the lookup above through
        # this block, so the flush, the generation snapshot and the schedule
        # latch all describe the same instant — the state the user's "stop"
        # was aimed at. The worker processes ONE inbox item per turn, so the
        # flush covers everything not already running: the only item beyond
        # the queue is the one turn in flight, which the interrupt targets.
        flushed = 0
        while True:
            try:
                session.inbox.get_nowait()
                flushed += 1
            except asyncio.QueueEmpty:
                break
        target_gen = session.turn_generation
        was_running = session.turn_in_progress
        sched = session.scheduling_tools
        if sched is not None:
            sched.begin_stop()
        # --- End of synchronous snapshot.

        def _target_turn_over() -> bool:
            # Generation-keyed, not boolean-keyed: turn_in_progress can flip
            # False→True for a follow-up's turn with zero event-loop yields in
            # between, so polling the boolean alone could mistake fresh work
            # for the stopped turn and escalate eviction into it.
            return (
                not session.turn_in_progress
                or session.turn_generation != target_gen
            )

        try:
            # A stop is human activity — keep the inactivity clock honest.
            try:
                await touch_session(session_id)
            except Exception:
                logger.exception("stop: touch_session failed for session %d", session_id)

            # cancel_pending is sync (no yield); the DB rows flip via each
            # cancelled task's own CancelledError handler, same as the
            # evict-on-close path.
            cancelled = sched.cancel_pending() if sched is not None else 0

            if not was_running:
                logger.info(
                    "Stop for session %d: no turn in flight (flushed=%d, cancelled=%d)",
                    session_id, flushed, cancelled,
                )
                return {
                    "disposition": "idle",
                    "flushed": flushed,
                    "cancelled_wakeups": cancelled,
                }

            # The awaits above yielded: the targeted turn may have ended on
            # its own (and a follow-up's fresh turn may already be running).
            # Interrupt only while the SAME generation is still in flight.
            acked = False
            if not _target_turn_over():
                # Deadline computed BEFORE the interrupt round-trip, so its
                # ack timeout eats into the grace window instead of stacking
                # on top — bounds the wait at STOP_GRACE_SECS total.
                deadline = time.monotonic() + STOP_GRACE_SECS
                acked = await session.loop.interrupt()
                # Arm the one-shot memo NOW (idempotent): if the turn unwinds
                # and a follow-up starts before our next poll, that turn still
                # learns the previous one was cut off.
                session.loop.note_user_stop()
                while not _target_turn_over() and time.monotonic() < deadline:
                    await asyncio.sleep(0.2)
            else:
                # Ended naturally between snapshot and here; the queued work
                # was still discarded, so the memo still applies.
                session.loop.note_user_stop()

            if not _target_turn_over():
                # The CLI never unwound (wedged tool call, dead transport).
                # Same teardown as idle-TTL eviction: cancel the worker; its
                # finally disconnects the client. sdk_session_id survives, so
                # the next message resumes the conversation with history. The
                # targeted generation is still the one running, so this can't
                # land on a follow-up's turn.
                logger.warning(
                    "Stop for session %d: turn still running %.0fs after interrupt "
                    "(acked=%s) — escalating to eviction",
                    session_id, STOP_GRACE_SECS, acked,
                )
                slack_tools = session.slack_tools
                await self._evict_session(session_id, reason="user stop escalation")
                # Eviction cancels only the worker — pending timers are plain
                # asyncio tasks that would survive, fire, and resurrect the
                # halted work via _scheduler_enqueue with no stop context.
                if sched is not None:
                    cancelled += sched.cancel_pending()
                if slack_tools is not None:
                    try:
                        await slack_tools.clear_live_status()
                    except Exception:
                        logger.debug("stop: could not clear live status", exc_info=True)
                return {
                    "disposition": "forced",
                    "flushed": flushed,
                    "cancelled_wakeups": cancelled,
                }

            # The targeted turn is gone. Second sweep: a schedule_message call
            # already past the latch check at stop time can have landed a task
            # after the first sweep ran — without this, the very work the user
            # stopped restarts itself minutes later.
            if sched is not None:
                cancelled += sched.cancel_pending()
            # Drop the stale live status ("watching CI", …) — unless a fresh
            # turn (the user's follow-up) is already running and owns it now.
            fresh_turn_running = (
                session.turn_in_progress and session.turn_generation != target_gen
            )
            if session.slack_tools is not None and not fresh_turn_running:
                try:
                    await session.slack_tools.clear_live_status()
                except Exception:
                    logger.debug("stop: could not clear live status", exc_info=True)
            logger.info(
                "Stop for session %d: turn stopped (acked=%s, flushed=%d, "
                "cancelled=%d, follow-up running=%s)",
                session_id, acked, flushed, cancelled, fresh_turn_running,
            )
            return {
                "disposition": "interrupted",
                "flushed": flushed,
                "cancelled_wakeups": cancelled,
            }
        finally:
            # Lift the schedule latch only after the final sweep, so the
            # dying turn never wins the race by scheduling between them.
            if sched is not None:
                sched.end_stop()

    # ------------------------------------------------------------------
    # Public-repo GitHub-write egress approval (Slack-button decisions)
    #
    # The per-session egress hook PARKS a public-repo write in self._egress and
    # denies it; the agent ends its turn. A human then clicks Approve/Reject on
    # the Slack message (handled by main.py's @app.action), which calls one of
    # these. Like the user-stop path, this is OUT-OF-BAND of the inbox: it
    # flips the request's state and enqueues a cue so the agent re-issues
    # (approved) or backs off (rejected) on its next turn. The button click is
    # the ONLY way a request becomes approved — the agent has no tool to
    # self-approve.
    # ------------------------------------------------------------------

    async def approve_egress(self, req_id: str, approver: str, slack_client) -> dict:
        """A human approved a parked public-repo write. Mark it approved and cue
        the owning session to re-issue the exact same call (the hook then
        matches the content hash and lets it through)."""
        req = self._egress.approve(req_id, approver)
        if req is None:
            # Unknown id or already approved/rejected (double-click, expired).
            logger.info("egress approve: %s not pending (already handled?)", req_id)
            return {"ok": False, "reason": "not_pending"}
        cue = (
            f"[bitclaw egress approved: {req_id}] A human (<@{approver}>) approved "
            f"publishing the GitHub write you parked for {req.target}. Re-issue "
            "the EXACT same tool call now, unchanged (same body/title/target/"
            "method) — it will go through this time. Do not alter the wording, or "
            "it will be gated again."
        )
        logger.info(
            "egress approve: %s for session %d (%s on %s) by %s — cueing re-issue",
            req_id, req.session_id, req.tool_name, req.target, approver,
        )
        await self._enqueue(req.session_id, cue, slack_client, is_human=True)
        return {"ok": True, "session_id": req.session_id, "target": req.target}

    async def reject_egress(
        self, req_id: str, approver: str, slack_client, reason: str | None = None,
    ) -> dict:
        """A human rejected a parked public-repo write. Drop the request and cue
        the session NOT to retry."""
        req = self._egress.reject(req_id, approver, reason)
        if req is None:
            logger.info("egress reject: %s not pending (already handled?)", req_id)
            return {"ok": False, "reason": "not_pending"}
        reason_clause = f" Reason: {reason}" if reason else ""
        cue = (
            f"[bitclaw egress rejected: {req_id}] A human (<@{approver}>) declined "
            f"the GitHub write you parked for {req.target}.{reason_clause} Do NOT "
            "retry that call. Ask in this thread what they want changed before "
            "drafting a new version."
        )
        logger.info(
            "egress reject: %s for session %d (%s on %s) by %s — cueing back-off",
            req_id, req.session_id, req.tool_name, req.target, approver,
        )
        await self._enqueue(req.session_id, cue, slack_client, is_human=True)
        return {"ok": True, "session_id": req.session_id, "target": req.target}

    # ------------------------------------------------------------------
    # Jira bootstrap
    # ------------------------------------------------------------------

    async def start_session_for_jira_issue(
        self,
        issue_key: str,
        summary: str,
        slack_client,
        initial_user_message: str,
    ) -> int | None:
        """Bootstrap a session for a Jira ticket assigned to the bot.

        The Slack thread is created lazily by the agent's first post_message,
        so the session starts with thread_ts=None. Idempotent: parallel webhook
        retries serialize via the atomic claim in state.claim_or_create_session
        — exactly one caller sees created=True and builds the session; losing
        retries just deliver duplicates into the same inbox.
        """
        channel = resolve_jira_channel()
        if not channel:
            logger.error(
                "JIRA_DEFAULT_CHANNEL not set — cannot bootstrap session for Jira %s",
                issue_key,
            )
            return None

        session_id, created = await claim_or_create_session(
            "jira_issue", issue_key,
            slack_channel=channel, slack_thread_ts=None,
        )
        if created:
            logger.info(
                "Created session %d for Jira %s (%s)", session_id, issue_key, summary,
            )
            # Build the session synchronously NOW. There is no await between
            # the claim returning and the dict write inside _build_session, so
            # a losing retry's _enqueue resumes only after the session is in
            # memory and skips its own build (the same invariant _enqueue's
            # re-check relies on); the `not in self.sessions` guard makes it
            # order-independent anyway. Building does NOT start the SDK — the
            # worker spins it up lazily on the first inbox item.
            if session_id not in self.sessions:
                self._build_session(
                    session_id=session_id,
                    channel=channel,
                    thread_ts=None,
                    slack_client=slack_client,
                    resume_session_id=None,
                    is_recovery=False,
                )
        elif await reopen_session(session_id):
            # The ticket's session was soft-closed and the ticket was assigned
            # to the bot again — a human deliberately handing the work back
            # (self-authored Jira events are suppressed upstream). Reopen with
            # the cue rather than enqueue into a stage='done' row.
            logger.info(
                "Reopened done session %d on Jira re-assignment of %s",
                session_id, issue_key,
            )
            self._mark_reopened_in_memory(session_id)
            initial_user_message = (
                _reopen_cue("the Jira ticket was re-assigned to you")
                + "\n\n" + initial_user_message
            )

        # is_human=False: this is a webhook-synthesized message, not a human
        # typing in the thread.
        await self._enqueue(session_id, initial_user_message, slack_client)
        return session_id

    # ------------------------------------------------------------------
    # Recovery (called once at startup)
    # ------------------------------------------------------------------

    async def restore_session(self, row: dict, slack_client) -> None:
        """Rebuild an in-memory Session from a persisted row, WITHOUT enqueueing.

        Because SDK start is lazy (the worker starts the client on the first
        inbox item), restoring N sessions at boot spawns ZERO subprocesses —
        a restored-but-idle session costs one asyncio task until an event
        actually hits its inbox. Eagerly starting N CLI + MCP stacks at once
        would stampede the host on every restart.
        """
        session_id = row["id"]
        if session_id in self.sessions:
            return
        if not row.get("slack_channel"):
            logger.warning("Session %d has no Slack channel — skipping restore", session_id)
            return
        self._build_session(
            session_id=session_id,
            channel=row["slack_channel"],
            thread_ts=row.get("slack_thread_ts"),  # may be None for lazy-thread sessions
            slack_client=slack_client,
            resume_session_id=row.get("sdk_session_id"),
            is_recovery=True,
        )

    async def reschedule_after_recovery(
        self,
        session_id: int,
        task_id: str,
        delay_seconds: float,
        message: str,
        db_row_id: int,
        slack_client,
    ) -> None:
        """Re-arm a persisted scheduled_messages row in-memory after restart.

        Builds the session in-memory if absent via `_build_session` directly —
        NOT `_enqueue`, because there is nothing to enqueue yet: the inbox.put
        happens when the timer fires, not now.

        Caller (main.recover_scheduled_messages) guarantees: the sessions row
        exists and is not stage='done'; the scheduled row is status='scheduled';
        delay_seconds is the remaining sleep, computed against now.
        """
        if session_id not in self.sessions:
            row = await get_session(session_id)
            if row is None:
                logger.warning(
                    "reschedule_after_recovery: session %d gone — skipping task %s",
                    session_id, task_id,
                )
                return
            if row.get("stage") == "done":
                logger.info(
                    "reschedule_after_recovery: session %d is closed — skipping task %s",
                    session_id, task_id,
                )
                return
            if not row.get("slack_channel"):
                logger.warning(
                    "reschedule_after_recovery: session %d has no slack_channel — skipping",
                    session_id,
                )
                return
            self._build_session(
                session_id=session_id,
                channel=row["slack_channel"],
                thread_ts=row.get("slack_thread_ts"),
                slack_client=slack_client,
                resume_session_id=row.get("sdk_session_id"),
                is_recovery=True,
            )

        session = self.sessions[session_id]
        if session.scheduling_tools is None:
            logger.warning(
                "reschedule_after_recovery: session %d has no scheduling_tools — "
                "skipping task %s",
                session_id, task_id,
            )
            return
        session.scheduling_tools.arm_recovered(
            task_id=task_id,
            delay_seconds=delay_seconds,
            message=message,
            db_row_id=db_row_id,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _enqueue(
        self,
        session_id: int,
        text: str,
        slack_client,
        *,
        is_human: bool = False,
        status_hint: str | None = None,
    ) -> None:
        """THE delivery chokepoint — every inbound event, from any edge, lands here.

        Bounded retry: every await below (DB reads, touch, the live-status
        Slack round-trip) yields the event loop, and eviction — fired
        deterministically by evict-on-close at closing-turn end — can pop the
        Session while this delivery is parked mid-flight. Each attempt
        re-resolves from scratch; losing the race twice in a row is already
        vanishingly unlikely, three times means something is structurally
        wrong, so we log rather than loop forever.
        """
        for _attempt in range(3):
            # Dead-worker recovery: a worker that crashed during loop.start()
            # (or any other failure) leaves a stale Session in self.sessions.
            # Evict synchronously so the rebuild below runs — no await between
            # this check and the build, per the sync-build invariant.
            existing = self.sessions.get(session_id)
            if existing is not None and existing.worker_task and existing.worker_task.done():
                logger.warning(
                    "Session %d has a dead worker — evicting and rebuilding",
                    session_id,
                )
                self._evict_session_sync(session_id, "dead worker detected in _enqueue")
                existing = None

            if existing is None:
                row = await get_session(session_id)
                if row is None:
                    logger.error("Session %d disappeared from the database", session_id)
                    return
                if row.get("stage") == "done":
                    # Backstop: every delivery edge gates on done before
                    # reaching _enqueue. The warm case (done session still in
                    # memory) is deliberately not re-checked here —
                    # deliver_to_anchor owns that, and the reopen paths
                    # legitimately enqueue right after flipping stage.
                    if is_human:
                        # A human message that raced the close: its delivery
                        # edge read stage='active', the closing turn then won
                        # and evict-on-close popped the Session before this
                        # delivery landed (we got here via a retry). Same
                        # human-intent rule as the delivery gates — reopen with
                        # the cue rather than silently dropping the message.
                        await reopen_session(session_id)
                        logger.info(
                            "Reopened done session %d in _enqueue — human "
                            "message raced the close", session_id,
                        )
                        if not text.startswith(_REOPEN_CUE_PREFIX):
                            text = (
                                _reopen_cue("a human returned to this conversation")
                                + "\n\n" + text
                            )
                    else:
                        # Refuse to cold-start a full SDK/MCP subprocess stack
                        # for a closed session on an automated event.
                        logger.warning(
                            "Refusing to rebuild done session %d — dropping message",
                            session_id,
                        )
                        return
                # The `await get_session` above yielded the event loop, so a
                # concurrent _enqueue for this same session_id (a webhook
                # delivery racing a Slack reply, or a scheduler fire) may have
                # built the Session while we awaited the DB read. Re-check the
                # dict before building. The re-check is safe ONLY because
                # _build_session is synchronous: no further yield between this
                # `in` test and its self.sessions[session_id] write, so the
                # loser of the race resumes after the winner's write is visible
                # and skips the build — preventing a second, orphaned worker
                # draining the same inbox.
                if session_id not in self.sessions:
                    self._build_session(
                        session_id=session_id,
                        channel=row["slack_channel"],
                        thread_ts=row.get("slack_thread_ts"),
                        slack_client=slack_client,
                        resume_session_id=row.get("sdk_session_id"),
                        is_recovery=False,
                    )
            # Inactivity clock: every inbound event refreshes updated_at, so
            # the auto-close sweeper only fires on genuinely silent sessions.
            await touch_session(session_id)
            session = self.sessions.get(session_id)
            if session is None:
                # Evicted while we were parked at the touch — retry cold.
                continue
            # Live status the instant the message lands, BEFORE inbox.put (and
            # so before the worker can pick it up and post its own status —
            # awaiting here keeps that ordering, which a fire-and-forget task
            # would not). Best-effort: a Slack failure never blocks delivery.
            if session.slack_tools is not None:
                try:
                    if status_hint:
                        # Event arrival: coarse "what just came in" cue.
                        await session.slack_tools.set_live_status(status_hint)
                    elif session.anchor is None or not session.anchor.last_status:
                        # Nothing remembered (previous turn cleared it) — adopt
                        # a coarse default so the turn never runs statusless.
                        await session.slack_tools.set_live_status(DEFAULT_LIVE_STATUS)
                except Exception:
                    logger.debug(
                        "Could not set live status on enqueue for session %d",
                        session_id, exc_info=True,
                    )
            # Re-resolve before the put: the status call above can yield while
            # an eviction pops this Session, and a put into the popped
            # instance's inbox would orphan the message in a dead queue. A
            # different (rebuilt) Session object is fine — its inbox is live.
            session = self.sessions.get(session_id)
            if session is None:
                continue
            await session.inbox.put((text, is_human))
            return
        logger.error(
            "Session %d: delivery raced eviction on every attempt — dropping message",
            session_id,
        )

    def _evict_session_sync(self, session_id: int, reason: str) -> None:
        """Sync eviction; fire-and-forget cancel; idempotent.

        Skips self-cancel: when _drain_inbox's failure path self-evicts, the
        worker is about to return naturally — cancelling it would schedule a
        CancelledError on its very next await (the loop.stop() in its finally)
        and propagate out of the task.
        """
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        logger.info("Evicting session %d (%s)", session_id, reason)
        if session.worker_task and not session.worker_task.done():
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if session.worker_task is not current:
                session.worker_task.cancel()

    async def _evict_session(self, session_id: int, *, reason: str) -> None:
        """Async eviction; delegates to the sync helper then awaits the worker."""
        # Snapshot the worker_task ref BEFORE the sync helper pops the entry —
        # the pop destroys the only reference.
        session = self.sessions.get(session_id)
        worker_task = session.worker_task if session else None
        self._evict_session_sync(session_id, reason)
        if worker_task and not worker_task.done():
            try:
                await worker_task
            except (asyncio.CancelledError, Exception):
                # The worker's own finally runs loop.stop(); never propagate.
                pass

    async def _scheduler_enqueue(
        self,
        session_id: int,
        text: str,
        slack_client,
    ) -> None:
        """Fire-callback for the per-session scheduler.

        On fire we go through _enqueue rather than session.inbox.put directly —
        _enqueue rebuilds the Session if it was evicted (this is what makes
        timers survive in-memory eviction), and we short-circuit cleanly if the
        session row is gone or stage='done'.
        """
        row = await get_session(session_id)
        if row is None or row.get("stage") == "done":
            logger.info(
                "Scheduled message for session %d dropped — session gone or done",
                session_id,
            )
            return
        await self._enqueue(session_id, text, slack_client)

    def _session_has_pending_scheduled_tasks(self, session_id: int) -> bool:
        """True if the session's SchedulingTools has any non-done scheduled task."""
        session = self.sessions.get(session_id)
        if session is None or session.scheduling_tools is None:
            return False
        return session.scheduling_tools.has_pending_tasks()

    # ------------------------------------------------------------------
    # Idle eviction (in-memory, reversible)
    # ------------------------------------------------------------------

    async def _idle_eviction_loop(self) -> None:
        """Periodically evict sessions idle past SESSION_IDLE_TTL_SECS.

        Skips sessions with a non-empty inbox, an in-progress turn, or pending
        scheduled tasks (a session with an armed timer must stay in memory —
        its SchedulingTools owns the asyncio timer task). The DB row stays, so
        the next inbound event rebuilds the session.
        """
        while True:
            try:
                await asyncio.sleep(SESSION_EVICTION_CHECK_SECS)
                now = time.monotonic()
                # Snapshot before iterating so eviction's pop doesn't mutate
                # the dict during the loop.
                candidates = [
                    s for s in list(self.sessions.values())
                    if now - s.last_activity > SESSION_IDLE_TTL_SECS
                    and s.inbox.empty()
                    and not s.turn_in_progress
                    and not self._session_has_pending_scheduled_tasks(s.id)
                ]
                for s in candidates:
                    await self._evict_session(s.id, reason="idle ttl")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("idle eviction loop iteration failed")

    def start_idle_eviction(self) -> asyncio.Task:
        """Spawn the idle-eviction sweeper; caller owns cancel + await on shutdown."""
        return asyncio.create_task(self._idle_eviction_loop())

    # ------------------------------------------------------------------
    # Inactivity auto-close (durable)
    # ------------------------------------------------------------------

    async def _autoclose_loop(self, slack_client) -> None:
        """Periodically hand long-idle sessions an inactivity cleanup cue.

        A session whose updated_at hasn't moved for SESSION_AUTOCLOSE_IDLE_DAYS
        (no message or anchor event — both flow through _enqueue's touch) is
        enqueued a self-contained cue. The agent posts a closing note, tears
        down its dev environments, and calls close_session() → stage='done', so
        the row stops being restored on every restart.

        The cue enqueue itself touches updated_at, resetting the clock: the cue
        fires ONCE and only recurs if another full idle window elapses. A
        consequence: a cleanup that errors before close_session retries only on
        the next full window — acceptable, since teardown is side-effecting and
        must not double-fire.

        Skips sessions that are in memory and busy (queued inbox or a turn in
        progress) — they're plainly active and their next turn refreshes the
        clock anyway. Re-checks freshness immediately before acting, to close
        the race where a real event lands mid-sweep. Each row is isolated so
        one failure doesn't abort the rest of the batch.
        """
        while True:
            try:
                await asyncio.sleep(SESSION_AUTOCLOSE_CHECK_SECS)
                stale = await load_stale_active_sessions(SESSION_AUTOCLOSE_IDLE_DAYS)
                # state.py stores timestamps as ISO-8601 UTC strings, which
                # compare correctly as plain strings (lexicographic order ==
                # chronological order for a fixed UTC format) — so the per-row
                # freshness re-check below is a string comparison.
                cutoff = (
                    datetime.now(timezone.utc)
                    - timedelta(days=SESSION_AUTOCLOSE_IDLE_DAYS)
                ).isoformat()
                for row in stale:
                    try:
                        await self._autoclose_one(row, cutoff, slack_client)
                    except Exception:
                        logger.exception(
                            "auto-close failed for session %s — continuing",
                            row.get("id"),
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("auto-close loop iteration failed")

    async def _autoclose_one(self, row: dict, cutoff: str, slack_client) -> None:
        """Decide and apply the auto-close action for a single stale row."""
        sid = row["id"]
        live = self.sessions.get(sid)
        if live is not None and (not live.inbox.empty() or live.turn_in_progress):
            return
        # Re-check the CURRENT row: a real event may have bumped updated_at (or
        # closed the session) between the sweep query and now. A NULL/missing
        # updated_at falls back to "" which sorts before every real ISO
        # timestamp, so such a row is treated as stale (proceeds to close).
        fresh = await get_session(sid)
        if (
            fresh is None
            or fresh.get("stage") == "done"
            or (fresh.get("updated_at") or "") >= cutoff
        ):
            return
        if not row.get("slack_channel"):
            # No thread to post into — reap directly instead of cueing a post
            # that could never land.
            logger.info(
                "Session %d idle >= %d days and has no slack_channel — "
                "closing directly",
                sid, SESSION_AUTOCLOSE_IDLE_DAYS,
            )
            await close_session(sid)
            return
        logger.info(
            "Session %d idle >= %d days — sending inactivity cleanup cue",
            sid, SESSION_AUTOCLOSE_IDLE_DAYS,
        )
        await self._enqueue(
            sid,
            _inactivity_autoclose_msg(SESSION_AUTOCLOSE_IDLE_DAYS),
            slack_client,
        )

    def start_autoclose(self, slack_client) -> asyncio.Task:
        """Spawn the inactivity auto-close sweeper; caller owns cancel + await."""
        return asyncio.create_task(self._autoclose_loop(slack_client))

    # ------------------------------------------------------------------
    # Session construction + the worker
    # ------------------------------------------------------------------

    def _build_session(
        self,
        session_id: int,
        channel: str,
        thread_ts: str | None,
        slack_client,
        resume_session_id: str | None,
        is_recovery: bool,
    ) -> Session:
        """Construct a Session, start its worker, register it. SYNCHRONOUS by design.

        Being synchronous — no awaits anywhere in this function — is
        load-bearing: _enqueue's check-then-build ("if session_id not in
        self.sessions: _build_session(...)") and the Jira bootstrap's identical
        guard are race-safe ONLY because there is no event-loop yield between
        the membership test and the dict write at the bottom. Adding a single
        await here (e.g. awaiting the reaction instead of create_task) silently
        reopens the double-worker race: two workers draining one inbox,
        interleaving turns on one SDK client.
        """
        logger.info(
            "Building session %d (channel=%s thread=%s%s)",
            session_id, channel, thread_ts or "(lazy)",
            f" resume {resume_session_id}" if resume_session_id else "",
        )

        anchor = ThreadAnchor(channel=channel, ts=thread_ts)

        # Hourglass reaction on the triggering message — a UI hint, not
        # load-bearing, so it is fire-and-forget: awaiting it would break the
        # sync invariant above. Skipped on recovery (nothing new to react to)
        # and for lazy-thread sessions (no message to react to yet).
        if not is_recovery and thread_ts is not None:
            async def _react():
                try:
                    await slack_client.reactions_add(
                        channel=channel, timestamp=thread_ts,
                        name="hourglass_flowing_sand",
                    )
                except Exception:
                    logger.debug("Could not add hourglass reaction", exc_info=True)
            asyncio.create_task(_react())

        slack_tools = SlackTools(
            slack_client,
            source_channel=channel,
            source_thread_ts=thread_ts,
            session_id=session_id,
            anchor=anchor,
            # Hard-refuses anchoring an external / fork / untrusted-author
            # github_pr (see github_trust.PullRequestTrustChecker).
            pr_trust_check=default_pr_trust_check(),
        )

        # The scheduler's fire-callback re-enters the gateway so an evicted
        # session is rebuilt on fire (see _scheduler_enqueue).
        sched_tools = SchedulingTools(
            session_id=session_id,
            enqueue=lambda sid, msg, _sc=slack_client: self._scheduler_enqueue(sid, msg, _sc),
        )

        async def on_session_id(sdk_sid: str):
            # Persist the CLI conversation id so the next rebuild of this
            # session resumes the same conversation via --resume.
            await update_session(session_id, sdk_session_id=sdk_sid)

        loop = AgentLoop(
            slack_tools=slack_tools,
            scheduling_tools=sched_tools,
            external_mcp_servers=self._external_mcp_servers,
            session_id=session_id,
            channel=channel,
            thread_ts=thread_ts,
            resume_session_id=resume_session_id,
            on_session_id=on_session_id,
            egress_registry=self._egress,
        )

        session = Session(
            id=session_id, slack_channel=channel, slack_thread_ts=thread_ts,
            loop=loop, scheduling_tools=sched_tools, anchor=anchor,
            slack_tools=slack_tools,
        )
        session.worker_task = asyncio.create_task(self._drain_inbox(session, slack_client))
        self.sessions[session_id] = session
        return session

    async def _drain_inbox(self, session: Session, slack_client) -> None:
        """The per-session worker: process inbox items sequentially, one per turn."""
        try:
            while True:
                text, is_human = await session.inbox.get()
                # No await between the get() above and the generation bump
                # below: a concurrently-issued stop always snapshots a
                # consistent (turn_in_progress, turn_generation) pair.
                session.turn_in_progress = True
                # Stamp WHICH turn is running (see Session.turn_generation).
                session.turn_generation += 1
                try:
                    # Lazy client start: spin up the SDK client (the bundled
                    # Claude Code CLI + its MCP server subprocesses) only when
                    # a message actually arrives — NOT at build/restore time.
                    # start() is idempotent, so this is a no-op after the first
                    # message. A restored-but-idle session therefore holds zero
                    # subprocesses; without this, restoring N active sessions
                    # on boot would spawn N full client stacks at once.
                    try:
                        await session.loop.start()
                    except Exception:
                        logger.exception(
                            "[session %d] agent loop failed to start — evicting; "
                            "next inbound message will rebuild",
                            session.id,
                        )
                        # If start was a resume attempt, the most likely cause
                        # is a stale sdk_session_id whose conversation file is
                        # gone (a restart wiped the filesystem holding the CLI's
                        # session files). Clear it so the rebuild starts FRESH
                        # instead of looping forever on the same broken id.
                        # Safe to read here: AgentLoop only mutates its
                        # _sdk_session_id once it receives an Assistant/Result
                        # message, so on a start failure it still equals the
                        # resume target we passed in.
                        stale = session.loop.sdk_session_id
                        if stale:
                            logger.warning(
                                "[session %d] clearing stale sdk_session_id %s "
                                "after start failure",
                                session.id, stale,
                            )
                            try:
                                await clear_sdk_session_id(session.id)
                            except Exception:
                                logger.exception(
                                    "[session %d] could not clear sdk_session_id",
                                    session.id,
                                )
                        # Self-eviction uses the sync helper — it detects the
                        # current task and skips cancel(), so the notify below
                        # runs to completion and the return reaches the outer
                        # finally's loop.stop() naturally.
                        self._evict_session_sync(session.id, "agent loop start failed")
                        await _maybe_notify_start_failure(session, slack_client)
                        return

                    # Drain-time done gate: a message can race the close — its
                    # delivery edge read stage='active', the turn then closed,
                    # and the message was queued bare. Run as-is it would land
                    # on a conversation whose history ends with close_session:
                    # a deterministic refusal (the exact bug the reopen cue
                    # exists to fix) with the row left done. Mirror the
                    # delivery-edge gate: a human message reopens with the cue;
                    # an automated one drops, same as deliver_to_anchor.
                    run_it = True
                    if session.slack_tools is not None and session.slack_tools.closed:
                        if not is_human:
                            logger.info(
                                "[session %d] dropping automated message queued "
                                "behind close_session", session.id,
                            )
                            run_it = False
                        else:
                            await reopen_session(session.id)
                            session.slack_tools.mark_reopened()
                            if not text.startswith(_REOPEN_CUE_PREFIX):
                                text = (
                                    _reopen_cue("a human returned to this conversation")
                                    + "\n\n" + text
                                )
                            logger.info(
                                "[session %d] drain-time reopen — human message "
                                "raced the close", session.id,
                            )
                    if run_it:
                        try:
                            await session.loop.run(text, is_human=is_human)
                        except Exception:
                            # Agent errors are logged, never fatal to the
                            # worker — the next message gets a fresh turn.
                            logger.exception("[session %d] agent error", session.id)
                finally:
                    session.turn_in_progress = False
                    # Stamped even on errors, so a failing turn still counts as
                    # activity for the idle-eviction clock.
                    session.last_activity = time.monotonic()

                # Evict-on-close: close_session flipped the session done
                # mid-turn; once that turn drains and nothing else is queued,
                # this whole in-memory stack (worker, SDK client, its CLI + MCP
                # subprocess tree) serves no one — without this it idles for up
                # to SESSION_IDLE_TTL_SECS, and a leftover scheduled timer pins
                # it past even that (the idle sweeper skips sessions with
                # pending timers). A human reopening later rebuilds from the DB
                # row via _enqueue's cold path. Three races are covered: a
                # message that slipped into the inbox before the close keeps
                # the worker alive for one more drain (empty() guard) and is
                # then handled by the drain-time gate above; a human reply that
                # reopened DURING the closing turn reset the close latch via
                # _mark_reopened_in_memory, so `closed` is already False here;
                # and a delivery parked mid-_enqueue re-resolves the Session
                # after the pop and retries through the cold path.
                if (
                    session.slack_tools is not None
                    and session.slack_tools.closed
                    and session.inbox.empty()
                ):
                    # Cancel armed timers BEFORE the pop, and on THIS eviction
                    # path only (plus user-stop escalation) — a timer that
                    # outlives its Session turns hostile after a reopen: the
                    # rebuilt SchedulingTools can't see or cancel it, and its
                    # stale fire would deliver into the reopened conversation.
                    # Cancelling marks the DB rows cancelled, so a later
                    # re-schedule of the same task_id UPSERT-resets cleanly.
                    # Dead-worker/idle-TTL evictions must NOT do this — they
                    # evict ACTIVE sessions whose timers must keep firing (the
                    # fire path rebuilds the session on demand).
                    if session.scheduling_tools is not None:
                        cancelled = session.scheduling_tools.cancel_pending()
                        if cancelled:
                            logger.info(
                                "[session %d] cancelled %d armed scheduled "
                                "task(s) on close", session.id, cancelled,
                            )
                    # Self-eviction: the sync helper skips cancelling the
                    # current task, so the return reaches the outer finally's
                    # loop.stop() naturally.
                    self._evict_session_sync(session.id, "session closed")
                    return
        finally:
            try:
                await session.loop.stop()
            except Exception:
                logger.debug(
                    "[session %d] loop.stop() raised during teardown",
                    session.id, exc_info=True,
                )

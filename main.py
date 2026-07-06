"""
Entry point.

Starts the Slack Socket Mode receiver and the GitHub/Jira webhook HTTP
listener side-by-side, and recovers persisted sessions and scheduled
messages from SQLite. Logging is configured before the app modules are
imported so nothing logs unconfigured — and the aiohttp access logger is
disabled outright, because the Jira webhook authenticates via a secret in
the URL path (see _make_webhook_runner).
"""
import asyncio
import logging
import os
import re
import sqlite3
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# aiohttp's access logger echoes the full request line of every webhook hit —
# and the Jira route authenticates via a shared secret in the URL PATH
# (/webhooks/jira/<secret>; Jira Cloud webhooks can't send custom headers), so
# a single access-log line would hand the secret to anyone with log-read
# access. The webhook runner is built with access_log=None (see
# _make_webhook_runner); this logger kill switch is the backstop so that even
# a future server wired up WITHOUT that parameter can't leak request lines to
# stdout. The NullHandler stops the record from falling through to logging's
# lastResort stderr handler once propagation is off.
_aiohttp_access_logger = logging.getLogger("aiohttp.access")
_aiohttp_access_logger.propagate = False
_aiohttp_access_logger.addHandler(logging.NullHandler())

from aiohttp import web

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler

from gateway import SlackGateway
from http_client import close_http_session, get_http_session
from state import (
    close_db,
    get_session,
    init_db,
    load_active_sessions,
    load_pending_scheduled_messages,
    lookup_anchor,
    mark_scheduled_cancelled,
    mark_scheduled_done,
    mark_scheduled_failed,
    mark_scheduled_running,
    slack_anchor_key,
)
from webhooks import make_webhook_app

logger = logging.getLogger(__name__)

# Friendly first-contact failure: running before .env exists should say what
# to do, not dump a KeyError traceback.
if not os.environ.get("SLACK_BOT_TOKEN"):
    sys.exit(
        "SLACK_BOT_TOKEN is not set. Copy .env.example to .env and fill in "
        "your Slack tokens (see README → Quickstart)."
    )

app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])
# Honor Slack 429s' Retry-After inside the SDK: a rate-limited API call sleeps
# and retries instead of raising into the agent's tool call — where the
# "retry" would be a whole new (paid) LLM turn rather than a cheap sleep.
app.client.retry_handlers.append(AsyncRateLimitErrorRetryHandler(max_retry_count=2))
gateway = SlackGateway()

_bot_user_id: str | None = None
_bot_id: str | None = None  # B-prefixed id of THIS app's own posts (from auth.test)
_github_bot_login: str | None = None  # Webhooks drop events authored by this login

# Slack can re-deliver the same Socket Mode event after a reconnect/retry.
# `event_ts` is the dedupe key (always present; `client_msg_id` is missing on
# `bot_message` subtypes so it can't be the key). Short TTL is enough —
# duplicates always arrive back-to-back, never minutes apart.
_seen_events: "OrderedDict[str, float]" = OrderedDict()
_DEDUPE_TTL_SECS = 60.0


def _already_handled(event: dict) -> bool:
    key = event.get("event_ts") or event.get("ts")
    if not key:
        return False
    now = time.monotonic()
    while _seen_events:
        k, t = next(iter(_seen_events.items()))
        if now - t > _DEDUPE_TTL_SECS:
            _seen_events.popitem(last=False)
        else:
            break
    if key in _seen_events:
        return True
    _seen_events[key] = now
    return False


# User stop command. The WHOLE message (after an optional leading or trailing
# @bot mention, and Slack mrkdwn emphasis like *stop* — Slack delivers the
# literal asterisks) must be a stop word — "stop", "/stop", "!stop", "abort",
# trailing ./!/, tolerated — so ordinary sentences containing "stop" still
# reach the agent as normal input. NOTE: this is deliberately a message-text
# trigger, not a registered Slack slash command: slash-command payloads carry
# no thread_ts, so a real /stop could never tell WHICH thread's session to
# stop (and an unregistered /stop typed in the composer is swallowed by Slack
# client-side anyway).
_STOP_RE = re.compile(r"^[/!]?\s*(?:stop|abort)\s*[.!,]*$", re.IGNORECASE)


def _is_stop_command(text: str, bot_user_id: str | None) -> bool:
    """True when the message text is solely a stop command for the bot."""
    t = (text or "").strip()
    if bot_user_id:
        mention = rf"<@{re.escape(bot_user_id)}>"
        t = re.sub(rf"^\s*{mention}\s*[,:]?\s*", "", t)   # "@bitclaw stop"
        t = re.sub(rf"\s*[,:]?\s*{mention}\s*$", "", t)   # "stop @bitclaw"
    # Slack sends emphasis markers literally ("*stop*", "_stop_", "`stop`").
    t = t.strip().strip("*_~`").strip()
    return bool(_STOP_RE.match(t))


def _format_stop_ack(result: dict) -> str:
    """Human-readable thread reply for a stop_session_work result."""
    bits = []
    if result.get("flushed"):
        bits.append(f"discarded {result['flushed']} queued message(s)")
    if result.get("cancelled_wakeups"):
        bits.append(f"cancelled {result['cancelled_wakeups']} scheduled wake-up(s)")
    detail = (" — " + ", ".join(bits)) if bits else ""
    disposition = result.get("disposition")
    if disposition == "idle":
        return f"No turn was running{detail}. I'm idle in this thread."
    if disposition == "forced":
        return (
            f"⏹ Stopped (forced){detail}. The turn didn't answer a graceful "
            "interrupt, so I tore its worker down; conversation history is "
            "preserved. Note: an RDE coding agent, if any, is still running. "
            "Send a new message to continue."
        )
    return (
        f"⏹ Stopped — interrupted the in-flight turn{detail}. "
        "Note: an RDE coding agent, if any, is still running. "
        "Send a new message to continue."
    )


async def _handle_stop_command(event: dict, client, *, addressed: bool) -> None:
    """Stop the session owning this thread, out-of-band of its inbox.

    Stays silent when the thread isn't ours and the bot wasn't @mentioned —
    the same contract as on_message's handling of non-owned thread replies.
    `addressed` (an explicit @mention) earns a reply even with no session.
    """
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    session_id = await lookup_anchor(
        "slack_thread", slack_anchor_key(channel, thread_ts)
    )
    if session_id is None:
        if addressed:
            try:
                await client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text="Nothing to stop — no session is attached to this thread.",
                )
            except Exception:
                logger.warning("stop: could not post no-session reply", exc_info=True)
        else:
            logger.info("stop: thread reply not for one of our sessions — ignoring")
        return

    # Instant feedback on the stop message itself — the stop can take a few
    # seconds (interrupt ack, grace wait), and the user asked for *now*.
    try:
        await client.reactions_add(
            channel=channel, timestamp=event["ts"], name="octagonal_sign"
        )
    except Exception:
        logger.debug("stop: could not add reaction", exc_info=True)

    logger.info(
        "Stop command from %s for session %d (%s:%s)",
        event.get("user"), session_id, channel, thread_ts,
    )
    result = await gateway.stop_session_work(session_id)
    try:
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=_format_stop_ack(result)
        )
    except Exception:
        logger.warning("stop: could not post stop ack", exc_info=True)


# Public-repo egress approval buttons. The per-session PreToolUse egress gate
# (tools.hooks.make_public_egress_gate) parks every PUBLIC-repo GitHub write
# and posts an Approve/Reject message whose buttons carry the parked request
# id as their `value`. A click here is a trusted human's decision (an outsider
# on the public PR has no Slack access), so we flip the request via the
# gateway — which cues the owning session to re-issue (approved) or back off
# (rejected) — and update the message so it can't be clicked twice. Requires
# the Slack app's Interactivity feature to be enabled (delivered over Socket
# Mode, no Request URL needed).
@app.action("egress_approve")
async def on_egress_approve(ack, body, client):
    await ack()
    await _handle_egress_decision(body, client, approve=True)


@app.action("egress_reject")
async def on_egress_reject(ack, body, client):
    await ack()
    await _handle_egress_decision(body, client, approve=False)


async def _handle_egress_decision(body: dict, client, *, approve: bool) -> None:
    actions = body.get("actions") or []
    req_id = actions[0].get("value") if actions else None
    user = (body.get("user") or {}).get("id") or "unknown"
    channel = (body.get("channel") or {}).get("id")
    message_ts = (body.get("message") or {}).get("ts")
    if not req_id:
        logger.warning("egress decision: button click carried no request id")
        return

    logger.info(
        "egress %s click on %s by %s", "approve" if approve else "reject", req_id, user,
    )
    if approve:
        result = await gateway.approve_egress(req_id, user, client)
    else:
        result = await gateway.reject_egress(req_id, user, client)

    if result.get("ok"):
        summary = (
            f"✅ Approved & posting by <@{user}>." if approve
            else f"🚫 Rejected by <@{user}>."
        )
    else:
        # Unknown id, or already approved/rejected (double-click / expired).
        verb = "approval" if approve else "rejection"
        summary = f"(This {verb} had no effect — the request was already handled or has expired.)"

    if channel and message_ts:
        try:
            await client.chat_update(
                channel=channel, ts=message_ts, text=summary,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": summary}}],
            )
        except Exception:
            logger.warning("egress decision: could not update approval message", exc_info=True)


@app.event("message")
async def on_message(event, client):
    if _already_handled(event):
        logger.info("on_message: skipping duplicate event_ts=%s", event.get("event_ts"))
        return
    # Skip our own messages (loop prevention) and Slack-internal subtypes
    # (channel_join, message_changed, message_deleted, …). bot_message from
    # *other* bots/integrations falls through so they can trigger the agent.
    # thread_broadcast falls through too: it's a regular human thread reply
    # whose "also send to #channel" box was ticked — dropping it would
    # silently eat real input (worst of all an urgent "stop").
    if _is_bot_event(event) or (
        event.get("subtype")
        and event.get("subtype") not in ("bot_message", "thread_broadcast")
    ):
        return

    text = event.get("text") or ""
    thread_ts = event.get("thread_ts")
    # Compute is_mention on the RAW text (before we attribute) — the trigger is
    # the bot's own id in the message, never the sender's.
    is_mention = _bot_user_id and f"<@{_bot_user_id}>" in text

    # User stop ("stop" / "/stop" / "@bitclaw stop" as the WHOLE message) is a
    # control command, not agent input: enqueueing it would park it behind the
    # very turn it is meant to kill, so it's detected here and handled
    # out-of-band — it never reaches a session inbox and never starts a turn.
    # Humans only: a third-party bot_message must not be able to halt work
    # (its "stop" falls through as ordinary agent input instead).
    if (
        (thread_ts or is_mention)
        and event.get("user")
        and not event.get("bot_id")
        and _is_stop_command(text, _bot_user_id)
    ):
        await _handle_stop_command(event, client, addressed=bool(is_mention))
        return

    # Tag the message with its sender so the agent knows WHO asked — in a
    # multi-person thread it otherwise can't tell speakers apart, and the
    # <@U…> mention token lets it @-reply correctly.
    sender = event.get("user")
    if sender:
        attributed = f"<@{sender}>: {text}"
        sender_is_human = True
    else:
        # bot_message: no `user` field. Attribute via the app's username /
        # bot_id so an integration's post can never land verbatim —
        # unattributed text could impersonate a system cue (e.g.
        # "[bitclaw session reopened: …]") at the literal start of the
        # delivered message. And route it as automated: an integration
        # posting into a done thread must not reopen the session the way a
        # returning human does (it still delivers normally to active
        # sessions, and an explicit @-mention still triggers regardless of
        # sender type via the branch below).
        author = event.get("username") or event.get("bot_id") or "unknown bot"
        attributed = f"{author} (bot): {text}"
        sender_is_human = False

    if is_mention:
        # A mention that arrives as a reply inside an existing thread already
        # carries thread_ts; a top-level mention does not (it starts a fresh
        # thread, so there's nothing before it). Only in the former case is
        # there pre-mention history worth pulling in — pass the mention's own
        # ts so the gateway can fetch the earlier messages and exclude the
        # mention itself.
        in_existing_thread = thread_ts is not None
        thread_ts = thread_ts or event["ts"]
        await gateway.start_or_resume_slack_thread(
            event["channel"], thread_ts, attributed, client,
            trigger_ts=event["ts"] if in_existing_thread else None,
        )
    elif thread_ts:
        # Reply inside an existing thread — only deliver if we own it.
        delivered = await gateway.deliver_to_anchor(
            "slack_thread", slack_anchor_key(event["channel"], thread_ts),
            attributed, client, is_human=sender_is_human,
        )
        if not delivered:
            logger.info("on_message: thread reply not for one of our sessions — ignoring")


@app.event("app_mention")
async def on_app_mention():
    # @mentions fire as both app_mention and message. on_message handles them;
    # this handler exists only to suppress Bolt's "unhandled request" warning.
    pass


def _is_bot_event(event: dict) -> bool:
    """True only for events originating from THIS bot (loop prevention).

    Other bots / webhook integrations (`bot_message` with a different `bot_id`)
    are intentionally NOT treated as 'our own' so they can trigger the agent.
    """
    bot_id = event.get("bot_id")
    if bot_id and _bot_id and bot_id == _bot_id:
        return True
    return event.get("user") == _bot_user_id


async def _resolve_github_bot_login() -> str | None:
    """Env override beats API lookup; None disables the self-echo filter (fail open)."""
    override = os.environ.get("GITHUB_BOT_LOGIN")
    if override:
        logger.info("GitHub bot login pinned via env: %s", override)
        return override
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.warning(
            "GITHUB_BOT_LOGIN unset and GITHUB_TOKEN missing — "
            "GitHub self-echo filter disabled"
        )
        return None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        session = get_http_session()
        async with session.get(
            "https://api.github.com/user", headers=headers, timeout=10
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(
                    "GitHub /user returned %d (%s) — self-echo filter disabled",
                    resp.status, body[:200],
                )
                return None
            data = await resp.json()
            login = data.get("login")
            logger.info("Resolved GitHub bot login from /user: %s", login)
            return login
    except Exception:
        logger.warning(
            "GitHub /user lookup failed — self-echo filter disabled",
            exc_info=True,
        )
        return None


async def recover_scheduled_messages(gateway, slack_client) -> asyncio.Task | None:
    """Replay persisted scheduled_messages rows after a restart.

    Must run after load_active_sessions/restore_session and BEFORE the webhook
    server and Slack Socket Mode handler start, so no fresh schedule_message
    call can race the snapshot we iterate. Classification (running → failed,
    closed-session → cancelled) and future-row re-arming complete synchronously
    under that guarantee.

    Past-due rows are the exception: delivering one starts a whole agent turn
    (SDK client boot + MCP subprocesses + an LLM call), so they are handed to
    a returned background task and startup isn't delayed by LLM turns. The
    caller owns the task (keep a reference; cancel on shutdown). Returns None
    when nothing is past due.
    """
    pending = await load_pending_scheduled_messages()
    if not pending:
        logger.info("No scheduled_messages to recover")
        return None
    logger.info("Recovering %d scheduled_messages row(s)", len(pending))

    now = datetime.now(timezone.utc)
    past_due: list[dict] = []

    for row in pending:
        row_id = row["id"]
        sid = row["session_id"]
        tid = row["task_id"]

        # Crashed-mid-fire: don't auto-retry side-effecting deliveries.
        if row["status"] == "running":
            await mark_scheduled_failed(row_id, "process restarted while running")
            logger.warning(
                "scheduled_message id=%d (sid=%d, task=%s) marked failed: "
                "process restarted while running",
                row_id, sid, tid,
            )
            continue

        # Session state check (closed/deleted → drop).
        sess = await get_session(sid)
        if sess is None:
            await mark_scheduled_failed(row_id, "session row missing")
            continue
        if sess.get("stage") == "done":
            await mark_scheduled_cancelled(sid, tid)
            logger.info(
                "scheduled_message id=%d (sid=%d, task=%s) cancelled: session done",
                row_id, sid, tid,
            )
            continue

        # fire_at comes back as a timezone-aware datetime. Keep a None guard
        # (a missing fire_at fires now) and back-fill UTC on a naive value.
        fire_at = row["fire_at"]
        if fire_at is None:
            logger.warning(
                "scheduled_message id=%d: missing fire_at — firing now", row_id,
            )
            fire_at = now
        elif fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=timezone.utc)
        remaining = (fire_at - now).total_seconds()

        if remaining <= 0:
            past_due.append(row)
        else:
            # Future: re-arm via the scheduler primitive.
            await gateway.reschedule_after_recovery(
                session_id=sid,
                task_id=tid,
                delay_seconds=remaining,
                message=row["message"],
                db_row_id=row_id,
                slack_client=slack_client,
            )
            logger.info(
                "scheduled_message id=%d (sid=%d, task=%s) re-armed (fires in %.1fs)",
                row_id, sid, tid, remaining,
            )

    if not past_due:
        return None
    logger.info("Firing %d past-due scheduled_message(s)", len(past_due))
    return asyncio.create_task(
        _fire_past_due_rows(gateway, slack_client, past_due)
    )


async def _fire_past_due_rows(gateway, slack_client, rows: list[dict]) -> None:
    """Deliver past-due scheduled rows, one at a time.

    Sequential is a deliberate simplification: a production deployment would
    bound the concurrency of the resulting agent turns instead (each delivery
    boots an SDK client and runs an LLM turn, so firing many at once would
    stampede the process). Per-row failures are logged and don't abort the
    rest of the batch.
    """
    for row in rows:
        try:
            # Re-check right before delivering: the listeners are already up
            # while this task drains, so the session may have closed since
            # classification.
            sess = await get_session(row["session_id"])
            if sess is None or sess.get("stage") == "done":
                await mark_scheduled_cancelled(row["session_id"], row["task_id"])
                logger.info(
                    "scheduled_message id=%d (sid=%d) cancelled before "
                    "past-due fire: session gone/done",
                    row["id"], row["session_id"],
                )
                continue
            # Claim the row BEFORE delivering: if we crash mid-delivery, the
            # next boot classifies the 'running' row as failed instead of
            # re-firing a possibly already-delivered message (side-effecting
            # deliveries are never auto-retried).
            await mark_scheduled_running(row["id"])
            try:
                await gateway.deliver_to_session(
                    row["session_id"], row["message"], slack_client
                )
            except Exception as exc:
                logger.exception(
                    "past-due delivery failed for scheduled_message id=%d (sid=%d)",
                    row["id"], row["session_id"],
                )
                await mark_scheduled_failed(row["id"], repr(exc))
                continue
            await mark_scheduled_done(row["id"])
            logger.info(
                "scheduled_message id=%d (sid=%d, task=%s) fired past-due",
                row["id"], row["session_id"], row["task_id"],
            )
        except Exception:
            logger.exception(
                "past-due fire failed for scheduled_message id=%d (sid=%d)",
                row["id"], row["session_id"],
            )
    logger.info("Past-due scheduled-message recovery complete (%d row(s))", len(rows))


def _log_capability_summary() -> None:
    """One glance at what this deployment can do, logged at startup.

    bitclaw's integrations are tiered: Slack + an Anthropic key alone gives a
    conversational bot; the Bitrise RDE integration gives it hands (coding);
    GitHub adds PR orchestration; Jira is optional on top. Each tier is
    enabled purely by setting its env vars, so this summary — not a crash —
    is how an operator learns what's missing. The coding tier gets a WARNING
    when absent: a bitclaw that can only talk is missing its point.
    """
    def have(*names: str) -> bool:
        return all(os.environ.get(n) for n in names)

    lines = ["capability summary:"]
    lines.append("  chat            ✓  Slack + Claude")
    rde_vars = ("BITRISE_TOKEN", "BITRISE_WORKSPACE_ID", "RDE_TEMPLATE_ID")
    coding = have(*rde_vars)
    if coding:
        lines.append("  coding (RDE)    ✓  Bitrise Remote Dev Environments")
    else:
        missing = ", ".join(n for n in rde_vars if not os.environ.get(n))
        lines.append(
            f"  coding (RDE)    ✗  set {missing} (see docs/rde-template.md) — "
            "without this, bitclaw can only talk; the coding agent is the "
            "point of this bot"
        )
    if have("GITHUB_TOKEN"):
        gh = "  github          ✓  PR orchestration"
        if not os.environ.get("GITHUB_ALLOWED_ORG"):
            gh += " (GITHUB_ALLOWED_ORG unset — every author-trust check fails closed)"
        if not os.environ.get("GITHUB_WEBHOOK_SECRET"):
            gh += " (GITHUB_WEBHOOK_SECRET unset — inbound PR/CI webhooks disabled)"
        lines.append(gh)
    else:
        lines.append(
            "  github          ✗  set GITHUB_TOKEN + GITHUB_ALLOWED_ORG to "
            "open and shepherd PRs"
        )
    if have("JIRA_AUTH", "JIRA_HOST"):
        lines.append("  jira            ✓")
    else:
        lines.append("  jira            ✗  optional (JIRA_AUTH + JIRA_HOST)")

    log = logger.info if coding else logger.warning
    log("%s", "\n".join(lines))


def _make_webhook_runner(web_app: web.Application) -> web.AppRunner:
    """Build the webhook server's AppRunner with access logging OFF.

    access_log=None is load-bearing, not an optimization: the Jira webhook
    authenticates via a shared secret in the URL path (/webhooks/jira/<secret>),
    so a default access logger would write the secret to stdout in every
    request line (`POST /webhooks/jira/<secret> HTTP/1.1`). Application-level
    logging in webhooks.py covers everything we want logged.
    """
    return web.AppRunner(web_app, access_log=None)


async def _start_webhook_server(slack_client, github_bot_login: str | None) -> web.AppRunner:
    """Start the aiohttp server for inbound GitHub/Jira webhooks."""
    web_app = make_webhook_app(gateway, slack_client, github_bot_login=github_bot_login)
    runner = _make_webhook_runner(web_app)
    await runner.setup()
    host = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
    port = int(os.environ.get("WEBHOOK_PORT", "8080"))
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Webhook server listening on %s:%d", host, port)
    return runner


async def main():
    global _bot_user_id, _bot_id, _github_bot_login

    _log_capability_summary()

    # Open the database before anything can touch it (session restore and
    # scheduled-message recovery below both hit it).
    try:
        await init_db()
    except sqlite3.OperationalError as exc:
        # The classic cause is a DB_PATH whose directory doesn't exist or
        # isn't writable by this user (in Docker: a named volume created
        # root-owned by an older image). Say so instead of dumping a
        # thread-teardown traceback.
        sys.exit(
            f"Could not open the SQLite database at "
            f"{os.environ.get('DB_PATH', './bitclaw.db')}: {exc}. "
            "Check that the directory exists and is writable by this user — "
            "in Docker, `docker volume rm` any data volume created by an "
            "older image so it is re-initialized with the right ownership."
        )

    try:
        auth = await app.client.auth_test()
    except Exception as exc:
        sys.exit(
            f"Slack rejected the bot token (auth.test failed: {exc}). "
            "Check SLACK_BOT_TOKEN — it must be the xoxb- token from the "
            "app's OAuth & Permissions page, after installing the app."
        )
    _bot_user_id = auth["user_id"]
    _bot_id = auth.get("bot_id")
    logger.info(
        "Bot user id: %s (%s)  bot_id: %s", _bot_user_id, auth.get("user"), _bot_id
    )
    try:
        scopes_header = auth.headers.get("x-oauth-scopes", "") or ""
        granted = {s.strip() for s in scopes_header.split(",") if s.strip()}
    except Exception:
        granted = set()
        scopes_header = "unavailable"
    logger.info("Bot scopes: %s", scopes_header or "(empty)")
    for needed in ("app_mentions:read", "channels:history", "chat:write"):
        if granted and needed not in granted:
            logger.warning("MISSING SCOPE: %s — bot may not receive events", needed)

    # Silently restore sessions that were active before the last restart.
    # The SDK resumes via sdk_session_id when the next inbound message
    # arrives — zero subprocesses at boot, no Slack post on recovery.
    active = await load_active_sessions()
    if active:
        logger.info("Restoring %d active session(s)...", len(active))
        for row in active:
            await gateway.restore_session(row, app.client)

    # Replay persisted scheduled_messages. Must precede both receivers so
    # concurrent schedule_message calls cannot race the snapshot we iterate.
    pastdue_task = await recover_scheduled_messages(gateway, app.client)

    _github_bot_login = await _resolve_github_bot_login()
    runner = await _start_webhook_server(app.client, _github_bot_login)
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        sys.exit(
            "SLACK_APP_TOKEN is not set — Socket Mode needs the xapp- token "
            "(see .env.example)."
        )
    handler = AsyncSocketModeHandler(app, app_token)

    # Idle-TTL sweeper reclaims SDK/MCP subprocesses for abandoned threads.
    eviction_task = gateway.start_idle_eviction()
    # Inactivity auto-close sweeper durably closes long-silent sessions so
    # their DB rows stop being restored on every restart.
    autoclose_task = gateway.start_autoclose(app.client)

    logger.info("Connecting to Slack via Socket Mode… (pid=%d)", os.getpid())
    try:
        await handler.start_async()
    finally:
        # Disconnect the Socket Mode websocket and close its HTTP session
        # first — otherwise shutdown ends with aiohttp "Unclosed client
        # session" noise from the still-open Slack connection.
        try:
            await handler.close_async()
        except Exception:
            logger.debug("socket handler close failed during shutdown", exc_info=True)
        for task in (eviction_task, autoclose_task, pastdue_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await runner.cleanup()
        # Shared aiohttp session (GitHub trust/visibility checks) — after the
        # listeners, so nothing is left that could request on it.
        await close_http_session()
        # Close the DB LAST so any teardown writes in the steps above still
        # have a live connection.
        await close_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl-C: asyncio.run() has already cancelled main() and executed its
        # finally-block cleanup (DB closed, listeners stopped) — the re-raised
        # KeyboardInterrupt would just dump a scary but meaningless traceback.
        print("bitclaw: interrupted — shut down.")

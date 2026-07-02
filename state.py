"""SQLite persistence for sessions, anchors, and durable scheduled messages.

A session is the bot's unit of work. Each session has at most one Slack thread
for primary communication, plus zero or more anchors binding it to external
entities (GitHub PRs, Jira issues, RDE sessions). Inbound events arrive via an
anchor; the gateway resolves the anchor to a session and routes the event into
that session's inbox. Storage is one module-level aiosqlite connection opened
by ``init_db()``; timestamps are ISO-8601 UTC strings throughout.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import aiosqlite

logger = logging.getLogger(__name__)

# Single module-level connection, opened by init_db(). aiosqlite funnels all
# statements for one connection through one worker thread, and SQLite itself
# is a single-writer database, so a shared connection is the simplest correct
# choice for this single-process bot.
_conn: aiosqlite.Connection | None = None

# Serializes check-then-write sequences (claim_or_create_session's
# check-then-insert, schedule_message_persist's check-then-upsert): between a
# coroutine's SELECT and its INSERT another coroutine could interleave on the
# same connection and both would "create". An asyncio.Lock is sufficient for a
# single process; a multi-process deployment would need a real database-side
# lock instead (e.g. Postgres advisory locks).
_write_lock = asyncio.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slack_channel TEXT,
  slack_thread_ts TEXT,
  sdk_session_id TEXT,
  stage TEXT NOT NULL DEFAULT 'active',          -- 'active' | 'done'
  created_at TEXT NOT NULL,                      -- ISO-8601 UTC
  updated_at TEXT NOT NULL                       -- ISO-8601 UTC
);

CREATE TABLE IF NOT EXISTS session_anchors (
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  session_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (kind, key),
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scheduled_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  task_id TEXT NOT NULL,
  fire_at TEXT NOT NULL,
  message TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled|running|done|failed|cancelled
  failed_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (session_id, task_id),
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
"""


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string.

    ISO-8601 UTC strings compare correctly lexicographically (string order ==
    chronological order), so date cutoffs can be plain ``WHERE updated_at < ?``
    with no parsing on the SQL side.
    """
    return datetime.now(timezone.utc).isoformat()


def _db() -> aiosqlite.Connection:
    """Return the live connection, or raise a clear error if init_db never ran."""
    if _conn is None:
        raise RuntimeError("database not initialized; call init_db() first")
    return _conn


# ----------------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------------

async def init_db() -> None:
    """Open the module-level connection and create the schema. Idempotent.

    The DB path comes from the ``DB_PATH`` env var (default ``./bitclaw.db``),
    read at THIS call time — importing the module never touches the disk.
    """
    global _conn
    if _conn is not None:
        return
    db_path = os.environ.get("DB_PATH", "./bitclaw.db")
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    # WAL lets readers proceed during a write. foreign_keys is OFF by default
    # in SQLite and must be enabled per connection, or ON DELETE CASCADE on
    # the anchor/scheduled-message tables silently does nothing.
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.executescript(_SCHEMA)
    await conn.commit()
    _conn = conn
    logger.info("SQLite database ready at %s", db_path)


async def close_db() -> None:
    """Close the connection. Idempotent — call from shutdown's finally."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


# ----------------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------------

async def update_session(
    session_id: int,
    *,
    slack_channel: str | None = None,
    slack_thread_ts: str | None = None,
    sdk_session_id: str | None = None,
    stage: str | None = None,
) -> None:
    """Partial update; None means 'leave this column alone'. Bumps updated_at."""
    fields: list[str] = []
    values: list = []
    if slack_channel is not None:
        fields.append("slack_channel = ?")
        values.append(slack_channel)
    if slack_thread_ts is not None:
        fields.append("slack_thread_ts = ?")
        values.append(slack_thread_ts)
    if sdk_session_id is not None:
        fields.append("sdk_session_id = ?")
        values.append(sdk_session_id)
    if stage is not None:
        fields.append("stage = ?")
        values.append(stage)
    if not fields:
        return
    fields.append("updated_at = ?")
    values.append(_now_iso())
    values.append(session_id)
    db = _db()
    await db.execute(
        f"UPDATE sessions SET {', '.join(fields)} WHERE id = ?", values
    )
    await db.commit()


async def clear_sdk_session_id(session_id: int) -> None:
    """NULL out sdk_session_id so the next rebuild starts a fresh CLI conversation.

    Why a dedicated function: update_session treats None as 'leave alone' to
    keep partial updates safe, so it can never clear a column. We need this
    when the Claude Code CLI rejects a resume target with 'No conversation
    found' — the conversation file under ~/.claude/projects/ is gone (e.g. a
    container restart wiped it) and retrying the same id would loop forever.
    """
    db = _db()
    await db.execute(
        "UPDATE sessions SET sdk_session_id = NULL, updated_at = ? WHERE id = ?",
        (_now_iso(), session_id),
    )
    await db.commit()


async def get_session(session_id: int) -> dict | None:
    """Fetch a session by id, with ``anchors: [(kind, key), ...]`` attached."""
    db = _db()
    cur = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    s = dict(row)
    cur = await db.execute(
        "SELECT kind, key FROM session_anchors WHERE session_id = ? "
        "ORDER BY created_at",
        (session_id,),
    )
    s["anchors"] = [(a["kind"], a["key"]) for a in await cur.fetchall()]
    return s


async def get_session_stage(session_id: int) -> str | None:
    """Fetch just the stage column; None if the row is gone.

    The done-session gate runs on every webhook and thread-reply delivery, so
    it must not pay get_session's full row-plus-anchors read.
    """
    db = _db()
    cur = await db.execute(
        "SELECT stage FROM sessions WHERE id = ?", (session_id,)
    )
    row = await cur.fetchone()
    return row["stage"] if row else None


async def touch_session(session_id: int) -> None:
    """Bump updated_at to now — the inactivity clock for durable auto-close.

    Called from the gateway's _enqueue chokepoint on every inbound event
    (Slack message, webhook/anchor delivery, scheduler fire), so updated_at
    reliably means 'last inbound event'. Plain session restore on startup
    deliberately does NOT touch — a session idle past the auto-close threshold
    that survives a restart must still get swept.
    """
    db = _db()
    await db.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        (_now_iso(), session_id),
    )
    await db.commit()


async def close_session(session_id: int) -> None:
    """Soft close: stage='done', row kept.

    The row is never deleted, so anchors keep resolving forever — every
    delivery edge must gate on stage explicitly rather than relying on the
    anchor row's absence.
    """
    db = _db()
    await db.execute(
        "UPDATE sessions SET stage = 'done', updated_at = ? WHERE id = ?",
        (_now_iso(), session_id),
    )
    await db.commit()


async def reopen_session(session_id: int) -> bool:
    """Flip a soft-closed session back to 'active'. Returns True if it was done.

    The WHERE stage='done' guard makes this safe to call unconditionally — an
    already-active row is untouched (returns False). updated_at is bumped so a
    reopened session starts a fresh inactivity window instead of being
    immediately re-swept by auto-close.
    """
    db = _db()
    cur = await db.execute(
        "UPDATE sessions SET stage = 'active', updated_at = ? "
        "WHERE id = ? AND stage = 'done'",
        (_now_iso(), session_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def load_active_sessions() -> list[dict]:
    """Return all non-done sessions with their anchors attached.

    Two queries total: the anchors for ALL returned sessions come back in one
    batched SELECT (never one query per session — this runs on every startup
    against however many sessions accumulated).
    """
    db = _db()
    cur = await db.execute(
        "SELECT * FROM sessions WHERE stage != 'done' ORDER BY id"
    )
    by_id: dict[int, dict] = {}
    for row in await cur.fetchall():
        s = dict(row)
        s["anchors"] = []
        by_id[s["id"]] = s
    if by_id:
        placeholders = ",".join("?" for _ in by_id)
        cur = await db.execute(
            f"SELECT session_id, kind, key FROM session_anchors "
            f"WHERE session_id IN ({placeholders}) "
            f"ORDER BY session_id, created_at",
            list(by_id.keys()),
        )
        for a in await cur.fetchall():
            by_id[a["session_id"]]["anchors"].append((a["kind"], a["key"]))
    return list(by_id.values())


async def load_stale_active_sessions(idle_days: int) -> list[dict]:
    """Return non-done sessions idle (no updated_at bump) for >= idle_days.

    Minimal projection — the auto-close sweeper only needs the id and whether
    a slack_channel exists. The cutoff comparison is plain string <: ISO-8601
    UTC timestamps sort lexicographically in chronological order.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=int(idle_days))
    ).isoformat()
    db = _db()
    cur = await db.execute(
        "SELECT id, slack_channel FROM sessions "
        "WHERE stage != 'done' AND updated_at < ? ORDER BY id",
        (cutoff,),
    )
    return [dict(r) for r in await cur.fetchall()]


# ----------------------------------------------------------------------------
# Anchors
# ----------------------------------------------------------------------------

async def attach_anchor(session_id: int, kind: str, key: str) -> None:
    """Bind (kind, key) to a session. Re-anchoring MOVES ownership: an
    INSERT OR REPLACE on the (kind, key) primary key repoints an existing
    anchor at the new session instead of failing."""
    db = _db()
    await db.execute(
        "INSERT OR REPLACE INTO session_anchors (kind, key, session_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        (kind, key, session_id, _now_iso()),
    )
    await db.commit()


async def lookup_anchor(kind: str, key: str) -> int | None:
    db = _db()
    cur = await db.execute(
        "SELECT session_id FROM session_anchors WHERE kind = ? AND key = ?",
        (kind, key),
    )
    row = await cur.fetchone()
    return row["session_id"] if row else None


async def list_anchors_for_session(session_id: int) -> list[tuple[str, str]]:
    """Return [(kind, key), ...] for one session, in insertion order."""
    db = _db()
    cur = await db.execute(
        "SELECT kind, key FROM session_anchors "
        "WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    )
    return [(r["kind"], r["key"]) for r in await cur.fetchall()]


async def claim_or_create_session(
    kind: str,
    key: str,
    *,
    slack_channel: str | None,
    slack_thread_ts: str | None,
) -> tuple[int, bool]:
    """Atomically resolve (kind, key) to a session, creating one if absent.

    Returns (session_id, created). Two parallel callers with the same
    (kind, key) — e.g. two webhooks for the same PR arriving together —
    serialize on the module write lock, so exactly one sees created=True; the
    other gets the winner's session_id with created=False. Callers gate
    post-claim side effects (like enriching the first message) on `created`.

    NOTE: an asyncio.Lock only serializes coroutines in THIS process. A
    multi-process deployment needs a real DB-side lock for this check-then-
    insert (e.g. Postgres advisory locks keyed on kind+":"+key).
    """
    db = _db()
    async with _write_lock:
        cur = await db.execute(
            "SELECT session_id FROM session_anchors WHERE kind = ? AND key = ?",
            (kind, key),
        )
        row = await cur.fetchone()
        if row:
            return row["session_id"], False
        now = _now_iso()
        cur = await db.execute(
            "INSERT INTO sessions "
            "(slack_channel, slack_thread_ts, stage, created_at, updated_at) "
            "VALUES (?, ?, 'active', ?, ?)",
            (slack_channel, slack_thread_ts, now, now),
        )
        session_id = cur.lastrowid
        await db.execute(
            "INSERT INTO session_anchors (kind, key, session_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (kind, key, session_id, now),
        )
        await db.commit()
        return session_id, True


# ----------------------------------------------------------------------------
# Anchor key conventions — keep callers from hand-formatting strings
# ----------------------------------------------------------------------------

def slack_anchor_key(channel: str, thread_ts: str) -> str:
    return f"{channel}:{thread_ts}"


def github_pr_anchor_key(repo_full_name: str, pr_number: int) -> str:
    return f"{repo_full_name}#{pr_number}"


# ----------------------------------------------------------------------------
# Scheduled messages (durability of mcp__sched__schedule_message)
# ----------------------------------------------------------------------------

async def schedule_message_persist(
    session_id: int,
    task_id: str,
    fire_at: datetime,
    message: str,
) -> tuple[int, bool]:
    """Create or reset a scheduled_messages row to 'scheduled'.

    Returns (row_id, is_fresh). is_fresh=False iff a prior row for this
    (session_id, task_id) is still LIVE (status 'scheduled' or 'running') —
    a live task is never overwritten; the caller surfaces 'already_scheduled'
    to the agent. Otherwise (no row, or a terminal done/failed/cancelled row)
    the UPSERT resets it to a clean 'scheduled' state: new fire_at + message,
    failed_reason cleared → (row_id, True).

    ``fire_at`` must be a timezone-aware datetime; it is stored as an ISO
    string and parsed back by load_pending_scheduled_messages.
    """
    db = _db()
    async with _write_lock:
        cur = await db.execute(
            "SELECT id, status FROM scheduled_messages "
            "WHERE session_id = ? AND task_id = ?",
            (session_id, task_id),
        )
        row = await cur.fetchone()
        if row and row["status"] in ("scheduled", "running"):
            return row["id"], False
        now = _now_iso()
        await db.execute(
            """INSERT INTO scheduled_messages
                 (session_id, task_id, fire_at, message, status,
                  failed_reason, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'scheduled', NULL, ?, ?)
               ON CONFLICT (session_id, task_id) DO UPDATE SET
                 fire_at = excluded.fire_at,
                 message = excluded.message,
                 status = 'scheduled',
                 failed_reason = NULL,
                 updated_at = excluded.updated_at""",
            (session_id, task_id, fire_at.isoformat(), message, now, now),
        )
        # Re-SELECT for the row id (covers both the insert and the
        # upsert-update path; lastrowid is unreliable on the update path).
        cur = await db.execute(
            "SELECT id FROM scheduled_messages "
            "WHERE session_id = ? AND task_id = ?",
            (session_id, task_id),
        )
        row = await cur.fetchone()
        await db.commit()
        return row["id"], True


async def mark_scheduled_running(row_id: int) -> None:
    """Transition scheduled → running."""
    db = _db()
    await db.execute(
        "UPDATE scheduled_messages SET status = 'running', updated_at = ? "
        "WHERE id = ?",
        (_now_iso(), row_id),
    )
    await db.commit()


async def mark_scheduled_done(row_id: int) -> None:
    """Transition any non-terminal → done."""
    db = _db()
    await db.execute(
        "UPDATE scheduled_messages SET status = 'done', updated_at = ? "
        "WHERE id = ?",
        (_now_iso(), row_id),
    )
    await db.commit()


async def mark_scheduled_failed(row_id: int, reason: str) -> None:
    """Transition any non-terminal → failed with a (truncated) reason."""
    db = _db()
    await db.execute(
        "UPDATE scheduled_messages "
        "SET status = 'failed', failed_reason = ?, updated_at = ? "
        "WHERE id = ?",
        (reason[:500], _now_iso(), row_id),
    )
    await db.commit()


async def mark_scheduled_cancelled(session_id: int, task_id: str) -> None:
    """Cancel by (session, task_id). Idempotent — the status guard means a
    missing or already-terminal row is a no-op, so cancel can never resurrect
    or overwrite a done/failed row."""
    db = _db()
    await db.execute(
        "UPDATE scheduled_messages SET status = 'cancelled', updated_at = ? "
        "WHERE session_id = ? AND task_id = ? "
        "AND status IN ('scheduled', 'running')",
        (_now_iso(), session_id, task_id),
    )
    await db.commit()


async def load_pending_scheduled_messages() -> list[dict]:
    """Snapshot of all scheduled or running rows, oldest first.

    Returned dicts carry {id, session_id, task_id, fire_at, message, status}
    with ``fire_at`` parsed back into a timezone-aware datetime (it was stored
    as a tz-aware ISO string; a naive value is defensively assumed UTC).
    """
    db = _db()
    cur = await db.execute(
        "SELECT id, session_id, task_id, fire_at, message, status "
        "FROM scheduled_messages "
        "WHERE status IN ('scheduled', 'running') ORDER BY id"
    )
    out: list[dict] = []
    for r in await cur.fetchall():
        row = dict(r)
        fire_at = datetime.fromisoformat(row["fire_at"])
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=timezone.utc)
        row["fire_at"] = fire_at
        out.append(row)
    return out

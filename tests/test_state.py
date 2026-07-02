"""SQLite persistence invariants (state.py), against a throwaway DB file.

NOTE: sessions rows are never hard-deleted in production (close_session is a
SOFT close so anchors keep resolving); the cascade test below deletes a row
directly only to prove the FK wiring (PRAGMA foreign_keys) actually works.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import state


async def _claim(kind="slack_thread", key="C1:1.0"):
    sid, created = await state.claim_or_create_session(
        kind, key, slack_channel="C1", slack_thread_ts="1.0"
    )
    return sid, created


async def _backdate_session(session_id, days):
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = state._db()  # test-only introspection of the module connection
    await conn.execute("UPDATE sessions SET updated_at=? WHERE id=?",
                       (old, session_id))
    await conn.commit()


async def _sched_status(session_id, task_id):
    cur = await state._db().execute(
        "SELECT status FROM scheduled_messages WHERE session_id=? AND task_id=?",
        (session_id, task_id),
    )
    return (await cur.fetchone())["status"]


async def test_claim_or_create_is_idempotent(db):
    """Same (kind, key) resolves to the same session; only the first creates."""
    sid1, created1 = await _claim()
    sid2, created2 = await _claim()
    assert created1 is True and created2 is False
    assert sid1 == sid2


async def test_claim_or_create_concurrent_single_winner(db):
    """5 parallel claims of one key (e.g. a webhook burst for the same PR)
    serialize on the write lock: exactly one created=True, one session row."""
    results = await asyncio.gather(*[_claim(key="C9:9.0") for _ in range(5)])
    ids = {sid for sid, _ in results}
    assert len(ids) == 1
    assert sum(1 for _, created in results if created) == 1


async def test_soft_close_and_reopen(db):
    """close is stage='done' with the row KEPT; reopen flips back exactly once."""
    sid, _ = await _claim()
    await state.close_session(sid)
    assert await state.get_session_stage(sid) == "done"
    assert await state.lookup_anchor("slack_thread", "C1:1.0") == sid  # anchor lives
    assert await state.reopen_session(sid) is True
    assert await state.get_session_stage(sid) == "active"
    assert await state.reopen_session(sid) is False  # already active → no-op


async def test_update_session_none_means_keep_and_explicit_clear(db):
    """Partial update semantics: None leaves a column alone — which is exactly
    why clearing sdk_session_id needs its own dedicated function."""
    sid, _ = await _claim()
    await state.update_session(sid, sdk_session_id="sdk-abc")
    await state.update_session(sid, stage="done")  # sdk_session_id not mentioned
    row = await state.get_session(sid)
    assert row["sdk_session_id"] == "sdk-abc" and row["stage"] == "done"
    await state.update_session(sid)  # all None → harmless no-op
    await state.clear_sdk_session_id(sid)
    assert (await state.get_session(sid))["sdk_session_id"] is None


async def test_schedule_persist_live_vs_terminal(db):
    """A LIVE (scheduled/running) row is never overwritten (is_fresh=False);
    a terminal row is UPSERT-reset to a clean 'scheduled' state."""
    sid, _ = await _claim()
    fire_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    row_id, fresh = await state.schedule_message_persist(sid, "poll", fire_at, "v1")
    assert fresh is True
    row_id2, fresh2 = await state.schedule_message_persist(sid, "poll", fire_at, "v2")
    assert fresh2 is False and row_id2 == row_id  # live row protected
    await state.mark_scheduled_done(row_id)
    row_id3, fresh3 = await state.schedule_message_persist(sid, "poll", fire_at, "v3")
    assert fresh3 is True and row_id3 == row_id  # terminal → reset in place
    pending = await state.load_pending_scheduled_messages()
    assert [(r["task_id"], r["message"]) for r in pending] == [("poll", "v3")]


async def test_mark_cancelled_never_resurrects_terminal_rows(db):
    """The status guard: cancelling a done/failed row is a no-op, so a late
    cancel can't rewrite history."""
    sid, _ = await _claim()
    fire_at = datetime.now(timezone.utc)
    row_id, _ = await state.schedule_message_persist(sid, "t", fire_at, "m")
    await state.mark_scheduled_done(row_id)
    await state.mark_scheduled_cancelled(sid, "t")
    assert await _sched_status(sid, "t") == "done"


async def test_load_stale_active_sessions_cutoff(db):
    """The auto-close sweeper sees only non-done sessions idle >= the cutoff;
    ISO-8601 UTC strings compare correctly as plain strings."""
    fresh_sid, _ = await _claim(key="C1:fresh")
    stale_sid, _ = await _claim(key="C1:stale")
    done_sid, _ = await _claim(key="C1:done-stale")
    await _backdate_session(stale_sid, days=8)
    await _backdate_session(done_sid, days=8)
    await state.close_session(done_sid)  # done rows are never swept...
    await _backdate_session(done_sid, days=8)  # ...even when ancient
    stale = await state.load_stale_active_sessions(idle_days=7)
    assert [r["id"] for r in stale] == [stale_sid]
    assert fresh_sid not in {r["id"] for r in stale}


async def test_load_pending_fire_at_tz_aware_roundtrip(db):
    """fire_at survives the ISO-string round-trip timezone-aware and equal."""
    sid, _ = await _claim()
    fire_at = datetime(2026, 7, 2, 12, 30, tzinfo=timezone.utc)
    await state.schedule_message_persist(sid, "t", fire_at, "m")
    [row] = await state.load_pending_scheduled_messages()
    assert row["fire_at"] == fire_at
    assert row["fire_at"].tzinfo is not None


async def test_anchor_cascade_on_hard_delete(db):
    """FK CASCADE is wired (PRAGMA foreign_keys=ON — silently off by default in
    SQLite). Prod never hard-deletes sessions; this guards the schema itself."""
    sid, _ = await _claim(key="C1:c")
    await state.attach_anchor(sid, "jira_issue", "PROJ-1")
    conn = state._db()
    await conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    await conn.commit()
    assert await state.lookup_anchor("jira_issue", "PROJ-1") is None
    assert await state.lookup_anchor("slack_thread", "C1:c") is None

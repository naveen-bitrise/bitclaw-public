"""Self-wake-up scheduler (tools/scheduling.py): fire → enqueue + durable
'done'; cancel-before-fire; the user-stop latch; duplicate-task refusal;
delay clamping; and the done-session drop.

_schedule clamps delays to >= 1s, so the fire tests monkeypatch asyncio.sleep
(a zero-yield stand-in) instead of really sleeping — deterministic and instant.
"""
import asyncio
import contextlib
from types import SimpleNamespace

import pytest

import state
import tools.scheduling as scheduling_module
from conftest import capture_tool_handlers, tool_payload
from tools.scheduling import SchedulingTools


@pytest.fixture
async def sched(db, monkeypatch):
    sid, _ = await state.claim_or_create_session(
        "slack_thread", "C1:1.0", slack_channel="C1", slack_thread_ts="1.0"
    )
    fired: list[tuple] = []

    async def enqueue(session_id, message):
        fired.append((session_id, message))

    st = SchedulingTools(sid, enqueue)
    handlers = capture_tool_handlers(monkeypatch, scheduling_module,
                                     st.build_mcp_server)
    yield SimpleNamespace(sid=sid, st=st, handlers=handlers, fired=fired)
    st.cancel_pending()
    # Let cancelled fire-tasks and their fire-and-forget DB marks settle
    # before the db fixture closes the connection.
    await asyncio.gather(*st._tasks.values(), return_exceptions=True)
    await asyncio.sleep(0.05)


def _fast_sleep(monkeypatch):
    """Neutralize the fire delay: asyncio.sleep becomes a bare yield."""
    real_sleep = asyncio.sleep

    async def fast(_delay):
        await real_sleep(0)

    monkeypatch.setattr(scheduling_module.asyncio, "sleep", fast)


async def _row_status(sid, task_id):
    cur = await state._db().execute(
        "SELECT status FROM scheduled_messages WHERE session_id=? AND task_id=?",
        (sid, task_id),
    )
    return (await cur.fetchone())["status"]


async def test_schedule_fires_enqueues_and_marks_done(sched, monkeypatch):
    """The full happy path: schedule persists a row, the timer fires, the
    message lands in the session's OWN inbox verbatim, and the row goes done."""
    _fast_sleep(monkeypatch)
    result = tool_payload(await sched.handlers["schedule_message"](
        {"task_id": "poll-rde", "delay_seconds": 120,
         "message": "Check the RDE tmux pane for new output"}))
    assert result["status"] == "scheduled"
    await sched.st._tasks["poll-rde"]                    # deterministic wait
    assert sched.fired == [(sched.sid, "Check the RDE tmux pane for new output")]
    assert await _row_status(sched.sid, "poll-rde") == "done"


async def test_cancel_before_fire(sched):
    """cancel_scheduled_task stops an armed timer: no enqueue, row cancelled."""
    await sched.handlers["schedule_message"](
        {"task_id": "poll", "delay_seconds": 3600, "message": "wake up"})
    task = sched.st._tasks["poll"]
    result = tool_payload(await sched.handlers["cancel_scheduled_task"](
        {"task_id": "poll"}))
    assert result["status"] == "cancelled"
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert sched.fired == []
    assert await _row_status(sched.sid, "poll") == "cancelled"


async def test_begin_stop_latch_rejects_new_wakeups(sched):
    """During a user stop the dying turn must not re-arm its own restart: the
    latch rejects schedule_message before anything is persisted."""
    sched.st.begin_stop()
    result = tool_payload(await sched.handlers["schedule_message"](
        {"task_id": "sneaky", "delay_seconds": 60, "message": "resume work"}))
    assert result["status"] == "error"
    assert "stopped" in result["reason"]
    assert "sneaky" not in sched.st._tasks
    sched.st.end_stop()
    result = tool_payload(await sched.handlers["schedule_message"](
        {"task_id": "ok-again", "delay_seconds": 60, "message": "m"}))
    assert result["status"] == "scheduled"


async def test_duplicate_live_task_reports_already_scheduled(sched):
    """A live task_id is never overwritten — in-memory dup AND a DB-only live
    row (a fresh SchedulingTools instance, the recovery-race shape) both
    surface as already_scheduled."""
    await sched.handlers["schedule_message"](
        {"task_id": "poll", "delay_seconds": 3600, "message": "v1"})
    dup = tool_payload(await sched.handlers["schedule_message"](
        {"task_id": "poll", "delay_seconds": 10, "message": "v2"}))
    assert dup["status"] == "already_scheduled"

    rebuilt = SchedulingTools(sched.sid, sched.st._enqueue)  # empty _tasks
    assert (await rebuilt._schedule("poll", 10, "v3"))["status"] == "already_scheduled"


@pytest.mark.parametrize("requested,clamped", [(0, 1), (-5, 1), (999999999, 86400)])
async def test_delay_clamped_to_1s_24h(sched, requested, clamped):
    result = tool_payload(await sched.handlers["schedule_message"](
        {"task_id": f"clamp-{requested}", "delay_seconds": requested,
         "message": "m"}))
    assert result["fires_in_seconds"] == clamped


async def test_fire_on_done_session_drops_and_cancels(sched, monkeypatch):
    """A timer outliving its session must not deliver: a done session drops
    the fire and marks the row cancelled (the done-session gate, timer edge)."""
    _fast_sleep(monkeypatch)
    await state.close_session(sched.sid)
    await sched.handlers["schedule_message"](
        {"task_id": "late", "delay_seconds": 60, "message": "zombie wake-up"})
    await sched.st._tasks["late"]
    assert sched.fired == []
    assert await _row_status(sched.sid, "late") == "cancelled"

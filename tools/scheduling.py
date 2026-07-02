"""
Self-message scheduler.

The agent calls schedule_message to deliver a message into its OWN
inbox after a delay. The same ClaudeSDKClient processes the delivered
message on its next turn with full conversation history — there is no
subagent. Use case: polling the RDE tmux for new output when there is
no webhook coverage.

The in-memory asyncio.Task registry is volatile across process
restarts; durability comes from the `scheduled_messages` table in
SQLite (write-through on schedule, recovery on startup).
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from claude_agent_sdk import tool, create_sdk_mcp_server

from state import (
    get_session,
    mark_scheduled_cancelled,
    mark_scheduled_done,
    mark_scheduled_failed,
    mark_scheduled_running,
    schedule_message_persist,
)

logger = logging.getLogger(__name__)


def _ok(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


# Type of the gateway-side enqueue callback. The slack_client is closed
# over per-session at construction time so the agent never sees it.
EnqueueFn = Callable[[int, str], Awaitable[None]]


class SchedulingTools:
    def __init__(self, session_id: int, enqueue: EnqueueFn):
        self.session_id = session_id
        self._enqueue = enqueue
        # Per-instance registry — task_id is scoped per session.
        self._tasks: dict[str, asyncio.Task] = {}
        # User-stop latch: while True, _schedule rejects new wake-ups. The
        # sched MCP server is in-process, so a turn being stopped can still
        # land schedule_message calls until it actually dies — without the
        # latch, the work the human just halted re-arms its own restart.
        self._stopping = False

    def build_mcp_server(self):
        @tool(
            "schedule_message",
            (
                "Schedule a message to be delivered to your OWN inbox after a delay "
                "(clamped 1s..24h). The same conversation processes the delivered "
                "message on its next turn — there is no subagent. Use for "
                "polling external state that has no webhook coverage "
                "(e.g. RDE tmux output). Message lands verbatim; craft it to be "
                "self-explanatory ('Check the RDE tmux pane for new output'). "
                "Returns 'already_scheduled' if a task with the same task_id is "
                "pending; cancel it first if you need to replace it."
            ),
            {"task_id": str, "delay_seconds": int, "message": str},
        )
        async def schedule_message(args):
            return _ok(await self._schedule(
                args["task_id"], int(args["delay_seconds"]), args["message"],
            ))

        @tool(
            "check_scheduled_task",
            "Check status of a scheduled task: scheduled | running | done | failed: <reason> | cancelled | not_found.",
            {"task_id": str},
        )
        async def check_scheduled_task(args):
            return _ok(self._check(args["task_id"]))

        @tool(
            "cancel_scheduled_task",
            "Cancel a pending scheduled task before it fires. No-op if already fired or unknown.",
            {"task_id": str},
        )
        async def cancel_scheduled_task(args):
            return _ok(await self._cancel(args["task_id"]))

        return create_sdk_mcp_server(
            name="sched",
            version="1.0.0",
            tools=[schedule_message, check_scheduled_task, cancel_scheduled_task],
        )

    @staticmethod
    def tool_names() -> list[str]:
        return [
            "mcp__sched__schedule_message",
            "mcp__sched__check_scheduled_task",
            "mcp__sched__cancel_scheduled_task",
        ]

    def has_pending_tasks(self) -> bool:
        """True if any scheduled task is still pending (armed, not yet fired).

        The shared predicate behind both 'keep the session in memory' (the
        gateway's idle-eviction sweep) and 'keep the live status across the
        cross-turn wait' (the agent loop's end_turn handling).
        """
        return any(not t.done() for t in self._tasks.values())

    def begin_stop(self) -> None:
        """Latch out new schedule_message calls for the span of a user stop."""
        self._stopping = True

    def end_stop(self) -> None:
        """Lift the user-stop latch; called after the stop's final sweep."""
        self._stopping = False

    def cancel_pending(self) -> int:
        """Cancel every armed, not-yet-fired task; returns how many were cancelled.

        Two callers, same need. The gateway's evict-on-close: a closed
        session's timers must not outlive its Session — after a human reopen,
        the rebuilt SchedulingTools has an empty _tasks dict, so the orphan
        could neither be seen nor cancelled, and its stale fire would deliver
        into the reopened conversation. And the user-stop path: a self-wake-up
        left armed would restart the very work the human just halted (e.g. an
        rde-poll firing two minutes after the stop).

        The sleeping task's own CancelledError handler marks its DB row
        cancelled — but a task cancelled before its coroutine ever ran never
        enters that handler (throwing into a not-yet-started coroutine skips
        the body), which would leave an orphaned 'scheduled' row that startup
        recovery re-arms after a restart. So the row is ALSO marked here, on a
        background task (the UPDATE's status guard makes the double mark
        idempotent; the method itself stays sync for its sync call sites).
        """
        cancelled = 0
        for task_id, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
                self._mark_cancelled_bg(task_id)
                cancelled += 1
        return cancelled

    def _mark_cancelled_bg(self, task_id: str) -> None:
        """Fire-and-forget DB mark for a cancelled task; never raises."""
        async def _mark():
            try:
                await mark_scheduled_cancelled(self.session_id, task_id)
            except Exception:
                logger.debug(
                    "Background cancel-mark failed for task %s (session %d)",
                    task_id, self.session_id, exc_info=True,
                )
        asyncio.create_task(_mark())

    async def _schedule(self, task_id: str, delay_seconds: int, message: str) -> dict:
        if self._stopping:
            # A human stop is sweeping this session; rejecting (before any
            # persist) is what keeps the dying turn from re-arming itself.
            return {
                "status": "error",
                "reason": "session is being stopped by the user — not scheduling",
            }
        if not task_id or not message or not message.strip():
            return {"status": "error", "reason": "task_id and message must be non-empty"}
        existing = self._tasks.get(task_id)
        if existing is not None and not existing.done():
            return {"status": "already_scheduled", "task_id": task_id}

        delay = max(1, min(int(delay_seconds), 86400))
        fire_at = datetime.now(timezone.utc) + timedelta(seconds=delay)

        row_id, is_fresh = await schedule_message_persist(
            self.session_id, task_id, fire_at, message,
        )
        if not is_fresh:
            # DB already had a live row — recovery may not yet have re-armed it
            # in-memory, or another caller raced us. Surface as already_scheduled.
            return {"status": "already_scheduled", "task_id": task_id}

        task = asyncio.create_task(
            self._schedule_in_memory_fire(task_id, delay, message, row_id)
        )
        self._tasks[task_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(task_id, None))
        logger.info(
            "Scheduled task %s (session %d) to fire in %ds (row %d)",
            task_id, self.session_id, delay, row_id,
        )
        return {"status": "scheduled", "task_id": task_id, "fires_in_seconds": delay}

    async def _schedule_in_memory_fire(
        self, task_id: str, delay: float, message: str, row_id: int,
    ) -> None:
        """Sleep, then deliver, persisting state transitions throughout."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            # Cancelled during sleep — cancel_scheduled_task path.
            await mark_scheduled_cancelled(self.session_id, task_id)
            logger.info(
                "Scheduled task %s (session %d) cancelled before firing",
                task_id, self.session_id,
            )
            raise

        # Session-state guard: closed session ⇒ drop and mark cancelled.
        sess_row = await get_session(self.session_id)
        if sess_row is None or sess_row.get("stage") == "done":
            logger.info(
                "Scheduled task %s for session %d: session gone/done — dropping",
                task_id, self.session_id,
            )
            await mark_scheduled_cancelled(self.session_id, task_id)
            return

        await mark_scheduled_running(row_id)
        try:
            await self._enqueue(self.session_id, message)
            logger.info(
                "Scheduled task %s (session %d) fired — message enqueued (row %d)",
                task_id, self.session_id, row_id,
            )
        except Exception as exc:
            logger.exception(
                "Scheduled task %s (session %d) — enqueue failed",
                task_id, self.session_id,
            )
            await mark_scheduled_failed(row_id, repr(exc))
            return
        await mark_scheduled_done(row_id)

    def arm_recovered(
        self, task_id: str, delay_seconds: float, message: str, db_row_id: int,
    ) -> None:
        """Recovery-only: arm a sleep-then-fire task for an EXISTING DB row.

        Does NOT call schedule_message_persist. Caller (the recovery hook,
        via gateway.reschedule_after_recovery) must guarantee db_row_id refers
        to a row with status='scheduled'. Idempotent guard: if task_id is
        already armed, no-op.
        """
        if task_id in self._tasks and not self._tasks[task_id].done():
            return
        task = asyncio.create_task(
            self._schedule_in_memory_fire(task_id, delay_seconds, message, db_row_id)
        )
        self._tasks[task_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(task_id, None))

    def _check(self, task_id: str) -> dict:
        task = self._tasks.get(task_id)
        if task is None:
            return {"task_id": task_id, "status": "not_found"}
        if task.cancelled():
            return {"task_id": task_id, "status": "cancelled"}
        if task.done():
            exc = task.exception()
            if exc:
                return {"task_id": task_id, "status": f"failed: {exc!r}"}
            return {"task_id": task_id, "status": "done"}
        return {"task_id": task_id, "status": "scheduled"}

    async def _cancel(self, task_id: str) -> dict:
        task = self._tasks.get(task_id)
        if task and not task.done():
            task.cancel()
            # The CancelledError handler in _schedule_in_memory_fire also calls
            # mark_scheduled_cancelled; calling it here too is idempotent.
            await mark_scheduled_cancelled(self.session_id, task_id)
            return {"task_id": task_id, "status": "cancelled"}
        # Even if in-memory is empty, defensively flip a live DB row
        # (covers the post-restart-pre-recovery edge that should be
        # unreachable but is cheap to guard against).
        await mark_scheduled_cancelled(self.session_id, task_id)
        return {"task_id": task_id, "status": "not_found_or_already_done"}

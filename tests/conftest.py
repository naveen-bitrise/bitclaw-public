"""Shared fixtures and helpers for the bitclaw test suite.

Conventions locked in here:
  * every DB test runs against a throwaway SQLite file (DB_PATH is read at
    init_db() call time, so a monkeypatched env var gives full isolation);
  * SDK hook inputs are plain DICTS ({"tool_name": ..., "tool_input": {...}}) —
    exactly what the Claude Agent SDK delivers (a TypedDict, never an object);
  * in-process MCP tools are exercised through their captured handlers, i.e.
    the exact surface the model calls (args dict in, {"content": [...]} out).
"""
import json
import os

# main.py builds the Slack AsyncApp at import time; give it a dummy token so
# test modules can import it (Socket Mode is never started in tests).
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-a-real-token")

import pytest

import state


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """A fresh SQLite database per test, torn down afterwards."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bitclaw-test.db"))
    await state.close_db()  # drop any connection a crashed test left behind
    await state.init_db()
    yield state
    await state.close_db()


class FakeSlackClient:
    """Records every Slack Web API call SlackTools makes; mints fresh ts."""

    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.statuses: list[dict] = []
        self._counter = 0

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        self._counter += 1
        return {"ts": f"1700000000.{self._counter:06d}"}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}

    async def assistant_threads_setStatus(self, **kwargs):
        self.statuses.append(kwargs)
        return {"ok": True}


@pytest.fixture
def slack():
    return FakeSlackClient()


def capture_tool_handlers(monkeypatch, module, build_fn) -> dict:
    """Capture a module's SdkMcpTool handlers by stubbing create_sdk_mcp_server.

    Lets tests drive the exact in-process tool surface the model calls
    (handler(args_dict) -> {"content": [...]}) without an MCP transport.
    """
    handlers: dict = {}

    def fake_create(name, version, tools):
        handlers.update({t.name: t.handler for t in tools})
        return {"type": "sdk", "name": name}

    monkeypatch.setattr(module, "create_sdk_mcp_server", fake_create)
    build_fn()
    return handlers


def tool_payload(result: dict) -> dict:
    """Decode the JSON payload of an _ok()-shaped tool result."""
    return json.loads(result["content"][0]["text"])


def hook_input(tool_name: str, tool_input: dict | None = None) -> dict:
    """A PreToolUse hook input in the SDK's dict shape (never an object)."""
    return {"tool_name": tool_name, "tool_input": tool_input or {}}


def decision(hook_result: dict) -> str | None:
    """The permissionDecision of a hook result; None ({} = pass-through)."""
    return (hook_result.get("hookSpecificOutput") or {}).get("permissionDecision")


def deny_reason(hook_result: dict) -> str:
    return (hook_result.get("hookSpecificOutput") or {}).get(
        "permissionDecisionReason", ""
    )

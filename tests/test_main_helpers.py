"""Pure helpers in main.py: the user-stop trigger, Slack event dedupe, and
the stop-acknowledgement formatter.

The stop command is a message-TEXT trigger by design (slash-command payloads
carry no thread_ts), so the matcher must be strict: the WHOLE message is a
stop word, or it falls through to the agent as ordinary input.
"""
import pytest

import main

BOT = "U0BOT"


@pytest.mark.parametrize("text", [
    "stop", "STOP", "Stop", "abort", "/stop", "!stop", "!abort",
    "stop.", "stop!", "stop,,", "  stop  ",
    "*stop*", "_stop_", "`abort`",              # Slack sends emphasis literally
    f"<@{BOT}> stop", f"<@{BOT}>, stop", f"<@{BOT}>: abort",
    f"stop <@{BOT}>", f"stop, <@{BOT}>",
])
def test_stop_command_positive(text):
    assert main._is_stop_command(text, BOT) is True


@pytest.mark.parametrize("text", [
    "please stop it", "stop the RDE", "don't stop", "stop now",
    "unstoppable", "stopping", "abort mission", "", "   ",
    f"<@{BOT}>",                       # bare mention is not a stop
    f"<@{BOT}> stop the deploy",       # trailing words → ordinary input
])
def test_stop_command_negative(text):
    """Ordinary sentences containing 'stop' must still reach the agent."""
    assert main._is_stop_command(text, BOT) is False


def test_stop_command_without_known_bot_id():
    assert main._is_stop_command("stop", None) is True
    assert main._is_stop_command("<@U123> stop", None) is False  # can't strip it


def test_already_handled_dedupes_within_ttl(monkeypatch):
    """Slack re-delivers Socket Mode events after reconnects; event_ts is the
    dedupe key and expires after the TTL (duplicates arrive back-to-back)."""
    main._seen_events.clear()
    now = {"t": 1000.0}
    monkeypatch.setattr(main.time, "monotonic", lambda: now["t"])
    ev = {"event_ts": "1718000000.000100"}
    assert main._already_handled(ev) is False   # first sighting
    assert main._already_handled(ev) is True    # duplicate suppressed
    assert main._already_handled({"event_ts": "other.ts"}) is False  # distinct ok
    now["t"] += main._DEDUPE_TTL_SECS + 1
    assert main._already_handled(ev) is False   # TTL expired → treated as new


def test_format_stop_ack_idle():
    assert "No turn was running" in main._format_stop_ack({"disposition": "idle"})


def test_format_stop_ack_graceful_with_counts():
    ack = main._format_stop_ack(
        {"disposition": "graceful", "flushed": 2, "cancelled_wakeups": 1}
    )
    assert "interrupted the in-flight turn" in ack
    assert "discarded 2 queued message(s)" in ack
    assert "cancelled 1 scheduled wake-up(s)" in ack
    assert "RDE coding agent" in ack  # the RDE is NOT stopped — the ack says so


def test_format_stop_ack_forced():
    ack = main._format_stop_ack({"disposition": "forced"})
    assert "(forced)" in ack
    assert "conversation history is preserved" in ack

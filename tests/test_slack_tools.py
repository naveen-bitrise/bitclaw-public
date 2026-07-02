"""SlackTools (tools/slack_tools.py): lazy thread bootstrap, the mandatory
live status, the close-session latch, the external-PR anchor gate, and the
egress approval message's Block Kit shape.

Tool handlers are exercised through the captured in-process MCP surface —
the exact functions the model calls.
"""
import state
import tools.slack_tools as slack_tools_module
from conftest import capture_tool_handlers, tool_payload
from tools.slack_tools import SlackTools, ThreadAnchor


async def make_tools(slack, monkeypatch, pr_trust_check=None):
    """A session with NO Slack thread yet (the Jira-bootstrap shape)."""
    sid, _ = await state.claim_or_create_session(
        "jira_issue", "PROJ-1", slack_channel=None, slack_thread_ts=None
    )
    st = SlackTools(
        slack_client=slack, source_channel="C-SRC", source_thread_ts=None,
        session_id=sid, anchor=ThreadAnchor(channel="C-OUT"),
        pr_trust_check=pr_trust_check,
    )
    handlers = capture_tool_handlers(monkeypatch, slack_tools_module,
                                     st.build_mcp_server)
    return st, handlers, sid


async def test_first_post_bootstraps_thread_and_anchors_it(db, slack, monkeypatch):
    """The first post creates the top-level message, records it as the
    session's thread (anchor row + session columns), and later posts thread."""
    st, _, sid = await make_tools(slack, monkeypatch)
    ts = await st.post_message("kicking off")
    assert slack.posts[0]["thread_ts"] is None          # top-level message
    assert st.out_thread_ts == ts
    assert await state.lookup_anchor("slack_thread", f"C-OUT:{ts}") == sid
    row = await state.get_session(sid)
    assert (row["slack_channel"], row["slack_thread_ts"]) == ("C-OUT", ts)
    await st.post_message("progress")
    assert slack.posts[1]["thread_ts"] == ts            # second post threads


async def test_status_set_before_thread_is_flushed_on_first_post(db, slack, monkeypatch):
    """A status picked before the thread exists is stashed and applied right
    after the bootstrap post — the agent's first status is never lost."""
    st, _, _ = await make_tools(slack, monkeypatch)
    await st.set_live_status("reading the ticket")
    assert slack.statuses == []                          # nowhere to put it yet
    await st.post_message("hello")
    assert slack.statuses[0]["status"] == "reading the ticket"


async def test_post_message_tool_rejects_blank_status(db, slack, monkeypatch):
    """`status` is MANDATORY on the post_message tool: a blank one is rejected
    without posting (a blank status used to wipe the live indicator)."""
    _, handlers, _ = await make_tools(slack, monkeypatch)
    result = await handlers["post_message"]({"text": "hi", "status": "   "})
    assert result.get("is_error") is True
    assert slack.posts == []
    result = await handlers["post_message"]({"text": "hi", "status": "watching CI"})
    assert tool_payload(result)["ts"]
    assert slack.statuses[-1]["status"] == "watching CI"  # re-applied after post


async def test_close_session_latch_and_reopen_reset(db, slack, monkeypatch):
    """close_session posts the final message and closes ONCE (idempotent);
    mark_reopened resets the latch so the NEXT legitimate close works again."""
    st, handlers, sid = await make_tools(slack, monkeypatch)
    result = await handlers["close_session"]({"final_message": "Done — merged."})
    assert tool_payload(result) == {"status": "closed", "posted_final_message": True}
    assert await state.get_session_stage(sid) == "done"
    posts_after_close = len(slack.posts)

    repeat = await handlers["close_session"]({"final_message": "Done — again?"})
    assert tool_payload(repeat)["status"] == "already_closed"
    assert len(slack.posts) == posts_after_close        # final message posted once

    await state.reopen_session(sid)
    st.mark_reopened()                                   # stale latch would no-op
    reclose = await handlers["close_session"]({"final_message": "Done — really."})
    assert tool_payload(reclose)["status"] == "closed"
    assert len(slack.posts) == posts_after_close + 1
    assert await state.get_session_stage(sid) == "done"


async def test_register_anchor_trusted_pr_allowed(db, slack, monkeypatch):
    async def trust_true(owner, repo, number):
        return True
    _, handlers, sid = await make_tools(slack, monkeypatch, pr_trust_check=trust_true)
    result = await handlers["register_anchor"](
        {"kind": "github_pr", "key": "bitrise-io/demo#7"})
    assert tool_payload(result)["status"] == "registered"
    assert await state.lookup_anchor("github_pr", "bitrise-io/demo#7") == sid


async def test_register_anchor_untrusted_pr_hard_denied(db, slack, monkeypatch):
    """A definitive False is a HARD refusal — no retry, no human override —
    and the anchor is never written."""
    async def trust_false(owner, repo, number):
        return False
    _, handlers, _ = await make_tools(slack, monkeypatch, pr_trust_check=trust_false)
    result = await handlers["register_anchor"](
        {"kind": "github_pr", "key": "bitrise-io/demo#7"})
    assert result.get("is_error") is True
    text = result["content"][0]["text"]
    assert "external / untrusted" in text
    assert "EVEN IF a human asks" in text and "Do NOT retry" in text
    assert await state.lookup_anchor("github_pr", "bitrise-io/demo#7") is None


async def test_register_anchor_unverifiable_pr_is_retry_once(db, slack, monkeypatch):
    """A transient None is refused fail-closed but framed as retryable ONCE —
    distinct from the permanent denial above."""
    async def trust_none(owner, repo, number):
        return None
    _, handlers, _ = await make_tools(slack, monkeypatch, pr_trust_check=trust_none)
    result = await handlers["register_anchor"](
        {"kind": "github_pr", "key": "bitrise-io/demo#7"})
    assert result.get("is_error") is True
    text = result["content"][0]["text"]
    assert "could not verify" in text and "Retry ONCE" in text
    assert await state.lookup_anchor("github_pr", "bitrise-io/demo#7") is None


async def test_register_anchor_malformed_pr_key_refused(db, slack, monkeypatch):
    async def trust_true(owner, repo, number):
        return True
    _, handlers, _ = await make_tools(slack, monkeypatch, pr_trust_check=trust_true)
    result = await handlers["register_anchor"](
        {"kind": "github_pr", "key": "not-a-pr-key"})
    assert result.get("is_error") is True
    assert "not a valid github_pr key" in result["content"][0]["text"]


async def test_register_anchor_other_kinds_bypass_pr_gate(db, slack, monkeypatch):
    """Only github_pr is gated; jira_issue/rde_session anchors register freely."""
    async def trust_false(owner, repo, number):
        return False
    _, handlers, sid = await make_tools(slack, monkeypatch, pr_trust_check=trust_false)
    result = await handlers["register_anchor"](
        {"kind": "rde_session", "key": "rde-123"})
    assert tool_payload(result)["status"] == "registered"
    assert await state.lookup_anchor("rde_session", "rde-123") == sid


async def test_approval_request_block_shape(db, slack, monkeypatch):
    """The Approve/Reject buttons must carry the req_id verbatim (value +
    block_id) — that's how a click resolves to the parked request — and show
    the proposed body in a rich_text_preformatted block (mrkdwn-proof)."""
    st, _, _ = await make_tools(slack, monkeypatch)
    await st.post_message("bootstrap")                  # materialize the thread
    ts = await st.post_approval_request(
        req_id="egr_abc123", action_label="post a comment",
        target_label="bitrise-io/demo#7", body_preview="hello ```world```",
    )
    assert ts is not None
    blocks = slack.posts[-1]["blocks"]
    actions = next(b for b in blocks if b["type"] == "actions")
    assert actions["block_id"] == "egress::egr_abc123"
    by_action = {e["action_id"]: e for e in actions["elements"]}
    assert set(by_action) == {"egress_approve", "egress_reject"}
    assert all(e["value"] == "egr_abc123" for e in actions["elements"])
    rich = next(b for b in blocks if b["type"] == "rich_text")
    assert rich["elements"][0]["type"] == "rich_text_preformatted"
    assert "hello ```world```" in rich["elements"][0]["elements"][0]["text"]


async def test_approval_request_without_preview_omits_code_block(db, slack, monkeypatch):
    """An empty preview (e.g. a fork) renders no preformatted block at all —
    never a '(...)' placeholder that could shadow a real body."""
    st, _, _ = await make_tools(slack, monkeypatch)
    await st.post_message("bootstrap")
    await st.post_approval_request(
        req_id="egr_x", action_label="fork the repository",
        target_label="bitrise-io/demo", body_preview="",
    )
    blocks = slack.posts[-1]["blocks"]
    assert not any(b["type"] == "rich_text" for b in blocks)

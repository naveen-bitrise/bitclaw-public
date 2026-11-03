"""Public-repo egress approval gate (tools/hooks.make_public_egress_gate +
tools/egress.EgressApprovalRegistry).

The output-side guarantee: on a PUBLIC repo every GitHub write is held for a
human Slack approval bound to the exact call bytes (content hash), and every
error/unknown path DENIES (a raising hook would fail OPEN at the SDK layer).
"""
from conftest import decision, deny_reason, hook_input
from tools import hooks
from tools.egress import EgressApprovalRegistry


class FakeEgressSlackTools:
    """Stands in for SlackTools: records approval prompts, optionally fails."""

    def __init__(self, fail=False):
        self.fail = fail
        self.approvals: list[dict] = []
        self.statuses: list[str] = []

    async def post_approval_request(self, **kwargs):
        self.approvals.append(kwargs)
        return None if self.fail else "1700000000.000001"

    async def set_live_status(self, status):
        self.statuses.append(status)


class Visibility:
    """Recording repo-visibility stub: verdict True/False/None or an Exception."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.calls: list = []

    async def __call__(self, owner, repo):
        self.calls.append((owner, repo))
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


def make_gate(verdict=True, fail_slack=False):
    registry = EgressApprovalRegistry()
    slack = FakeEgressSlackTools(fail=fail_slack)
    vis = Visibility(verdict)
    gate = hooks.make_public_egress_gate(
        session_id=7, slack_tools=slack, egress_registry=registry,
        repo_visibility_check=vis,
    )
    return gate, registry, slack, vis


def write_call(body="hello from bitclaw", tool="mcp__github__add_issue_comment"):
    return hook_input(tool, {"owner": "bitrise-io", "repo": "demo",
                             "issue_number": 5, "body": body})


async def test_private_repo_write_passes_untouched():
    """Private/internal repos are internal-audience: no gate, no slowdown."""
    gate, _, slack, _ = make_gate(verdict=False)
    assert await gate(write_call(), "t1", None) == {}
    assert slack.approvals == []


async def test_public_write_parked_denied_and_approval_posted():
    gate, _, slack, _ = make_gate()
    result = await gate(write_call(), "t1", None)
    assert decision(result) == "deny"
    assert len(slack.approvals) == 1
    req_id = slack.approvals[0]["req_id"]
    assert req_id in deny_reason(result)  # agent is told which request to wait on
    assert "held for HUMAN approval" in deny_reason(result)
    assert slack.statuses == ["awaiting approval"]


async def test_identical_reissue_before_approval_parks_idempotently():
    """Re-issuing the same denied call must NOT spam a second Slack prompt."""
    gate, _, slack, _ = make_gate()
    await gate(write_call(), "t1", None)
    result = await gate(write_call(), "t2", None)
    assert decision(result) == "deny"
    assert len(slack.approvals) == 1


async def test_approved_reissue_allowed_exactly_once():
    """Approval is one-shot: the matching re-issue goes through and consumes
    the entry; a further identical call re-parks from scratch."""
    gate, registry, slack, _ = make_gate()
    await gate(write_call(), "t1", None)
    req_id = slack.approvals[0]["req_id"]
    assert registry.approve(req_id, "alice") is not None
    assert await gate(write_call(), "t2", None) == {}       # human-approved bytes
    assert decision(await gate(write_call(), "t3", None)) == "deny"  # consumed
    assert len(slack.approvals) == 2


async def test_altered_body_after_approval_reparks():
    """Approval binds to the content hash: 'approved text A → posted text B'
    is impossible — B re-parks for its own approval."""
    gate, registry, slack, _ = make_gate()
    await gate(write_call("text A"), "t1", None)
    registry.approve(slack.approvals[0]["req_id"], "alice")
    result = await gate(write_call("text B"), "t2", None)
    assert decision(result) == "deny"
    assert len(slack.approvals) == 2


async def test_unknown_github_tool_on_public_repo_denied_outright():
    """Fail-closed catch-all: a public-repo github tool that is neither a known
    safe read nor an approvable write (future/renamed writes) is DENIED."""
    gate, _, slack, _ = make_gate()
    result = await gate(write_call(tool="mcp__github__create_gist"), "t1", None)
    assert decision(result) == "deny"
    assert "BLOCKED" in deny_reason(result)
    assert slack.approvals == []  # no approval path for unrecognized tools


async def test_safe_read_on_public_repo_skips_visibility_lookup():
    """Reads can't egress — allowed with no gate and no API round-trip."""
    gate, _, _, vis = make_gate()
    call = hook_input("mcp__github__get_pull_request",
                      {"owner": "bitrise-io", "repo": "demo", "pullNumber": 5})
    assert await gate(call, "t1", None) == {}
    assert vis.calls == []


async def test_unconfirmed_visibility_gated_as_public():
    """None from the visibility check = 'if unsure, assume PUBLIC' → gate."""
    gate, _, slack, _ = make_gate(verdict=None)
    result = await gate(write_call(), "t1", None)
    assert decision(result) == "deny"
    assert len(slack.approvals) == 1


async def test_slack_post_failure_denies_and_forgets_the_park():
    """If the buttons never reached Slack the parked request is dropped, so the
    next re-issue re-parks and re-posts instead of wedging buttonless forever."""
    gate, _, slack, _ = make_gate(fail_slack=True)
    result = await gate(write_call(), "t1", None)
    assert decision(result) == "deny"
    assert "FAILED" in deny_reason(result)  # posted=False variant of the reason
    slack.fail = False
    await gate(write_call(), "t2", None)
    assert len(slack.approvals) == 2  # forgotten → re-parked and re-posted


async def test_raising_visibility_check_fails_closed():
    """A raising hook fails OPEN at the SDK layer — so every error path DENIES."""
    gate, _, slack, _ = make_gate(verdict=RuntimeError("github down"))
    result = await gate(write_call(), "t1", None)
    assert decision(result) == "deny"
    assert "could not run the approval check" in deny_reason(result)
    assert slack.approvals == []


async def test_forbidden_tools_left_to_the_write_deny_hook():
    """FORBIDDEN_GITHUB_TOOLS are denied by deny_github_write_tools; the egress
    gate must not double-deny or waste a visibility call on them."""
    gate, _, _, vis = make_gate()
    assert await gate(write_call(tool="mcp__github__create_issue"), "t1", None) == {}
    assert await gate(hook_input("mcp__slack__post_message", {}), "t1", None) == {}
    assert vis.calls == []


def test_registry_approve_and_reject_lifecycle():
    """Registry unit semantics: approve flips pending once; reject removes;
    unknown/handled ids answer None (double-click safe)."""
    reg = EgressApprovalRegistry()
    req, created = reg.park(session_id=1, content_hash="h1", tool_name="t",
                            summary="s", target="o/r#1")
    assert created is True
    _, created2 = reg.park(session_id=1, content_hash="h1", tool_name="t",
                           summary="s", target="o/r#1")
    assert created2 is False                      # idempotent while pending
    assert reg.approve("egr_nope", "a") is None   # unknown id
    assert reg.approve(req.req_id, "alice").status == "approved"
    assert reg.approve(req.req_id, "bob") is None  # already handled
    assert reg.take_approved(session_id=1, content_hash="h1") is True
    assert reg.take_approved(session_id=1, content_hash="h1") is False  # one-shot
    req2, _ = reg.park(session_id=1, content_hash="h2", tool_name="t",
                       summary="s", target="o/r#1")
    assert reg.reject(req2.req_id, "alice", "too spicy") is not None
    assert reg.get(req2.req_id) is None           # removed on reject


async def test_gate_all_repos_holds_private_repo_writes(monkeypatch):
    """EGRESS_GATE_ALL_REPOS=1: a private-repo write is parked for approval too,
    without a visibility lookup, and the Slack prompt doesn't call it public."""
    monkeypatch.setenv("EGRESS_GATE_ALL_REPOS", "1")
    gate, _, slack, vis = make_gate(verdict=False)
    result = await gate(write_call(), "t1", None)
    assert decision(result) == "deny"
    assert "held for HUMAN approval" in deny_reason(result)
    assert "PUBLIC" not in deny_reason(result)
    assert vis.calls == []
    assert len(slack.approvals) == 1
    assert "PUBLIC" not in slack.approvals[0]["header"]


async def test_gate_all_repos_off_keeps_private_writes_ungated(monkeypatch):
    monkeypatch.setenv("EGRESS_GATE_ALL_REPOS", "0")
    gate, _, slack, _ = make_gate(verdict=False)
    assert await gate(write_call(), "t1", None) == {}
    assert slack.approvals == []

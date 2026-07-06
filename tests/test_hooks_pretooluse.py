"""PreToolUse hook guardrails (tools/hooks.py).

Every test feeds the hook a DICT-shaped input exactly as the SDK delivers it
({"tool_name": ..., "tool_input": {...}}). SDK hooks receive a TypedDict, not
an object — a hook written against attributes raises, and a raising hook fails
OPEN at the SDK layer, silently disabling the guardrail (a real regression
class in this project's history). These tests lock the dict contract in.
"""
import pytest

from conftest import decision, deny_reason, hook_input
from tools import hooks

CREATE = "mcp__infra__bitrise_devenv_create"


# ---------------------------------------------------------------------------
# deny_github_write_tools — "orchestrator, not a coder"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name,theme", [
    # (1) repo/code/file mutation — all code changes go through the RDE over git
    ("mcp__github__create_or_update_file", "never changes a repository"),
    ("mcp__github__push_files", "never changes a repository"),
    ("mcp__github__create_branch", "never changes a repository"),
    # (2) out-of-scope lookups — work is tracked in Jira + PRs
    ("mcp__github__get_teams", "not part of bitclaw's workflow"),
    ("mcp__github__sub_issue_write", "not part of bitclaw's workflow"),
    # (3) code reads — the RDE reads source, never the orchestrator
    ("mcp__github__get_file_contents", "never reads repository"),
    ("mcp__github__search_code", "never reads repository"),
    ("mcp__github__get_pull_request_diff", "never reads repository"),
    # (4) non-orchestration writes — ungated egress channels if left enabled
    ("mcp__github__create_issue", "ONLY GitHub writes"),
    ("mcp__github__run_workflow", "ONLY GitHub writes"),
    ("mcp__github__create_and_submit_pull_request_review", "ONLY GitHub writes"),
])
async def test_forbidden_github_tools_denied_with_group_reason(tool_name, theme):
    """Each forbidden-tool group is denied with its own teaching reason."""
    result = await hooks.deny_github_write_tools(hook_input(tool_name), "t1", None)
    assert decision(result) == "deny"
    assert theme in deny_reason(result)


@pytest.mark.parametrize("method", ["get_diff", "get_files", "get_commits"])
async def test_pull_request_read_code_methods_denied(method):
    """The consolidated pull_request_read tool is KEPT, but its code-reading
    methods are denied — a bare name-deny can't gate a method enum."""
    result = await hooks.deny_github_write_tools(
        hook_input("mcp__github__pull_request_read", {"method": method}), "t1", None
    )
    assert decision(result) == "deny"
    assert "never reads repository" in deny_reason(result)


@pytest.mark.parametrize("method", ["get", "get_reviews", "get_comments", "get_status"])
async def test_pull_request_read_orchestration_methods_pass(method):
    """Orchestration reads on pull_request_read (merge-readiness, reviews) pass."""
    result = await hooks.deny_github_write_tools(
        hook_input("mcp__github__pull_request_read", {"method": method}), "t1", None
    )
    assert result == {}


@pytest.mark.parametrize("tool_name", [
    "mcp__github__create_pull_request",   # bitclaw's legitimate writes...
    "mcp__github__merge_pull_request",
    "mcp__github__search_repositories",   # ...and orchestration-metadata reads
    "mcp__github__fork_repository",       # the deliberate escape hatch
    "mcp__slack__post_message",           # non-github tools are never touched
])
async def test_kept_tools_pass_through(tool_name):
    result = await hooks.deny_github_write_tools(hook_input(tool_name), "t1", None)
    assert result == {}


# ---------------------------------------------------------------------------
# include_secrets deny — the agent must never read live secrets back
# ---------------------------------------------------------------------------

_SECRET_HOOKS = [
    (hooks.deny_include_secrets_on_get_template, "mcp__infra__bitrise_devenv_get_template"),
    (hooks.deny_include_secrets_on_get_session, "mcp__infra__bitrise_devenv_get"),
]


@pytest.mark.parametrize("hook,tool_name", _SECRET_HOOKS)
@pytest.mark.parametrize("flag", [True, "true", "True", 1, "yes"])
async def test_include_secrets_truthy_denied(hook, tool_name, flag):
    """include_secrets in every serialization the model produces (bool/str/int)
    is denied — the flag would surface live credentials in the tool result."""
    result = await hook(hook_input(tool_name, {"include_secrets": flag}), "t1", None)
    assert decision(result) == "deny"
    assert "include_secrets" in deny_reason(result)


@pytest.mark.parametrize("hook,tool_name", _SECRET_HOOKS)
@pytest.mark.parametrize("tool_input", [{}, {"include_secrets": False},
                                        {"include_secrets": "false"},
                                        {"include_secrets": 0}])
async def test_include_secrets_absent_or_falsy_passes(hook, tool_name, tool_input):
    """Absent/false include_secrets passes — notably the STRING "false", which a
    naive bool() check would wrongly treat as truthy."""
    result = await hook(hook_input(tool_name, tool_input), "t1", None)
    assert result == {}


# ---------------------------------------------------------------------------
# require_ai_prompt — an RDE without ai_prompt boots with no coding agent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_input", [{}, {"ai_prompt": ""}, {"ai_prompt": "   \n"}])
async def test_devenv_create_without_ai_prompt_denied(tool_input):
    """Missing/empty/whitespace ai_prompt is denied — without it every later
    `tmux send-keys` to the RDE is a silent no-op."""
    result = await hooks.require_ai_prompt_on_devenv_create(
        hook_input(CREATE, tool_input), "t1", None
    )
    assert decision(result) == "deny"
    assert "ai_prompt" in deny_reason(result)


async def test_devenv_create_with_real_ai_prompt_passes():
    result = await hooks.require_ai_prompt_on_devenv_create(
        hook_input(CREATE, {"ai_prompt": "PROJ-1: fix the flaky test, branch main"}),
        "t1", None,
    )
    assert result == {}


# ---------------------------------------------------------------------------
# require_pinned_template — only the operator-approved template may launch
# ---------------------------------------------------------------------------

async def test_pinned_template_env_unset_fails_closed(monkeypatch):
    """No RDE_TEMPLATE_ID means NO template is approved: every create denied."""
    monkeypatch.delenv("RDE_TEMPLATE_ID", raising=False)
    result = await hooks.require_pinned_template_on_devenv_create(
        hook_input(CREATE, {"template_id": "tmpl-anything"}), "t1", None
    )
    assert decision(result) == "deny"
    assert "RDE_TEMPLATE_ID" in deny_reason(result)


async def test_pinned_template_exact_match_passes(monkeypatch):
    monkeypatch.setenv("RDE_TEMPLATE_ID", "tmpl-approved")
    result = await hooks.require_pinned_template_on_devenv_create(
        hook_input(CREATE, {"template_id": "tmpl-approved"}), "t1", None
    )
    assert result == {}


@pytest.mark.parametrize("template_id", [" tmpl-approved ", "tmpl-other", "", None])
async def test_pinned_template_padded_or_other_id_denied(monkeypatch, template_id):
    """The match is deliberately exact (no strip): the hook can't rewrite the
    input, so a padded id that 'passed' would still reach the server padded."""
    monkeypatch.setenv("RDE_TEMPLATE_ID", "tmpl-approved")
    result = await hooks.require_pinned_template_on_devenv_create(
        hook_input(CREATE, {"template_id": template_id}), "t1", None
    )
    assert decision(result) == "deny"


async def test_devenv_hooks_ignore_other_tools(monkeypatch):
    """Each create-gate hook passes any other tool through untouched."""
    monkeypatch.delenv("RDE_TEMPLATE_ID", raising=False)
    other = hook_input("mcp__github__create_pull_request", {"include_secrets": True})
    assert await hooks.require_ai_prompt_on_devenv_create(other, "t1", None) == {}
    assert await hooks.require_pinned_template_on_devenv_create(other, "t1", None) == {}
    assert await hooks.deny_include_secrets_on_get_template(other, "t1", None) == {}


# ---------------------------------------------------------------------------
# inject_saved_input_mapping_on_devenv_create — the template's required
# session inputs carry no values (templates are workspace-shared; the secrets
# live as saved inputs on the bot's account), so every create must request
# saved-input auto-mapping or it fails with "missing required input".
# ---------------------------------------------------------------------------

def updated_input(hook_result: dict) -> dict | None:
    return (hook_result.get("hookSpecificOutput") or {}).get("updatedInput")


async def test_saved_input_mapping_injected_on_create():
    """A create call without the flag gets map_saved_to_session_inputs=True,
    with every other field preserved verbatim."""
    result = await hooks.inject_saved_input_mapping_on_devenv_create(
        hook_input(CREATE, {"template_id": "tmpl-1", "ai_prompt": "fix it", "name": "s1"}),
        "t1", None,
    )
    updated = updated_input(result)
    assert updated == {
        "template_id": "tmpl-1",
        "ai_prompt": "fix it",
        "name": "s1",
        "map_saved_to_session_inputs": True,
    }
    # updatedInput only — an input-mutating hook must never carry a
    # permission decision (it could override a validator's deny).
    assert decision(result) is None


@pytest.mark.parametrize("explicit", [True, False])
async def test_saved_input_mapping_respects_explicit_flag(explicit):
    """An agent-supplied flag — either way — is left alone."""
    result = await hooks.inject_saved_input_mapping_on_devenv_create(
        hook_input(CREATE, {"map_saved_to_session_inputs": explicit}), "t1", None,
    )
    assert result == {}


async def test_saved_input_mapping_ignores_other_tools():
    other = hook_input("mcp__infra__bitrise_devenv_get", {})
    assert await hooks.inject_saved_input_mapping_on_devenv_create(other, "t1", None) == {}

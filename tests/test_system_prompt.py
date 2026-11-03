"""The system prompt is assembled per capability tier (see agent_loop).

Guardrail locked in here: the prompt must match the actual tool surface.
Teaching the model about tools its deployment doesn't have produces failed
calls; conversely, a configured integration's battle-tested rules must all be
present. These tests pin both directions for every tier.
"""
import pytest

from agent_loop import _build_system_prompt

FULL = {"github", "infra", "atlassian"}


@pytest.fixture
def full_env(monkeypatch):
    monkeypatch.setenv("GITHUB_ALLOWED_ORG", "acme-org")
    monkeypatch.setenv("RDE_TEMPLATE_ID", "tmpl-42")
    monkeypatch.setenv("JIRA_HOST", "example.atlassian.net")


def test_full_config_contains_all_sections(full_env):
    p = _build_system_prompt(FULL)
    assert "%%" not in p, "unsubstituted placeholder"
    # GitHub rules present, with the org substituted.
    assert "mergeable_state" in p
    assert "acme-org" in p
    assert "EGRESS GATE" in p
    assert "[thread_root_id=N]" in p
    # RDE rules present, with the pinned template substituted.
    assert "tmpl-42" in p
    assert "tmux send-keys" in p
    assert "Never block on input" in p
    # The agent is taught that session inputs auto-fill from saved inputs
    # (the mapping flag itself is injected by a hook) and that a
    # missing-required-input failure is an operator setup gap, not retryable.
    assert "saved-input auto-mapping" in p
    assert "missing required input" in p
    # Jira section present with the host as cloudId.
    assert "example.atlassian.net" in p
    # No "not configured" notes in a full build.
    assert "NOT configured" not in p and "not configured" not in p


def test_chat_only_has_no_phantom_tools(monkeypatch):
    monkeypatch.delenv("JIRA_HOST", raising=False)
    p = _build_system_prompt(set())
    assert "%%" not in p
    # No tools the deployment doesn't have.
    assert "mcp__github__" not in p
    assert "bitrise_devenv" not in p
    assert "tmux" not in p
    # The bot is told its limits and what unlocks them.
    assert "NO coding backend" in p
    assert "RDE_TEMPLATE_ID" in p
    assert "GITHUB_TOKEN" in p
    # Universal machinery still taught.
    assert "post_message" in p
    assert "close_session" in p
    assert "mcp__sched__schedule_message" in p


def test_rde_only_pushes_branches_without_github(monkeypatch):
    monkeypatch.setenv("RDE_TEMPLATE_ID", "tmpl-42")
    monkeypatch.delenv("JIRA_HOST", raising=False)
    p = _build_system_prompt({"infra"})
    assert "%%" not in p
    assert "tmux send-keys" in p and "tmpl-42" in p
    # No GitHub tool names anywhere; humans open the PRs.
    assert "mcp__github__" not in p
    assert "THEY open the PR" in p
    assert "GitHub is NOT configured" in p


def test_github_only_orchestrates_without_coding(monkeypatch):
    monkeypatch.setenv("GITHUB_ALLOWED_ORG", "acme-org")
    monkeypatch.delenv("JIRA_HOST", raising=False)
    p = _build_system_prompt({"github"})
    assert "%%" not in p
    assert "mergeable_state" in p and "EGRESS GATE" in p
    # No RDE machinery in the prompt.
    assert "bitrise_devenv" not in p and "tmux" not in p
    assert "NO coding backend" in p
    # But it can still act on GitHub on human instruction.
    assert "merge on explicit" in p or "merge on explicit human instruction" in p


def test_rde_template_unset_becomes_denied_notice(monkeypatch):
    monkeypatch.delenv("RDE_TEMPLATE_ID", raising=False)
    p = _build_system_prompt({"infra"})
    assert "RDE creation is NOT configured" in p
    assert "denied in code" in p


def test_jira_absent_note_matches_github_presence(monkeypatch):
    monkeypatch.setenv("GITHUB_ALLOWED_ORG", "acme-org")
    monkeypatch.delenv("JIRA_HOST", raising=False)
    with_gh = _build_system_prompt({"github"})
    assert "this Slack thread and the PRs are the records" in with_gh
    without_gh = _build_system_prompt(set())
    assert "this Slack thread is the record" in without_gh


def test_jira_server_without_host_degrades(monkeypatch):
    # The atlassian server can be configured while JIRA_HOST is forgotten —
    # the prompt must not emit a broken cloudId rule.
    monkeypatch.delenv("JIRA_HOST", raising=False)
    p = _build_system_prompt(FULL - {"github", "infra"} | {"atlassian"})
    assert "cloudId" not in p
    assert "Jira is not configured" in p


def test_demo_knobs_unset_keep_default_prompt(full_env, monkeypatch):
    for k in ("RDE_WARM_POOL_ID", "RDE_REPO_DIR", "RDE_AGENT_BRIEF"):
        monkeypatch.delenv(k, raising=False)
    p = _build_system_prompt(FULL)
    assert "%%" not in p
    assert "git clone github.com/<owner>/<repo> and work there" in p
    assert "warm_pool_id" not in p
    assert "verification loop" not in p
    assert "device.page_url" in p


def test_demo_knobs_set(full_env, monkeypatch):
    monkeypatch.setenv("RDE_WARM_POOL_ID", "pool-7")
    monkeypatch.setenv("RDE_REPO_DIR", "~/shopfront")
    monkeypatch.setenv("RDE_AGENT_BRIEF", "run npm test and screenshot the simulator")
    p = _build_system_prompt(FULL)
    assert "%%" not in p
    assert "warm_pool_id `pool-7`" in p and "tmpl-42" in p
    assert "cd ~/shopfront" in p and "NEVER re-clone" in p
    assert "run npm test and screenshot the simulator" in p


def test_release_section_only_with_bitrise_server(full_env, monkeypatch):
    monkeypatch.setenv("CODEPUSH_DEPLOYMENT_IDS", "dep-1,dep-2")
    monkeypatch.setenv("BITRISE_APP_SLUG", "slug-9")
    assert "RELEASE-CHANGE GATE" not in _build_system_prompt(FULL)
    p = _build_system_prompt(FULL | {"bitrise"})
    assert "%%" not in p
    assert "RELEASE-CHANGE GATE" in p and "`dep-1`, `dep-2`" in p and "slug-9" in p

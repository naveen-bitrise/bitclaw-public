"""Bitrise CI/CodePush gate (tools/codepush.py): scope pins + human approval."""
import pytest

from conftest import decision, deny_reason, hook_input
from tools import codepush
from tools.egress import EgressApprovalRegistry

DEP = "55b32445-198a-44d8-ae3e-110be21c1888"
OTHER_DEP = "1513cc3b-4b77-4511-a7af-425e36331dda"
UPD = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
P = "mcp__bitrise__"


class FakeSlack:
    def __init__(self, ts="123.4"):
        self.ts = ts
        self.posts = []
        self.statuses = []

    async def post_approval_request(self, **kw):
        self.posts.append(kw)
        return self.ts

    async def set_live_status(self, s):
        self.statuses.append(s)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("CODEPUSH_DEPLOYMENT_IDS", DEP)
    monkeypatch.setenv("CODEPUSH_APP_IDS", "app-ios")
    monkeypatch.setenv("BITRISE_APP_SLUG", "app-slug")
    codepush._KNOWN_UPDATES.clear()
    yield
    codepush._KNOWN_UPDATES.clear()


def gate(slack=None, reg=None):
    return codepush.make_codepush_gate(7, slack or FakeSlack(), reg or EgressApprovalRegistry())


async def test_ci_read_pinned_app_only():
    g = gate()
    assert await g(hook_input(P + "get_build_log", {"app_slug": "app-slug", "build_slug": "b"}), "t", None) == {}
    r = await g(hook_input(P + "get_build_log", {"app_slug": "other", "build_slug": "b"}), "t", None)
    assert decision(r) == "deny"


async def test_list_updates_pinned_deployment_only():
    g = gate()
    assert await g(hook_input(P + "codepush_list_updates", {"deployment_id": DEP}), "t", None) == {}
    r = await g(hook_input(P + "codepush_list_updates", {"deployment_id": OTHER_DEP}), "t", None)
    assert decision(r) == "deny"


async def test_unset_deployments_denies_codepush(monkeypatch):
    monkeypatch.delenv("CODEPUSH_DEPLOYMENT_IDS")
    r = await gate()(hook_input(P + "codepush_list_updates", {"deployment_id": DEP}), "t", None)
    assert "CODEPUSH_DEPLOYMENT_IDS" in deny_reason(r)


async def test_patch_unknown_update_refused():
    r = await gate()(hook_input(P + "codepush_patch_update", {"id": UPD, "disabled": "true"}), "t", None)
    assert decision(r) == "deny"
    assert "codepush_list_updates" in deny_reason(r)


async def test_recorder_learns_update_ids_from_pinned_deployment_only():
    resp = [{"type": "text", "text": '{"items":[{"id":"%s","label":"v5"}]}' % UPD}]
    await codepush.record_codepush_updates(
        {"tool_name": P + "codepush_list_updates", "tool_input": {"deployment_id": OTHER_DEP},
         "tool_response": resp}, "t", None)
    assert UPD not in codepush._KNOWN_UPDATES
    await codepush.record_codepush_updates(
        {"tool_name": P + "codepush_list_updates", "tool_input": {"deployment_id": DEP},
         "tool_response": resp}, "t", None)
    assert codepush._KNOWN_UPDATES[UPD] == DEP


async def test_patch_parks_for_approval_then_passes_after_approve():
    codepush._KNOWN_UPDATES[UPD] = DEP
    slack, reg = FakeSlack(), EgressApprovalRegistry()
    g = gate(slack, reg)
    call = hook_input(P + "codepush_patch_update", {"id": UPD, "disabled": "true"})
    r = await g(call, "t", None)
    assert decision(r) == "deny" and "RELEASE-CHANGE GATE" in deny_reason(r)
    assert len(slack.posts) == 1 and "PAUSE" in slack.posts[0]["body_preview"]
    req_id = slack.posts[0]["req_id"]
    # re-issue before approval: still parked, no second post
    await g(call, "t", None)
    assert len(slack.posts) == 1
    reg.approve(req_id, "U1")
    assert await g(call, "t", None) == {}
    # approval is single-use
    assert decision(await g(call, "t", None)) == "deny"


async def test_approval_bound_to_exact_call():
    codepush._KNOWN_UPDATES[UPD] = DEP
    slack, reg = FakeSlack(), EgressApprovalRegistry()
    g = gate(slack, reg)
    await g(hook_input(P + "codepush_patch_update", {"id": UPD, "disabled": "true"}), "t", None)
    reg.approve(slack.posts[0]["req_id"], "U1")
    r = await g(hook_input(P + "codepush_patch_update", {"id": UPD, "rollout": 100}), "t", None)
    assert decision(r) == "deny"


async def test_rollback_pinned_and_gated():
    slack = FakeSlack()
    g = gate(slack)
    r = await g(hook_input(P + "codepush_rollback_deployment", {"id": OTHER_DEP}), "t", None)
    assert decision(r) == "deny" and not slack.posts
    r = await g(hook_input(P + "codepush_rollback_deployment", {"id": DEP}), "t", None)
    assert "RELEASE-CHANGE GATE" in deny_reason(r) and len(slack.posts) == 1


async def test_failed_post_forgets_request():
    codepush._KNOWN_UPDATES[UPD] = DEP
    reg = EgressApprovalRegistry()
    g = gate(FakeSlack(ts=None), reg)
    r = await g(hook_input(P + "codepush_patch_update", {"id": UPD, "disabled": "true"}), "t", None)
    assert "FAILED" in deny_reason(r)
    assert not reg._by_id


async def test_non_bitrise_tools_ignored():
    assert await gate()(hook_input("mcp__github__create_pull_request", {}), "t", None) == {}

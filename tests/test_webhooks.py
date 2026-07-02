"""Webhook ingress (webhooks.py) over a real aiohttp server.

Locks in the inbound trust chain: HMAC/secret auth first, then self-echo
suppression, then the anchor check (unowned PRs drop before any API spend),
then the fail-closed author-trust gate — and the exact synthesized message
text the agent receives (thread_root_id, cleanup cues).
"""
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import state
from webhooks import _adf_to_text, make_webhook_app

GH_SECRET = "gh-hook-secret"
JIRA_SECRET = "jira-hook-secret"
BOT_LOGIN = "bitclaw-bot"
JIRA_BOT = "712020:bitclaw"
REPO = "bitrise-io/demo"


class FakeGateway:
    def __init__(self):
        self.delivered: list[tuple] = []
        self.jira_starts: list[tuple] = []

    async def deliver_to_anchor(self, kind, key, text, slack_client, status_hint=None):
        self.delivered.append((kind, key, text, status_hint))
        return True

    async def start_session_for_jira_issue(self, *, issue_key, summary,
                                           slack_client, initial_user_message):
        self.jira_starts.append((issue_key, summary, initial_user_message))
        return 42


class MemberCheck:
    def __init__(self, verdict=None):
        self.verdict = verdict
        self.calls: list = []

    async def __call__(self, login):
        self.calls.append(login)
        return self.verdict


@pytest.fixture
async def wh(db, monkeypatch):
    """Webhook app under TestServer, with a recording gateway + member check."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", GH_SECRET)
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", JIRA_SECRET)
    monkeypatch.setenv("JIRA_BOT_ACCOUNT_ID", JIRA_BOT)
    monkeypatch.delenv("JIRA_TERMINAL_STATUSES", raising=False)
    gw, member_check = FakeGateway(), MemberCheck()
    client = TestClient(TestServer(make_webhook_app(
        gw, slack_client=None, github_bot_login=BOT_LOGIN,
        org_member_check=member_check,
    )))
    await client.start_server()
    yield SimpleNamespace(client=client, gw=gw, member_check=member_check)
    await client.close()


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(GH_SECRET.encode(), body, hashlib.sha256).hexdigest()


async def gh_post(client, event, payload, signature=None):
    body = json.dumps(payload).encode()
    return await client.post("/webhooks/github", data=body, headers={
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": signature if signature is not None else _sign(body),
    })


async def anchor_pr(number=7, repo=REPO):
    sid, _ = await state.claim_or_create_session(
        "github_pr", state.github_pr_anchor_key(repo, number),
        slack_channel="C1", slack_thread_ts="1.0",
    )
    return sid


def issue_comment(sender="alice", assoc="NONE", body="please do X",
                  sender_type="User", number=7):
    return {
        "action": "created",
        "repository": {"full_name": REPO},
        # GitHub flags PR comments by a (non-empty) pull_request object.
        "issue": {"number": number,
                  "pull_request": {"url": f"https://api.github.com/{REPO}/pulls/{number}"}},
        "comment": {"body": body, "author_association": assoc},
        "sender": {"login": sender, "type": sender_type},
    }


async def test_missing_secret_returns_503(db, monkeypatch):
    """No GITHUB_WEBHOOK_SECRET → reject loudly rather than accept unsigned."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", JIRA_SECRET)
    client = TestClient(TestServer(make_webhook_app(FakeGateway(), None)))
    await client.start_server()
    try:
        resp = await client.post("/webhooks/github", data=b"{}")
        assert resp.status == 503
    finally:
        await client.close()


async def test_bad_signature_rejected_401(wh):
    resp = await gh_post(wh.client, "ping", {}, signature="sha256=" + "0" * 64)
    assert resp.status == 401


async def test_valid_hmac_ping_pongs(wh):
    resp = await gh_post(wh.client, "ping", {"zen": "Keep it simple."})
    assert resp.status == 200
    assert (await resp.json()) == {"pong": True}


async def test_bot_self_echo_suppressed(wh):
    """The bot's own comment webhooks must not re-trigger it (feedback loop)."""
    await anchor_pr()
    resp = await gh_post(wh.client, "issue_comment", issue_comment(sender=BOT_LOGIN))
    assert resp.status == 204
    assert wh.gw.delivered == []


async def test_unanchored_pr_dropped_before_org_check(wh):
    """No session owns the PR → drop cheaply: gateway NOT called and the
    membership API NOT consulted (public-repo webhook floods stay free)."""
    resp = await gh_post(wh.client, "issue_comment", issue_comment())
    assert resp.status == 204
    assert wh.gw.delivered == []
    assert wh.member_check.calls == []


@pytest.mark.parametrize("verdict", [False, None])
async def test_untrusted_author_comment_dropped(wh, verdict):
    """Fail CLOSED both on a definitive non-member (False) AND on an
    inconclusive membership check (None) — never act on unattributed input."""
    await anchor_pr()
    wh.member_check.verdict = verdict
    resp = await gh_post(wh.client, "issue_comment", issue_comment(sender="rando"))
    assert resp.status == 204
    assert wh.gw.delivered == []
    assert wh.member_check.calls == ["rando"]


async def test_trusted_association_delivered_without_api_call(wh):
    """author_association MEMBER on the signed payload is trusted directly."""
    await anchor_pr()
    resp = await gh_post(wh.client, "issue_comment",
                         issue_comment(sender="member", assoc="MEMBER"))
    assert resp.status == 200
    assert wh.member_check.calls == []
    kind, key, text, _hint = wh.gw.delivered[0]
    assert (kind, key) == ("github_pr", f"{REPO}#7")
    assert "comment on PR #7 by member" in text


async def test_app_bot_sender_exempt_from_org_gate(wh):
    """GitHub App bots (sender.type == 'Bot') were installed by an org admin —
    the installation is the trust gate, no membership check."""
    await anchor_pr()
    resp = await gh_post(wh.client, "issue_comment", issue_comment(
        sender="coderabbitai[bot]", sender_type="Bot", assoc="NONE"))
    assert resp.status == 200
    assert wh.member_check.calls == []


@pytest.mark.parametrize("in_reply_to,expected_root", [(None, 100), (55, 55)])
async def test_review_comment_carries_thread_root_id(wh, in_reply_to, expected_root):
    """The synthesized message embeds thread_root_id so the agent can reply
    under the right review thread: a root comment IS the root; a reply's
    in_reply_to_id is."""
    await anchor_pr()
    comment = {"id": 100, "path": "src/x.py", "body": "nit: rename",
               "author_association": "MEMBER"}
    if in_reply_to:
        comment["in_reply_to_id"] = in_reply_to
    resp = await gh_post(wh.client, "pull_request_review_comment", {
        "repository": {"full_name": REPO},
        "pull_request": {"number": 7},
        "comment": comment,
        "sender": {"login": "member", "type": "User"},
    })
    assert resp.status == 200
    text = wh.gw.delivered[0][2]
    assert f"[thread_root_id={expected_root}]" in text
    assert f"review comment on {REPO}#7" in text


async def test_pr_closed_carries_per_pr_cleanup_cue(wh):
    """One PR closing retires ONE workstream — the per-PR cue, never the
    session-wide one (sibling RDEs must survive)."""
    await anchor_pr()
    resp = await gh_post(wh.client, "pull_request", {
        "action": "closed",
        "repository": {"full_name": REPO},
        "pull_request": {"number": 7, "merged": True},
        "sender": {"login": "alice", "type": "User"},
    })
    assert resp.status == 200
    text = wh.gw.delivered[0][2]
    assert "PR #7 merged by alice" in text
    assert "[bitclaw cleanup cue (single PR)" in text
    assert "[bitclaw cleanup cue: external signal" not in text


# ----------------------------------------------------------------------------
# Jira
# ----------------------------------------------------------------------------

def jira_update(items, actor_id="acct-human", actor_name="Human"):
    return {
        "webhookEvent": "jira:issue_updated",
        "issue": {"key": "PROJ-1",
                  "fields": {"project": {"key": "PROJ"}, "summary": "Ship it"}},
        "user": {"accountId": actor_id, "displayName": actor_name},
        "changelog": {"items": items},
    }


async def test_jira_bad_token_rejected_401(wh):
    resp = await wh.client.post("/webhooks/jira/wrong-token", json={})
    assert resp.status == 401
    assert wh.gw.delivered == []


async def test_jira_terminal_transition_carries_session_cleanup_cue(wh):
    """A transition into a terminal status appends the session-wide cleanup
    cue — including the CURLY-apostrophe "Won’t Do" Jira actually emits, which
    must match the default list's straight-apostrophe "Won't Do"."""
    payload = jira_update([{"field": "status", "fromString": "In Progress",
                            "toString": "Won’t Do"}])
    resp = await wh.client.post(f"/webhooks/jira/{JIRA_SECRET}", json=payload)
    assert resp.status == 200
    kind, key, text, _hint = wh.gw.delivered[0]
    assert (kind, key) == ("jira_issue", "PROJ-1")
    assert "[bitclaw cleanup cue: external signal" in text


async def test_jira_assignment_bootstraps_session(wh):
    """A human assigning the ticket to the bot starts a session (the bootstrap
    edge), instead of routing through deliver_to_anchor."""
    payload = jira_update([{"field": "assignee", "to": JIRA_BOT,
                            "toString": "bitclaw"}])
    resp = await wh.client.post(f"/webhooks/jira/{JIRA_SECRET}", json=payload)
    assert resp.status == 200
    issue_key, summary, msg = wh.gw.jira_starts[0]
    assert issue_key == "PROJ-1" and summary == "Ship it"
    assert "assigned to you by Human" in msg
    assert wh.gw.delivered == []


def test_adf_to_text_flattens_nested_documents():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Hello "},
            {"type": "mention", "attrs": {"text": "@Bob"}},
            {"type": "hardBreak"},
            {"type": "emoji", "attrs": {"shortName": ":tada:"}},
            {"type": "inlineCard", "attrs": {"url": "https://x.test"}},
        ]},
        {"type": "codeBlock", "content": [{"type": "text", "text": "x = 1"}]},
    ]}
    out = _adf_to_text(doc)
    for expected in ("Hello @Bob", "\n", ":tada:", "https://x.test", "x = 1"):
        assert expected in out


@pytest.mark.parametrize("garbage", [
    None, 42, "plain", [1, {"type": "???"}, None],
    {"type": "unknown", "attrs": {"x": object()}},  # json.dumps fails → str()
])
def test_adf_to_text_never_raises_on_garbage(garbage):
    assert isinstance(_adf_to_text(garbage), str)

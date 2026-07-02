"""Shared author/repo/PR trust primitives (github_trust.py).

One definition of "trusted" backs the webhook ingress, the read filter, and
the PR-anchor gate — these tests pin the tri-state semantics (True / False /
None-means-unknown-fail-closed) and the no-caching-of-transient-verdicts rule.
"""
import pytest

import github_trust
from github_trust import (
    PullRequestTrustChecker,
    author_is_trusted,
    membership_from_status,
    pr_is_cross_repo,
)


class MemberCheck:
    def __init__(self, verdicts=None):
        self.verdicts = verdicts or {}
        self.calls: list = []

    async def __call__(self, login):
        self.calls.append(login)
        return self.verdicts.get(login)


@pytest.mark.parametrize("status,expected", [
    (204, True),    # member
    (404, False),   # definitively not a member
    (302, None),    # requester itself isn't a member — can't determine
    (401, None), (403, None), (500, None),
])
def test_membership_from_status_tri_state(status, expected):
    assert membership_from_status(status) is expected


async def test_author_is_trusted_app_bot_short_circuits():
    check = MemberCheck({"x": False})
    assert await author_is_trusted(login="x", association=None, is_bot=True,
                                   org_member_check=check) is True
    assert check.calls == []


@pytest.mark.parametrize("assoc", ["MEMBER", "owner", "Collaborator"])
async def test_author_is_trusted_association_fast_path(assoc):
    """OWNER/MEMBER/COLLABORATOR trusts with no API call, case-insensitively."""
    check = MemberCheck()
    assert await author_is_trusted(login="x", association=assoc, is_bot=False,
                                   org_member_check=check) is True
    assert check.calls == []


@pytest.mark.parametrize("verdict", [True, False, None])
async def test_author_is_trusted_membership_fallback_passthrough(verdict):
    """CONTRIBUTOR/NONE falls back to the membership API; the tri-state passes
    straight through so callers can tell 'non-member' from 'couldn't check'."""
    check = MemberCheck({"x": verdict})
    assert await author_is_trusted(login="x", association="NONE", is_bot=False,
                                   org_member_check=check) is verdict


def _pr(head_full="org/repo", base_full="org/repo", head_deleted=False):
    head_repo = None if head_deleted else {"full_name": head_full}
    return {"head": {"repo": head_repo}, "base": {"repo": {"full_name": base_full}}}


def test_pr_is_cross_repo_same_repo_false():
    assert pr_is_cross_repo(_pr()) is False


def test_pr_is_cross_repo_fork_true():
    assert pr_is_cross_repo(_pr(head_full="outsider/repo")) is True


def test_pr_is_cross_repo_deleted_head_fork_still_true():
    """head.repo == null (fork deleted) — it WAS a fork; still cross-repo."""
    assert pr_is_cross_repo(_pr(head_deleted=True)) is True


@pytest.mark.parametrize("pr", [
    None,                                              # not a dict
    {"head": {"repo": {"full_name": "a/b"}}},          # missing base
    {"base": {"repo": {"full_name": "a/b"}}},          # missing head entirely
    {"base": {"repo": {"full_name": "a/b"}}, "head": {}},  # head lacks 'repo' key
])
def test_pr_is_cross_repo_unreadable_shapes_return_none(pr):
    """Malformed head/base can't confirm same-repo → None (caller fails closed)."""
    assert pr_is_cross_repo(pr) is None


# ---------------------------------------------------------------------------
# PullRequestTrustChecker with a stubbed HTTP layer
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status, body=None):
        self.status = status
        self._body = body

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeHTTP:
    """Stub for the shared aiohttp session; pops queued responses per get()."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self._responses.pop(0)


def _pr_body(author="dev", assoc="MEMBER", head_full="bitrise-io/x"):
    return {
        "user": {"login": author}, "author_association": assoc,
        "head": {"repo": {"full_name": head_full} if head_full else None},
        "base": {"repo": {"full_name": "bitrise-io/x"}},
    }


def _checker(check=None, *, org="bitrise-io", http=None, monkeypatch=None):
    if http is not None:
        monkeypatch.setattr(github_trust, "get_http_session", lambda: http)
    return PullRequestTrustChecker("tok", check or MemberCheck(),
                                   self_login="bitclaw-bot", allowed_org=org)


async def test_pr_checker_outside_org_repo_refused_without_fetch(monkeypatch):
    """The repo-ownership gate is authoritative from the anchor key alone —
    an outsider's PR in their own repo would report OWNER and pass otherwise."""
    http = _FakeHTTP()
    checker = _checker(http=http, monkeypatch=monkeypatch)
    assert await checker.is_trusted("evil-org", "repo", 1) is False
    assert http.calls == []


async def test_pr_checker_no_org_configured_refuses_unknown(monkeypatch):
    checker = _checker(org=None, http=_FakeHTTP(), monkeypatch=monkeypatch)
    assert await checker.is_trusted("bitrise-io", "repo", 1) is None


async def test_pr_checker_same_repo_trusted_author_passes(monkeypatch):
    http = _FakeHTTP(_FakeResp(200, _pr_body()))
    checker = _checker(http=http, monkeypatch=monkeypatch)
    assert await checker.is_trusted("bitrise-io", "x", 7) is True


async def test_pr_checker_fork_refused_regardless_of_author(monkeypatch):
    """A fork is an outside contribution no matter who opened it — the author
    check is never even reached."""
    check = MemberCheck()
    http = _FakeHTTP(_FakeResp(200, _pr_body(assoc="MEMBER", head_full="fork/x")))
    checker = _checker(check, http=http, monkeypatch=monkeypatch)
    assert await checker.is_trusted("bitrise-io", "x", 7) is False
    assert check.calls == []


async def test_pr_checker_fetch_failure_not_cached(monkeypatch):
    """A transient fetch failure returns None and is NOT cached — a one-off
    GitHub blip must not pin a member's PR as 'external' for the whole TTL."""
    http = _FakeHTTP(_FakeResp(502), _FakeResp(200, _pr_body()))
    checker = _checker(http=http, monkeypatch=monkeypatch)
    assert await checker.is_trusted("bitrise-io", "x", 7) is None
    assert await checker.is_trusted("bitrise-io", "x", 7) is True
    assert len(http.calls) == 2


async def test_pr_checker_inconclusive_author_not_cached(monkeypatch):
    """Author-membership None → verdict None, uncached; a later conclusive
    check gets a fresh fetch and can succeed."""
    check = MemberCheck({"dev": None})
    http = _FakeHTTP(_FakeResp(200, _pr_body(assoc="NONE")),
                     _FakeResp(200, _pr_body(assoc="NONE")))
    checker = _checker(check, http=http, monkeypatch=monkeypatch)
    assert await checker.is_trusted("bitrise-io", "x", 7) is None
    check.verdicts["dev"] = True
    assert await checker.is_trusted("bitrise-io", "x", 7) is True


async def test_pr_checker_self_login_trusted_without_membership_call(monkeypatch):
    check = MemberCheck()
    http = _FakeHTTP(_FakeResp(200, _pr_body(author="bitclaw-bot", assoc="NONE")))
    checker = _checker(check, http=http, monkeypatch=monkeypatch)
    assert await checker.is_trusted("bitrise-io", "x", 7) is True
    assert check.calls == []

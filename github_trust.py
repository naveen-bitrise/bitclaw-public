"""Shared GitHub author-trust primitives.

One trust gate guards two surfaces, because both turn a potentially-untrusted
GitHub user's free text into agent input:

  * inbound webhooks (webhooks.py) — drop comment/review *events* whose author
    we can't confirm before they reach the agent as an instruction;
  * outbound MCP reads (tools/hooks.py, a PostToolUse hook) — redact the body/
    title of comment/review/PR/issue objects a GitHub read tool returns whose
    author we can't confirm, before the agent ever sees the text.

A public repo lets anyone comment on / review (or open an issue/PR against) a
bot-touched repo, and that text would otherwise reach the owning Slack session
— over a webhook push *or* over a GitHub read the agent makes itself. Both
paths must apply the SAME definition of "trusted", so it lives here.

The gate fails CLOSED: anything we can't positively attribute to a confirmed
org OWNER/MEMBER/COLLABORATOR (or an installed GitHub App bot) is untrusted.
"""
import logging
import os
import time

from http_client import get_http_session

logger = logging.getLogger(__name__)


# GitHub `author_association` values that, by themselves, prove the author may
# instruct the agent: the repo OWNER, public MEMBERs of the owning org, and
# explicitly-invited COLLABORATORs. This is the cheap fast-path read straight
# from a signed webhook payload or an MCP comment object (no API call). NOTE
# author_association only reports MEMBER for *public* org membership — a member
# whose membership is private (GitHub's default) shows up as CONTRIBUTOR/NONE,
# which is why the gate falls back to the membership API rather than dropping.
# It is also *omitted entirely* from some github-mcp-server "minimal" read
# shapes (a PR fetch, list_pull_requests), so the read side often has only the
# login to go on and leans wholly on the membership fallback.
TRUSTED_AUTHOR_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def is_bot_user(user: dict | None) -> bool:
    """True if a GitHub user object is a GitHub App bot (``type == "Bot"``).

    App bots (dependabot[bot], coderabbitai[bot], …) can only act on a repo
    where an org *admin* installed the App, so that installation is itself the
    trust gate — they're exempt from the org-membership check (an App is
    installed, not an org member, and commonly reports author_association NONE
    / isn't in /orgs/{org}/members, so it would otherwise be wrongly dropped).
    Machine *user* accounts (type == "User") are NOT exempt: they must clear
    the author-trust check.

    Reads the webhook/REST ``type`` field. github-mcp-server "minimal" read
    shapes omit ``type`` entirely, so the read side ALSO uses login_is_bot()
    (the reserved ``[bot]`` login suffix) — see that helper.
    """
    return (user or {}).get("type") == "Bot"


def login_is_bot(login: str | None) -> bool:
    """True if a login is a GitHub App bot login (ends in the reserved ``[bot]``).

    GitHub appends ``[bot]`` to every App bot's actor login and forbids ``[``/
    ``]`` in ordinary usernames, so the suffix is an unforgeable App-bot signal.
    This is the read-side equivalent of is_bot_user(): github-mcp-server's
    "minimal" comment/review shapes carry only ``login`` (no ``type``), so the
    suffix is the only bot signal available there.
    """
    return bool(login) and login.endswith("[bot]")


def membership_from_status(status: int) -> bool | None:
    """Map a `GET /orgs/{org}/members/{user}` response status to membership.

    - 204 → the user is a member of the org.
    - 404 → the user is definitively NOT a member (requester is itself a
      member, so this is authoritative).
    - anything else (302 = the requester/bot is itself not an org member and
      GitHub redirects to the public-members endpoint; 401/403/5xx) → we
      cannot determine membership. Returns None; the caller treats "unknown"
      as unauthorized and drops (fail CLOSED) — this is a trust boundary, so
      we never act on input we can't 100% attribute to a confirmed member.
    """
    if status == 204:
        return True
    if status == 404:
        return False
    return None


class GitHubOrgChecker:
    """Resolve 'is this login a member of <org>?' via the GitHub API, cached.

    Membership rarely changes, so positive/negative answers are cached per
    login for `ttl` seconds to avoid an API round-trip (and rate-limit burn)
    on every comment. Undeterminable answers (None) are NOT cached so a
    transient API error doesn't pin a login as unknown for an hour.
    """

    def __init__(self, org: str | None, token: str | None, ttl: float = 3600.0):
        self._org = org
        self._token = token
        self._ttl = ttl
        self._cache: dict[str, tuple[float, bool]] = {}

    async def is_member(self, login: str | None) -> bool | None:
        if not login:
            return None
        if not (self._org and self._token):
            # No org configured or no token to authenticate the check. Return
            # None ("unknown"); the caller treats unknown as unauthorized and
            # drops (fail closed). GITHUB_ALLOWED_ORG has no default, so an
            # unset org (or a missing GITHUB_TOKEN) is a misconfig that must
            # (loudly) drop every gated author — never silently allow them.
            return None

        now = time.monotonic()
        hit = self._cache.get(login)
        if hit and now - hit[0] < self._ttl:
            return hit[1]

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"https://api.github.com/orgs/{self._org}/members/{login}"
        try:
            # Shared process-wide session: this check sits on the webhook ACK
            # path, so a per-call session would redo the TLS handshake on every
            # gated comment. allow_redirects=False: a 302 means the bot itself
            # isn't an org member; we want to see that status, not follow it to
            # the public-members endpoint (which would mask private members).
            session = get_http_session()
            async with session.get(
                url, headers=headers, timeout=10, allow_redirects=False
            ) as resp:
                result = membership_from_status(resp.status)
        except Exception:
            logger.warning(
                "GitHub org-membership check for %r in %s failed — "
                "treating author as unauthorized",
                login, self._org, exc_info=True,
            )
            return None

        if result is None:
            logger.warning(
                "GitHub org-membership check for %r in %s inconclusive "
                "(HTTP %d) — treating author as unauthorized",
                login, self._org, resp.status,
            )
            return None

        self._cache[login] = (now, result)
        return result


async def author_is_trusted(
    *,
    login: str | None,
    association: str | None,
    is_bot: bool,
    org_member_check,
) -> bool | None:
    """Apply the two-signal author-trust gate (fail CLOSED), TRI-STATE.

    1. App bots are trusted — an org admin installed the App.
    2. author_association OWNER/MEMBER/COLLABORATOR is trusted with no API call
       (covers owners, *public* org members, invited collaborators).
    3. Otherwise fall back to the cached org-membership API (authoritative for
       *private* members too): True (a confirmed member), False (a definitive
       non-member), or None (the check was inconclusive — a transient API error).

    Returns the membership tri-state straight through in case 3: True (trusted),
    False (definitively untrusted), or None (couldn't determine). Callers that
    only want a yes/no still fail CLOSED for free — `if not trusted:` treats both
    False and None as untrusted (the webhook gate and the read filter rely on
    this). But a caller that must NOT cache a transient verdict (the PR-anchor
    gate) can now tell a definitive non-member (False, safe to cache) apart from
    an undeterminable answer (None, must NOT be cached, retryable).

    `org_member_check` is the async `GitHubOrgChecker.is_member`-shaped callable
    (injectable for tests).
    """
    if is_bot:
        return True
    if (association or "").upper() in TRUSTED_AUTHOR_ASSOCIATIONS:
        return True
    return await org_member_check(login)


_default_checker: GitHubOrgChecker | None = None


def default_org_member_check():
    """Return the process-wide `is_member` check, bound from env once.

    A single cached GitHubOrgChecker is shared across callers (the MCP posthook
    runs per AgentLoop) so its per-login membership cache is warmed once, not
    rebuilt per session. Reads GITHUB_ALLOWED_ORG (NO default — unset means
    every membership check answers "unknown" and callers fail closed) and
    GITHUB_TOKEN — the same env the webhook app uses.
    """
    global _default_checker
    if _default_checker is None:
        _default_checker = GitHubOrgChecker(
            os.environ.get("GITHUB_ALLOWED_ORG") or None,
            os.environ.get("GITHUB_TOKEN"),
        )
    return _default_checker.is_member


# --------------------------------------------------------------------------
# Repo visibility (drives the public-repo egress approval gate)
# --------------------------------------------------------------------------
#
# The egress gate (tools.hooks.make_public_egress_gate) only holds a GitHub
# WRITE for human approval when its target repo is PUBLIC — private/internal
# repos are internal-audience and pass through untouched. A PreToolUse hook
# can't trust an LLM-derived "is this public" claim for a security boundary, so
# visibility is resolved in CODE here, mirroring GitHubOrgChecker.


def repo_is_public_from_payload(repo: dict | None) -> bool | None:
    """Map a `GET /repos/{owner}/{repo}` JSON body to public?/unknown.

    Returns True (public), False (private or internal), or None when the shape
    is unrecognizable. GitHub's `visibility` is the authoritative field
    (public/private/internal); the older `private` bool is the fallback. Treat
    `internal` (enterprise) as NOT public — its audience is the enterprise, not
    the world — but still NOT "the open internet", so the gate's caller decides;
    here we only answer the literal question "is this world-readable public".
    """
    if not isinstance(repo, dict):
        return None
    visibility = repo.get("visibility")
    if isinstance(visibility, str) and visibility:
        return visibility.lower() == "public"
    private = repo.get("private")
    if isinstance(private, bool):
        return not private
    return None


class RepoVisibilityChecker:
    """Resolve 'is owner/repo a PUBLIC repo?' via the GitHub API, cached.

    Visibility almost never changes, so definitive answers (public/non-public)
    are cached per `owner/repo` for `ttl` seconds. Undeterminable answers (None
    — 404/401/403/5xx/transport error) are NOT cached, so a transient blip
    doesn't pin a repo as unknown; the caller treats None as "gate it"
    (fail closed: an unconfirmed repo is assumed public, matching the
    'if visibility is unknown, assume PUBLIC and sanitize' prompt rule).
    """

    def __init__(self, token: str | None, ttl: float = 3600.0):
        self._token = token
        self._ttl = ttl
        self._cache: dict[str, tuple[float, bool]] = {}

    async def is_public(self, owner: str | None, repo: str | None) -> bool | None:
        if not (owner and repo):
            return None
        key = f"{owner}/{repo}"
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self._ttl:
            return hit[1]

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"https://api.github.com/repos/{owner}/{repo}"
        try:
            session = get_http_session()
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    result = repo_is_public_from_payload(body)
                else:
                    # 404 (private + no access, or nonexistent), 401/403, 5xx —
                    # we can't confirm. Stay None → caller fails closed (gates).
                    result = None
        except Exception:
            logger.warning(
                "GitHub repo-visibility check for %s failed — treating as "
                "unconfirmed (gate as public)", key, exc_info=True,
            )
            return None

        if result is None:
            logger.warning(
                "GitHub repo-visibility check for %s inconclusive — treating as "
                "unconfirmed (gate as public)", key,
            )
            return None

        self._cache[key] = (now, result)
        return result


_default_visibility_checker: RepoVisibilityChecker | None = None


def default_repo_visibility_check():
    """Return the process-wide `is_public(owner, repo)` check, bound from env once.

    Shared across every AgentLoop's egress hook so the per-repo visibility cache
    is warmed once. Reads GITHUB_TOKEN (same as the membership check); a public
    repo is readable even unauthenticated, so this degrades gracefully if the
    token is absent.
    """
    global _default_visibility_checker
    if _default_visibility_checker is None:
        _default_visibility_checker = RepoVisibilityChecker(
            os.environ.get("GITHUB_TOKEN"),
        )
    return _default_visibility_checker.is_public


# --------------------------------------------------------------------------
# Pull-request author trust (gates register_anchor for external PRs)
# --------------------------------------------------------------------------
#
# A `github_pr` anchor is created ONLY by the agent's register_anchor tool, and
# in normal operation bitclaw only anchors PRs IT opened (its own branch in the
# upstream repo → trusted). But to be safe against a prompt-injected agent (or a
# misguided human request), register_anchor refuses to anchor a PR that is a fork
# / cross-repo PR, OR whose author can't be confirmed as a trusted org member —
# an external / fork / untrusted-author PR. This is a HARD rule with NO opt-in
# (not even on a human's request): the orchestrator holds internal context +
# privileged MCP access, so it must never take an external contribution into its
# autonomous workflow. Enforced inside register_anchor (the in-process Slack
# tool) since PreToolUse hooks only fire on the external github/infra MCP
# servers.


def pr_is_cross_repo(pr: dict | None) -> bool | None:
    """Is this PR a cross-repo (fork) PR — head repo != base repo?

    Returns True (the head branch lives in a DIFFERENT repo than the base — a
    fork / cross-repo PR), False (head and base are the SAME repo — a branch
    pushed straight to the upstream, the only shape bitclaw itself ever opens),
    or None when head/base can't be read (a malformed response → caller fails
    CLOSED). A deleted head fork — GitHub returns `head.repo == null` once the
    fork repo is gone — still counts as cross-repo (it WAS a fork) → True.

    Compares `full_name` ("owner/repo"); both sides come from the same PR object,
    so a rename can't desync them within one response. This is the same signal
    GitHub Actions uses to tell a fork PR from an internal one.
    """
    if not isinstance(pr, dict):
        return None
    base_repo = (pr.get("base") or {}).get("repo")
    base_full = base_repo.get("full_name") if isinstance(base_repo, dict) else None
    if not base_full:
        return None  # can't read the base repo — don't guess same-repo
    head = pr.get("head")
    if not isinstance(head, dict) or "repo" not in head:
        return None  # malformed head — can't confirm same-repo
    head_repo = head.get("repo")
    if head_repo is None:
        return True  # head fork was deleted → it WAS a cross-repo PR
    head_full = head_repo.get("full_name") if isinstance(head_repo, dict) else None
    if not head_full:
        return None
    return head_full != base_full


class PullRequestTrustChecker:
    """Resolve 'is owner/repo#N a trusted PR to anchor?' via the GitHub API.

    A PR's repo owner, author, and head/base are immutable for a given PR
    number, so a definitive verdict is cached per `owner/repo#N`. Returns:

      * True  — a PR in a repo owned by the trusted org, same-repo (not a fork),
                authored by a trusted party (the bot itself, a confirmed org
                OWNER/MEMBER/COLLABORATOR, or an installed App bot);
      * False — a DEFINITIVE refusal: the repo is NOT owned by the trusted org,
                OR it's a fork / cross-repo PR (regardless of author), OR it's a
                same-repo PR by a confirmed non-member. Cacheable — all three are
                immutable for this PR;
      * None  — the verdict is UNDETERMINED (no trusted org configured, the PR
                couldn't be fetched, the head/base repo couldn't be read, or the
                membership check was inconclusive — all transient/config). NOT
                cached, so a blip doesn't pin a legitimate PR as refused; the
                caller fails CLOSED but may retry.

    Checks run outermost-first: (1) repo ownership — only anchor PRs in repos the
    trusted org owns; (2) fork — a fork is an outside contribution no matter who
    opened it; (3) author. The repo-ownership gate also makes the author check
    sound: `author_association` (OWNER/MEMBER/COLLABORATOR) is GitHub-computed
    relative to the PR's OWN repo, so without (1) an outsider's PR in their own
    repo would report OWNER and pass — (1) confines that fast-path to trusted-org
    repos. Author trust reuses the SAME `author_is_trusted` gate as the webhook
    ingress and the read filter, so 'trusted' means one thing everywhere.
    """

    def __init__(self, token: str | None, org_member_check,
                 self_login: str | None = None,
                 allowed_org: str | None = None, ttl: float = 3600.0):
        self._token = token
        self._check = org_member_check
        self._self_login = self_login
        # The org that must OWN the PR's repo for it to be anchorable (the base
        # repo owner — the URL we fetch is repos/{owner}/{repo}/pulls/{N}, so the
        # `owner` arg IS the base repo owner, authoritative with no API call).
        self._allowed_org = allowed_org
        self._ttl = ttl
        self._cache: dict[str, tuple[float, bool]] = {}

    async def is_trusted(self, owner: str | None, repo: str | None,
                         number: int | None) -> bool | None:
        if not (owner and repo and number is not None):
            return None
        key = f"{owner}/{repo}#{number}"
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self._ttl:
            return hit[1]

        # (1) Repo-ownership gate FIRST — only anchor a PR whose repo is owned by
        # the trusted org. `owner` is the base repo's owner (we fetch
        # repos/{owner}/{repo}/pulls/{N}, so the PR's base is necessarily that
        # repo), so this is authoritative with NO API call and rejects an
        # outside-org repo up front, before any fetch.
        if not self._allowed_org:
            # No trusted org configured — the gate can't do its job. Refuse
            # (fail closed) and don't cache (a config fix should take effect).
            logger.error(
                "PR trust check for %s: no trusted org configured "
                "(GITHUB_ALLOWED_ORG) — refusing to anchor (fail closed)", key,
            )
            return None
        if (owner or "").lower() != self._allowed_org.lower():
            logger.warning(
                "register_anchor DENIED for %s: repo owner %r is not the trusted "
                "org %r — refusing to anchor a PR outside the org", key, owner,
                self._allowed_org,
            )
            self._cache[key] = (now, False)  # immutable for this repo → cacheable
            return False

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
        try:
            session = get_http_session()
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning(
                        "PR-author trust check for %s inconclusive (HTTP %d) — "
                        "refusing to anchor (fail closed)", key, resp.status,
                    )
                    return None
                pr = await resp.json()
        except Exception:
            logger.warning(
                "PR-author trust check for %s failed — refusing to anchor "
                "(fail closed)", key, exc_info=True,
            )
            return None

        # (2) Fork / cross-repo gate — a PR whose head branch lives in a
        # different repo than the base is an outside contribution regardless of
        # who authored it, so refuse it before even looking at the author.
        cross = pr_is_cross_repo(pr)
        if cross is None:
            logger.warning(
                "PR trust check for %s: head/base repo unreadable — refusing to "
                "anchor (fail closed, no cache)", key,
            )
            return None  # can't confirm same-repo → transient/unknown, retryable
        if cross is True:
            logger.warning(
                "register_anchor DENIED for %s: fork / cross-repo PR (head repo "
                "!= base repo) — refusing to anchor an outside contribution", key,
            )
            self._cache[key] = (now, False)  # immutable for this PR → cacheable
            return False

        # (3) Same-repo PR in a trusted-org repo → resolve author trust (tri-state).
        user = pr.get("user") if isinstance(pr, dict) else None
        user = user if isinstance(user, dict) else {}
        login = user.get("login")
        assoc = pr.get("author_association") if isinstance(pr, dict) else None
        is_bot = is_bot_user(user) or login_is_bot(login)

        # The bot's own login is always trusted (its own PRs are not "external").
        if self._self_login and login == self._self_login:
            result = True
        else:
            result = await author_is_trusted(
                login=login, association=assoc, is_bot=is_bot,
                org_member_check=self._check,
            )
        if result is None:
            # Author membership couldn't be determined (a transient API error,
            # NOT a confirmed outsider). Fail closed but do NOT cache — caching a
            # transient False would pin a legitimate member's PR as "external" for
            # the whole TTL. The caller surfaces this as a retry-once message.
            logger.warning(
                "PR trust check for %s: author %r org-membership inconclusive — "
                "refusing to anchor (fail closed, no cache)", key, login,
            )
            return None
        self._cache[key] = (now, result)  # definitive True/False → cacheable
        return result


_default_pr_trust_checker: PullRequestTrustChecker | None = None


def default_pr_trust_check():
    """Return the process-wide `is_trusted(owner, repo, number)` check, bound from
    env once. Reuses the shared org-membership checker (one warm cache), trusts
    GITHUB_BOT_LOGIN (the bot's own login) without an API call, and gates
    anchoring to repos owned by GITHUB_ALLOWED_ORG (NO default — unset means
    every check refuses, fail closed). It is the SAME org the membership check
    uses, so 'trusted org' means one thing."""
    global _default_pr_trust_checker
    if _default_pr_trust_checker is None:
        _default_pr_trust_checker = PullRequestTrustChecker(
            os.environ.get("GITHUB_TOKEN"),
            default_org_member_check(),
            os.environ.get("GITHUB_BOT_LOGIN") or None,
            os.environ.get("GITHUB_ALLOWED_ORG") or None,
        )
    return _default_pr_trust_checker.is_trusted

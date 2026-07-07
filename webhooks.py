"""Webhook listeners (GitHub, Jira) — the inbound edges that turn external
events into messages in a session's inbox, routed by anchor via the gateway.

GitHub auth is HMAC-SHA256 over the raw request body (X-Hub-Signature-256,
secret = GITHUB_WEBHOOK_SECRET). Jira Cloud webhooks can't send custom headers
or HMAC, so the Jira route authenticates by a shared secret in the URL path
(/webhooks/jira/{token}, token = JIRA_WEBHOOK_SECRET) — which is why this
server runs with HTTP access logging disabled (see main.py): a default access
logger would print the secret to stdout on every request. Never log
request.path on Jira routes here either.

Slack Socket Mode gives this process no inbound URL by default; expose these
routes via a tunnel (cloudflared, ngrok, smee.io) or a reverse proxy.
"""
import hashlib
import hmac
import json
import logging
import os
import unicodedata

from aiohttp import web

from github_trust import (
    TRUSTED_AUTHOR_ASSOCIATIONS as _TRUSTED_AUTHOR_ASSOCIATIONS,
    default_org_member_check,
    is_bot_user,
)
from state import github_pr_anchor_key, lookup_anchor

logger = logging.getLogger(__name__)


def _verify_signature(body: bytes, header: str, secret: str) -> bool:
    if not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _extract_pr_ref(event: str, payload: dict) -> tuple[str, int] | None:
    """Return (repo_full_name, pr_number) if the payload references a PR."""
    repo = (payload.get("repository") or {}).get("full_name")
    if not repo:
        return None

    if event in ("pull_request", "pull_request_review", "pull_request_review_comment"):
        pr = payload.get("pull_request") or {}
        if pr.get("number"):
            return (repo, pr["number"])

    if event == "issue_comment":
        issue = payload.get("issue") or {}
        # GitHub flags PR-comments by presence of the "pull_request" key on the issue
        if issue.get("pull_request") and issue.get("number"):
            return (repo, issue["number"])

    if event in ("check_run", "check_suite", "workflow_run"):
        target = payload.get(event) or {}
        prs = target.get("pull_requests") or []
        if prs and prs[0].get("number"):
            return (repo, prs[0]["number"])

    if event == "status":
        # `status` events carry no PR reference — resolving one would need an
        # API lookup by commit SHA. Deliberately unsupported: the PR-referencing
        # events above already cover the CI signals bitclaw routes.
        return None

    return None


def _adf_to_text(node) -> str:
    # Flatten Atlassian Document Format dict into plain text; never raise.
    if node is None:
        return ""
    if isinstance(node, list):
        return "".join(_adf_to_text(n) for n in node)
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return str(node)

    ntype = node.get("type")

    if ntype == "text":
        return node.get("text", "")

    if ntype == "mention":
        attrs = node.get("attrs") or {}
        return attrs.get("text") or f"@{attrs.get('displayName') or attrs.get('id') or '?'}"

    if ntype == "emoji":
        attrs = node.get("attrs") or {}
        return attrs.get("shortName") or attrs.get("text") or ""

    if ntype == "inlineCard":
        attrs = node.get("attrs") or {}
        return attrs.get("url") or ""

    if ntype == "hardBreak":
        return "\n"

    block_types = {"paragraph", "heading", "codeBlock", "blockquote",
                   "listItem", "bulletList", "orderedList", "doc"}
    if ntype in block_types or "content" in node:
        inner = _adf_to_text(node.get("content"))
        if ntype in block_types and ntype != "doc":
            return inner + "\n"
        return inner

    # Unknown shape — JSON dump keeps logs greppable instead of crashing.
    try:
        return json.dumps(node, separators=(",", ":"))
    except Exception:
        return str(node)


def _comment_body_to_text(body) -> str:
    # Accept both legacy string bodies and modern ADF dicts from Jira Cloud.
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        return _adf_to_text(body).strip()
    if body is None:
        return ""
    return str(body)


def _trunc(s: str, n: int = 500) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


# Most webhook bodies are truncated at 500 chars — the agent can re-fetch the
# full text via the GitHub MCP when it needs more. PR review bodies and inline
# review comments get a more generous cap: they are relayed verbatim to the
# coding agent, so a hard truncation would silently drop the feedback it is
# supposed to act on. Bounded (not unbounded) to keep one huge comment from
# bloating context.
_REVIEW_BODY_MAX_CHARS = 2000


# Sender-bearing GitHub events we filter when the bot authored them.
# CI events (check_run/check_suite/workflow_run) use the integration as sender — don't filter.
_GITHUB_SELF_FILTER_EVENTS = {
    "issue_comment",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request",
}


def _is_self_authored_github(event: str, payload: dict, bot_login: str | None) -> bool:
    if not bot_login or event not in _GITHUB_SELF_FILTER_EVENTS:
        return False
    return (payload.get("sender") or {}).get("login") == bot_login


# Events whose human-authored free-text body reaches the agent as an
# instruction. These are gated behind GitHub org membership: on a *public*
# repo anyone can comment on / review a PR the bot opened, and that text would
# otherwise be routed straight into the owning Slack session as agent input.
# Only members of the allowed org (GITHUB_ALLOWED_ORG) may drive the agent
# this way. State-only events (pull_request open/close, CI runs) carry no
# instruction body and are NOT gated.
_GITHUB_ORG_GATED_EVENTS = {
    "issue_comment",
    "pull_request_review",
    "pull_request_review_comment",
}


def _is_app_bot_sender(payload: dict) -> bool:
    """True if the event's *sender* is a GitHub App bot. See is_bot_user()."""
    return is_bot_user(payload.get("sender"))


def _event_author_association(event: str, payload: dict) -> str | None:
    """Read `author_association` off a gated event's comment/review object.

    GitHub stamps author_association on the webhook payload's comment/review
    object, so this is read from the HMAC-verified payload itself — no API call.
    Returns the raw value (e.g. "MEMBER") or None when absent/non-string.
    """
    if event in ("issue_comment", "pull_request_review_comment"):
        obj = payload.get("comment") or {}
    elif event == "pull_request_review":
        obj = payload.get("review") or {}
    else:
        return None
    assoc = obj.get("author_association")
    return assoc if isinstance(assoc, str) else None


def _is_self_authored_jira(payload: dict, bot_account_id: str | None) -> bool:
    """True for any Jira event the bot itself triggered — loop prevention.

    Like the GitHub `sender.login` self-echo filter, the only thing that
    matters is the *actor* of the event: the bot must never be (re)triggered
    by its own action — assigning a ticket, transitioning it, or commenting.
    A ticket the bot assigns to ITSELF is not a bootstrap signal; only a
    human assigning a ticket to the bot (actor != bot) passes through.
    """
    if not bot_account_id:
        return False
    event = payload.get("webhookEvent", "")
    if event == "comment_created":
        author = (payload.get("comment") or {}).get("author") or {}
        return author.get("accountId") == bot_account_id
    if event in ("jira:issue_created", "jira:issue_updated"):
        return (payload.get("user") or {}).get("accountId") == bot_account_id
    return False


# Session-wide cleanup cue: the WHOLE job is over — tear down ALL RDEs on
# this session and close it. Appended ONLY to the assigned ticket's terminal
# Jira transition (that ticket is the only jira_issue anchored to the
# session). A PR closing uses the per-PR cue below instead, because one PR
# among several finishing must not nuke the session.
_CLEANUP_CUE = (
    "\n[bitclaw cleanup cue: external signal indicates the RDE work is "
    "over. Call mcp__slack__list_anchors(); for every rde_session anchor, "
    "mcp__infra__bitrise_devenv_terminate(session_id=<key>), then poll "
    "mcp__infra__bitrise_devenv_get(session_id=<key>) until status is "
    "SESSION_STATUS_TERMINATED (terminate is async — delete fails until it "
    "finishes), then mcp__infra__bitrise_devenv_delete(session_id=<key>); "
    "treat 'already terminated' / 404 as success. Then call "
    "mcp__slack__close_session(final_message='Done — …') — it posts your "
    "final milestone to the thread AND closes in one atomic step. Do not "
    "post the milestone separately first, and never post after close.]"
)

# Per-PR cleanup cue: a single PR closing means ONE workstream finished, not
# the whole session. A session can own multiple concurrent RDEs (one per
# parallel workstream); merging one PR must retire only that PR's RDE and must
# NOT tear down siblings. The session closes only once no rde_session anchors
# remain.
_PR_CLEANUP_CUE = (
    "\n[bitclaw cleanup cue (single PR): this PR's workstream is over. "
    "Retire ONLY this PR's RDE — from your own records, match this PR to the "
    "rde_session you spun up for that workstream: "
    "mcp__infra__bitrise_devenv_terminate(session_id=<that key>), then poll "
    "mcp__infra__bitrise_devenv_get(session_id=<that key>) until status is "
    "SESSION_STATUS_TERMINATED (terminate is async — delete fails until it "
    "finishes), then mcp__infra__bitrise_devenv_delete(session_id=<that "
    "key>) ('already terminated' / 404 = success). Do NOT delete other RDEs — "
    "sibling workstreams may still be working. THEN call "
    "mcp__slack__list_anchors(): if NO rde_session anchors remain, all work "
    "is done — finish with mcp__slack__close_session(final_message='Done — "
    "…') (posts your final milestone AND closes atomically; never post after "
    "close). If rde_session anchors remain, post a brief progress milestone "
    "and KEEP the session open.]"
)


def _normalize_status(s: str) -> str:
    # Atlassian frequently smart-quotes apostrophes (e.g. "Won't Do" →
    # "Won’t Do") in workflow status names. NFKC handles compatibility
    # forms but does NOT equate U+2019 (right single quotation mark) with
    # U+0027 (apostrophe); explicit fold for the curly variants ensures the
    # default value matches what Jira actually emits in changelog toString.
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("’", "'").replace("‘", "'").replace("ʼ", "'")
    return s.strip().casefold()


def _is_terminal_jira_status(to_status: str | None) -> bool:
    if not to_status:
        return False
    terminal = (
        os.environ.get("JIRA_TERMINAL_STATUSES")
        or "Done,Closed,Cancelled,Won't Do"
    )
    return _normalize_status(to_status) in {
        _normalize_status(s) for s in terminal.split(",") if s.strip()
    }


def _format_event(event: str, payload: dict, repo: str = "", pr_number: int | None = None) -> str | None:
    """One-line synthesized message describing the event for the agent.

    `repo` ("owner/repo") and `pr_number` are surfaced from `_extract_pr_ref`
    so the formatter doesn't have to re-derive them; the inline-review-comment
    branch embeds both inline so the agent has everything it needs to call
    `add_reply_to_pull_request_comment(owner, repo, pullNumber, commentId, body)`
    from a single inbound message.
    """
    pr = (payload.get("pull_request") or {})
    if pr_number is None:
        pr_number = pr.get("number") or (payload.get("issue") or {}).get("number")
    user = (payload.get("sender") or {}).get("login", "?")
    action = payload.get("action", "")

    if event == "pull_request_review":
        review = payload.get("review") or {}
        state = review.get("state", "?")
        body = _trunc(review.get("body") or "", _REVIEW_BODY_MAX_CHARS)
        return f"GitHub: {state} review on PR #{pr_number} by {user}: {body}"

    if event == "pull_request_review_comment":
        comment = payload.get("comment") or {}
        path = comment.get("path", "")
        body = _trunc(comment.get("body") or "", _REVIEW_BODY_MAX_CHARS)
        comment_id = comment.get("id")
        in_reply_to_id = comment.get("in_reply_to_id")
        # GitHub's reply API posts under the thread root. If this comment is
        # itself a reply, in_reply_to_id IS the root; otherwise the comment's
        # own id IS the root.
        thread_root = in_reply_to_id or comment_id
        pr_ref = f"{repo}#{pr_number}" if repo else f"PR #{pr_number}"
        return (
            f"GitHub: review comment on {pr_ref} by {user} ({path}) "
            f"[thread_root_id={thread_root}]: {body}"
        )

    if event == "issue_comment":
        comment = payload.get("comment") or {}
        body = _trunc(comment.get("body") or "")
        return f"GitHub: comment on PR #{pr_number} by {user}: {body}"

    if event == "pull_request":
        merged = (pr.get("merged") is True)
        if action == "closed":
            verb = "merged" if merged else "closed without merge"
            return f"GitHub: PR #{pr_number} {verb} by {user}{_PR_CLEANUP_CUE}"
        return f"GitHub: PR #{pr_number} {action} by {user}"

    if event == "check_run":
        run = payload.get("check_run") or {}
        return (
            f"GitHub: check '{run.get('name')}' {run.get('conclusion')} on PR #{pr_number} "
            f"({run.get('html_url')})"
        )

    if event == "check_suite":
        suite = payload.get("check_suite") or {}
        return f"GitHub: check_suite {suite.get('conclusion')} on PR #{pr_number}"

    if event == "workflow_run":
        run = payload.get("workflow_run") or {}
        return (
            f"GitHub: workflow '{run.get('name')}' {run.get('conclusion')} on PR #{pr_number} "
            f"({run.get('html_url')})"
        )

    return None


def _github_status_hint(event: str, payload: dict) -> str:
    """Coarse 'what just landed' live-status label for a GitHub webhook.

    Set programmatically the instant the event is routed, before the agent
    runs, so the thread shows life during the gap to the agent's first post.
    """
    if event == "pull_request_review":
        return "PR review received"
    if event == "pull_request_review_comment":
        return "review comment received"
    if event == "issue_comment":
        return "PR comment received"
    if event == "pull_request" and payload.get("action") == "closed":
        return "PR closed"
    if event in ("check_run", "check_suite", "workflow_run"):
        return "CI update received"
    return "GitHub event received"


# ----------------------------------------------------------------------------
# Jira
# ----------------------------------------------------------------------------

def _jira_extract(payload: dict, bot_account_id: str | None) -> dict | None:
    """Classify a Jira webhook payload.

    Returns one of:
      - {"kind": "assignment", "issue_key": ..., "project_key": ..., "summary": ..., "assigner": ...}
      - {"kind": "comment", "issue_key": ..., "project_key": ..., "author": ..., "body": ...}
      - {"kind": "transition", "issue_key": ..., "project_key": ..., "from": ..., "to": ..., "user": ...}
      - None  (uninteresting / not bot-relevant)
    """
    event = payload.get("webhookEvent", "")
    issue = payload.get("issue") or {}
    fields = issue.get("fields") or {}
    issue_key = issue.get("key")
    if not issue_key:
        return None

    project_key = (fields.get("project") or {}).get("key", "")
    summary = fields.get("summary", "")
    user = payload.get("user") or {}

    if event == "comment_created":
        comment = payload.get("comment") or {}
        # Flatten ADF dict bodies before truncation so the cap applies to readable text.
        body_text = _comment_body_to_text(comment.get("body"))
        return {
            "kind": "comment",
            "issue_key": issue_key,
            "project_key": project_key,
            "author": (comment.get("author") or {}).get("displayName", "?"),
            "body": _trunc(body_text),
        }

    if event == "jira:issue_created":
        # New ticket assigned directly to the bot at creation time.
        if bot_account_id and (fields.get("assignee") or {}).get("accountId") == bot_account_id:
            return {
                "kind": "assignment",
                "issue_key": issue_key,
                "project_key": project_key,
                "summary": summary,
                "assigner": user.get("displayName", "?"),
            }
        return None

    if event == "jira:issue_updated":
        changelog = payload.get("changelog") or {}
        items = changelog.get("items") or []

        # Combo events (assignee + status in one save) are real; capture both in one walk.
        assignment_to_bot = False
        transition: dict | None = None
        assignee_change: dict | None = None

        for it in items:
            field = it.get("field")
            if field == "assignee":
                # `to` is the accountId (the identity signal); fromString/toString
                # are display names, kept only for the human-readable ride-along.
                if bot_account_id and it.get("to") == bot_account_id:
                    assignment_to_bot = True
                else:
                    assignee_change = {
                        "from": it.get("fromString"),
                        "to": it.get("toString"),
                    }
            elif field == "status":
                transition = {
                    "from": it.get("fromString"),
                    "to": it.get("toString"),
                }

        # Strongest signal wins; the other rides along as "extra".
        if assignment_to_bot:
            result = {
                "kind": "assignment",
                "issue_key": issue_key,
                "project_key": project_key,
                "summary": summary,
                "assigner": user.get("displayName", "?"),
            }
            if transition:
                result["extra"] = (
                    f"also transitioned {transition['from']} → {transition['to']}"
                )
            return result

        if transition:
            result = {
                "kind": "transition",
                "issue_key": issue_key,
                "project_key": project_key,
                "from": transition["from"],
                "to": transition["to"],
                "user": user.get("displayName", "?"),
            }
            if assignee_change:
                result["extra"] = (
                    f"assignee changed: {assignee_change['from']} → {assignee_change['to']}"
                )
            return result

    return None


def _format_jira_event(parsed: dict) -> str:
    # Combo events (assignment+transition, transition+assignee-change) ride along in "extra".
    extra = parsed.get("extra")
    suffix = f" ({extra})" if extra else ""

    if parsed["kind"] == "assignment":
        return (
            f"Jira: {parsed['issue_key']} assigned to you by {parsed['assigner']} — "
            f"{parsed['summary']}{suffix}"
        )
    if parsed["kind"] == "comment":
        return (
            f"Jira: comment on {parsed['issue_key']} by {parsed['author']}: {parsed['body']}"
        )
    if parsed["kind"] == "transition":
        base = (
            f"Jira: {parsed['issue_key']} transitioned {parsed['from']} → {parsed['to']} "
            f"by {parsed['user']}{suffix}"
        )
        if _is_terminal_jira_status(parsed.get("to")):
            return base + _CLEANUP_CUE
        return base
    return f"Jira: {parsed.get('issue_key', '?')} (unrecognized event)"


# ----------------------------------------------------------------------------
# aiohttp app wiring
# ----------------------------------------------------------------------------

def make_webhook_app(
    gateway,
    slack_client,
    github_bot_login: str | None = None,
    org_member_check=None,
) -> web.Application:
    gh_secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    jira_secret = os.environ.get("JIRA_WEBHOOK_SECRET")
    jira_bot_account_id = os.environ.get("JIRA_BOT_ACCOUNT_ID")

    # Gate comment/review events behind membership of this GitHub org so an
    # outsider on a public repo can't instruct the agent through a PR comment.
    # `org_member_check` is injectable for tests; in production it's the
    # API-backed checker authenticated with GITHUB_TOKEN. The gate is always on
    # and fails CLOSED (see handle_github). GITHUB_ALLOWED_ORG only names the
    # org for the membership-API fallback — it is NOT a kill switch; leaving it
    # unset just means the fallback can't confirm private members (they then
    # fail closed), it does NOT let untrusted authors through.
    allowed_org = os.environ.get("GITHUB_ALLOWED_ORG") or None
    if org_member_check is None:
        # Share the process-wide checker with the read-side trust filter
        # (tools.hooks.make_github_trust_filter) so a login resolved on one
        # path is cached for the other — one membership cache, one definition
        # of "trusted", no drift. Reads the same GITHUB_ALLOWED_ORG/GITHUB_TOKEN
        # env this function already uses.
        org_member_check = default_org_member_check()

    async def handle_github(request: web.Request) -> web.Response:
        body = await request.read()
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not gh_secret:
            logger.error("GITHUB_WEBHOOK_SECRET not set — rejecting webhook")
            return web.Response(status=503, text="webhook secret not configured")
        if not _verify_signature(body, sig, gh_secret):
            logger.warning("Bad GitHub webhook signature")
            return web.Response(status=401, text="bad signature")

        event = request.headers.get("X-GitHub-Event", "")
        try:
            payload = json.loads(body)
        except ValueError:
            logger.warning(
                "Discarding GitHub webhook (event=%s): request body is not valid JSON",
                event,
            )
            return web.Response(status=400, text="bad json")

        if event == "ping":
            return web.json_response({"pong": True})

        if _is_self_authored_github(event, payload, github_bot_login):
            logger.info("Suppressing bot-self-authored GitHub event: %s", event)
            return web.Response(status=204)

        ref = _extract_pr_ref(event, payload)
        if not ref:
            return web.Response(status=204)

        repo, pr_number = ref
        pr_url = f"https://github.com/{repo}/pull/{pr_number}"
        anchor_key = github_pr_anchor_key(repo, pr_number)

        # Anchor check FIRST: only a PR some session owns can be routed
        # anywhere. Bail before the org-membership gate so the flood of webhook
        # events for PRs we don't own (anything on a public repo) is dropped
        # cheaply — without the per-event GitHub membership API round-trip the
        # gate would otherwise make for every comment, owned or not.
        if await lookup_anchor("github_pr", anchor_key) is None:
            return web.Response(status=204)

        # Author-trust gate (fail CLOSED): only authors GitHub vouches for may
        # steer the agent via comment/review text. Two complementary signals,
        # because neither alone is sufficient:
        #   1. author_association from the signed payload (no API call) trusts
        #      OWNER/MEMBER/COLLABORATOR — this catches owners, *public* org
        #      members, and invited outside collaborators (who aren't org
        #      members, so a membership check alone would miss them).
        #   2. But author_association reports MEMBER only for *public* org
        #      membership; a private member (GitHub's default) shows as
        #      CONTRIBUTOR/NONE. So when the association doesn't prove trust,
        #      fall back to the membership API (authoritative regardless of
        #      visibility) before dropping — this catches private members.
        # App bots are exempt (an org admin installed the App). The gate is
        # ALWAYS on — there is no opt-out: we must never act on a comment whose
        # author we can't confirm. (With GITHUB_ALLOWED_ORG unset the fallback
        # can't run, so anything not trusted by author_association fails closed.)
        if event in _GITHUB_ORG_GATED_EVENTS and not _is_app_bot_sender(payload):
            assoc = _event_author_association(event, payload)
            if (assoc or "").upper() not in _TRUSTED_AUTHOR_ASSOCIATIONS:
                sender = (payload.get("sender") or {}).get("login")
                member = await org_member_check(sender)
                if member is not True:
                    reason = (
                        "not an org member" if member is False
                        else "org membership could not be determined"
                    )
                    logger.warning(
                        "Dropping GitHub %s from %r on %s (association=%s, org=%s): %s",
                        event, sender, pr_url, assoc, allowed_org, reason,
                    )
                    return web.Response(status=204)

        text = _format_event(event, payload, repo, pr_number)
        if not text:
            return web.Response(status=204)

        delivered = await gateway.deliver_to_anchor(
            "github_pr", anchor_key, text, slack_client,
            status_hint=_github_status_hint(event, payload),
        )
        if not delivered:
            return web.Response(status=204)
        return web.Response(status=200, text="ok")

    async def handle_jira(request: web.Request) -> web.Response:
        if not jira_secret:
            logger.error("JIRA_WEBHOOK_SECRET not set — rejecting webhook")
            return web.Response(status=503, text="webhook secret not configured")

        token = request.match_info.get("token", "")
        if not hmac.compare_digest(token, jira_secret):
            logger.warning("Bad Jira webhook token")
            return web.Response(status=401, text="bad token")

        try:
            payload = await request.json()
        except Exception as exc:
            # No path/URL here — the Jira route's URL carries the shared secret
            # (see the module docstring). content_type + the exception message
            # are safe and enough to diagnose the malformed request.
            logger.warning(
                "Discarding Jira webhook: request body could not be parsed as "
                "JSON (content_type=%s): %s",
                request.content_type,
                exc,
            )
            return web.Response(status=400, text="bad json")

        # Full raw payload at DEBUG only (opt-in via LOG_LEVEL=DEBUG): Jira
        # descriptions can contain secrets, so we never dump the body at INFO.
        # Capped so a giant description can't blow up a log line.
        if logger.isEnabledFor(logging.DEBUG):
            try:
                logger.debug(
                    "Jira webhook raw payload: %s",
                    json.dumps(payload, default=str)[:8000],
                )
            except Exception:
                logger.debug("Jira webhook raw payload (unserializable): %r", payload)

        if _is_self_authored_jira(payload, jira_bot_account_id):
            logger.info(
                "Suppressing bot-self-authored Jira event: %s",
                payload.get("webhookEvent"),
            )
            return web.Response(status=204)

        parsed = _jira_extract(payload, jira_bot_account_id)
        if not parsed:
            return web.Response(status=204)

        text = _format_jira_event(parsed)
        issue_key = parsed["issue_key"]

        if parsed["kind"] == "assignment":
            if not jira_bot_account_id:
                logger.error("JIRA_BOT_ACCOUNT_ID not set — cannot bootstrap assignment session")
                return web.Response(status=503, text="bot account id not configured")
            session_id = await gateway.start_session_for_jira_issue(
                issue_key=issue_key,
                summary=parsed["summary"],
                slack_client=slack_client,
                initial_user_message=text,
            )
            if session_id is None:
                return web.Response(status=503, text="could not bootstrap session")
            return web.Response(status=200, text="ok")

        # Comments and transitions: route to existing session if any.
        jira_hint = (
            "Jira comment received" if parsed["kind"] == "comment"
            else "Jira update received"
        )
        delivered = await gateway.deliver_to_anchor(
            "jira_issue", issue_key, text, slack_client, status_hint=jira_hint,
        )
        if not delivered:
            return web.Response(status=204)
        return web.Response(status=200, text="ok")

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_post("/webhooks/github", handle_github)
    app.router.add_post("/webhooks/jira/{token}", handle_jira)
    app.router.add_get("/healthz", health)
    return app

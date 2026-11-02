"""Bitrise CI + CodePush tool surface for the orchestrator (MCP server key
"bitrise", the hosted https://mcp.bitrise.io limited to the `builds` and
`release-management-code-push` API groups by header).

What bitclaw may do there, enforced in code:
  * READ CI builds/logs of ONE pinned Bitrise app (BITRISE_APP_SLUG) — to
    diagnose a failed PR check and relay the log excerpt to the coding agent;
  * READ CodePush deployments/updates of the pinned deployments only
    (CODEPUSH_DEPLOYMENT_IDS, comma-separated);
  * CHANGE a live release — pause/resume or re-scope an update
    (codepush_patch_update) or roll a deployment back
    (codepush_rollback_deployment) — ONLY on those deployments, and ONLY after a
    human clicks Approve in the Slack thread.

Everything else in those API groups (trigger/abort builds, create/delete/promote
deployments, upload or delete updates) is not allowlisted: denied by the
allowlist and stripped from the model's context.

codepush_patch_update takes only an update id, which says nothing about its
deployment. So an update id is accepted only if this process has seen it in a
codepush_list_updates / codepush_get_update result for a pinned deployment
(the PostToolUse recorder below). An id the agent never listed — say, one
pasted into Slack — is refused.
"""
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

BITRISE_TOOL_PREFIX = "mcp__bitrise__"
BITRISE_TOOL_MATCHER = BITRISE_TOOL_PREFIX + ".*"

_CI_READS = {"list_builds", "get_build", "get_build_log", "get_build_steps"}
_CODEPUSH_READS = {
    "codepush_list_deployments", "codepush_get_deployment",
    "codepush_list_updates", "codepush_get_update", "codepush_get_update_status",
}
_CODEPUSH_WRITES = {"codepush_patch_update", "codepush_rollback_deployment"}

# Positive allowlist (bare tool names) wired into allowed_tools.
BITRISE_ALLOWED_TOOLS = sorted(_CI_READS | _CODEPUSH_READS | _CODEPUSH_WRITES)

# Tools the two enabled API groups expose that bitclaw never uses: bare-denied
# so they leave the model's context (execution is already denied by the
# allowlist above).
BITRISE_CONTEXT_DENIES = [BITRISE_TOOL_PREFIX + t for t in [
    "trigger_bitrise_build", "abort_build", "get_build_bitrise_yml",
    "list_build_workflows",
    "codepush_create_deployment", "codepush_update_deployment",
    "codepush_delete_deployment", "codepush_promote_deployment",
    "codepush_delete_update", "codepush_generate_update_upload_url",
    "codepush_get_metrics",
]]

_UUID_RE = re.compile(r'"id"\s*:\s*"([0-9a-fA-F-]{36})"')

# update id -> deployment id, learned from list/get results on pinned
# deployments. Process-wide and volatile: after a restart the agent lists again.
_KNOWN_UPDATES: dict[str, str] = {}


def _csv_env(name: str) -> set[str]:
    return {v.strip() for v in os.environ.get(name, "").split(",") if v.strip()}


def pinned_deployments() -> set[str]:
    return _csv_env("CODEPUSH_DEPLOYMENT_IDS")


def pinned_codepush_apps() -> set[str]:
    return _csv_env("CODEPUSH_APP_IDS")


def pinned_app_slug() -> str | None:
    return os.environ.get("BITRISE_APP_SLUG", "").strip() or None


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _content_hash(tool_name: str, tool_input: dict) -> str:
    import hashlib
    canonical = json.dumps(
        {"tool": tool_name, "input": tool_input}, sort_keys=True, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _scope_check(short: str, tool_input: dict) -> str | None:
    """Return a deny reason if the call reaches outside the pinned scope."""
    deployments = pinned_deployments()
    if short in _CI_READS:
        slug = pinned_app_slug()
        if not slug:
            return ("Bitrise CI reads are disabled: BITRISE_APP_SLUG is not set "
                    "(operator config).")
        if tool_input.get("app_slug") != slug:
            return (f"bitclaw may only read CI builds of Bitrise app {slug}. "
                    "Retry with that exact app_slug.")
        return None
    if not deployments:
        return ("CodePush is disabled: CODEPUSH_DEPLOYMENT_IDS is not set "
                "(operator config). Tell the human; don't retry.")
    if short == "codepush_list_deployments":
        apps = pinned_codepush_apps()
        if tool_input.get("app_id") not in apps:
            return ("bitclaw may only list CodePush deployments of the pinned "
                    f"apps ({', '.join(sorted(apps)) or 'none configured'}).")
        return None
    if short in ("codepush_get_deployment", "codepush_rollback_deployment"):
        if tool_input.get("id") not in deployments:
            return ("bitclaw may only act on the pinned CodePush deployments "
                    f"({', '.join(sorted(deployments))}). Retry with one of those ids.")
        return None
    if short == "codepush_list_updates":
        if tool_input.get("deployment_id") not in deployments:
            return ("bitclaw may only list updates of the pinned CodePush "
                    f"deployments ({', '.join(sorted(deployments))}).")
        return None
    if short in ("codepush_get_update", "codepush_get_update_status",
                 "codepush_patch_update"):
        uid = tool_input.get("id")
        if uid not in _KNOWN_UPDATES or _KNOWN_UPDATES[uid] not in deployments:
            return ("Unknown CodePush update id. First list the updates of the "
                    "pinned deployment with codepush_list_updates "
                    f"(deployment_id one of {', '.join(sorted(deployments))}), "
                    "then use an id from that result.")
        return None
    return f"{short} is not allowed for bitclaw."


def _describe_write(short: str, tool_input: dict) -> tuple[str, str, str]:
    """(action_label, target_label, preview) for the Slack approval prompt."""
    if short == "codepush_rollback_deployment":
        dep = tool_input.get("id", "?")
        pkg = tool_input.get("package_id")
        action = "roll back the CodePush deployment" + (
            f" to package {pkg}" if pkg else " to its previous release")
        return action, f"deployment {dep}", ""
    uid = tool_input.get("id", "?")
    changes = []
    if "disabled" in tool_input:
        changes.append("PAUSE (disabled=true): devices stop downloading it"
                       if str(tool_input["disabled"]).lower() == "true"
                       else "RESUME (disabled=false)")
    if "rollout" in tool_input:
        changes.append(f"rollout -> {tool_input['rollout']}% of users")
    if "mandatory" in tool_input:
        changes.append(f"mandatory -> {tool_input['mandatory']}")
    dep = _KNOWN_UPDATES.get(uid, "?")
    return ("change a live CodePush update", f"update {uid} (deployment {dep})",
            "\n".join(changes) or "(no field changes)")


def make_codepush_gate(session_id, slack_tools, egress_registry):
    """PreToolUse hook for every mcp__bitrise__ call: scope pin for reads and
    writes, plus human approval (the same Approve/Reject rail as public GitHub
    writes) for the two release-changing writes. Fails closed."""

    async def codepush_gate(input, tool_use_id, context):
        tool_name = input.get("tool_name") or ""
        if not tool_name.startswith(BITRISE_TOOL_PREFIX):
            return {}
        short = tool_name[len(BITRISE_TOOL_PREFIX):]
        try:
            tool_input = input.get("tool_input") or {}
            reason = _scope_check(short, tool_input)
            if reason:
                logger.warning("codepush gate: deny %s (%s)", tool_name, reason)
                return _deny(reason)
            if short not in _CODEPUSH_WRITES:
                return {}
            if egress_registry is None:
                return _deny("Release changes need human approval, but the "
                             "approval system isn't wired in this deployment.")
            h = _content_hash(tool_name, tool_input)
            if egress_registry.take_approved(session_id=session_id, content_hash=h):
                logger.info("codepush gate: human-approved %s (session %d)",
                            tool_name, session_id)
                return {}
            action, target, preview = _describe_write(short, tool_input)
            req, created = egress_registry.park(
                session_id=session_id, content_hash=h, tool_name=tool_name,
                summary=preview, target=target,
            )
            posted = True
            if created:
                ts = None
                try:
                    ts = await slack_tools.post_approval_request(
                        req_id=req.req_id, action_label=action,
                        target_label=target, body_preview=preview,
                        header=(f":rotating_light: *Release change needs approval* — "
                                f"I want to {action}: *{target}*. This changes what "
                                "users' devices get. Approve to apply it, or reject."),
                        fallback=f"Approval needed to {action} ({target}).",
                        approve_label="Approve & apply",
                    )
                except Exception:
                    logger.exception("codepush gate: posting approval failed")
                if ts is None:
                    posted = False
                    egress_registry.forget(req.req_id)
                else:
                    try:
                        await slack_tools.set_live_status("awaiting approval")
                    except Exception:
                        logger.debug("codepush gate: status failed", exc_info=True)
            if not posted:
                return _deny(
                    "RELEASE-CHANGE GATE: this is held for human approval, but "
                    "posting the Approve/Reject buttons FAILED. Re-issue the "
                    "EXACT same call once to retry; if it fails again, say you're "
                    "blocked on the approval system and STOP.")
            return _deny(
                f"RELEASE-CHANGE GATE: '{action}' on {target} is held for HUMAN "
                "approval (enforced in code; not an error). The Approve/Reject "
                f"buttons are posted in this thread (request {req.req_id}). Post "
                "ONE brief line that you're waiting on approval, then END your "
                "turn. On '[bitclaw egress approved: <id>]' re-issue the EXACT "
                "same call unchanged; on '[bitclaw egress rejected: <id>]' do NOT "
                "retry — ask what they want instead.")
        except Exception:
            logger.exception("codepush gate: error on %s — denying", tool_name)
            return _deny("The release-change gate errored; the call is blocked. "
                         "Say so in the thread and stop.")

    return codepush_gate


def _response_text(tool_response) -> str:
    try:
        if isinstance(tool_response, str):
            return tool_response
        return json.dumps(tool_response, default=str)
    except Exception:
        return ""


async def record_codepush_updates(input, tool_use_id, context):
    """PostToolUse on codepush_list_updates / codepush_get_update: remember
    which update ids belong to which pinned deployment, so a later
    patch_update on that id passes the scope check."""
    tool_name = input.get("tool_name") or ""
    short = tool_name[len(BITRISE_TOOL_PREFIX):]
    tool_input = input.get("tool_input") or {}
    try:
        text = _response_text(input.get("tool_response"))
        # MCP text content is JSON-in-a-string; undo one level of escaping.
        text = text.replace('\\"', '"')
        if short == "codepush_list_updates":
            dep = tool_input.get("deployment_id")
            if dep in pinned_deployments():
                for uid in _UUID_RE.findall(text):
                    if uid != dep:
                        _KNOWN_UPDATES[uid] = dep
        elif short == "codepush_get_update":
            uid = tool_input.get("id")
            m = re.search(r'"deployment_id"\s*:\s*"([0-9a-fA-F-]{36})"', text)
            if uid and m and m.group(1) in pinned_deployments():
                _KNOWN_UPDATES[uid] = m.group(1)
    except Exception:
        logger.debug("record_codepush_updates failed", exc_info=True)
    return {}

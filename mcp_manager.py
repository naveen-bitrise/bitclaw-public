"""Loads external MCP server definitions from .mcp.json and returns them in
the shape `claude-agent-sdk` expects for `ClaudeAgentOptions.mcp_servers`.

Values in "env" or "headers" support $VAR / ${VAR} substitution from
os.environ anywhere in the string. If any referenced var is unset, the whole
server is skipped with a loud log line — silently launching a server with a
literal "$VAR" in its env produces confusing downstream auth errors.

Format mirrors the Claude Code mcpServers spec:
{
  "mcpServers": {
    "my-server": {
      "command": "some-mcp-server",
      "args": ["stdio"],
      "env": { "SOME_TOKEN": "$SOME_TOKEN" }
    },
    "my-http-server": {
      "type": "http",
      "url": "https://...",
      "headers": { "Authorization": "Bearer $TOKEN" }
    }
  }
}
"""
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_MCP_CONFIG_FILE = os.path.join(os.path.dirname(__file__), ".mcp.json")

# Matches $VAR or ${VAR}. Identifier = [A-Za-z_][A-Za-z0-9_]*.
_VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _resolve(value, *, _missing: list[str]):
    """Substitute $VAR / ${VAR} from os.environ; None sentinel on miss."""
    if not isinstance(value, str):
        return value
    if "$" not in value:
        return value

    miss_in_this_value: list[str] = []

    def sub(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        if name in os.environ:
            return os.environ[name]
        miss_in_this_value.append(name)
        return ""

    resolved = _VAR_RE.sub(sub, value)
    if miss_in_this_value:
        _missing.extend(miss_in_this_value)
        return None
    return resolved


def _resolve_dict(d: dict, *, _missing: list[str]) -> dict:
    """Apply _resolve to every value; caller skips the server if any becomes None."""
    return {k: _resolve(v, _missing=_missing) for k, v in d.items()}


def load_external_mcp_servers() -> dict[str, dict]:
    """Return a dict ready for ClaudeAgentOptions.mcp_servers.

    Returns {} when the file is missing or unparseable — the bot still runs,
    just without external tools.
    """
    if not os.path.exists(_MCP_CONFIG_FILE):
        logger.debug(".mcp.json not found — no external MCP servers loaded")
        return {}

    try:
        with open(_MCP_CONFIG_FILE) as f:
            data = json.load(f)
    except Exception:
        logger.exception("Failed to parse .mcp.json")
        return {}

    raw: dict = data.get("mcpServers", {})
    out: dict[str, dict] = {}
    for name, cfg in raw.items():
        resolved = dict(cfg)
        missing: list[str] = []
        if isinstance(cfg.get("env"), dict):
            resolved["env"] = _resolve_dict(cfg["env"], _missing=missing)
        if isinstance(cfg.get("headers"), dict):
            resolved["headers"] = _resolve_dict(cfg["headers"], _missing=missing)
        if missing:
            # Skip the WHOLE server, loudly. Partially resolving (or passing
            # an empty string) would let the server start and then fail auth
            # in ways that are much harder to trace back to a missing var.
            logger.error(
                "Skipping MCP server %r — required env var(s) missing: %s",
                name, ", ".join(sorted(set(missing))),
            )
            continue
        out[name] = resolved
        logger.info("MCP server registered from .mcp.json: %s", name)

    return out

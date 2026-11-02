#!/usr/bin/env bash
# bitclaw orchestrator host — On restore: processes don't survive a stop.
exec >> "$HOME/bitclaw-host.log" 2>&1
echo "[host:on_restore] $(date -u +%FT%TZ)"
cd "$HOME/bitclaw" && git pull --ff-only && "$HOME/.local/bin/uv" sync --frozen --no-dev && cp .mcp.json.example .mcp.json
"$HOME/bin/bitclaw-start.sh"

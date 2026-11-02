#!/usr/bin/env bash
# bitclaw orchestrator host — On create (runs once). Installs toolchains, clones
# bitclaw, builds the two Go MCP servers, installs cloudflared, then starts.
# Template variables: BITCLAW_REPO_URL, BITCLAW_BRANCH. Secrets arrive as
# shared session inputs (see start.sh) and are never exposed in the terminal.
set -uo pipefail
cd "$HOME"
exec >> "$HOME/bitclaw-host.log" 2>&1
echo "[host:on_create] start $(date -u +%FT%TZ)"
mkdir -p "$HOME/bin" "$HOME/bitclaw-data/claude"
export PATH="$HOME/bin:$HOME/.local/bin:$HOME/go/bin:/usr/local/go/bin:$PATH"

# uv (Python + deps from the lockfile)
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
# Go (only to build the MCP servers)
if ! command -v go >/dev/null; then
  arch=$(dpkg --print-architecture)
  curl -fsSL "https://go.dev/dl/$(curl -fsSL 'https://go.dev/VERSION?m=text' | head -1).linux-${arch}.tar.gz" | sudo tar -C /usr/local -xz
fi
# Node (the Agent SDK's Claude Code CLI)
command -v node >/dev/null || { curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash - && sudo apt-get install -y nodejs; }
# cloudflared (named tunnel for GitHub webhooks)
if ! command -v cloudflared >/dev/null; then
  arch=$(dpkg --print-architecture)
  curl -fsSL -o /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}.deb" && sudo dpkg -i /tmp/cloudflared.deb
fi
echo "[host:on_create] uv $(uv --version 2>&1) | go $(go version 2>&1 | cut -d' ' -f3) | node $(node -v 2>&1) | cloudflared $(cloudflared --version 2>&1 | head -1 | cut -d' ' -f3)"

GOBIN="$HOME/bin" go install github.com/github/github-mcp-server/cmd/github-mcp-server@latest
GOBIN="$HOME/bin" go install github.com/bitrise-io/bitrise-mcp-dev-environments@latest
ls -1 "$HOME/bin"

[ -d "$HOME/bitclaw/.git" ] || git clone --branch "${BITCLAW_BRANCH:-shopfront-demo}" "${BITCLAW_REPO_URL:-https://github.com/naveen-bitrise/bitclaw-public.git}" "$HOME/bitclaw"
cd "$HOME/bitclaw" && git pull --ff-only
uv sync --frozen --no-dev
cp -n .mcp.json.example .mcp.json
echo "[host:on_create] bitclaw at $(git rev-parse --short HEAD)"

cat > "$HOME/bin/bitclaw-start.sh" <<'START'
#!/usr/bin/env bash
# (Re)start bitclaw + the tunnel in tmux. Called by On create and On restore
# with the secrets in its env; writes them to ~/bitclaw/.env (0600) so a manual
# `~/bin/bitclaw-start.sh` over SSH restarts with the same config.
set -uo pipefail
export PATH="$HOME/bin:$HOME/.local/bin:$HOME/go/bin:/usr/local/go/bin:$PATH"
cd "$HOME/bitclaw"
if [ -n "${BITCLAW_SLACK_BOT_TOKEN:-}" ]; then
  umask 077
  {
    echo "SLACK_BOT_TOKEN=$BITCLAW_SLACK_BOT_TOKEN"
    echo "SLACK_APP_TOKEN=$BITCLAW_SLACK_APP_TOKEN"
    [ "${BITCLAW_ANTHROPIC_API_KEY:-unset}" != unset ] && echo "ANTHROPIC_API_KEY=$BITCLAW_ANTHROPIC_API_KEY"
    if [ "${BITCLAW_CLAUDE_CODE_OAUTH_TOKEN:-unset}" != unset ]; then echo "CLAUDE_CODE_OAUTH_TOKEN=$BITCLAW_CLAUDE_CODE_OAUTH_TOKEN"
    elif [ "${BITCLAW_ANTHROPIC_API_KEY:-unset}" = unset ] && [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then echo "CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN"; fi
    echo "BITRISE_TOKEN=$BITCLAW_BITRISE_TOKEN"
    echo "GITHUB_TOKEN=$BITCLAW_GITHUB_TOKEN"
    echo "GITHUB_WEBHOOK_SECRET=$BITCLAW_GITHUB_WEBHOOK_SECRET"
    [ "${BITCLAW_CLOUDFLARE_TUNNEL_TOKEN:-unset}" != unset ] && echo "CLOUDFLARE_TUNNEL_TOKEN=$BITCLAW_CLOUDFLARE_TUNNEL_TOKEN"
    # non-secret config (template variables)
    echo "BITRISE_WORKSPACE_ID=${BITRISE_WORKSPACE_ID}"
    echo "RDE_TEMPLATE_ID=${RDE_TEMPLATE_ID}"
    echo "RDE_WARM_POOL_ID=${RDE_WARM_POOL_ID:-}"
    echo "RDE_REPO_DIR=${RDE_REPO_DIR:-}"
    echo "RDE_AGENT_BRIEF=${RDE_AGENT_BRIEF:-}"
    echo "GITHUB_ALLOWED_ORG=${GITHUB_ALLOWED_ORG:-}"
    echo "CLAUDE_MODEL=${CLAUDE_MODEL:-claude-sonnet-5}"
    echo "AUTO_TRIGGER_CHANNELS=${AUTO_TRIGGER_CHANNELS:-}"
    echo "DB_PATH=$HOME/bitclaw-data/bitclaw.db"
    echo "CLAUDE_CONFIG_DIR=$HOME/bitclaw-data/claude"
    echo "WEBHOOK_PORT=8080"
  } > .env
fi
tmux kill-session -t bitclaw 2>/dev/null
tmux new-session -d -s bitclaw "cd $HOME/bitclaw && while true; do .venv/bin/python main.py 2>&1 | tee -a $HOME/bitclaw.log; echo '[bitclaw exited, restarting in 5s]' | tee -a $HOME/bitclaw.log; sleep 5; done"
tmux kill-session -t tunnel 2>/dev/null
if grep -q '^CLOUDFLARE_TUNNEL_TOKEN=.' .env; then
  (umask 077; sed -n 's/^CLOUDFLARE_TUNNEL_TOKEN=//p' .env > "$HOME/bitclaw-data/tunnel-token")
  tmux new-session -d -s tunnel "while true; do cloudflared tunnel --no-autoupdate run --token-file $HOME/bitclaw-data/tunnel-token 2>&1 | tee -a $HOME/tunnel.log; sleep 5; done"
  echo "[start] bitclaw + tunnel started"
else
  echo "[start] bitclaw started (no tunnel token: GitHub webhooks won't arrive)"
fi
START
chmod +x "$HOME/bin/bitclaw-start.sh"
"$HOME/bin/bitclaw-start.sh"
echo "[host:on_create] done $(date -u +%FT%TZ)"

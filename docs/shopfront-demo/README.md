# Shopfront self-healing demo — bitclaw setup

Demo-only notes for running bitclaw against `naveen-bitrise/bitrise-shopfront`
on Bitrise RDE (branch `shopfront-demo`; not for upstream main).

## RDE template `shopfront-ios-agent` (1ef3c02a-8933-4f2f-8e4a-5706bee3187b)

| Hook | What it does |
|---|---|
| On create (pool) | Shopfront clone + `npm ci` + prebuild + pods + Release sim build, background installer, then [`on-create-agent.sh`](on-create-agent.sh): deny-GitHub-API hook, Claude auto mode + `GITHUB_ORG` allow rule, `~/bin/shopfront-verify.sh`, `~/CLAUDE.md` with the inner loop. No secrets. |
| On claim (claimant) | [`on-claim.sh`](on-claim.sh): git identity; with `GIT_SSH_KEY_B64` set, removes the platform's GitHub App credential and installs the deploy key (SSH-only mode); push check. Log: `~/bitclaw-setup.log`. |
| On restore | Re-applies the GitHub App removal in SSH-only mode, then re-runs the background installer. |

Session inputs (scope `on_claim`, optional, not exposed in the terminal):
`GIT_SSH_KEY_B64` (set = SSH-only mode; default `unset` = keep the platform's GitHub App credential), `GIT_USER_EMAIL`, `GIT_USER_NAME`.
Template variable `GITHUB_ORG=naveen-bitrise` (scope `on_create`).

Verified 2026-11-01 with a warm-pool claim using `ai_prompt` +
`map_saved_to_session_inputs` (both now accepted on a personal claim):
claim instant, On claim ran, Claude Code started in tmux in auto mode,
`gh` blocked by the hook. Note: Claude's tmux starts ~3s *before* On claim
finishes, so anything Claude needs at boot must live in On create; the push
key is only needed later, so On claim is fine for it.

## bitclaw `.env` additions

```bash
RDE_TEMPLATE_ID=1ef3c02a-8933-4f2f-8e4a-5706bee3187b
RDE_WARM_POOL_ID=6569df42-53d4-4c00-8fc3-d996c2aa5150   # omit for cold creates
RDE_REPO_DIR=~/shopfront
RDE_AGENT_BRIEF=Follow the "Inner loop" section of ~/CLAUDE.md exactly: typecheck + tests, ~/bin/shopfront-verify.sh on the simulator with screenshots of the changed screen, adversarial self-review of the full diff, a verdict on whether the change is CodePush-eligible (JS-only), then print the EVIDENCE block. Do not push before every step passes. Never publish a CodePush update or add a release label; releases happen in CI after merge.
#AUTO_TRIGGER_CHANNELS=<Sentry alert channel id>
GITHUB_ALLOWED_ORG=naveen-bitrise
```

## GitHub access in the RDE

Two GitHub credentials are involved, and they are separate:
- **Orchestrator (bitclaw):** `GITHUB_TOKEN` in bitclaw's `.env`, used by its
  GitHub MCP to open PRs, read reviews and merge. Never enters the RDE.
- **Coding agent (RDE):** git push only, from a repo **deploy key**.

The platform's personalize step logs git and `gh` in with the claimant's
GitHub App user token (`naveen-bitrise`). That token can also call the GitHub
API as you, so the agent runs in **SSH-only mode**: when `GIT_SSH_KEY_B64` is
set, On claim removes the GitHub App credential (verified 2026-11-01 by hand:
afterwards `gh` is logged out, an HTTPS push fails, public reads still work):
- `~/.config/bitrise/git-credential.env` (token + refresh URL + refresh secret)
- the `credential.https://github.com.helper` git config
- `gh`'s login (hosts.yml + `gh:github.com` keychain item)

### Set up the deploy key (once, on your laptop)

```bash
ssh-keygen -t ed25519 -f bitclaw_deploy -N "" -C "bitclaw-shopfront"
# GitHub → naveen-bitrise/bitrise-shopfront → Settings → Deploy keys → Add:
#   paste bitclaw_deploy.pub, tick "Allow write access"
base64 < bitclaw_deploy | tr -d '\n' | pbcopy
# Bitrise → Remote Dev Environments → Saved inputs → New:
#   key GIT_SSH_KEY_B64, secret, paste; then delete the local key files
```

Then claim a session: `~/bitclaw-setup.log` should show `mode: SSH deploy key
only`, `gh: logged-out` and `push check: OK`.

Not yet verified: whether personalize re-installs the GitHub App credential
on a restore *after* On restore runs (On restore re-scrubs, but ordering is
unconfirmed). Check `gh auth status` after the first restore.

## Quota

The workspace has 12 m2pro vCPUs standalone; the pool (6) plus one claimed
session (6) uses all of it, so a second concurrent task falls back to a cold
create that will be refused. Delete idle claimed sessions.

## Orchestrator host: RDE template `bitclaw-orchestrator` (390fed9d-2c47-49be-ade0-fbac3360cf78)

bitclaw itself runs in its own Linux RDE session (`ubuntu-noble-24.04-bitrise-2025-base`,
`g2.linux.amd-zen4.2c-8g`, 2 vCPU / 8 GB), from source, not Docker:

| Hook | What it does |
|---|---|
| On create | Installs uv, Go, Node, cloudflared if missing; builds `github-mcp-server` and `bitrise-mcp-dev-environments` into `~/bin`; clones `BITCLAW_REPO_URL`@`BITCLAW_BRANCH` to `~/bitclaw`; `uv sync`; writes `~/bin/bitclaw-start.sh` and runs it. Script: [`orchestrator/on-create.sh`](orchestrator/on-create.sh). |
| On restore | `git pull` + `uv sync`, then `bitclaw-start.sh` (so a stop/restore also deploys new commits). |

`bitclaw-start.sh` writes `~/bitclaw/.env` (0600) from the inputs below and
runs bitclaw and the tunnel in tmux sessions `bitclaw` and `tunnel`, each in a
restart loop. Logs: `~/bitclaw.log`, `~/tunnel.log`, `~/bitclaw-host.log`.
State: `~/bitclaw-data/` (SQLite + Claude session files) on the session's
persistent disk.

Secrets are **shared-scope session inputs, not exposed in the terminal**. Save
them as secret saved inputs with these exact keys, then create the session with
saved-input mapping:

| Saved input | Required | Value |
|---|---|---|
| `BITCLAW_SLACK_BOT_TOKEN` | yes | `xoxb-…` from the demo Slack app |
| `BITCLAW_SLACK_APP_TOKEN` | yes | `xapp-…` with `connections:write` |
| `BITCLAW_BITRISE_TOKEN` | yes | Bitrise PAT (the account that claims coding RDEs) |
| `BITCLAW_GITHUB_TOKEN` | yes | fine-grained PAT on `bitrise-shopfront` (see permissions above) |
| `BITCLAW_GITHUB_WEBHOOK_SECRET` | yes | `openssl rand -hex 32`; same value in the GitHub webhook |
| `BITCLAW_ANTHROPIC_API_KEY` | one of these two | Anthropic API key (preferred) |
| `BITCLAW_CLAUDE_CODE_OAUTH_TOKEN` | | Claude Code OAuth token |
| `BITCLAW_CLOUDFLARE_TUNNEL_TOKEN` | later | named-tunnel token from Cloudflare Zero Trust |

Non-secret config lives in template variables (`BITRISE_WORKSPACE_ID`,
`RDE_TEMPLATE_ID`, `RDE_WARM_POOL_ID`, `RDE_REPO_DIR`, `RDE_AGENT_BRIEF`,
`GITHUB_ALLOWED_ORG`, `BITCLAW_REPO_URL`, `BITCLAW_BRANCH`); edit them on the
template.

Create the host with **auto-terminate off** (`auto_terminate_minutes: 0`).
Restart bitclaw by hand over SSH: `~/bin/bitclaw-start.sh`. Watch it:
`tmux attach -t bitclaw`.

Tunnel: in Cloudflare Zero Trust → Networks → Tunnels, create tunnel
`bitclaw`, add public hostname `bitclaw.<domain>` → `http://localhost:8080`,
copy its token into `BITCLAW_CLOUDFLARE_TUNNEL_TOKEN`. GitHub webhook on
`bitrise-shopfront`: `https://bitclaw.<domain>/webhooks/github`, content type
JSON, the webhook secret above, events: pull requests, PR reviews, PR review
comments, issue comments, check runs/suites, workflow runs, statuses.

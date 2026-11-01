# Shopfront self-healing demo — bitclaw setup

Demo-only notes for running bitclaw against `naveen-bitrise/bitrise-shopfront`
on Bitrise RDE (branch `shopfront-demo`; not for upstream main).

## RDE template `shopfront-ios-agent` (1ef3c02a-8933-4f2f-8e4a-5706bee3187b)

| Hook | What it does |
|---|---|
| On create (pool) | Shopfront clone + `npm ci` + prebuild + pods + Release sim build, background installer, then [`on-create-agent.sh`](on-create-agent.sh): deny-GitHub-API hook, Claude auto mode + `GITHUB_ORG` allow rule, `~/bin/shopfront-verify.sh`, `~/CLAUDE.md` with the inner loop. No secrets. |
| On claim (claimant) | [`on-claim.sh`](on-claim.sh): git identity, a push check, and an optional SSH-key override. Log: `~/bitclaw-setup.log`. |
| On restore | Re-runs the background installer. |

Session inputs (scope `on_claim`, optional, not exposed in the terminal):
`GIT_SSH_KEY_B64` (default `unset` = no push), `GIT_USER_EMAIL`, `GIT_USER_NAME`.
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
RDE_AGENT_BRIEF=Follow the "Inner loop" section of ~/CLAUDE.md exactly: typecheck + tests, ~/bin/shopfront-verify.sh on the simulator with screenshots of the changed screen, adversarial self-review of the full diff, CodePush-safety verdict, then print the EVIDENCE block. Do not push before every step passes.
#AUTO_TRIGGER_CHANNELS=<Sentry alert channel id>
GITHUB_ALLOWED_ORG=naveen-bitrise
```

## GitHub access in the RDE

No SSH key is needed. The claimant's connected GitHub App is applied by the
platform's personalize step (before On claim): git gets a credential helper
(`~/.local/bin/bitrise-git-credential`) and `gh` is logged in with a `ghu_`
user token as `naveen-bitrise`. Verified 2026-11-01: `git push --dry-run` of a
new branch to `bitrise-shopfront` succeeds on a fresh pool claim.

Trade-offs versus bitclaw's original git-only design:
- Pushes and commits act as your GitHub account, not a bot.
- The token also grants GitHub API access in the box. The deny hook still
  blocks `gh` and `api.github.com` in the agent's Bash, but the credential is
  reachable, so treat the RDE as able to act on GitHub as you. Fine for a
  demo; use a machine-user SSH key (`GIT_SSH_KEY_B64`) to go back to git-only.

## Quota

The workspace has 12 m2pro vCPUs standalone; the pool (6) plus one claimed
session (6) uses all of it, so a second concurrent task falls back to a cold
create that will be refused. Delete idle claimed sessions.

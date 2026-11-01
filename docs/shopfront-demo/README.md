# Shopfront self-healing demo — bitclaw setup

Demo-only notes for running bitclaw against `naveen-bitrise/bitrise-shopfront`
on Bitrise RDE (branch `shopfront-demo`; not for upstream main).

## RDE template `shopfront-ios-agent` (1ef3c02a-8933-4f2f-8e4a-5706bee3187b)

| Hook | What it does |
|---|---|
| On create (pool) | Shopfront clone + `npm ci` + prebuild + pods + Release sim build, background installer, then [`on-create-agent.sh`](on-create-agent.sh): deny-GitHub-API hook, Claude auto mode + `GITHUB_ORG` allow rule, `~/bin/shopfront-verify.sh`, `~/CLAUDE.md` with the inner loop. No secrets. |
| On claim (claimant) | [`on-claim.sh`](on-claim.sh): git identity, SSH push key, pinned host keys, https→ssh rewrite. Log: `~/bitclaw-setup.log`. |
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

## Still to do by hand

1. Push key: `ssh-keygen -t ed25519 -f bitclaw_ed25519 -N ""`, add the `.pub`
   as a **deploy key with write access** on `bitrise-shopfront`, then save
   `base64 < bitclaw_ed25519 | tr -d '\n'` as secret saved input
   `GIT_SSH_KEY_B64` on the account that owns `BITRISE_TOKEN`. Optional saved
   input `GIT_USER_NAME`.
2. Quota: the workspace has 12 m2pro vCPUs standalone; the pool (6) plus one
   claimed session (6) uses all of it, so a second concurrent task falls back
   to a cold create that will be refused. Delete idle claimed sessions.

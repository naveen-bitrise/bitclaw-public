# Building the coding-agent RDE template

bitclaw launches every coding task into a Bitrise Remote Development
Environment (RDE) created from **one operator-created template**. The template
id goes into `RDE_TEMPLATE_ID`, and a PreToolUse hook refuses any
`bitrise_devenv_create` call that names a different template (or none) — the
orchestrator can never improvise an environment. This document shows how to
build that template.

## Template concepts, briefly

An RDE [template](https://docs.bitrise.io/en/bitrise-rde/configuration/templates)
is a reusable session configuration: a stack (OS image), a machine type, a
working directory, two scripts, and typed inputs.

- **Warmup script** — Bash, runs **once** when the session is first created.
  All the setup below lives here.
- **Startup script** — runs on **every** start, including after a restore;
  must be idempotent. This template leaves it empty.
- **Template variables** — values baked into the template (markable as
  secret, encrypted at rest). Use these for org-wide credentials the
  orchestrator should never see or supply.
- **Session inputs** — filled in per session at create time, optionally
  exposed as environment variables. This is how bitclaw parameterizes each
  box.
- **[Saved inputs](https://docs.bitrise.io/en/bitrise-rde/configuration/saved-inputs)**
  — account-scoped reusable credentials a session input can pull from; the
  value is resolved server-side and never shown in the UI.

Sessions snapshot the template at create time, so editing the template later
never changes already-running sessions.

## What this template needs

**Stack / machine type:** any Linux stack works; pick one that matches your
toolchains. Nothing below is stack-specific.

**Session inputs** (all required, all exposed as env vars — bitclaw's create
call supplies them, typically backed by saved inputs):

| Input | What it is |
|---|---|
| `GIT_SSH_KEY_B64` | Base64 (single line: `base64 < key \| tr -d '\n'`) of an SSH **private key with push access** to the target repos. Use a dedicated machine user, not a personal key. This is deliberately the box's **only** GitHub credential — git push/pull is its entire GitHub access; there is **no API token in the box** (see guardrail 9 in the README). |
| `GIT_USER_EMAIL` | Commit author email. If the machine user has commit-email privacy on, this must be its noreply address (`<id>+<login>@users.noreply.github.com`) or every push fails with GitHub's GH007 error. |
| `GIT_USER_NAME` | Commit author name. |

**Claude Code credential as a saved input:** store a
`CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`) once as a saved input on
the account that creates sessions. The create-time `ai_prompt` field — which
boots Claude Code automatically inside the session — requires a saved Claude
Code credential, and this is what makes every session start with an already
authenticated agent.

**The `ai_prompt` is not part of the template.** bitclaw composes it per
task: which repo to clone, the full task text verbatim, branch rules, and the
never-block guardrail. When a session is created with an `ai_prompt`, Claude
Code boots inside a tmux session on the box; bitclaw talks to the coding
agent by sending keys into that tmux and reads its progress by capturing the
pane.

## The warmup script

```bash
# The platform spawns the warmup with cwd=/root (unreadable to the session
# user), and git dies on repo discovery from an unreadable cwd — even for
# `git config --global`. Anchor to $HOME first; nothing below may depend on
# the cwd.
cd "$HOME"

git config --global user.email "$GIT_USER_EMAIL"
git config --global user.name "$GIT_USER_NAME"

# --- GitHub access: git over SSH only. ---
# The key authenticates as a machine user and grants git push/pull ONLY —
# this box deliberately has no GitHub API credential.
mkdir -p ~/.ssh
chmod 700 ~/.ssh
printf '%s' "$GIT_SSH_KEY_B64" | base64 -d > ~/.ssh/id_ed25519
chmod 600 ~/.ssh/id_ed25519

# GitHub host keys PINNED from https://api.github.com/meta — pinned at
# template-authoring time, NOT ssh-keyscan'd at boot, so a first contact
# can't be man-in-the-middled. Verify/refresh these against /meta when you
# create the template (the ssh-rsa key is omitted here for length; the
# ed25519 key suffices for modern OpenSSH).
cat > ~/.ssh/known_hosts <<'EOF'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
EOF
chmod 644 ~/.ssh/known_hosts

# Route ALL GitHub HTTPS access through SSH — covers https:// clones, private
# Go modules, npm git dependencies, and submodules pointing at github.com.
git config --global url."git@github.com:".insteadOf "https://github.com/"

# Verify the git-side setup actually took — these lines land in the warmup
# log, so a regression is visible there at a glance.
echo "[warmup] git identity: $(git config --global user.name) <$(git config --global user.email)>"
echo "[warmup] url rewrite: $(git config --global --get url.git@github.com:.insteadof || echo MISSING)"
echo "[warmup] jq (required by the deny hook below): $(command -v jq || echo MISSING — install it above this line)"

# Deny hook: reject gh CLI / GitHub API usage from the coding agent's Bash.
# Defense in depth behind the prompt rules — the box has no API credential,
# but unauthenticated public-API reads would otherwise still be possible.
hook="$HOME/.claude/hooks/deny-github-api.sh"
mkdir -p "$(dirname "$hook")"
cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
# Fail CLOSED if jq is missing: an unparseable input must deny, not approve —
# a security hook that can't inspect the command is an open gate.
if ! command -v jq >/dev/null 2>&1; then
  echo "deny-github-api hook: jq is not installed, denying Bash call (install jq in the warmup)" >&2
  exit 2
fi
cmd="$(jq -r '.tool_input.command // empty' 2>/dev/null)"
if printf '%s' "$cmd" | grep -Eq '(^|[;&|(`]|\$\(|/)[[:space:]]*gh([[:space:]]|$)|api\.github\.com'; then
  echo "GitHub API access is not allowed in this environment for security reasons (this includes the gh CLI and api.github.com). Interact with the repository ONLY via git — clone, branch, commit, push. For anything GitHub-side (opening PRs, reading comments, CI status) report it clearly in this terminal and rely on what the orchestrator relays." >&2
  exit 2
fi
exit 0
HOOK
chmod +x "$hook"

# Register the deny hook in Claude Code's settings. If you want fully
# unattended operation, this is also where you'd set your permission mode and
# any auto-approval rules for repos/commands YOUR org trusts — that decision
# is deliberately left to you.
settings="$HOME/.claude/settings.json"
mkdir -p "$(dirname "$settings")"
[ -f "$settings" ] || echo '{}' > "$settings"

tmp="$(mktemp)"
jq --arg hook "$hook" '. + {
  hooks: ((.hooks // {}) + {
    PreToolUse: (((.hooks // {}).PreToolUse // []) + [
      {matcher: "Bash", hooks: [{type: "command", command: $hook}]}
    ])
  })
}' "$settings" > "$tmp"
mv "$tmp" "$settings"

# --- Org-specific setup goes here ---
# e.g. docker login against your registry (store the password as a SECRET
# template variable), private gem/npm package sources, extra toolchains.

cat > ~/CLAUDE.md <<'EOF'
# General
- Git is preconfigured: an SSH key authenticates you to GitHub, and every
  https://github.com/ URL is rewritten to SSH automatically. Just `git clone`
  the repository you are told to work on — either URL form works, private
  repos included.
- Always run `git clone` synchronously (never in the background).
- Never create repository forks. If a push fails with a permission error,
  STOP and report it in the terminal — do not work around it.

# GitHub policy — git only; the orchestrator does the rest
- Your ONLY GitHub interaction is git itself: clone, branch, commit, push.
  You have NO GitHub API access — no gh CLI auth, no tokens — and you must
  NEVER call the GitHub API in any form, not even unauthenticated reads.
  This is a security rule, not a convenience.
- You cannot open PRs, read PR/issue comments or reviews, or check CI. The
  orchestrator watching this session does all of that and relays what
  matters into this terminal.
- Treat whatever the orchestrator relays (a review comment, a CI failure) as
  the complete, authoritative version. Act on exactly that, nothing else. If
  you need more GitHub-side context, ask for it in the terminal — never try
  to fetch it yourself.
- Always work on your own new branch; never commit or push to the default
  branch. When your work is ready: push your branch, then state clearly in
  the terminal that it needs a PR — branch name plus a proposed PR title and
  summary — and wait for relayed instructions. The orchestrator polls this
  session, so a clear terminal message is how you reach it.
- To confirm the repo's default branch without the API:
  `git ls-remote --symref origin HEAD`.

# Toolchains
# (Add your org's toolchain conventions here — pinned language versions,
#  version-manager rules, "always run frontend tests on the host", etc.)
EOF
```

Why the fussy details are there:

- **`cd "$HOME"` first** — the warmup does not run from the session user's
  home directory, and git refuses to run from an unreadable cwd. Everything
  else in the script breaks without this line.
- **Pinned host keys, not `ssh-keyscan`** — scanning at boot trusts whatever
  answers on first contact; pinning the published keys from
  `api.github.com/meta` means a man-in-the-middle at boot time gets rejected.
- **The `url.insteadOf` rewrite** — task briefs, lockfiles, and submodules
  reference `https://github.com/...` URLs; the rewrite makes all of them use
  the SSH key without the agent having to think about it.
- **The deny hook + `~/CLAUDE.md` policy** — the hard boundary is the absence
  of an API credential; the hook and the policy text keep the agent from
  wasting turns discovering that boundary (and block unauthenticated API
  reads, which need no credential at all).

## Create it and wire it up

1. Create the template in the [Bitrise app](https://app.bitrise.io) under
   **Remote Dev Environments → Templates** (or with
   `bitrise-cli rde template create` — see the
   [CLI](https://docs.bitrise.io/en/bitrise-rde/ways-to-use-rde/bitrise-rde-cli)):
   pick a Linux stack and machine type, paste the warmup script, declare the
   three session inputs above (required, exposed as env vars), and leave the
   startup script empty.
2. Save your Claude Code credential and the machine user's SSH key as
   [saved inputs](https://docs.bitrise.io/en/bitrise-rde/configuration/saved-inputs)
   so session creation can reference them without ever handling the values.
3. Test it once by hand: create a session from the template with a trivial
   `ai_prompt` ("clone <some repo> and summarize its README") and watch the
   warmup log for the `[warmup]` echo lines.
4. Grab the template id (`bitrise-cli rde template list`, or from the
   template's URL in the UI) and set it as `RDE_TEMPLATE_ID` in bitclaw's
   `.env`.

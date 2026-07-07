#!/usr/bin/env bash
# bitclaw coding-agent RDE template — warmup script.
# Paste this into the template's "Warmup script" field AS-IS (see
# docs/rde-template.md, step 3) — there are no placeholders to edit. It runs
# ONCE, when a session is created.
#
# Everything org-specific arrives as env vars declared on the template:
#   - session inputs (steps 2-3): GIT_SSH_KEY_B64, GIT_USER_EMAIL, GIT_USER_NAME
#   - template variable (step 3): GITHUB_ORG — your GitHub org name; scopes
#     the coding agent's pre-approved install/build/test trust to repos
#     cloned from that org (unset → that allow rule is skipped)

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

# Claude Code settings: register the deny hook AND turn on auto mode. This
# box is unattended — nobody is at the prompt to answer "Do you want to
# proceed?", so without auto mode the agent stalls forever on its very first
# `git clone`. Autonomy HERE is the design: the safety boundary is the box
# itself (no GitHub API credential, a push-only SSH key, and the deny hook —
# hooks run on every Bash call regardless of permission mode), not
# per-command prompts.
# The allow rule pre-trusts install/build/lint/test commands only inside
# repos cloned from YOUR GitHub org — it is built from the GITHUB_ORG
# template variable (step 3), so there is nothing to edit here. Unset →
# the rule is skipped: the agent still runs, it just gets no pre-approved
# clone-and-build trust.
allow_rule=""
if [ -n "${GITHUB_ORG:-}" ]; then
  echo "[warmup] auto-mode allow rule scoped to GitHub org: $GITHUB_ORG"
  allow_rule="Code in repositories under the $GITHUB_ORG GitHub organization (https://github.com/$GITHUB_ORG/*, including any repo cloned from that org via HTTPS or SSH such as git@github.com:$GITHUB_ORG/*) is trusted by the user. Running install, build, lint, and test commands on a freshly cloned $GITHUB_ORG repo (e.g. npm install, npm ci, npm test, npx jest, yarn, pnpm, bundle install, rake, ruby, go test) is allowed without further prompting, even though the repo was just cloned. This trust applies to all current and future $GITHUB_ORG repositories. It does NOT extend to repos from other GitHub orgs."
else
  echo "[warmup] GITHUB_ORG is not set — skipping the org-scoped auto-mode allow rule"
fi

settings="$HOME/.claude/settings.json"
mkdir -p "$(dirname "$settings")"
[ -f "$settings" ] || echo '{}' > "$settings"

tmp="$(mktemp)"
jq --arg hook "$hook" --arg allow "$allow_rule" '. + {
  permissions: ((.permissions // {}) + {defaultMode: "auto"}),
  skipAutoPermissionPrompt: true,
  autoMode: ((.autoMode // {}) + {
    allow: (["$defaults"] + (if $allow == "" then [] else [$allow] end))
  }),
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

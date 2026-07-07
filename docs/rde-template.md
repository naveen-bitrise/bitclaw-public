# Building the coding-agent RDE template

bitclaw launches every coding task into a Bitrise Remote Development
Environment (RDE) created from **one operator-created template**. The template
id goes into `RDE_TEMPLATE_ID`, and a PreToolUse hook refuses any
`bitrise_devenv_create` call that names a different template (or none) — the
orchestrator can never improvise an environment.

This is a one-time setup, roughly **10 minutes**. Follow the steps in order;
each says exactly what to create and where.

## First, the 30-second security model (read this — it's why the steps look the way they do)

Secrets flow through three layers, and the split is deliberate:

| Layer | Scope | Holds |
|---|---|---|
| **Template** | shared with the whole Bitrise **workspace** | scripts + the *names* of the inputs it needs — never per-user secret values (anyone in the workspace can create a session from a template and read its env, so a secret baked in here would be usable by every member) |
| **Saved inputs** | private to one Bitrise **account** — for bitclaw, the account that owns `BITRISE_TOKEN` | the actual secret values (SSH key, Claude credential) |
| **Create call** | per session | joins the two: bitclaw requests *saved-input auto-mapping*, and the platform fills the template's inputs from the creating account's saved inputs **by exact key match** |

So: the template *declares* `GIT_SSH_KEY_B64` and friends; your bot account's
saved inputs *hold* the values under those exact names; and bitclaw's create
call (a code-injected `map_saved_to_session_inputs` flag — nothing for you to
configure) wires them together at session-creation time. If a key name doesn't
match exactly, creation fails with `missing required input: <KEY>` — that
error always means "fix the saved inputs on the bot account".

## What you'll create

- **4 saved inputs** on the bot's Bitrise account (steps 1–2),
- **1 template** with 3 declared session inputs and a warmup script (step 3),
- then hand-test it (step 4) and wire the id into `.env` (step 5).

## Step 1 — the SSH key (the box's ONLY GitHub credential)

The coding agent talks to GitHub through **git over SSH only** — deliberately
no API token in the box (README guardrail 9). Use a **dedicated machine
user**'s key with push access to the repos the agent will work on.

> **First-test shortcut:** for your very first run, a personal SSH key that
> can push to one scratch repo works fine — switch to a machine user before
> letting it near real repos.

```bash
# A fresh key for the agent (no passphrase — it's used non-interactively):
ssh-keygen -t ed25519 -f bitclaw_ed25519 -N "" -C "bitclaw-coding-agent"
# Add bitclaw_ed25519.pub to the machine user (or as a deploy key with write
# access on the scratch repo), then one-line-base64 the PRIVATE key:
base64 < bitclaw_ed25519 | tr -d '\n' > bitclaw_ed25519.b64
```

Also note the commit author identity you'll use: if the machine user has
commit-email privacy on, the email **must** be its noreply address
(`<id>+<login>@users.noreply.github.com`, shown on the account's email
settings page) or every push fails with GitHub's `GH007` error.

## Step 2 — four saved inputs, on the BOT's account

⚠️ Log in to Bitrise as the **same account whose PAT is in `BITRISE_TOKEN`**.
Saved inputs are account-scoped, and only the *creating* account's saved
inputs are consulted at session-create time — values saved on your personal
account do nothing if the bot authenticates as someone else.

In [the Bitrise app](https://app.bitrise.io): **Remote Dev Environments →
Saved inputs → New**, create these — keys verbatim, they must match the
template's input names character for character:

| Key | Secret | Value |
|---|---|---|
| `GIT_SSH_KEY_B64` | ✓ | contents of `bitclaw_ed25519.b64` (one line) |
| `GIT_USER_EMAIL` |  | the commit author email from step 1 |
| `GIT_USER_NAME` |  | e.g. `bitclaw coding agent` |
| `CLAUDE_CODE_OAUTH_TOKEN` *or* `ANTHROPIC_API_KEY` | ✓ | the Claude credential — in the UI use the dedicated **Claude Code credential** input type. This is what lets `ai_prompt` boot an already-authenticated Claude Code in each session |

Or from a terminal, with [`bitrise-cli`](https://github.com/bitrise-io/bitrise-cli)
logged in **as the bot account**:

```bash
bitrise-cli rde saved-input create --key GIT_SSH_KEY_B64 --value-stdin --secret < bitclaw_ed25519.b64
bitrise-cli rde saved-input create --key GIT_USER_EMAIL --value '<id>+<login>@users.noreply.github.com'
bitrise-cli rde saved-input create --key GIT_USER_NAME --value 'bitclaw coding agent'
echo -n "sk-ant-..." | bitrise-cli rde saved-input create --key ANTHROPIC_API_KEY --value-stdin --secret
```

## Step 3 — the template

**Remote Dev Environments → Templates → New** (or `bitrise-cli rde template
create --file template.json` — run `bitrise-cli rde template create --help`
for the JSON spec):

1. **Stack / machine type:** any Linux stack works; nothing below is
   stack-specific. Pick a mid-size machine type.
2. **Startup script:** leave empty. (It runs on *every* start and must be
   idempotent; everything here is one-time setup, which belongs in warmup.)
3. **Warmup script:** paste the script from the next section.
4. **Session inputs** — declare these three, names exactly matching step 2's
   saved-input keys:

   | Key | Required | Expose as env var | Description |
   |---|---|---|---|
   | `GIT_SSH_KEY_B64` | ✓ | ✓ | base64 of the agent's SSH private key |
   | `GIT_USER_EMAIL` | ✓ | ✓ | commit author email |
   | `GIT_USER_NAME` | ✓ | ✓ | commit author name |

   Declaring them *required with no default* is what makes a misconfigured
   setup fail loudly at create time instead of producing a half-working box.
   (No session input is needed for the Claude credential — the platform picks
   that up from the creating account's saved input directly.)
5. **Template variables:** none. Per the table up top, anything you'd bake in
   here is readable/usable by the whole workspace — reserve it for genuinely
   org-wide, low-sensitivity config (a mirror URL, a registry username),
   never keys or tokens.

Sessions snapshot the template at create time, so editing the template later
never changes already-running sessions.

## The warmup script

Runs once, when a session is created. This is where the box's own guardrails
are installed — the pinned host keys, the git-only policy, and the deny hook
that keeps the coding agent off the GitHub API.

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

# Claude Code settings: register the deny hook AND turn on auto mode. This
# box is unattended — nobody is at the prompt to answer "Do you want to
# proceed?", so without auto mode the agent stalls forever on its very first
# `git clone`. Autonomy HERE is the design: the safety boundary is the box
# itself (no GitHub API credential, a push-only SSH key, and the deny hook —
# hooks run on every Bash call regardless of permission mode), not
# per-command prompts.
# ⚠️ EDIT the allow rule below: replace your-github-org with YOUR GitHub org.
# It pre-trusts install/build/lint/test commands only inside repos cloned
# from that org — trust does not extend to repos from anywhere else.
settings="$HOME/.claude/settings.json"
mkdir -p "$(dirname "$settings")"
[ -f "$settings" ] || echo '{}' > "$settings"

tmp="$(mktemp)"
jq --arg hook "$hook" '. + {
  permissions: ((.permissions // {}) + {defaultMode: "auto"}),
  skipAutoPermissionPrompt: true,
  autoMode: ((.autoMode // {}) + {
    allow: [
      "$defaults",
      "Code in repositories under the your-github-org GitHub organization (https://github.com/your-github-org/*, including any repo cloned from that org via HTTPS or SSH such as git@github.com:your-github-org/*) is trusted by the user. Running install, build, lint, and test commands on a freshly cloned your-github-org repo (e.g. npm install, npm ci, npm test, npx jest, yarn, pnpm, bundle install, rake, ruby, go test) is allowed without further prompting, even though the repo was just cloned. This trust applies to all current and future your-github-org repositories. It does NOT extend to repos from other GitHub orgs."
    ]
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
- **Auto mode, scoped by the allow rule** — an unattended agent that waits at
  a permission prompt is a hung agent (the orchestrator talks to it over
  tmux, it doesn't click Yes for it). Auto mode is safe to grant *here*
  because everything dangerous was already taken away from the box; the
  allow rule then keeps the pre-approved install/build/test trust confined
  to repos from your own org. You can also pin a `model` in the same jq
  object if you want the coding agent on a specific one.

## Step 4 — test it once by hand

Before pointing bitclaw at it, create one session from the template in the
UI: pick the template, let the three inputs fill from your saved inputs
(select them if the UI doesn't preselect), and set a trivial AI prompt like
*"clone <your scratch repo> and summarize its README in the terminal"*. Then:

- watch the warmup log for the three `[warmup]` echo lines (identity, url
  rewrite, jq);
- open the web terminal and check Claude Code is running in tmux
  (`tmux list-sessions`) — and that it is actually working, not sitting at a
  "Do you want to proceed?" approval prompt (if it is, the auto-mode settings
  block didn't apply — see troubleshooting);
- confirm the clone worked over SSH (`ls`, `git -C <repo> remote -v`).

Terminate and delete the session when done.

## Step 5 — wire it up

Grab the template id (from the template's URL in the UI, or
`bitrise-cli rde template list`) and set it in bitclaw's `.env`:

```bash
RDE_TEMPLATE_ID=<the id>
```

Restart the bot — the startup capability summary should now show
`coding (RDE) ✓`. From here bitclaw handles session creation entirely on its
own: every create it issues auto-fills the template's inputs from the bot
account's saved inputs (a code-injected `map_saved_to_session_inputs` flag),
so there is nothing to supply per task.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| create fails: `missing required input: <KEY>` | No saved input named `<KEY>` on the **bot's** account (the `BITRISE_TOKEN` owner). Keys are exact-match — check spelling/case, and that you weren't logged in as a different account in step 2. |
| push fails with `GH007` | The commit author email must be the machine user's noreply address — fix the `GIT_USER_EMAIL` saved input. |
| Claude Code isn't running in tmux | The creating account has no Claude Code credential saved input (step 2, fourth row) — `ai_prompt` needs it to boot an authenticated agent. |
| `git clone` host-key errors | GitHub rotated its host keys — refresh the pinned `known_hosts` block from `https://api.github.com/meta` and update the template. |
| warmup log says `jq: MISSING` | The stack image lacks `jq`; install it in the warmup before the deny-hook block (the hook fails closed without it, denying every Bash call). |
| coding agent sits at a `Do you want to proceed?` prompt | The auto-mode settings never applied — the session was created from an older template version, or the settings jq failed. Update the template's warmup to the current script (sessions snapshot the template, so create a NEW session) and check `cat ~/.claude/settings.json` in the web terminal for `"defaultMode": "auto"`. |

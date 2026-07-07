# Building the coding-agent RDE template

bitclaw launches every coding task into a Bitrise Remote Development
Environment (RDE) created from **one operator-created template**. The template
id goes into `RDE_TEMPLATE_ID`, and a PreToolUse hook refuses any
`bitrise_devenv_create` call that names a different template (or none) — the
orchestrator can never improvise an environment.

This is a one-time setup, roughly **10 minutes**. You'll create:

- **4 saved inputs** on the bot's Bitrise account (steps 1–2),
- **1 template** with 3 declared session inputs and a warmup script (step 3),
- then hand-test it (step 4) and wire the id into `.env` (step 5).

Follow the steps in order; each says exactly what to create and where.

<details>
<summary><b>Why a disposable VM, and the 30-second security model</b> — worth a minute; it's why the steps look the way they do</summary>

Why a disposable VM instead of just giving the orchestrator a shell: the coding
agent needs Bash, file access, and its own GitHub credential to actually work —
capabilities the [security model](../README.md#security-model--lessons-from-production)
deliberately keeps off the bot host. An RDE gives it all of that on a box that
is git-only, has no GitHub API credential, and is thrown away when the task
ends — so the blast radius of a fully capable coding agent is one disposable
sandbox, not the orchestrator's own host.

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

</details>

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
3. **Warmup script:** paste [`warmup.sh`](warmup.sh) as-is — nothing to
   edit; its one org-specific knob is the `GITHUB_ORG` template variable
   (item 5 below). See [the warmup script](#the-warmup-script).
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
5. **Template variables** (each exposed as an env var): this layer is for
   genuinely org-wide, NON-secret config every session should share — per
   the table up top, anything baked in here is usable by the whole
   workspace, so never keys or tokens. One the warmup script reads, plus
   two optional Claude Code knobs:

   | Key | Example value | Effect |
   |---|---|---|
   | `GITHUB_ORG` (recommended) | `acme` | your GitHub org name — the warmup scopes the coding agent's pre-approved install/build/test trust to repos cloned from this org; unset → that allow rule is skipped |
   | `ANTHROPIC_MODEL` (optional) | `opus[1m]` | which model Claude Code runs in the box |
   | `CLAUDE_CODE_EFFORT_LEVEL` (optional) | `xhigh` | its reasoning effort |

Sessions snapshot the template at create time, so editing the template later
never changes already-running sessions.

## The warmup script

Runs once, when a session is created. This is where the box's own guardrails
are installed — the pinned host keys, the git-only policy, and the deny hook
that keeps the coding agent off the GitHub API.

The full script is **[`docs/warmup.sh`](warmup.sh)** — paste it as-is;
there is nothing to edit inside. Everything org-specific reaches it as env
vars declared on the template: the three session inputs, and the
`GITHUB_ORG` template variable (step 3, item 5), which scopes the
pre-approved install/build/test trust to repos cloned from your own org
(left unset, that allow rule is simply skipped and the agent gets no
pre-approved clone-and-build trust).

Why the fussy details in it are there:

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
  allow rule (built from `GITHUB_ORG`) then keeps the pre-approved
  install/build/test trust confined to repos from your own org. (All the
  script's knobs — `GITHUB_ORG`, `ANTHROPIC_MODEL`,
  `CLAUDE_CODE_EFFORT_LEVEL` — are template variables from step 3, visible
  and editable in the template UI; the script itself never needs editing
  for per-org config.)

## Step 4 — test it once by hand

Before pointing bitclaw at it, create one session from the template in the
UI: pick the template, let the three inputs fill from your saved inputs
(select them if the UI doesn't preselect), and set a trivial AI prompt like
*"clone <your scratch repo> and summarize its README in the terminal"*. Then:

- watch the warmup log for the `[warmup]` echo lines (identity, url
  rewrite, jq, the org-scoped allow rule);
- open the web terminal and check Claude Code is running in tmux
  (`tmux list-sessions`) — and that it is actually working, not sitting at a
  "Do you want to proceed?" approval prompt (if it is, the auto-mode settings
  block didn't apply — see troubleshooting);
- confirm the clone worked over SSH (`ls`, `git -C <repo> remote -v`).

Terminate and delete the session when done.

Note bitclaw's own cleanup terminates the RDE when a PR closes — but that path
can fail to run (a crash, a missed webhook). RDE's own `auto_terminate_minutes`
(defaults to 5 days) is an independent safety net so an abandoned session
doesn't run forever regardless; lower it on the template if you want a tighter
bound.

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
| `git clone` host-key errors | GitHub rotated its host keys — refresh the pinned `known_hosts` block in the warmup from `https://api.github.com/meta` and update the template. |
| warmup log says `jq: MISSING` | The stack image lacks `jq`; install it in the warmup before the deny-hook block (the hook fails closed without it, denying every Bash call). |
| coding agent sits at a `Do you want to proceed?` prompt | The auto-mode settings never applied — the session was created from an older template version, or the settings jq failed. Update the template's warmup to the current script (sessions snapshot the template, so create a NEW session) and check `cat ~/.claude/settings.json` in the web terminal for `"defaultMode": "auto"`. |
| agent asks permission before `npm install` / build / test on a fresh clone | The `GITHUB_ORG` template variable is unset (the warmup log says so) or doesn't match the org the repo was cloned from — set it on the template and create a new session. |

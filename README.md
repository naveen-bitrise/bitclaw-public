# bitclaw

A reference implementation of a **Slack-native AI engineering agent**: a Python
async bot that listens to Slack, routes every thread to a persistent Claude
conversation, and delegates all actual coding work to **disposable cloud VMs**
([Bitrise Remote Development Environments](https://docs.bitrise.io/en/bitrise-rde/getting-started/remote-dev-environments-overview))
running Claude Code. GitHub PRs and Jira issues are *anchored* to Slack
threads, so review comments, CI results, and ticket transitions flow back into
the conversation that owns them.

**This project is educational.** It is not a supported product and it is not
maintained. It exists to be read, forked, and adapted: it shows one complete,
working answer to "how do you run an autonomous coding agent against
GitHub/Slack/Jira without it leaking secrets, obeying strangers, or merging
its own PRs". If you want something like this, build your own — tuned to your
workflows, your repos, and your security posture. The most valuable part of
this repo is the [security model](#security-model--lessons-from-production),
where every guardrail comes with the production incident that motivated it.

---

## Architecture

```
      Slack workspace                          GitHub / Jira
          │    ▲                                    │ webhooks
   events │    │ posts, live statuses,              │ (GitHub: HMAC + author
 (Socket  │    │ approve/reject buttons             │  gate; Jira: path secret)
   Mode)  ▼    │                                    ▼
┌─────────────────────────────────────────────────────────────────┐
│ bitclaw — the orchestrator (this repo)                          │
│                                                                 │
│   main.py ──▶ gateway.py ──▶ Session (one per Slack thread)     │
│                  │               │ inbox (asyncio.Queue)        │
│                  ▼               ▼                              │
│               SQLite         agent_loop.py                      │
│        (sessions, anchors,       │ Claude Agent SDK — one       │
│         scheduled messages)      │ persistent conversation      │
│                                  ▼                              │
│        MCP tools only (no Bash, no file access on this host):   │
│        slack · sched (in-process) · github · infra · atlassian  │
└────────────────────────────────┬────────────────────────────────┘
                                 │ mcp__infra__* (Bitrise RDE)
                                 ▼
                  ┌───────────────────────────────┐
                  │ Bitrise RDE — disposable VM   │
                  │ Claude Code running in tmux   │
                  │ git-only GitHub access        │
                  │ (push-scoped SSH key, no API) │
                  └───────────────────────────────┘
```

### Modules

| Module | Role |
|---|---|
| `main.py` | Slack event handlers (mention/reply routing, the stop command, egress approval buttons), startup, restart recovery |
| `gateway.py` | `SlackGateway`: resolves anchors (Slack threads, PRs, Jira issues) to a `Session`; per-session inbox + worker; idle eviction and inactivity auto-close |
| `agent_loop.py` | `AgentLoop`: drives one persistent Claude Agent SDK conversation per session; the system prompt; the child-env credential mask; hook wiring |
| `state.py` | SQLite persistence: `sessions`, `session_anchors`, `scheduled_messages` |
| `webhooks.py` | aiohttp ingress for GitHub/Jira webhooks: signature verification, self-echo filtering, the author-trust gate, event formatting |
| `github_trust.py` | Shared trust primitives: two-signal author trust, repo visibility resolution, PR trust (org → fork → author) |
| `mcp_manager.py` | Loads `.mcp.json`, resolves `$VAR` references from the environment, skips misconfigured servers with a loud error |
| `http_client.py` | One shared `aiohttp` session for direct API calls |
| `tools/slack_tools.py` | In-process MCP server: `post_message`, `update_message`, `register_anchor`, `set_status`, `list_anchors`, `close_session` |
| `tools/scheduling.py` | In-process MCP server: `schedule_message` — durable self-wake-ups delivered into the same session's inbox |
| `tools/hooks.py` | PreToolUse / PostToolUse guardrails: GitHub tool denies, the read redaction filter, the public-repo egress gate, RDE-create validators |
| `tools/egress.py` | `EgressApprovalRegistry`: public-repo GitHub writes parked for human approval, bound to a content hash |

### Request flow

1. A user @mentions the bot (or replies in a thread the bot owns).
2. `main.py` routes the event to the gateway: a new mention claims/creates a
   session for that thread; a reply resolves the thread's anchor to the
   existing session.
3. The message is queued to the session's inbox.
4. A per-session background worker drains the queue and calls
   `agent_loop.run(text)`.
5. The `AgentLoop` drives the Claude Agent SDK: the model calls MCP tools
   (post to Slack, create an RDE, open a PR, ...) until the turn ends.
6. The SDK conversation id is persisted, so the next message — minutes or
   weeks later, before or after a process restart — resumes the same
   conversation with full history.

### How a coding task flows end to end

1. A teammate @mentions the bot in a channel: *"@bitclaw fix the flaky retry
   test in acme/payments-service, ticket PAY-123."* A session is created for
   the thread.
2. The agent creates an RDE from the **one pinned template**
   (`RDE_TEMPLATE_ID`, see [docs/rde-template.md](docs/rde-template.md)) with
   a **self-contained brief** as the `ai_prompt`: which repo to clone, the
   full task text verbatim, branch rules (own new branch, never the default
   branch), and the "never block waiting for input" guardrail. It registers
   an `rde_session` anchor and posts a progress line to the thread.
3. Inside the VM, Claude Code clones the repo over SSH, does the work on its
   own branch, pushes, and reports in its terminal. It has **no GitHub API
   access** — pushing and printing is all it can do.
4. The orchestrator's scheduled self-wake-up fires, it captures the tmux
   pane, sees the pushed branch, and — as the **only GitHub API actor** —
   opens the PR itself, then registers a `github_pr` anchor.
5. Review comments and CI conclusions arrive as webhooks, pass the
   author-trust gate, and are delivered into the owning thread. The agent
   relays vetted review text **verbatim** into the coding agent's tmux; the
   coding agent fixes and pushes again.
6. A human replies *"merge it"*. On a public repo the actual merge call is
   parked and an Approve/Reject button appears in the thread; a click lets
   the exact same call through.
7. The PR-closed webhook carries a cleanup cue: the agent terminates and
   deletes the RDE, posts a final summary, and calls `close_session`.
8. Weeks later someone replies *"thanks — one more thing..."* — the done
   session reopens with a cue and the same conversation continues, history
   intact.

---

## Sessions, anchors, and out-of-turn work

**One Slack thread = one session = one Claude conversation.** The session row
in SQLite stores the SDK conversation id for `--resume`; the conversation
history itself lives in the Claude Code CLI's own session files. In-memory
sessions are evicted after an idle TTL (reversible — the next event rebuilds
them); sessions nobody closes are durably auto-closed after
`SESSION_AUTOCLOSE_IDLE_DAYS`.

**Anchors** bind external entities to the owning session:

| Anchor kind | Created when | Routes |
|---|---|---|
| `slack_thread` | first post into the thread | thread replies |
| `github_pr` | the agent opens a PR (`register_anchor`) | PR comments, reviews, CI, close/merge |
| `jira_issue` | ticket assigned to the bot, or attached mid-session | comments, transitions |
| `rde_session` | right after `bitrise_devenv_create` | cleanup bookkeeping |

**Out-of-turn work** — things that happen while no user message is in flight —
uses three patterns, and deliberately **no in-process subagents** (an early
background-agent runtime — second SDK client, duplicated prompt, parallel MCP
fan-out — was replaced by "deliver a message into the same session's inbox
later": one conversation with full history beats a second agent with none).

1. **Webhooks** for anchored entities: GitHub/Jira events become messages in
   the owning session's inbox. If a webhook covers a wait, the agent must not
   also poll for it.
2. **The RDE coding agent** for any work that must outlive a turn: the
   orchestrator talks to it by sending keys into its tmux session and polls
   the pane for output.
3. **`schedule_message`** self-wake-ups for waits nothing pushes (e.g. the
   RDE poll): the scheduler delivers the message into the *same* session's
   inbox after a delay, so the agent's full history applies when it wakes.
   Scheduled messages are persisted and recovered across restarts.

---

## Quickstart

### Prerequisites

- **Python 3.11+** and [uv](https://docs.astral.sh/uv/).
- **A Slack app** created from the included
  [`slack-app-manifest.json`](slack-app-manifest.json) (Socket Mode and
  Interactivity are enabled in the manifest — Interactivity is what makes the
  egress Approve/Reject buttons deliver). Generate an app-level token with
  `connections:write`, install the app, and grab the bot token.
- **An Anthropic API key** (or be logged into the `claude` CLI on the host).
- **A GitHub personal access token** with `repo` + `read:org` scope, and the
  name of the GitHub org whose members you trust (`GITHUB_ALLOWED_ORG`).
- **Webhook exposure**: GitHub (and optionally Jira) must reach
  `http://<host>:8080` — for local development a tunnel such as `ngrok http
  8080` works. Point an org or repo webhook at `/webhooks/github`
  (content type JSON, an HMAC secret you generate, events: pull requests,
  issue comments, PR reviews and review comments, check runs, workflow runs).
- **A Bitrise workspace with RDE access** (RDE is in beta — [contact
  Bitrise](https://docs.bitrise.io/en/bitrise-rde/getting-started/remote-dev-environments-overview)
  if your workspace doesn't have it), a [personal access
  token](https://app.bitrise.io/me/account/security), and the coding-agent
  template — build it once following
  [docs/rde-template.md](docs/rde-template.md) and put its id in
  `RDE_TEMPLATE_ID`.
- **Optional: Jira** — an API token for the hosted Atlassian MCP server, plus
  a webhook (assignment bootstrap, comments, transitions).

### Setup

```bash
git clone <this repo> && cd bitclaw

cp .env.example .env          # fill in tokens, org, template id
cp .mcp.json.example .mcp.json

# The two stdio MCP binaries must be on PATH:
go install github.com/github/github-mcp-server/cmd/github-mcp-server@latest
# ...and the Bitrise RDE server — build/install per its README:
#   https://github.com/bitrise-io/bitrise-mcp-dev-environments

uv sync
uv run python main.py
```

Invite the bot to a channel and @mention it. First useful test:
*"@bitclaw what open PRs does &lt;your-github-org&gt;/&lt;some-repo&gt; have?"* —
then graduate to a real coding task once the RDE template is wired.

**Running without Jira:** delete the `atlassian` block from your `.mcp.json`
and leave the `JIRA_*` vars unset. (If you keep the block with `JIRA_AUTH`
unset, the loader logs a loud `Skipping MCP server atlassian` error at
startup — deliberate for genuine misconfigurations, harmless here.)

**Live status indicator:** the "watching CI…" status above the thread uses
Slack's `assistant.threads.setStatus`. Depending on your workspace and app
configuration you may need the app's *Agents & AI Apps* feature toggled on
for statuses to render; every status call is best-effort, so the bot works
fine (minus the indicator) either way.

### Running in Docker

The included `Dockerfile` pre-builds the two Go MCP binaries and runs the bot
as a non-root user. Two things live on disk and must be volume-mounted to
survive container restarts: the SQLite database and the Claude Code CLI's
conversation files (what `--resume` replays).

```bash
docker build -t bitclaw .
docker run --env-file .env \
  -e DB_PATH=/data/bitclaw.db \
  -v bitclaw-data:/data \
  -v bitclaw-claude:/home/app/.claude \
  -v ./.mcp.json:/app/.mcp.json:ro \
  -p 8080:8080 bitclaw
```

(`.env` and `.mcp.json` are covered by `.dockerignore` so they are never
baked into the image — pass them at run time as above.)

---

## Configuration reference

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | yes | — | Bot OAuth token (`xoxb-`) |
| `SLACK_APP_TOKEN` | yes | — | App-level Socket Mode token (`xapp-`, `connections:write`) |
| `ANTHROPIC_API_KEY` | yes* | — | Claude API key (*or be logged into the `claude` CLI) |
| `CLAUDE_MODEL` | no | `claude-sonnet-5` | Model passed to the Agent SDK |
| `GITHUB_TOKEN` | yes | — | PAT for the GitHub MCP server and the code-side trust/visibility checks (`repo` + `read:org`) |
| `GITHUB_WEBHOOK_SECRET` | for webhooks | — | HMAC-SHA256 shared secret for `/webhooks/github` |
| `GITHUB_ALLOWED_ORG` | yes | — | The trusted GitHub org. **No default** — unset means every author-trust check fails closed |
| `GITHUB_BOT_LOGIN` | no | `GET /user` | The bot's GitHub login, for webhook self-echo filtering |
| `BITRISE_TOKEN` | for RDE | — | Bitrise personal access token |
| `BITRISE_WORKSPACE_ID` | for RDE | — | Workspace holding the RDE template |
| `RDE_TEMPLATE_ID` | for RDE | — | The ONE template bitclaw may launch (see [docs/rde-template.md](docs/rde-template.md)); unset → `devenv_create` is denied by a hook |
| `JIRA_AUTH` | no | — | `Basic base64(email:api_token)` header for the hosted Atlassian MCP |
| `JIRA_HOST` | no | — | e.g. `example.atlassian.net`; passed to the agent as the Jira cloudId |
| `JIRA_BOT_ACCOUNT_ID` | for Jira webhooks | — | Atlassian accountId of the bot (assignment detection + self-echo suppression) |
| `JIRA_WEBHOOK_SECRET` | for Jira webhooks | — | Secret embedded in the webhook URL path |
| `JIRA_DEFAULT_CHANNEL` | for Jira webhooks | — | Slack channel where ticket-assignment threads start |
| `JIRA_TERMINAL_STATUSES` | no | `Done,Closed,Cancelled,Won't Do` | Statuses that trigger the cleanup cue |
| `WEBHOOK_HOST` / `WEBHOOK_PORT` | no | `0.0.0.0` / `8080` | Webhook listener bind |
| `DB_PATH` | no | `./bitclaw.db` | SQLite file |
| `LOG_LEVEL` | no | `INFO` | Root log level |
| `SESSION_IDLE_TTL_SECS` | no | `3600` | Idle TTL before in-memory eviction (reversible) |
| `SESSION_EVICTION_CHECK_SECS` | no | `300` | Eviction sweeper tick |
| `SESSION_AUTOCLOSE_IDLE_DAYS` | no | `7` | Days without any inbound event before a durable auto-close |
| `SESSION_AUTOCLOSE_CHECK_SECS` | no | `3600` | Auto-close sweeper tick |
| `AGENT_TURN_IDLE_TIMEOUT_SECS` | no | `600` | Per-turn watchdog on the SDK stream; a wedged tool call gets the client torn down and rebuilt with `--resume` |
| `STOP_GRACE_SECS` | no | `15` | After a stop's graceful interrupt, how long to wait before escalating to eviction |

---

## Security model — lessons from production

Everything in this section is enforced **in code** — hooks, gates, and
filters — not in the system prompt. That distinction is the point: a prompt
rule is visible to and steerable by whatever text reaches the model, so on
any injection-reachable surface a prompt rule eventually becomes a hook. Each
guardrail below comes out of running the internal version of this bot in
production — incidents, near-misses, and review findings alike, retold here
without the identifying details.

### 1. No built-in tools on the orchestrator, and a masked child environment

An early version left the agent a "narrow door": a few read-only shell
binaries, opened as a convenience for reading oversized tool results off the
host disk. The door had no path restriction — a prompt-injected agent could
just as well read the bot's own `.env` or `/proc/self/environ` and exfiltrate
every credential through channels it already controls (a Slack post, a PR
comment).

The fix is wholesale: `tools=[]` — the agent has **no Bash and no file access
on the bot host**; its entire tool surface is MCP. And as defense in depth,
the CLI child process (and every stdio MCP server it spawns) inherits the
bot's environment with credentials blanked: an explicit floor list, plus a
pattern rule that blanks **any** variable whose name contains a
`TOKEN`/`SECRET`/`KEY`/`AUTH`/`PASSWORD`/... segment — so a secret added next
year is masked by default. MCP servers still get their own credentials via
the resolved `env`/`headers` blocks of `.mcp.json`.

Related: the webhook server disables HTTP access logging entirely. The Jira
webhook authenticates via a secret in the URL path, and a default access
logger prints every request line — secret included — to stdout and from there
into whatever collects your logs. Never log a secret-bearing route's path.

*Lesson: deny-by-default beats curated allowlists for secrets, and every
capability opened "for convenience" is an exfiltration path until proven
otherwise.*

### 2. The webhook author-trust gate (two signals, fail closed)

On a public repo, **anyone** can comment on a PR the bot opened — and comment
text used to be routed straight into the owning Slack session as agent input.
An outsider could literally instruct the agent.

The gate on comment-bearing webhook events went through a full evolution arc
worth retelling. v1 checked org membership but *failed open* on inconclusive
API results — a misconfigured token silently disabled the trust boundary — so
it was flipped: proceed **only** on confirmed membership, drop loudly
otherwise. Then a real false positive: an invited machine collaborator (not
an org member) was dropped, so the gate switched to the `author_association`
field on the signed payload — until, days later, a *private* org member's PR
approval was dropped, because GitHub reports private members as
`CONTRIBUTOR`/`NONE`. The final design accepts that **neither signal alone is
correct**: trust `OWNER`/`MEMBER`/`COLLABORATOR` from the payload with no API
call, and fall back to the org-membership API (authoritative regardless of
visibility) when the association doesn't prove trust. GitHub App bots are
exempt — installing an App is itself an org-admin trust decision. And the
"unset org disables the gate" kill switch was deleted: the gate is always on;
an unset `GITHUB_ALLOWED_ORG` just means nothing can be confirmed and
everything fails closed.

*Lesson: trust gates fail closed, kill switches on trust boundaries get
deleted, and real-world GitHub identity is messy enough to need two
complementary signals.*

### 3. The GitHub read redaction filter (shape-agnostic, fail closed)

The webhook gate stops untrusted comments from *triggering* the agent — but
the agent can *pull* the exact same text itself by reading PR comments,
reviews, and issues over the GitHub MCP. The first mitigation was a prompt
rule, adopted knowing it was injection-exposed (the attacker's own text is
what instructs the model). It was replaced by a `PostToolUse` hook that
parses every `mcp__github__*` result, walks it **recursively**, and redacts
the `body`/`title` of every comment/review/PR/issue whose author the same
two-signal trust gate can't confirm — replacing prose with a
`[bitclaw: withheld ...]` marker *before the model sees it*. It keys on
result **content** (any node with an author and free text), not tool names,
so a server-side tool rename or consolidation can't route around it. A
raising hook would fail *open* at the SDK layer, so any error in the walk
withholds the whole result instead.

Hardening this filter produced the sharpest lessons in the repo:

- **Hook inputs are plain dicts.** The earliest security hooks read
  `input.tool_name` as an attribute; the SDK passes a TypedDict, both hooks
  crashed on every call, and the SDK *logged the error and proceeded* — the
  guards never fired, and tests passed because their fixtures never matched
  the real contract. A broken security hook is an open gate; only real-shape
  tests catch it.
- **Matchers are anchored full regexes.** `HookMatcher(matcher="mcp__github__")`
  looks like a prefix match. It is not — it's a full-match regex that matches
  *no* tool name, so the filter was registered and never invoked (an
  untrusted comment reached the model unredacted before this was found).
  `"mcp__github__.*"` is the correct form.
- **Test with the shapes the runtime actually delivers.** The filter once
  no-op'd on raw-array results because every fixture had been pre-wrapped in
  an assumed envelope shape. And the SDK's *bundled* CLI is not necessarily
  the one on your PATH — verify hook behavior against the real binary.
- **Teach the model which layer already decided.** The opposite failure also
  happened: the agent re-rejected a comment the code had already vetted,
  because the synthesized webhook message carries no `author_association`.
  The prompt now says: if it was delivered, it was vetted — don't re-derive
  trust from missing fields.

### 4. Orchestrator, not a coder: hard denies on GitHub mutation and code reads

The orchestrator kept drifting into doing the work itself — using GitHub API
tools to write files directly and reading repository source to plan changes,
instead of delegating to the coding agent that has the full working tree.
Mutation tools (`create_or_update_file`, `push_files`, `create_branch`, ...)
were denied first. Code *reads* (`get_file_contents`, `search_code`, diffs,
commits) stayed a prompt rule — "planning vs orchestration is contextual" —
until they were escalated to a hard deny: a prompt rule is injection-exposed,
the tools have no orchestration use, and — decisively — **denying them covers
the redaction filter's blind spot**: file contents, commit messages, and
diffs carry no attributable author, so the only way to keep that unvetted
free text from the model is to never fetch it. A method-aware deny on the
consolidated `pull_request_read` tool (`get_diff`/`get_files`/`get_commits`
denied, metadata methods kept) keeps a server-side tool consolidation from
reopening the surface. One deliberate exception: `fork_repository` survives
as an explicit-human-request escape hatch, precisely *because* the git-only
coding agent physically cannot fork.

*Lesson: soft rules on injection-reachable surfaces eventually harden, and
you deny by capability, not by today's tool names.*

### 5. Public-repo egress approval (content-hash-bound, fail-closed catch-all)

The design admission behind this gate: **input filtering can never be 100%**.
A clever injection, or untrusted text quoted inside a trusted comment, can
always slip through — so the guarantee moves to the *output* side, where the
blast radius is. On a public repo, a GitHub **write** is the one channel an
outside attacker can read: it's where a slipped injection becomes a harmful
public action, and where an accidental paste of internal detail escapes.

So every public-repo GitHub write is held for a human: the hook parks the
call in a registry, posts an Approve/Reject button to the thread, and denies
the tool call. A click enqueues an approval cue; the agent re-issues the
call; the hook matches it against the parked request **by a content hash of
the exact call** — so "human approved text A, agent posts text B" is
impossible (B parks as a new request). The buttons are the only path to
approval: the agent has no self-approve tool, and an outsider on the PR has
no Slack access. Merges ride the same rail — even after a human says "merge
it", the merge call on a public repo surfaces a final confirmation button,
which also kills the "agent merges the moment CI is green" class of bug.

Review of the first version found the gap that defines the final design: a
six-name allow-list over a **wildcard** tool surface meant `create_issue`,
formal review submission, CI-control tools, and any *future or renamed* write
bypassed the gate entirely — an injected agent could exfiltrate via
`create_issue` and never touch it. The fix is a fail-closed catch-all: on a
public repo, a call is allowed only if it's a known-safe read or one of the
approvable writes; **everything else is denied**. Repo visibility is resolved
in code (never trusted from the model), unknown visibility is treated as
public, and — because a raising hook fails open at the SDK layer — every
error path denies.

*Lesson: an allow-list gate over an open-ended surface must default-deny the
remainder, or it's decoration.*

### 6. External / fork PR refusal (no human override)

An anchor is what makes a PR "owned": its webhook events route into the
session and the agent treats it as its own work. To keep a prompt-injected
agent — or a well-meaning human — from pulling an outside contribution into
an agent that holds internal context and privileged tool access,
`register_anchor(kind="github_pr", ...)` is refused **in code** for any PR
that fails three checks, outermost first:

1. **Org-owned repo.** This must run first, because `author_association` is
   computed relative to the PR's *own* repo — without this gate, an
   outsider's PR in their own repo reports `OWNER` and sails through the
   author fast path.
2. **Not a fork.** A fork is an outside contribution *no matter who opened
   it* — a trusted member's fork still isn't the bot's work — so cross-repo
   PRs are refused regardless of author.
3. **Trusted author**, via the same two-signal gate as everything else.

There is deliberately **no human override** — stricter than the egress gate,
which is human-approvable. The egress gate governs what the bot *publishes*;
this gate refuses *taking on outside work* at all. If asked, the bot tells
the human to handle that PR themselves. One subtlety earned its own fix:
transient "couldn't verify" verdicts (an API blip) are never cached, so a
one-off failure can't pin a legitimate member's PR as "external" for a whole
cache TTL. Definitive refusals are cached; "I don't know" never is.

*Lesson: each check's soundness can depend on another check running first —
and never cache an "I couldn't tell".*

### 7. The out-of-band stop command

For a while there was literally no way to stop a runaway multi-turn task:
the session worker drains its inbox sequentially, so a "stop" message queued
*behind the very turn it was meant to kill*. The fix makes stop a control
command, not agent input. A human reply whose whole message is a stop word
(`stop`, `/stop`, `abort`) is detected in `main.py` and routed around the
inbox entirely: flush the queue, cancel the session's scheduled wake-ups,
send the SDK's graceful interrupt, and escalate to teardown after a grace
period if the turn won't die. (It's a message-text trigger, not a Slack slash
command, because slash-command payloads carry no `thread_ts` — a real `/stop`
could never tell which thread to stop.)

Two races found in review shaped the final design. First, the grace wait is
keyed on a **turn-generation snapshot**, not a "turn in progress" boolean — a
boolean can't distinguish the dying turn from the user's own follow-up
starting right after, and would escalate into fresh work the user just asked
for. Second, the scheduler MCP runs in-process, which means **the dying turn
can re-arm its own wake-up mid-stop** — so stops latch the scheduler shut and
run a second cancel sweep after the turn actually ends.

*Lesson: stopping an agent is an adversarial problem against the agent's own
machinery.*

### 8. The done-session gate and the reopen cue

Closing a session is soft — the row stays, anchors keep resolving. That
meant every post-merge PR comment, every CI run on a merged PR, every
transition on a finished ticket cold-started a full SDK + MCP subprocess
stack and spent a paid model turn posting into a thread the agent had
declared finished. The gate: **automated events on a done session drop;
a returning human reopens it.**

The reopen produced the best "LLM state machine" bug in the project: the
reopened agent *refused to work*. Its resumed conversation ends with its own
`close_session` call, and its instructions say a closed thread is severed —
so it answered "my session is already closed" even though every tool worked.
Flipping a row in the database isn't enough; the transition has to be
**narrated to the model**: every reopen path prepends a
`[bitclaw session reopened: ...]` cue the system prompt teaches is the one
thing that un-severs a thread. The cue carries a re-close contract (a bare
"thanks!" gets a brief reply and an immediate re-close, not a session pinned
open for days), and bot-authored thread replies are attributed and routed as
non-human, so an integration can neither spoof the cue nor reopen a done
session.

*Lesson: state machines shared between code and an LLM need the transition
announced in-band, not just written to the DB.*

### 9. The coding agent is git-only

The webhook gate and read filter vet everything the *orchestrator* sees. But
the first coding-agent template shipped with a GitHub API credential — and a
coding agent that can read the PR thread itself can re-read the **unfiltered**
conversation and act on a stranger's instructions: a complete bypass of every
upstream filter.

So the RDE gets **no GitHub API credential at all**: a push-scoped SSH key
for a machine user, a shell hook that rejects `gh` and `api.github.com` usage
(defense in depth — even unauthenticated public API reads are off-limits),
and an HTTPS→SSH URL rewrite so every clone path works with the key. The
orchestrator is the sole GitHub API actor: it opens the PRs, reads the
comments, and relays vetted review text verbatim into the tmux. What it
relays is *all* the coding agent gets — which is exactly the point: a trust
filter is only authoritative if every reader downstream is forced through it.
See [docs/rde-template.md](docs/rde-template.md) for the full template.

### More lessons, briefly

- **Deny the harness's phantom tools.** The bundled Claude Code CLI
  advertises scheduling/monitoring tools that *return success but are no-ops*
  in this harness — a monitoring task once "promised to poll every 60s" and
  silently died. `permission_mode` does not deny them; an explicit disallow
  list does.
- **Every write channel is an input channel back to yourself.** The bot's own
  Jira/GitHub comments came back as webhooks into its own session — one paid
  turn per echo. Self-authored events drop at the ingress, with a carve-out
  so CI events (whose "sender" is the CI integration) still pass.
- **Verify what payloads actually carry before filtering on them.** An
  identity-based filter is only as good as the identity fields the payloads
  actually populate — verify them against real traffic before relying on the
  filter, and prefer a fix that removes the triggering condition over one
  that tries to attribute it: one ticket = one session, never self-assign
  sub-tickets.
- **Audit what an MCP server can *return*, not just what it can do.** A
  security review flagged that documented-optional `include_secrets` flags on
  the RDE read tools return live credential values. Two deny hooks close
  them, with truthiness handling so a model stringifying `"false"` can't trip
  the gate.
- **Enforce invariants at the boundary, not in prose.** An RDE created
  without an `ai_prompt` boots with no coding agent inside, making every
  later tmux message a silent no-op. A deny hook requires the prompt and pins
  the template id.
- **Separate WHO may act from WHETHER the act is possible.** The agent once
  declared a PR "ready to merge" off green CI while required reviewers were
  pending. Merging is a human's act, always; readiness comes from GitHub's
  computed `mergeable_state` string (`clean` is the only "ready"; `blocked`
  is ambiguous on merge-queue repos; `unknown` means still computing), never
  from reconstructed signals like approvals + green checks.
- **Public artifacts are world-readable forever.** A coding agent that
  doesn't know a repo is public will happily paste ticket ids, internal
  hostnames, and coworker names into commit messages, PR text, and even
  source comments — and git history never forgets. The prompt carries
  visibility-matched hygiene rules; unknown visibility = public.

---

## What we deliberately left out

The internal version this was distilled from runs with more machinery. Each
item below is a real production need — and deliberate noise for a reference
implementation:

- **Postgres and real migrations** — this repo uses SQLite and a module-level
  write lock; multi-process deployments need a real database and advisory
  locks (comments in `state.py` mark the spots).
- **A push channel from the coding agent** — this repo polls the RDE tmux;
  production adds a way for the coding agent to say "pushed / blocked / done"
  in real time instead of waiting for the next poll.
- **Oversized-tool-result spillover reading** — huge MCP results spill to
  disk; production adds a path-validated tool to read them back.
- **Restore memos** — `--resume` replays the *original* system prompt, so
  prompt-rule changes need a one-shot in-band memo to reach in-flight sessions.
- **Bounded recovery concurrency** — restart recovery here fires past-due
  scheduled messages sequentially; production bounds the stampede.
- **Log shipping** — structured JSON logs with severity fields for a log
  platform; this repo logs plain text to stdout.

---

## Further reading

**Bitrise RDE** (the coding-agent substrate):

- [RDE overview](https://docs.bitrise.io/en/bitrise-rde/getting-started/remote-dev-environments-overview) — what it is, beta access
- [Key concepts](https://docs.bitrise.io/en/bitrise-rde/getting-started/key-concepts) — sessions, lifecycle, templates, saved inputs
- [The RDE MCP server](https://docs.bitrise.io/en/bitrise-rde/ways-to-use-rde/bitrise-rde-mcp-server) — hosted vs local binary, auth
- [Templates](https://docs.bitrise.io/en/bitrise-rde/configuration/templates) — warmup/startup scripts, variables vs session inputs
- [Saved inputs](https://docs.bitrise.io/en/bitrise-rde/configuration/saved-inputs) — account-scoped credentials, Claude Code credential

**The two stdio MCP servers:**

- [github/github-mcp-server](https://github.com/github/github-mcp-server)
- [bitrise-io/bitrise-mcp-dev-environments](https://github.com/bitrise-io/bitrise-mcp-dev-environments)

**Claude Agent SDK:**

- [Agent SDK documentation](https://docs.claude.com/en/api/agent-sdk/overview)
- [anthropics/claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python)

---

## Disclaimer

This repository is a snapshot published for educational purposes. It is not a
Bitrise product, carries no support or maintenance commitment, and will not
track upstream API changes. The guardrails here were built against real
incidents but are a *reference*, not a guarantee — if you deploy an
autonomous agent against your own repos and tools, do your own threat
modeling and security review first.

## License

[MIT](LICENSE) — copyright 2026 Bitrise Ltd.

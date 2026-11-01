# --- bitclaw coding-agent setup (secret-free, machine-wide) -------------------
# Appended to the Shopfront On create hook. Everything here is safe to bake
# into a shared warm-pool machine: no keys, no tokens. Per-user credentials are
# installed by the On claim hook (on-claim.sh).
(
set -uo pipefail
cd "$HOME"
exec >> "$HOME/bitclaw-setup.log" 2>&1
echo "[bitclaw:on_create] start $(date -u +%FT%TZ)"
command -v jq >/dev/null 2>&1 || { echo "[bitclaw:on_create] jq missing, trying brew"; brew install jq || true; }
echo "[bitclaw:on_create] jq: $(command -v jq || echo MISSING)"

# Deny hook: no gh CLI / GitHub API from the coding agent's Bash (git only).
hook="$HOME/.claude/hooks/deny-github-api.sh"
mkdir -p "$(dirname "$hook")"
cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
if ! command -v jq >/dev/null 2>&1; then
  echo "deny-github-api hook: jq is not installed, denying Bash call" >&2
  exit 2
fi
cmd="$(jq -r '.tool_input.command // empty' 2>/dev/null)"
if printf '%s' "$cmd" | grep -Eq '(^|[;&|(`]|\$\(|/)[[:space:]]*gh([[:space:]]|$)|api\.github\.com'; then
  echo "GitHub API access is not allowed here (gh CLI and api.github.com included). Use git only — clone, branch, commit, push — and report anything GitHub-side in this terminal for the orchestrator." >&2
  exit 2
fi
exit 0
HOOK
chmod +x "$hook"

# Claude Code settings: deny hook + auto mode (the box is unattended).
allow_rule=""
if [ -n "${GITHUB_ORG:-}" ]; then
  allow_rule="Code in repositories under the $GITHUB_ORG GitHub account/organization (https://github.com/$GITHUB_ORG/*, git@github.com:$GITHUB_ORG/*) is trusted by the user, including the pre-cloned ~/shopfront. Running install, build, lint, test, xcodebuild, xcrun simctl and npx expo commands on these repos, and the helper ~/bin/shopfront-verify.sh, is allowed without further prompting. This trust does NOT extend to repos from other GitHub accounts."
fi
settings="$HOME/.claude/settings.json"
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
}' "$settings" > "$tmp" && mv "$tmp" "$settings"
echo "[bitclaw:on_create] settings: $(jq -c '.permissions' "$settings")"

# Inner-loop helper: rebuild the Release simulator app from the working tree,
# reinstall + relaunch it on the booted simulator, and save a screenshot.
mkdir -p "$HOME/bin" "$HOME/evidence"
cat > "$HOME/bin/shopfront-verify.sh" <<'VERIFY'
#!/usr/bin/env bash
# Usage: shopfront-verify.sh [screenshot-name]   (run from anywhere)
set -euo pipefail
DIR="$HOME/shopfront"
BUNDLE_ID="${SHOPFRONT_BUNDLE_ID:-io.bitrise.naveen.shopfront}"
NAME="${1:-verify-$(date +%H%M%S)}"
LOG="$HOME/shopfront-verify.log"
cd "$DIR"
echo "[verify] xcodebuild (Release, simulator) at $(git rev-parse --short HEAD) + working tree"
if ! xcodebuild -workspace ios/Shopfront.xcworkspace -scheme Shopfront -configuration Release \
  -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' -derivedDataPath "$DIR/build" \
  ARCHS=arm64 ONLY_ACTIVE_ARCH=YES CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="" \
  build > "$LOG" 2>&1; then
  grep -E "error:" "$LOG" | sort -u | head -20; echo "[verify] BUILD FAILED (full log: $LOG)"; exit 1
fi
APP=$(find "$DIR/build/Build/Products" -type d -name 'Shopfront.app' -path '*iphonesimulator*' | head -1)
UDID=$(xcrun simctl list devices booted | sed -nE 's/.*\(([0-9A-F-]{36})\) \(Booted\).*/\1/p' | head -1)
[ -n "$UDID" ] || { echo "[verify] no booted simulator"; exit 1; }
xcrun simctl terminate "$UDID" "$BUNDLE_ID" >/dev/null 2>&1 || true
xcrun simctl install "$UDID" "$APP"
xcrun simctl launch "$UDID" "$BUNDLE_ID"
sleep 6
mkdir -p "$HOME/evidence"
xcrun simctl io "$UDID" screenshot "$HOME/evidence/$NAME.png" >/dev/null
echo "[verify] OK: installed + launched on $UDID; screenshot ~/evidence/$NAME.png"
VERIFY
chmod +x "$HOME/bin/shopfront-verify.sh"

cat > "$HOME/CLAUDE.md" <<'EOF'
# General
- Git is preconfigured: an SSH key authenticates you to GitHub and every
  https://github.com/ URL is rewritten to SSH. Never create forks. If a push
  fails with a permission error, STOP and report it in the terminal.
- The Shopfront app repo is ALREADY cloned at ~/shopfront on main with
  node_modules, Pods and a Release simulator build in place. Work there; do
  not re-clone it.

# GitHub policy — git only; the orchestrator does the rest
- Your ONLY GitHub interaction is git: clone, branch, commit, push. No gh CLI,
  no tokens, never the GitHub API (not even unauthenticated reads).
- You cannot open PRs, read comments/reviews or check CI. The orchestrator
  relays what matters into this terminal; treat it as complete and act on
  exactly that.
- Always work on your own new branch; never commit or push to main. When the
  work is ready: push the branch, then state clearly in the terminal that it
  needs a PR — branch name, proposed PR title and summary, and your
  verification evidence — and wait.

# Inner loop — REQUIRED before you push (Shopfront, Expo 58 / RN 0.88)
1. Implement the change with tests where the repo has them.
2. Run the same checks as CI's `pr` workflow: the typecheck and test scripts
   in package.json (e.g. `npm run typecheck`, `npm test -- --watchAll=false`).
   Fix every failure.
3. Run it on the simulator: `~/bin/shopfront-verify.sh <short-name>` rebuilds
   the Release app from your working tree, reinstalls and relaunches it on
   the booted iPhone simulator and saves ~/evidence/<short-name>.png. Then
   drive the app to the changed screen (`xcrun simctl openurl booted <deeplink>`
   if one exists) and take another screenshot with
   `xcrun simctl io booted screenshot ~/evidence/<name>.png`. Read the
   screenshots and confirm the change is visible and nothing else regressed.
4. Adversarial self-review: re-read your full diff (`git diff main...`) as a
   skeptical reviewer — edge cases, empty/loading/error states, accessibility
   labels, typing, CodePush compatibility (JS-only change? any native module
   or app.json change makes it NOT CodePush-able — say so explicitly).
5. Print an "EVIDENCE" block in the terminal: commands run with pass/fail,
   test counts, screenshot paths, the self-review findings and what you fixed,
   and whether the change is CodePush-safe.
EOF
echo "[bitclaw:on_create] done $(date -u +%FT%TZ)"
)

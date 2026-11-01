#!/usr/bin/env bash
# bitclaw coding-agent On claim hook — per-user credentials only.
# Runs in the claimant's private scope (after a warm-pool claim, or right after
# On create for a direct create). Inputs (scope on_claim, NOT exposed in the
# terminal): GIT_SSH_KEY_B64, GIT_USER_EMAIL, GIT_USER_NAME. The Claude
# credential comes from the claimant's CLAUDE_CODE_OAUTH_TOKEN saved input.
#
# GitHub access: the platform's personalize step (runs before this hook) logs
# git and gh in with the claimant's GitHub App user token. That token can also
# call the GitHub API as the claimant. When GIT_SSH_KEY_B64 is set (a repo
# deploy key), this hook REMOVES that credential and leaves git-over-SSH as the
# box's only GitHub access. Without a key, the GitHub App credential stays.
cd "$HOME"
exec >> "$HOME/bitclaw-setup.log" 2>&1
echo "[bitclaw:on_claim] start $(date -u +%FT%TZ)"

git config --global user.email "${GIT_USER_EMAIL:-bitclaw@users.noreply.github.com}"
git config --global user.name "${GIT_USER_NAME:-bitclaw coding agent}"

if [ -n "${GIT_SSH_KEY_B64:-}" ] && [ "$GIT_SSH_KEY_B64" != "unset" ]; then
  # 1. Remove the platform's GitHub App credential (token, refresh secret,
  #    git helper, gh login). Re-applied by On restore via the marker file.
  cat > "$HOME/bin/bitclaw-scrub-github-app.sh" <<'SCRUB'
#!/usr/bin/env bash
rm -f "$HOME/.config/bitrise/git-credential.env"
git config --global --unset-all credential.https://github.com.helper 2>/dev/null
git config --global --remove-section credential.https://github.com 2>/dev/null
for u in $(gh auth status --hostname github.com 2>&1 | sed -nE 's/.*account ([^ ]+).*/\1/p'); do
  gh auth logout --hostname github.com --user "$u" >/dev/null 2>&1
done
security delete-generic-password -s gh:github.com >/dev/null 2>&1
echo "[bitclaw:scrub] GitHub App credential removed; gh: $(gh auth status >/dev/null 2>&1 && echo LOGGED-IN || echo logged-out)"
SCRUB
  chmod +x "$HOME/bin/bitclaw-scrub-github-app.sh"
  mkdir -p "$HOME/.config/bitclaw" && touch "$HOME/.config/bitclaw/ssh-only"
  "$HOME/bin/bitclaw-scrub-github-app.sh"

  # 2. Install the deploy key; GitHub host keys pinned from api.github.com/meta.
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  printf '%s' "$GIT_SSH_KEY_B64" | base64 -d > ~/.ssh/id_ed25519
  chmod 600 ~/.ssh/id_ed25519
  cat > ~/.ssh/known_hosts <<'EOF'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
EOF
  chmod 644 ~/.ssh/known_hosts
  git config --global url."git@github.com:".insteadOf "https://github.com/"
  echo "[bitclaw:on_claim] mode: SSH deploy key only"
else
  echo "[bitclaw:on_claim] mode: platform GitHub App credential (no SSH key provided)"
fi
echo "[bitclaw:on_claim] push check: $(cd ~/shopfront 2>/dev/null && GIT_TERMINAL_PROMPT=0 timeout 30 git push --dry-run origin HEAD:refs/heads/bitclaw-push-check >/dev/null 2>&1 && echo OK || echo FAILED)"
echo "[bitclaw:on_claim] git identity: $(git config --global user.name) <$(git config --global user.email)>"
echo "[bitclaw:on_claim] done $(date -u +%FT%TZ)"

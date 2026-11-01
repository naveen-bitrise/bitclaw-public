#!/usr/bin/env bash
# bitclaw coding-agent On claim hook — per-user credentials only.
# Runs in the claimant's private scope (after a warm-pool claim, or right after
# On create for a direct create). Inputs (scope on_claim, NOT exposed in the
# terminal): GIT_SSH_KEY_B64, GIT_USER_EMAIL, GIT_USER_NAME. The Claude
# credential comes from the claimant's CLAUDE_CODE_OAUTH_TOKEN saved input, and
# git/gh are logged in to GitHub by the platform's personalize step (GitHub App);
# GIT_SSH_KEY_B64 is only an optional override.
cd "$HOME"
exec >> "$HOME/bitclaw-setup.log" 2>&1
echo "[bitclaw:on_claim] start $(date -u +%FT%TZ)"

git config --global user.email "${GIT_USER_EMAIL:-bitclaw@users.noreply.github.com}"
git config --global user.name "${GIT_USER_NAME:-bitclaw coding agent}"

if [ -n "${GIT_SSH_KEY_B64:-}" ] && [ "$GIT_SSH_KEY_B64" != "unset" ]; then
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  printf '%s' "$GIT_SSH_KEY_B64" | base64 -d > ~/.ssh/id_ed25519
  chmod 600 ~/.ssh/id_ed25519
  # GitHub host keys pinned from https://api.github.com/meta (no keyscan).
  cat > ~/.ssh/known_hosts <<'EOF'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
EOF
  chmod 644 ~/.ssh/known_hosts
  git config --global url."git@github.com:".insteadOf "https://github.com/"
  echo "[bitclaw:on_claim] ssh key installed"
else
  echo "[bitclaw:on_claim] no SSH key: git uses the platform's GitHub credential (GitHub App)"
fi
echo "[bitclaw:on_claim] push check: $(cd ~/shopfront 2>/dev/null && GIT_TERMINAL_PROMPT=0 timeout 30 git push --dry-run origin HEAD:refs/heads/bitclaw-push-check >/dev/null 2>&1 && echo OK || echo FAILED)"
echo "[bitclaw:on_claim] git identity: $(git config --global user.name) <$(git config --global user.email)>"
echo "[bitclaw:on_claim] done $(date -u +%FT%TZ)"

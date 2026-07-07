# syntax=docker/dockerfile:1.6

# -----------------------------------------------------------------------------
# Stage 1: pre-build the two Go MCP server binaries.
# This is a correctness fix, not an optimization: when the Claude Code CLI
# spawns an stdio MCP child, the child's env drops GOPATH/GOCACHE, so a
# `go run …@latest` server can't reuse any Go cache and re-downloads +
# recompiles from scratch (~80s) — while the MCP stdio init timeout is ~30s,
# leaving the server "still connecting" forever. Pre-building drops MCP
# startup to <100ms and removes runtime network dependence. @latest is fine:
# the image tag is the pin (resolved once, at build time).
# -----------------------------------------------------------------------------
# The stage always runs on the build host's own arch ($BUILDPLATFORM) and
# cross-compiles for the target — Go does this natively, so the arm64 leg of a
# multi-arch build doesn't pay the QEMU-emulation tax for the compile.
FROM --platform=$BUILDPLATFORM golang:1.26-bookworm AS go-mcp-builder
ARG TARGETOS TARGETARCH
ENV CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH
# `go install` refuses GOBIN for cross-compiled targets: binaries land in
# $GOPATH/bin natively but $GOPATH/bin/${GOOS}_${GOARCH} when crossing —
# hence the two-path copy.
RUN mkdir -p /out \
    && go install github.com/github/github-mcp-server/cmd/github-mcp-server@latest \
    && go install github.com/bitrise-io/bitrise-mcp-dev-environments@latest \
    && { cp "$(go env GOPATH)/bin/${GOOS}_${GOARCH}/"* /out/ 2>/dev/null \
         || cp "$(go env GOPATH)/bin/github-mcp-server" \
               "$(go env GOPATH)/bin/bitrise-mcp-dev-environments" /out/; }

# -----------------------------------------------------------------------------
# Stage 2: runtime. No Go toolchain — just the compiled binaries.
# -----------------------------------------------------------------------------
FROM python:3.13-slim

# Node.js is required by the Claude Agent SDK's bundled Claude Code CLI.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# uv (package manager) + a non-root user. The user needs a REAL home dir:
# without one HOME resolves to "/" and every tool wanting $HOME/.cache fails.
# uv:latest is fine for the same reason as the Go @latest above: the published
# image tag is the pin (resolved once, at build time), and dependency versions
# themselves come from the committed lockfile, not from uv.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
RUN useradd --create-home --shell /bin/sh app
ENV HOME=/home/app PATH="/home/app/bin:${PATH}" PYTHONUNBUFFERED=1

# The MCP binaries land on PATH — .mcp.json references them by bare name.
COPY --from=go-mcp-builder --chown=app:app /out/ /home/app/bin/

# Pre-create the two persistent-state directories WITH app ownership: a named
# volume mounted at a path that doesn't exist in the image is initialized
# root-owned, and the non-root app user then can't create the SQLite file
# ("unable to open database file") or the Claude CLI's session files. When
# the path exists in the image, the fresh volume inherits its ownership.
RUN mkdir -p /data /home/app/.claude && chown app:app /data /home/app/.claude
ENV DB_PATH=/data/bitclaw.db
# By default the Claude Code CLI keeps its config at ~/.claude.json — OUTSIDE
# the ~/.claude directory the quickstart volume-mounts — so every container
# restart would lose it and the CLI would warn "configuration file not found"
# (its periodic backups land inside ~/.claude/backups, which survives, making
# the warning louder). Point the whole config at the persisted directory.
ENV CLAUDE_CONFIG_DIR=/home/app/.claude

WORKDIR /app
RUN chown app:app /app
USER app

# Install deps first for layer caching (from the committed lockfile, so the
# image gets the exact pinned versions), then copy the app itself. No second
# sync after the copy: the project is never installed as a package
# ([tool.uv] package = false) — modules import straight from /app.
COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY --chown=app:app . .
# Bake the example MCP config in as the default, so the prebuilt image runs
# with zero mounted files: it contains only $VAR references (no secrets),
# resolved from the environment at runtime — servers whose vars are unset are
# simply disabled (that's how the capability tiers work). Mount your own file
# at /app/.mcp.json to override. (.mcp.json itself is dockerignored, so a
# local secret-bearing copy can never end up in the image.)
RUN cp .mcp.json.example .mcp.json

EXPOSE 8080
# Run the synced venv's python directly: `uv run` would re-resolve the
# environment at container start (installing dev deps, hitting the network);
# the image's venv is already built above.
CMD ["/app/.venv/bin/python", "main.py"]

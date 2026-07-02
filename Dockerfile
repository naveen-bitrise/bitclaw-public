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
FROM golang:1.26-bookworm AS go-mcp-builder
ENV GOBIN=/out CGO_ENABLED=0
RUN mkdir -p /out \
    && go install github.com/github/github-mcp-server/cmd/github-mcp-server@latest \
    && go install github.com/bitrise-io/bitrise-mcp-dev-environments@latest

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
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
RUN useradd --create-home --shell /bin/sh app
ENV HOME=/home/app PATH="/home/app/bin:${PATH}" PYTHONUNBUFFERED=1

# The MCP binaries land on PATH — .mcp.json references them by bare name.
COPY --from=go-mcp-builder --chown=app:app /out/ /home/app/bin/

WORKDIR /app
RUN chown app:app /app
USER app

# Install deps first for layer caching (from the committed lockfile, so the
# image gets the exact pinned versions), then the app itself.
COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY --chown=app:app . .
RUN uv sync --frozen --no-dev

EXPOSE 8080
CMD ["uv", "run", "python", "main.py"]

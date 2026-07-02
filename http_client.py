"""Process-wide shared aiohttp.ClientSession.

aiohttp's connection pooling (keep-alive) only works across calls that share a
ClientSession, so building a session per request re-does the TCP+TLS handshake
every time. The callers here hit the same few hosts repeatedly — the GitHub
org-membership and repo-visibility checks sit on the webhook ACK and tool-hook
paths — so per-call sessions would be pure churn.

Auth differs per caller, so the session carries no default headers; pass them
per request (aiohttp merges request headers over session headers anyway).
Created lazily on first use, which must happen inside the running event loop;
``main()`` closes it on shutdown via ``close_http_session()``.
"""
import aiohttp

_session: aiohttp.ClientSession | None = None


# Session-level default timeout — a backstop for callers that forget to pass
# one per request (aiohttp's own default is 5 minutes). Callers can still
# override it with a per-request timeout.
_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)


def get_http_session() -> aiohttp.ClientSession:
    """Return the shared session, creating it on first use (or after close)."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=_DEFAULT_TIMEOUT)
    return _session


async def close_http_session() -> None:
    """Close the shared session. Idempotent; call from shutdown's finally."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None

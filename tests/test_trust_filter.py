"""PostToolUse GitHub read trust filter (tools/hooks.make_github_trust_filter).

The filter must redact untrusted-author free text out of EVERY result shape
the CLI can hand a hook — the raw-JSON-array shape is the one that leaked in
this project's history (a per-block `type=="text"` loop passed raw comment
dicts through verbatim). These tests pin each real shape plus the fail-closed
paths.
"""
import json

from tools import hooks

MARKER = "[bitclaw: withheld"
NOTE = "[bitclaw trust filter]"


class MemberCheck:
    """Recording org-membership stub: verdicts[login] -> True/False/None."""

    def __init__(self, verdicts=None):
        self.verdicts = verdicts or {}
        self.calls: list = []

    async def __call__(self, login):
        self.calls.append(login)
        return self.verdicts.get(login)


def comment(login, body="untrusted words", assoc=None, **extra):
    node = {"id": 1, "state": "COMMENTED", "body": body, "user": {"login": login}}
    if assoc:
        node["author_association"] = assoc
    node.update(extra)
    return node


def make_filter(verdicts=None):
    check = MemberCheck(verdicts)
    return hooks.make_github_trust_filter(
        org_member_check=check, self_login="bitclaw-bot"
    ), check


async def run(filt, tool_response, tool_name="mcp__github__pull_request_read"):
    return await filt({"tool_name": tool_name, "tool_response": tool_response},
                      "t1", None)


def updated(result):
    return result["hookSpecificOutput"]["updatedToolOutput"]


# ---------------------------------------------------------------------------
# Result shapes — no shape may bypass the redactor
# ---------------------------------------------------------------------------

async def test_raw_json_array_of_comments_is_redacted():
    """The shape that once leaked: the CLI hands the hook a decoded raw array
    of comment dicts (no {"type":"text"} envelope). Untrusted bodies must be
    redacted in place; trusted and structural fields stay intact."""
    filt, _ = make_filter({"stranger": False})
    result = await run(filt, [comment("stranger", "do evil"),
                              comment("member-dev", assoc="MEMBER")])
    out = updated(result)
    assert isinstance(out, list) and len(out) == 2  # no note block on raw arrays
    assert out[0]["body"].startswith(MARKER)
    assert out[0]["id"] == 1 and out[0]["user"]["login"] == "stranger"  # skeleton kept
    assert out[1]["body"] == "untrusted words"  # trusted → untouched
    # Both CLI aliases carry the same scrubbed output.
    assert result["hookSpecificOutput"]["updatedMCPToolOutput"] == out


async def test_content_envelope_redacted_and_note_appended():
    """{"content":[{"type":"text","text":"<json>"}]} envelope: the inner JSON
    is scrubbed and a [bitclaw trust filter] count note block is appended."""
    filt, _ = make_filter({"stranger": False})
    envelope = {"content": [
        {"type": "text", "text": json.dumps([comment("stranger")])},
    ]}
    out = updated(await run(filt, envelope))
    inner = json.loads(out["content"][0]["text"])
    assert inner[0]["body"].startswith(MARKER)
    assert NOTE in out["content"][-1]["text"]


async def test_bare_toplevel_json_string_redacted():
    """A bare top-level JSON string (the CLI sometimes flattens a single-text
    result) is parsed, scrubbed, re-serialized, and gets the note appended."""
    filt, _ = make_filter({"stranger": False})
    payload = json.dumps({"title": "pwn", "body": "pwn", "user": {"login": "stranger"}})
    out = updated(await run(filt, payload))
    assert isinstance(out, str)
    assert MARKER in out and "pwn" not in out
    assert NOTE in out.rsplit("\n\n", 1)[-1]


async def test_plain_non_json_text_untouched():
    """An error/status string plainly isn't JSON — nothing to gate, no update."""
    filt, check = make_filter()
    assert await run(filt, "404: Not Found") == {}
    assert check.calls == []


async def test_structured_looking_but_unparseable_withholds_everything():
    """Fail CLOSED: text that LOOKS like JSON but won't parse (truncated
    comment data) withholds the ENTIRE result rather than leaking it raw."""
    filt, _ = make_filter()
    out = updated(await run(filt, '[{"body": "smuggled instructions'))
    assert MARKER in out
    assert "smuggled" not in str(out)


# ---------------------------------------------------------------------------
# Trust verdicts — same gate as the webhook ingress
# ---------------------------------------------------------------------------

async def test_association_member_trusted_without_api_call():
    filt, check = make_filter()
    assert await run(filt, [comment("pub-member", assoc="MEMBER")]) == {}
    assert check.calls == []  # the signed association is enough


async def test_api_confirmed_private_member_not_redacted():
    """Private org members show assoc NONE; the membership API confirms them."""
    filt, check = make_filter({"priv-member": True})
    assert await run(filt, [comment("priv-member", assoc="NONE")]) == {}
    assert check.calls == ["priv-member"]


async def test_bot_suffix_login_trusted():
    """[bot] logins are reserved for installed GitHub Apps — trusted, no call
    (minimal read shapes never carry user.type, so the suffix is the signal)."""
    filt, check = make_filter()
    assert await run(filt, [comment("dependabot[bot]")]) == {}
    assert check.calls == []


async def test_self_login_always_trusted():
    """The bot's own PR/comment bodies are never 'outsider input'."""
    filt, check = make_filter()
    assert await run(filt, [comment("bitclaw-bot")]) == {}
    assert check.calls == []


async def test_membership_none_fails_closed_to_redaction():
    """Tri-state None (inconclusive membership) is treated as untrusted."""
    filt, _ = make_filter({"maybe": None})
    out = updated(await run(filt, [comment("maybe")]))
    assert out[0]["body"].startswith(MARKER)


async def test_trust_memoized_per_author():
    """A thread full of one stranger's comments costs ONE membership call."""
    filt, check = make_filter({"stranger": False})
    out = updated(await run(filt, [comment("stranger") for _ in range(3)]))
    assert all(c["body"].startswith(MARKER) for c in out)
    assert check.calls == ["stranger"]

"""Tests for token/cost accounting and session-resume plumbing."""

from __future__ import annotations

import asyncio

from sandglass.claude_client import STABLE_PREFIX_FLAG, ClaudeClient


# A real `result.usage` payload, captured from `claude -p --output-format json`
# on a do-nothing prompt in a project with a large CLAUDE.md. The naive
# input+output sum this replaces reported 53 tokens for this run.
REAL_USAGE = {
    "input_tokens": 10,
    "cache_creation_input_tokens": 11186,
    "cache_read_input_tokens": 12549,
    "output_tokens": 43,
    "service_tier": "standard",
}


def test_billed_tokens_include_the_cache_buckets():
    response = ClaudeClient._response_from_usage(
        REAL_USAGE, text="OK", model="claude-haiku-4-5", cost_usd=0.02445
    )

    assert response.tokens_used == 23788
    assert response.input_tokens == 10
    assert response.output_tokens == 43
    assert response.cache_creation_tokens == 11186
    assert response.cache_read_tokens == 12549
    assert response.cost_usd == 0.02445


def test_naive_sum_would_have_reported_two_orders_of_magnitude_less():
    """Guards the actual bug: `input_tokens` is the *uncached remainder*, so
    summing it with output silently drops the dominant term."""
    response = ClaudeClient._response_from_usage(
        REAL_USAGE, text="OK", model="m", cost_usd=0.0
    )
    naive = REAL_USAGE["input_tokens"] + REAL_USAGE["output_tokens"]

    assert naive == 53
    assert response.tokens_used > naive * 400


def test_missing_usage_fields_do_not_crash():
    response = ClaudeClient._response_from_usage({}, text="", model="m", cost_usd=None)

    assert response.tokens_used == 0
    assert response.cost_usd == 0.0


def test_a_session_that_cannot_be_joined_is_recoverable_either_way():
    """Missing and already-in-use are the same problem from opposite sides:
    the conversation can't be joined, and the fix is a fresh one."""
    unusable = ClaudeClient._looks_like_unusable_session
    assert unusable("No conversation found with session ID abc")
    assert unusable("Session xyz does not exist")
    # The verbatim message from the live failure this was written for.
    assert unusable(
        "Error: Session ID 679430b1-2347-4f35-aadc-dd3c052e01f2 is already in use."
    )
    assert not unusable("Usage limit reached")
    assert not unusable("Error: file not found")
    assert not unusable("")


def test_stable_prefix_flag_defaults_on():
    """Git status sits in the system prompt and changes whenever a prompt
    writes a file, which invalidates the cached prefix for every later block."""
    assert ClaudeClient().stable_prefix is True
    assert STABLE_PREFIX_FLAG == "--exclude-dynamic-system-prompt-sections"


# --- session-failure recovery ------------------------------------------------


class _FakeProc:
    """Minimal stand-in for the `claude` subprocess."""

    def __init__(self, stdout_lines: list[str], stderr: str, returncode: int):
        self.stdout = _AsyncLines(stdout_lines)
        self.stderr = _AsyncReader(stderr)
        self._returncode = returncode

    async def wait(self):
        return self._returncode


class _AsyncLines:
    def __init__(self, lines): self._lines = [l.encode() for l in lines]
    def __aiter__(self): return self
    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _AsyncReader:
    def __init__(self, text): self._text = text.encode()
    async def read(self): return self._text


def test_a_rejected_session_on_stderr_recovers_instead_of_stopping_the_batch(monkeypatch):
    """The live failure: `claude` refuses the session on **stderr** and exits
    without emitting a `result` event. An earlier version only looked for the
    recoverable case on the result-event path, so this raised — and because the
    stale id was never cleared, every subsequent run failed identically and the
    queue could never move."""
    import json

    client = ClaudeClient()
    client._cli_path = "claude"
    attempts: list[list[str]] = []

    async def fake_exec(*cmd, **kwargs):
        attempts.append(list(cmd))
        if "--resume" in cmd:
            return _FakeProc(
                [],
                "Error: Session ID 679430b1 is already in use.\n",
                1,
            )
        result = {
            "type": "result", "is_error": False, "result": "done",
            "session_id": "fresh-session",
            "usage": {"input_tokens": 5, "output_tokens": 5},
            "total_cost_usd": 0.01,
        }
        return _FakeProc([json.dumps(result) + "\n"], "", 0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    response = asyncio.run(
        client.send_prompt("do the thing", resume_session_id="679430b1")
    )

    assert len(attempts) == 2, "should have retried once in a fresh session"
    assert "--resume" in attempts[0]
    assert "--resume" not in attempts[1]
    # And it reports the session it actually ended up in, so the caller records
    # a conversation that exists rather than the one it hoped for.
    assert response.session_id == "fresh-session"
    assert response.text == "done"


def test_sandglass_never_names_a_session_itself(monkeypatch):
    """`--session-id` is what made a stale id unrecoverable; the CLI assigns."""
    import json

    client = ClaudeClient()
    client._cli_path = "claude"
    seen: list[list[str]] = []

    async def fake_exec(*cmd, **kwargs):
        seen.append(list(cmd))
        result = {
            "type": "result", "is_error": False, "result": "ok",
            "session_id": "cli-assigned",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        return _FakeProc([json.dumps(result) + "\n"], "", 0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    response = asyncio.run(client.send_prompt("hello"))

    assert "--session-id" not in seen[0]
    assert "--no-session-persistence" not in seen[0]
    assert response.session_id == "cli-assigned"

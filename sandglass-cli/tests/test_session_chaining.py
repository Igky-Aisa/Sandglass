"""Tests for how prompts map onto Claude Code sessions.

The point of chaining is that a queue run unattended should not pay for a cold
start per block. These tests pin the behaviour that makes that safe: blocks
still read as separate tasks, an interrupted drain rejoins its session, a
finished drain doesn't, and a block can opt out.
"""

from __future__ import annotations

import asyncio

import pytest

from sandglass.claude_client import QuotaExceededError
from sandglass.execution_engine import (
    CHAIN_TURN_SEPARATOR,
    SESSION_MODE_CHAIN,
    SESSION_MODE_ISOLATE,
    SESSION_MODE_PROMPT,
    ExecutionEngine,
)
from sandglass.models import Response
from sandglass.queue_manager import QueueManager
from sandglass.storage import StorageService


@pytest.fixture
def qm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return QueueManager(StorageService(base_path=str(tmp_path / ".sandglass")))


class _RecordingClient:
    """Stands in for `claude -p`, including who names the session.

    Mirrors the real contract deliberately: the *client* mints a session id and
    reports it back, and the caller resumes whatever it was told. Sandglass
    never names a session up front — an earlier version did, and an id it had
    hoped for but never confirmed deadlocked the queue permanently.
    """

    model = "claude-opus-4-8"
    effort = None

    def __init__(self, fail_on: str | None = None, unusable_session: bool = False):
        self.calls: list[dict] = []
        self.fail_on = fail_on
        self.unusable_session = unusable_session
        self._minted = 0

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, model=None, effort=None,
                          resume_session_id=None, persist_session=True):
        self.calls.append(
            {
                "text": text,
                "resume_session_id": resume_session_id,
                "persist_session": persist_session,
            }
        )
        if self.fail_on and self.fail_on in text:
            raise QuotaExceededError("usage limit reached")
        if resume_session_id and self.unusable_session:
            # What the real client does on an unusable session: fall back to a
            # fresh one rather than failing the prompt.
            resume_session_id = None
        if resume_session_id:
            session_id = resume_session_id
        elif persist_session:
            self._minted += 1
            session_id = f"sess-{self._minted}"
        else:
            session_id = None
        return Response(
            prompt_id="", text="done", tokens_used=10, model=self.model,
            session_id=session_id,
        )


def _engine(qm, client, **kwargs):
    return ExecutionEngine(qm, client, **kwargs)


# --- chain mode (the default) ------------------------------------------------


def test_chain_is_the_default(qm):
    assert _engine(qm, _RecordingClient()).session_mode == SESSION_MODE_CHAIN


def test_only_the_first_block_pays_for_a_cold_start(qm):
    for text in ("first task", "second task", "third task"):
        qm.add_prompt(text)
    client = _RecordingClient()

    asyncio.run(_engine(qm, client).execute_queue())

    assert len(client.calls) == 3
    # Block 1 opens the session; the other two rejoin the one it created.
    assert [c["resume_session_id"] for c in client.calls] == [None, "sess-1", "sess-1"]


def test_warm_blocks_are_told_they_are_a_new_task(qm):
    """Without this the model reads block 2 as a continuation of block 1."""
    qm.add_prompt("first task")
    qm.add_prompt("second task")
    client = _RecordingClient()

    asyncio.run(_engine(qm, client).execute_queue())

    assert CHAIN_TURN_SEPARATOR not in client.calls[0]["text"]
    assert client.calls[1]["text"].startswith(CHAIN_TURN_SEPARATOR)
    assert client.calls[1]["text"].endswith("second task")


def test_the_project_brief_is_sent_once_not_per_block(qm, tmp_path):
    """The brief is the expensive part; a warm session already has it."""
    (tmp_path / "master_plan").mkdir()
    (tmp_path / "master_plan" / "work_log.md").write_text(
        "# Work Log\n\n## 2026-08-01 - agent - Task\n\nDid a thing.\n", encoding="utf-8"
    )
    qm.add_prompt("first task")
    qm.add_prompt("second task")
    client = _RecordingClient()

    asyncio.run(_engine(qm, client).execute_queue())

    assert "<project_state>" in client.calls[0]["text"]
    assert "<project_state>" not in client.calls[1]["text"]


def test_an_interrupted_drain_rejoins_its_session(qm):
    """A quota hit is a pause, not an ending — the retry must stay warm."""
    qm.add_prompt("ok task")
    qm.add_prompt("boom task")
    client = _RecordingClient(fail_on="boom")

    result = asyncio.run(_engine(qm, client).execute_queue())
    assert result.stopped_reason == "quota"
    first_session = "sess-1"  # minted by the client on the block that succeeded

    # Quota refreshes; the queue is picked back up in a new process.
    client2 = _RecordingClient()
    asyncio.run(_engine(qm, client2).execute_queue())

    assert client2.calls[0]["resume_session_id"] == first_session


def test_a_finished_drain_starts_the_next_one_clean(qm):
    """An unrelated batch tomorrow must not append to today's conversation."""
    qm.add_prompt("task one")
    client = _RecordingClient()
    asyncio.run(_engine(qm, client).execute_queue())

    qm.add_prompt("an unrelated task later")
    client2 = _RecordingClient()
    asyncio.run(_engine(qm, client2).execute_queue())

    assert client2.calls[0]["resume_session_id"] is None


def test_a_stale_chain_is_not_rejoined(qm, monkeypatch):
    qm.add_prompt("task one")
    qm.add_prompt("task two")
    client = _RecordingClient(fail_on="two")
    asyncio.run(_engine(qm, client).execute_queue())

    # Age the stored session past the cutoff.
    state = qm.storage.load_json(qm.storage.run_state_path)
    state["started_at"] -= 60 * 60 * 48
    qm.storage.save_json(qm.storage.run_state_path, state)

    client2 = _RecordingClient()
    asyncio.run(_engine(qm, client2).execute_queue())

    assert client2.calls[0]["resume_session_id"] is None


# --- per-block opt-out -------------------------------------------------------


def test_a_block_can_isolate_itself_and_the_chain_continues(qm):
    qm.add_prompt("first task")
    qm.add_prompt("isolate: true\n\nsecond opinion, no prior context")
    qm.add_prompt("third task")
    client = _RecordingClient()

    asyncio.run(_engine(qm, client).execute_queue())

    assert client.calls[1]["persist_session"] is False
    assert client.calls[1]["resume_session_id"] is None
    # The chain picks up again afterwards rather than restarting.
    assert client.calls[2]["resume_session_id"] == "sess-1"


def test_isolate_header_only_counts_when_truthy(qm):
    qm.add_prompt("isolate: nope\n\ndo the thing")
    assert qm.get_prompt(1).isolate is False


# --- the other two modes -----------------------------------------------------


def test_prompt_mode_keeps_blocks_apart(qm):
    qm.add_prompt("first task")
    qm.add_prompt("second task")
    client = _RecordingClient()

    asyncio.run(_engine(qm, client, session_mode=SESSION_MODE_PROMPT).execute_queue())

    # Each block opens its own session; neither rejoins the other's.
    assert [c["resume_session_id"] for c in client.calls] == [None, None]


def test_isolate_mode_persists_no_session_at_all(qm):
    qm.add_prompt("first task")
    qm.add_prompt("second task")
    client = _RecordingClient()

    asyncio.run(_engine(qm, client, session_mode=SESSION_MODE_ISOLATE).execute_queue())

    assert all(c["persist_session"] is False for c in client.calls)
    assert all(c["resume_session_id"] is None for c in client.calls)


def test_an_unknown_session_mode_is_rejected_up_front(qm):
    with pytest.raises(ValueError, match="session_mode"):
        _engine(qm, _RecordingClient(), session_mode="warm-ish")


def test_a_quota_hit_mid_block_still_records_its_session(qm):
    """The case chaining exists for. If the interrupted attempt's session
    isn't recorded, the retry starts cold and re-derives work already done."""
    qm.add_prompt("boom task")
    qm.add_prompt("later task")

    class _QuotaAfterSessionClient(_RecordingClient):
        async def send_prompt(self, text, on_chunk=None, model=None, effort=None,
                              resume_session_id=None, persist_session=True):
            self.calls.append(
                {"text": text, "resume_session_id": resume_session_id,
                 "persist_session": persist_session}
            )
            # The CLI created the session, then the quota ran out mid-work.
            raise QuotaExceededError("usage limit reached", session_id="sess-live")

    asyncio.run(_engine(qm, _QuotaAfterSessionClient()).execute_queue())

    client2 = _RecordingClient()
    asyncio.run(_engine(qm, client2).execute_queue())

    assert client2.calls[0]["resume_session_id"] == "sess-live"

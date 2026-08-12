"""Tests for dropping blocks another runner already executed and cut.

The risk here is asymmetric and the tests are written around that: re-running
finished work costs money, but *skipping unfinished work* silently loses a
deliverable. So the rule under test is that a skip needs positive evidence —
gone from the source AND present in the history — and anything less runs.
"""

from __future__ import annotations

import asyncio

import pytest

from sandglass import prompt_source
from sandglass.execution_engine import ExecutionEngine
from sandglass.models import Response
from sandglass.queue_manager import QueueManager
from sandglass.storage import StorageService

BLOCK = (
    "**TIER: SONNET** — polling with diffing.\n\n"
    "### P6.04 — MT5 polling → diffed events `M`\n"
    "**Depends on**: P6.03\n"
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompt_tools").mkdir()
    return tmp_path


def _write(project, source: str = "", history: str = "") -> str:
    (project / "prompt_tools" / "future_prompts.md").write_text(source, encoding="utf-8")
    (project / "prompt_tools" / "prompt_history.md").write_text(history, encoding="utf-8")
    return str(project / "prompt_tools" / "future_prompts.md")


def test_still_in_the_source_means_still_to_do(project):
    src = _write(project, source=BLOCK)
    assert prompt_source.already_executed(src, BLOCK) is False


def test_gone_from_source_and_in_history_means_done(project):
    """The real shape: the history heading carries an execution marker."""
    src = _write(
        project,
        source="### P6.05 — Mt5Broker write side `L`\n**Depends on**: P6.04\n",
        history="### P6.04 — MT5 polling → diffed events `M`  "
        "[executed 2026-08-11 — Claude Opus 5, interactive]\n",
    )
    assert prompt_source.already_executed(src, BLOCK) is True


def test_a_mere_mention_in_history_is_not_evidence(project):
    """`**Depends on**: P6.04` is a reference, not a record of execution."""
    src = _write(project, source="", history="**Depends on**: P6.04 and P6.03\n")
    assert prompt_source.already_executed(src, BLOCK) is False


def test_missing_from_both_files_runs_rather_than_skips(project):
    """Absence is not evidence. A re-authored or hand-added block lives here,
    and skipping it would silently drop real work."""
    src = _write(project, source="", history="")
    assert prompt_source.already_executed(src, BLOCK) is False


def test_a_block_with_no_heading_is_never_skipped(project):
    src = _write(project, source="", history="### something else entirely\n")
    assert prompt_source.already_executed(src, "just do the thing, no heading") is False


def test_a_tiny_heading_is_not_an_identity(project):
    """'## Notes' would prefix-match half a file."""
    assert prompt_source.block_identity("## Notes\nbody") is None


def test_heading_matching_survives_dash_and_emphasis_differences(project):
    src = _write(
        project,
        source="",
        history="### P6.04 - MT5 polling -> diffed events `M` [executed]\n",
    )
    assert prompt_source.already_executed(src, "### P6.04 — MT5 polling -> diffed events `M`") is True


# --- end to end through the engine -------------------------------------------


class _Client:
    model = "m"
    effort = None

    def __init__(self):
        self.sent: list[str] = []

    def estimate_tokens(self, text):
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, **kwargs):
        self.sent.append(text)
        return Response(prompt_id="", text="done", tokens_used=10, model="m")


def test_an_already_executed_block_is_dropped_without_being_sent(project):
    """The live failure: a block cut from the markdown by an interactive session
    stayed in queue.json, got re-dispatched, was correctly declined, changed no
    files, and tripped the artifact gate — $4.92 to be told it was already done."""
    src = _write(
        project,
        source="",
        history="### P6.04 — MT5 polling → diffed events `M` [executed 2026-08-11]\n",
    )
    store = StorageService(base_path=str(project / ".sandglass"))
    qm = QueueManager(store)
    qm.add_prompt(BLOCK, origin_file=src)
    client = _Client()

    result = asyncio.run(ExecutionEngine(qm, client).execute_queue())

    assert client.sent == [], "must not spend a token on finished work"
    assert result.skipped == 1
    assert result.completed == 0
    assert qm.load_queue() == [], "and it must not be left blocking the queue"


def test_skipping_can_be_turned_off(project):
    src = _write(
        project, source="", history="### P6.04 — MT5 polling → diffed events `M` [executed]\n"
    )
    store = StorageService(base_path=str(project / ".sandglass"))
    qm = QueueManager(store)
    qm.add_prompt(BLOCK, origin_file=src)
    client = _Client()

    asyncio.run(
        ExecutionEngine(qm, client, require_artifact=False, skip_executed=False).execute_queue()
    )

    assert len(client.sent) == 1

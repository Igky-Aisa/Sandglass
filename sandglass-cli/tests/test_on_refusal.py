"""Tests for asking a block why it changed nothing, then acting on the answer.

The invariant that matters most here is the one that isn't about progress: a
block is NEVER cut on the strength of what the model said about it. Cutting
destroys the only copy of the block text, so it stays tied to evidence (files
changed) no matter how confidently the run reports success.
"""

from __future__ import annotations

import asyncio

import pytest

from sandglass.execution_engine import (
    ON_REFUSAL_STOP,
    ExecutionEngine,
)
from sandglass.models import Response
from sandglass.queue_manager import QueueManager
from sandglass.storage import StorageService

BLOCK_A = "### P1 — first thing\nbuild the first thing\n"
BLOCK_B = "### P2 — second thing\nbuild the second thing\n"


def _git_repo(tmp_path) -> bool:
    """Init a real git repo with one commit; False if git isn't usable.

    The artifact gate measures the git working tree and fails open without
    one, so these tests need a real repo or they'd assert nothing.
    """
    import subprocess

    def run(*args):
        return subprocess.run(
            args, cwd=str(tmp_path), capture_output=True, text=True, check=False
        )

    if run("git", "init").returncode != 0:
        return False
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-m", "seed")
    return run("git", "rev-parse", "HEAD").returncode == 0


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    if not _git_repo(tmp_path):
        pytest.skip("git not available")
    (tmp_path / "prompt_tools").mkdir()
    source = tmp_path / "prompt_tools" / "future_prompts.md"
    source.write_text(f"{BLOCK_A}\n====\n{BLOCK_B}", encoding="utf-8")
    return tmp_path, str(source)


class _Client:
    """A run that writes nothing and then answers the follow-up question."""

    model = "m"
    effort = None

    def __init__(self, verdict: str | None = "NOOP", why: str = "nothing to write"):
        self.verdict = verdict
        self.why = why
        self.sent: list[str] = []

    def estimate_tokens(self, text):
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, resume_session_id=None, **kwargs):
        self.sent.append(text)
        if "VERDICT:" in text:
            body = (
                f"VERDICT: {self.verdict}\nWHY: {self.why}"
                if self.verdict
                else "I'm not sure what you mean."
            )
            return Response(prompt_id="", text=body, tokens_used=5, model="m",
                            session_id=resume_session_id or "s1")
        return Response(prompt_id="", text="I changed nothing.", tokens_used=10,
                        model="m", session_id=resume_session_id or "s1")


def _queue(project):
    tmp_path, source = project
    store = StorageService(base_path=str(tmp_path / ".sandglass"))
    qm = QueueManager(store)
    qm.add_prompt(BLOCK_A, origin_file=source)
    qm.add_prompt(BLOCK_B, origin_file=source)
    return qm, source


def _asked(client) -> bool:
    return any("VERDICT:" in t for t in client.sent)


def test_noop_keeps_the_queue_moving(project):
    qm, source = _queue(project)
    client = _Client(verdict="NOOP", why="verification-only block")

    asyncio.run(ExecutionEngine(qm, client).execute_queue())

    assert _asked(client)
    # It went on to the second block instead of stopping the batch.
    assert any(t.endswith(BLOCK_B) or BLOCK_B in t for t in client.sent)


def test_a_left_in_place_block_is_never_cut_from_its_source(project):
    """The whole point: the model's word moves the queue on, it does not
    authorize destroying the only copy of a block."""
    qm, source = _queue(project)

    asyncio.run(ExecutionEngine(qm, _Client(verdict="DONE")).execute_queue())

    assert BLOCK_A.strip() in open(source, encoding="utf-8").read()
    # And it is still queued, waiting for a human to agree.
    assert BLOCK_A.strip() in [p.text.strip() for p in qm.load_queue()]


def test_a_run_that_only_left_blocks_behind_does_not_claim_to_be_finished(project):
    """It drained no work and the queue is not empty; calling that 'complete'
    would send someone looking for output that was never produced."""
    qm, _ = _queue(project)

    result = asyncio.run(ExecutionEngine(qm, _Client(verdict="NOOP")).execute_queue())

    from sandglass import run_report

    assert result.stopped_reason == run_report.REASON_LEFT_IN_PLACE
    saved = run_report.load(qm.storage)
    assert saved.remaining == 2
    assert "Finished" not in run_report.explain(saved)[0]


def test_blocked_stops_the_run_and_names_the_cause(project):
    qm, source = _queue(project)
    client = _Client(verdict="BLOCKED", why="P0 was never built")

    result = asyncio.run(ExecutionEngine(qm, client).execute_queue())

    assert result.stopped_reason == "no_artifact"
    # The second block never ran -- the blocks behind a blocked one depend on it.
    assert not any(BLOCK_B in t for t in client.sent)

    from sandglass import run_report

    saved = run_report.load(qm.storage)
    assert "P0 was never built" in saved.detail


def test_an_unparseable_answer_stops_rather_than_guessing(project):
    """Continuing is the optimistic branch and has to be earned."""
    qm, source = _queue(project)

    result = asyncio.run(ExecutionEngine(qm, _Client(verdict=None)).execute_queue())

    assert result.stopped_reason == "no_artifact"


def test_stop_mode_never_asks(project):
    qm, source = _queue(project)
    client = _Client(verdict="NOOP")

    result = asyncio.run(
        ExecutionEngine(qm, client, on_refusal=ON_REFUSAL_STOP).execute_queue()
    )

    assert not _asked(client)
    assert result.stopped_reason == "no_artifact"


def test_left_in_place_notifies_even_without_auto_resume(project, monkeypatch):
    """A left-in-place stop used to reach the phone only via
    run_with_auto_resume's post-hoc wrapper -- meaning `--once`, which never
    calls that wrapper, notified nobody. Fixed by notifying inline in
    execute_queue itself, at the point the stop reason is set."""
    sent = []
    monkeypatch.setattr(
        "sandglass.execution_engine.notify.send",
        lambda message, title="Sandglass", priority="default": sent.append(title),
    )
    qm, _ = _queue(project)

    asyncio.run(ExecutionEngine(qm, _Client(verdict="NOOP")).execute_queue())

    assert sent == ["Sandglass: nothing left to build"]


def test_no_artifact_notifies_exactly_once_through_auto_resume(project, monkeypatch):
    """The bug this pins: execute_queue already notified "no work product"
    inline, and run_with_auto_resume's wrapper notified the SAME stop a
    second time -- a real, pre-existing double-fire, confirmed by tracing the
    code before this test existed."""
    sent = []
    monkeypatch.setattr(
        "sandglass.execution_engine.notify.send",
        lambda message, title="Sandglass", priority="default": sent.append(title),
    )
    qm, _ = _queue(project)
    client = _Client(verdict="BLOCKED", why="P0 was never built")

    asyncio.run(ExecutionEngine(qm, client).run_with_auto_resume(poll_interval=0.05))

    assert sent == ["Sandglass: no work product"]


def test_left_in_place_notifies_exactly_once_through_auto_resume(project, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "sandglass.execution_engine.notify.send",
        lambda message, title="Sandglass", priority="default": sent.append(title),
    )
    qm, _ = _queue(project)

    asyncio.run(
        ExecutionEngine(qm, _Client(verdict="NOOP")).run_with_auto_resume(poll_interval=0.05)
    )

    assert sent == ["Sandglass: nothing left to build"]


def test_an_invalid_mode_is_rejected_up_front(project):
    qm, _ = _queue(project)
    with pytest.raises(ValueError):
        ExecutionEngine(qm, _Client(), on_refusal="maybe")


def test_the_right_block_is_cut_when_an_earlier_one_was_left_behind(project):
    """With a block left in place, 'the first block in the file' and 'the block
    that just ran' are different blocks -- and cutting the wrong one destroys
    unbuilt work."""
    qm, source = _queue(project)

    class _FirstRefusesThenWorks(_Client):
        async def send_prompt(self, text, on_chunk=None, resume_session_id=None, **kwargs):
            self.sent.append(text)
            if "VERDICT:" in text:
                return Response(prompt_id="", text="VERDICT: NOOP\nWHY: nothing needed",
                                tokens_used=5, model="m", session_id="s1")
            if BLOCK_B.strip() in text:
                # Only the second block does real work, so only it is cut.
                (project[0] / "made_by_b.txt").write_text("x", encoding="utf-8")
            return Response(prompt_id="", text="ok", tokens_used=10, model="m",
                            session_id="s1")

    asyncio.run(ExecutionEngine(qm, _FirstRefusesThenWorks()).execute_queue())

    remaining = open(source, encoding="utf-8").read()
    assert "P1 — first thing" in remaining, "the left-behind block must survive"
    assert "P2 — second thing" not in remaining, "the completed one must be cut"


def test_a_cold_block_is_still_asked_why(project):
    """Observed on a 30-block run: 28 blocks were routed externally, so they ran
    cold, the chain never opened, and the stop was reported with no verdict at
    all — the recovery skipped exactly the queue that needed it. A block with no
    session to resume now gets the same question with the evidence attached."""
    qm, _ = _queue(project)
    client = _Client(verdict="BLOCKED", why="P0 was never built")

    asyncio.run(
        ExecutionEngine(qm, client, session_mode="isolate").execute_queue()
    )

    asked = [t for t in client.sent if "VERDICT:" in t]
    assert asked, "a cold block must still be asked"
    # It cannot remember the refusal, so the question has to carry it.
    assert "WHAT IT ANSWERED" in asked[0]
    assert "I changed nothing." in asked[0]
    assert BLOCK_A.strip() in asked[0]

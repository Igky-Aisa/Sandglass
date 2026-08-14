"""Tests for the 5%-prompt-throughput-milestone notification and the
Progress.md-keeping-live hook it's built on.

Also carries the regression tests for `_project_dir`: this feature depends on
it to find `master_plan/`, and tracing that dependency surfaced a real,
independent bug -- a single `dirname()` on the queue source lands on
`prompt_tools/` itself (a sibling of `master_plan/`, not its parent), which
silently defeated the project-state brief for every project using the
default queue layout. Confirmed empirically before the fix, in this
session, with a script identical in shape to `test_project_dir_...` below.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from sandglass.execution_engine import ExecutionEngine
from sandglass.models import PromptObject, Response
from sandglass.queue_manager import QueueManager
from sandglass.storage import StorageService

PROGRESS_SEED = """# Progress

---

_Last updated: 2026-01-01 — someone_

## Against the main idea (`MASTER_ARQ_SYSTEM_MAP.md`)

- placeholder, never touched by this hook

## Prompt throughput

- **Done** (`prompt_tools/prompt_history.md`): 0
- **Remaining** (`prompt_tools/future_prompts.md`): 0
- **Total:** 0 — **0%** complete

## Blockers / notes

- None.
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "master_plan").mkdir()
    (tmp_path / "master_plan" / "Progress.md").write_text(PROGRESS_SEED, encoding="utf-8")
    (tmp_path / "prompt_tools").mkdir()
    return tmp_path


class _OkClient:
    model = "claude-opus-4-8"
    effort = None

    def __init__(self):
        self.sent_texts: list[str] = []

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, **kwargs):
        self.sent_texts.append(text)
        return Response(prompt_id="", text="done", tokens_used=10, model=self.model)


def _engine(project):
    qm = QueueManager(storage=StorageService(base_path=str(project / ".sandglass")))
    client = _OkClient()
    return ExecutionEngine(qm, client), qm, client


def _write_queue(project, blocks: list[str]) -> str:
    source = project / "prompt_tools" / "future_prompts.md"
    source.write_text("\n\n====\n\n".join(blocks) + "\n", encoding="utf-8")
    return str(source)


def _write_history(project, n: int) -> None:
    lines = "".join(f"## Done {i}\n\nalready complete\n\n" for i in range(n))
    (project / "prompt_tools" / "prompt_history.md").write_text(lines, encoding="utf-8")


# --- _project_dir regression -----------------------------------------------


def test_project_dir_finds_master_plan_above_prompt_tools(project):
    """master_plan/ is a SIBLING of prompt_tools/, not its parent -- a single
    dirname() on the queue source lands on prompt_tools/ itself and never
    finds it."""
    prompt = PromptObject(
        id="001", title="t", text="x",
        origin_file=str(project / "prompt_tools" / "future_prompts.md"),
    )

    assert ExecutionEngine._project_dir(prompt) == str(project)


def test_project_dir_returns_none_without_an_origin_file():
    prompt = PromptObject(id="001", title="t", text="x")
    assert ExecutionEngine._project_dir(prompt) is None


def test_brief_is_actually_injected_for_a_markdown_sourced_prompt(project):
    """End-to-end version of the regression above: before the fix, this sent
    the raw prompt text with no project-state brief at all, silently paying
    the full re-read cost the brief exists to eliminate."""
    (project / "master_plan" / "work_log.md").write_text(
        "## 2026-01-01 - someone - an entry\n\nsomething happened\n", encoding="utf-8"
    )
    _write_queue(project, ["Do the thing."])
    engine, qm, client = _engine(project)
    qm.import_from_markdown(str(project / "prompt_tools" / "future_prompts.md"))

    asyncio.run(engine.execute_queue())

    assert "<project_state>" in client.sent_texts[0]


# --- the 5% milestone -------------------------------------------------------


def test_notifies_on_crossing_a_5_percent_milestone(project, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "sandglass.execution_engine.notify.send",
        lambda message, title="Sandglass", priority="default": sent.append((title, message)),
    )
    # 10 already done, 10 queued: completing the first moves 10/20 -> 11/20,
    # i.e. exactly 55% -- the same milestone the user asked about by name.
    _write_history(project, 10)
    _write_queue(project, [f"Block {i}" for i in range(10)])
    engine, qm, client = _engine(project)
    qm.import_from_markdown(str(project / "prompt_tools" / "future_prompts.md"))

    result = asyncio.run(engine.execute_queue())

    assert result.completed == 10  # sanity: the whole queue actually ran
    milestones = [(t, m) for t, m in sent if "% complete" in t and "batch" not in t]
    titles = [t for t, _ in milestones]
    # 11/20=55%, 12/20=60%, ... 20/20=100% -- one push per block, since each
    # block here happens to land on a fresh 5% line.
    assert titles == [
        "Sandglass: 55% complete", "Sandglass: 60% complete", "Sandglass: 65% complete",
        "Sandglass: 70% complete", "Sandglass: 75% complete", "Sandglass: 80% complete",
        "Sandglass: 85% complete", "Sandglass: 90% complete", "Sandglass: 95% complete",
        "Sandglass: 100% complete",
    ]


def test_progress_md_is_updated_with_live_numbers(project):
    _write_history(project, 10)
    _write_queue(project, ["Only block"])
    engine, qm, _ = _engine(project)
    qm.import_from_markdown(str(project / "prompt_tools" / "future_prompts.md"))

    asyncio.run(engine.execute_queue())

    text = (project / "master_plan" / "Progress.md").read_text(encoding="utf-8")
    assert "- **Done** (`prompt_tools/prompt_history.md`): 11" in text
    assert "- **Remaining** (`prompt_tools/future_prompts.md`): 0" in text
    assert "- **Total:** 11 — **100%** complete" in text
    # The qualitative section is a human/agent judgment call this mechanical
    # hook has no business touching.
    assert "placeholder, never touched by this hook" in text


def test_does_not_renotify_the_same_milestone_on_a_fresh_engine(project, monkeypatch):
    """Milestone state persists in .sandglass/, so a restarted run (a fresh
    ExecutionEngine instance, same project) doesn't re-announce a milestone
    the phone has already seen."""
    sent = []
    monkeypatch.setattr(
        "sandglass.execution_engine.notify.send",
        lambda message, title="Sandglass", priority="default": sent.append(title),
    )
    _write_history(project, 9)  # 9 done
    _write_queue(project, ["A", "B"])  # completing A -> 10/11 = 90%
    engine, qm, _ = _engine(project)
    qm.import_from_markdown(str(project / "prompt_tools" / "future_prompts.md"))
    asyncio.run(engine.execute_queue())
    assert "Sandglass: 90% complete" in sent
    sent.clear()

    # Simulate a restart: brand-new engine/queue-manager over the same
    # storage, one more block added at the SAME resulting percentage.
    qm2 = QueueManager(storage=StorageService(base_path=str(project / ".sandglass")))
    _write_queue(project, [])  # nothing new queued
    # Directly re-invoke the hook with an unchanged percentage by calling the
    # private method with the same prompt shape -- simplest way to assert
    # "same milestone, no second push" without contriving exact percentages
    # across a second real queue drain.
    from sandglass.models import PromptObject
    engine2 = ExecutionEngine(qm2, _OkClient())
    prompt = PromptObject(
        id="999", title="t", text="x",
        origin_file=str(project / "prompt_tools" / "future_prompts.md"),
    )
    engine2._maybe_notify_progress(prompt)

    assert sent == []  # still at 90% (10 done, 1 left after B never ran... )


def test_no_op_without_master_plan_convention(tmp_path, monkeypatch):
    """No master_plan/ at all -- must be a silent no-op, not an error, and
    must not invent Progress.md out of nothing."""
    sent = []
    monkeypatch.setattr(
        "sandglass.execution_engine.notify.send",
        lambda message, title="Sandglass", priority="default": sent.append(title),
    )
    (tmp_path / "prompt_tools").mkdir()
    source = tmp_path / "prompt_tools" / "future_prompts.md"
    source.write_text("Only block\n", encoding="utf-8")
    qm = QueueManager(storage=StorageService(base_path=str(tmp_path / ".sandglass")))
    qm.import_from_markdown(str(source))
    engine = ExecutionEngine(qm, _OkClient())

    asyncio.run(engine.execute_queue())

    # "batch complete" is unrelated to this hook and expected regardless;
    # what matters here is that no MILESTONE notification was sent, since
    # there is no master_plan/ convention to compute one against.
    assert not any("% complete" in t for t in sent)
    assert not (tmp_path / "master_plan").exists()


def test_no_op_for_directly_queued_prompts(project, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "sandglass.execution_engine.notify.send",
        lambda message, title="Sandglass", priority="default": sent.append(title),
    )
    qm = QueueManager(storage=StorageService(base_path=str(project / ".sandglass")))
    qm.add_prompt(text="a directly queued prompt")  # no origin_file
    engine = ExecutionEngine(qm, _OkClient())

    asyncio.run(engine.execute_queue())

    # Same reasoning as above: "batch complete" is expected; a milestone
    # notification is not, since a `queue add` prompt has no origin_file to
    # count throughput against.
    assert not any("% complete" in t for t in sent)


def test_a_broken_progress_hook_never_fails_a_completed_block(project, monkeypatch):
    """A status ping is a nice-to-have; the block it's attached to already
    succeeded and must not be reported as failed because of it."""
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "sandglass.execution_engine.project_docs.update_progress_throughput", _boom
    )
    _write_history(project, 1)
    _write_queue(project, ["Only block"])
    engine, qm, _ = _engine(project)
    qm.import_from_markdown(str(project / "prompt_tools" / "future_prompts.md"))

    result = asyncio.run(engine.execute_queue())

    assert result.completed == 1
    assert result.stopped_reason is None

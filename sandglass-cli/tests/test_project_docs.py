"""Tests for the bounded project brief and log rotation."""

from __future__ import annotations

from sandglass import project_docs


def _write_log(tmp_path, entries: int, body: str = "detail") -> None:
    master_plan = tmp_path / "master_plan"
    master_plan.mkdir(exist_ok=True)
    text = "# Work Log\n\nIntro paragraph.\n"
    for i in range(1, entries + 1):
        text += f"\n## 2026-08-{i:02d} - agent - Task {i}\n\n{body} {i}\n"
    (master_plan / "work_log.md").write_text(text, encoding="utf-8")


def test_no_master_plan_means_no_brief(tmp_path, monkeypatch):
    """Sandglass runs in other people's repos — it must not assume conventions."""
    monkeypatch.chdir(tmp_path)
    assert project_docs.uses_convention() is False
    assert project_docs.build_brief() is None


def test_brief_carries_only_the_last_entries(tmp_path, monkeypatch):
    _write_log(tmp_path, entries=6)
    monkeypatch.chdir(tmp_path)

    brief = project_docs.build_brief(entries=2)

    assert brief is not None
    assert "Task 6" in brief and "Task 5" in brief
    # The whole point: older entries are not sent.
    assert "Task 4" not in brief
    assert "Task 1" not in brief


def test_brief_tells_the_agent_not_to_re_read_the_file(tmp_path, monkeypatch):
    """Without this instruction the agent reads the brief *and* the full log,
    which is strictly worse than injecting nothing at all."""
    _write_log(tmp_path, entries=3)
    monkeypatch.chdir(tmp_path)

    brief = project_docs.build_brief()

    assert "work_log.md" in brief
    assert "replaces reading" in brief


def test_brief_includes_progress_when_present(tmp_path, monkeypatch):
    _write_log(tmp_path, entries=1)
    (tmp_path / "master_plan" / "Progress.md").write_text(
        "16 done / 1 remaining (94%)", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    brief = project_docs.build_brief()

    assert "16 done / 1 remaining" in brief


def test_brief_is_capped_even_when_the_entries_are_huge(tmp_path, monkeypatch):
    """A brief that can grow without bound is the original problem renamed."""
    _write_log(tmp_path, entries=2, body="x" * 20_000)
    monkeypatch.chdir(tmp_path)

    brief = project_docs.build_brief(max_chars=2000)

    assert len(brief) <= 2000
    assert "truncated" in brief


def test_rotate_moves_old_entries_and_leaves_a_pointer(tmp_path):
    _write_log(tmp_path, entries=8)
    log = tmp_path / "master_plan" / "work_log.md"

    moved, archive_path = project_docs.rotate_log(str(log), keep=3)

    assert moved == 5
    live = log.read_text(encoding="utf-8")
    with open(archive_path, encoding="utf-8") as fh:
        archived = fh.read()

    assert "Task 8" in live and "Task 6" in live
    assert "Task 5" not in live
    # Moved, never deleted — history has to stay findable.
    assert "Task 5" in archived and "Task 1" in archived
    assert "Older entries live in" in live
    assert "Intro paragraph." in live


def test_rotate_is_a_noop_below_the_threshold(tmp_path):
    _write_log(tmp_path, entries=2)
    log = tmp_path / "master_plan" / "work_log.md"
    before = log.read_text(encoding="utf-8")

    assert project_docs.rotate_log(str(log), keep=5) is None
    assert log.read_text(encoding="utf-8") == before


def test_repeated_rotation_does_not_stack_pointer_lines(tmp_path):
    _write_log(tmp_path, entries=10)
    log = tmp_path / "master_plan" / "work_log.md"

    project_docs.rotate_log(str(log), keep=4)
    project_docs.rotate_log(str(log), keep=2)

    assert log.read_text(encoding="utf-8").count("Older entries live in") == 1


def test_apply_brief_leaves_the_prompt_alone_when_there_is_no_brief():
    assert project_docs.apply_brief("do the thing", None) == "do the thing"

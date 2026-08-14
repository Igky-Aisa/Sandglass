"""Unit tests for the artifact gate's fingerprint.

Each of these encodes a bug that was actually made while building this module —
they are regression tests, not coverage padding.
"""

import subprocess

import pytest

from sandglass import workspace


def _git(tmp_path, *args):
    return subprocess.run(
        ["git", *args], cwd=str(tmp_path), capture_output=True, text=True, check=False
    )


@pytest.fixture
def repo(tmp_path):
    if _git(tmp_path, "init").returncode != 0:
        pytest.skip("git unavailable")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "seed")
    return tmp_path


def test_returns_none_outside_a_git_repo(tmp_path):
    """None means 'cannot tell' — callers must fall back to permissive behaviour."""
    assert workspace.workspace_fingerprint(cwd=str(tmp_path)) is None


def test_produced_work_product_fails_open_when_it_cannot_measure():
    assert workspace.produced_work_product(None, None) is True
    assert workspace.produced_work_product("a", None) is True
    assert workspace.produced_work_product(None, "b") is True


def test_identical_tree_produces_identical_fingerprint(repo):
    first = workspace.workspace_fingerprint(cwd=str(repo))
    second = workspace.workspace_fingerprint(cwd=str(repo))
    assert first == second
    assert workspace.produced_work_product(first, second) is False


def test_new_untracked_file_registers(repo):
    before = workspace.workspace_fingerprint(cwd=str(repo))
    (repo / "new.txt").write_text("x", encoding="utf-8")
    after = workspace.workspace_fingerprint(cwd=str(repo))
    assert workspace.produced_work_product(before, after) is True


def test_editing_a_tracked_file_registers(repo):
    before = workspace.workspace_fingerprint(cwd=str(repo))
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8")
    after = workspace.workspace_fingerprint(cwd=str(repo))
    assert workspace.produced_work_product(before, after) is True


def test_second_edit_to_an_already_untracked_file_registers(repo):
    """`git status --porcelain` names an untracked file but not its content.

    Without the stat stamp this returns byte-identical output and the gate would
    wrongly block a prompt that edited a file an earlier prompt had created.
    """
    (repo / "new.txt").write_text("first", encoding="utf-8")
    before = workspace.workspace_fingerprint(cwd=str(repo))
    (repo / "new.txt").write_text("first and second", encoding="utf-8")
    after = workspace.workspace_fingerprint(cwd=str(repo))
    assert workspace.produced_work_product(before, after) is True


def test_a_commit_registers_even_with_a_clean_tree(repo):
    (repo / "committed.txt").write_text("work", encoding="utf-8")
    before = workspace.workspace_fingerprint(cwd=str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "the prompt committed its own work")
    after = workspace.workspace_fingerprint(cwd=str(repo))
    assert workspace.produced_work_product(before, after) is True


def test_sandglass_own_files_never_count_as_a_work_product(repo):
    """The bug that would have made this whole module a no-op.

    Sandglass writes `.sandglass/responses/response_<id>.json` for EVERY prompt,
    productive or not. If that counted, the gate would never fire.
    """
    before = workspace.workspace_fingerprint(cwd=str(repo))
    responses = repo / ".sandglass" / "responses"
    responses.mkdir(parents=True)
    (responses / "response_001.json").write_text("{}", encoding="utf-8")
    (repo / ".sandglass" / "queue.json").write_text("[]", encoding="utf-8")
    after = workspace.workspace_fingerprint(cwd=str(repo))
    assert workspace.produced_work_product(before, after) is False


def test_sandglass_writes_alongside_real_work_still_count_as_work(repo):
    before = workspace.workspace_fingerprint(cwd=str(repo))
    responses = repo / ".sandglass" / "responses"
    responses.mkdir(parents=True)
    (responses / "response_001.json").write_text("{}", encoding="utf-8")
    (repo / "real.txt").write_text("actual output", encoding="utf-8")
    after = workspace.workspace_fingerprint(cwd=str(repo))
    assert workspace.produced_work_product(before, after) is True


def test_is_ignored_prefix_not_charset(repo):
    """`lstrip('./')` strips characters, turning '.sandglass/' into 'sandglass/'.

    That exact mistake silently disabled the exclusion during development.
    """
    assert workspace._is_ignored(".sandglass/") is True
    assert workspace._is_ignored(".sandglass/responses/response_1.json") is True
    assert workspace._is_ignored("./.sandglass/queue.json") is True
    assert workspace._is_ignored(".sandglass\\responses\\r.json") is True
    # A project file that merely starts with similar characters must NOT be ignored.
    assert workspace._is_ignored("sandglass/real_source.py") is False
    assert workspace._is_ignored("src/app.py") is False


def test_status_entries_handles_renames_and_quoting():
    status = (
        ' M src/a.py\n'
        '?? new file.txt\n'
        'R  old.py -> new.py\n'
        '?? .sandglass/\n'
    )
    entries = workspace._status_entries(status)
    paths = [p for _code, p in entries]
    assert "src/a.py" in paths
    assert "new.py" in paths, "a rename should register its destination"
    assert "new file.txt" in paths
    assert not any(p.startswith(".sandglass") for p in paths)


def test_a_work_log_entry_alone_is_not_a_work_product(repo):
    """The failure this exclusion exists for, seen live.

    A block that cannot do its job writes a work-log entry saying why — the
    project's CLAUDE.md mandates one for every task. That write used to satisfy
    the artifact gate, so the block was cut from `future_prompts.md` and
    archived into `prompt_history.md` as executed, having built nothing. A
    P6.08 block whose own response opens "P6.08 is **BLOCKED**. I made no code
    changes" was archived exactly this way.

    It is self-reinforcing: every refusal deletes a block from the plan and adds
    a false "done" to the history, so its dependents later arrive to find the
    dependency missing and refuse in turn.
    """
    (repo / "master_plan").mkdir()
    (repo / "master_plan" / "work_log.md").write_text("# Work Log\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "work log")

    before = workspace.workspace_fingerprint(cwd=str(repo))
    # The refusing block does exactly what the convention asks of it.
    with open(repo / "master_plan" / "work_log.md", "a", encoding="utf-8") as fh:
        fh.write("\n## 2026-08-14 - agent - P6.08 BLOCKED, no code changes\n")
    after = workspace.workspace_fingerprint(cwd=str(repo))

    assert workspace.produced_work_product(before, after) is False


def test_the_queue_files_themselves_are_not_a_work_product(repo):
    """Cutting a block edits both queue files; that is the run's bookkeeping,
    not its output."""
    (repo / "prompt_tools").mkdir()
    (repo / "prompt_tools" / "future_prompts.md").write_text("block\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "queue")

    before = workspace.workspace_fingerprint(cwd=str(repo))
    (repo / "prompt_tools" / "prompt_history.md").write_text("archived\n", encoding="utf-8")
    (repo / "prompt_tools" / "future_prompts.md").write_text("", encoding="utf-8")
    after = workspace.workspace_fingerprint(cwd=str(repo))

    assert workspace.produced_work_product(before, after) is False


def test_a_real_doc_deliverable_still_counts(repo):
    """The exclusion is four named files, not `master_plan/` wholesale — a block
    whose job is editing the architecture doc has produced its deliverable."""
    (repo / "master_plan").mkdir()
    (repo / "master_plan" / "ARCHITECTURE.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "arch")

    before = workspace.workspace_fingerprint(cwd=str(repo))
    (repo / "master_plan" / "ARCHITECTURE.md").write_text("v2\n", encoding="utf-8")
    after = workspace.workspace_fingerprint(cwd=str(repo))

    assert workspace.produced_work_product(before, after) is True

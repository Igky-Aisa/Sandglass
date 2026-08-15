"""Tests for the static HTML status page (`sandglass dashboard`).

Assertions stay at the level of "does the generated page say the right
thing" rather than pinning exact markup, since the visual design is free to
change -- what must not silently break is that the numbers and labels shown
are the ones the underlying data actually has.
"""

from __future__ import annotations

import os

import pytest

from sandglass import dashboard, run_report
from sandglass.storage import StorageService


@pytest.fixture
def storage(tmp_path):
    return StorageService(base_path=str(tmp_path / ".sandglass"))


def _source(tmp_path):
    return str(tmp_path / "prompt_tools" / "future_prompts.md")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_generate_reports_no_data_yet(tmp_path, storage):
    html = dashboard.generate(_source(tmp_path), "My Project", storage=storage)
    assert "My Project" in html
    assert "No blocks queued or completed yet" in html


def test_generate_shows_overall_progress(tmp_path, storage):
    source = _source(tmp_path)
    _write(source, "First block\n\n====\n\nSecond block\n")
    _write(
        os.path.join(os.path.dirname(source), "prompt_history.md"),
        "# History\n\n---\n\n## Done one\n\nstuff\n",
    )

    html = dashboard.generate(source, "My Project", storage=storage)

    assert "33%" in html
    assert ">1<" in html  # done
    assert ">2<" in html  # remaining
    assert ">3<" in html  # total


def test_generate_omits_phases_section_when_no_block_declares_one(tmp_path, storage):
    source = _source(tmp_path)
    _write(source, "Just a plain block\n")

    html = dashboard.generate(source, "My Project", storage=storage)

    assert "Phases" not in html


def test_generate_includes_phases_when_declared(tmp_path, storage):
    source = _source(tmp_path)
    _write(source, "phase: Phase 1\n\nStill queued\n")
    _write(
        os.path.join(os.path.dirname(source), "prompt_history.md"),
        "# History\n\n---\n\n## Done one\n\n**Executed:** 2026-08-14\n\n```\n"
        "============================\n\nphase: Phase 1\n\nDo the thing.\n\n"
        "============================\n```\n",
    )

    html = dashboard.generate(source, "My Project", storage=storage)

    assert "Phases" in html
    assert "Phase 1" in html
    assert ">1/2<" in html


def test_generate_shows_idle_when_no_run_recorded(tmp_path, storage):
    html = dashboard.generate(_source(tmp_path), "My Project", storage=storage)
    assert "Idle" in html


def test_generate_shows_running_status(tmp_path, storage):
    run_report.save(
        storage,
        run_report.RunReport(status=run_report.STATUS_RUNNING, prompt_id="003", prompt_title="Do a thing"),
    )
    html = dashboard.generate(_source(tmp_path), "My Project", storage=storage)
    assert "Running" in html
    assert "Do a thing" in html


def test_generate_shows_quota_stop_as_a_stopped_reason_not_generic(tmp_path, storage):
    run_report.save(
        storage,
        run_report.RunReport(
            status=run_report.STATUS_STOPPED,
            reason=run_report.REASON_QUOTA,
            detail="You've hit your session limit",
            remaining=5,
        ),
    )
    html = dashboard.generate(_source(tmp_path), "My Project", storage=storage)
    assert "Quota hit" in html


def test_generate_escapes_title_and_phase_names(tmp_path, storage):
    source = _source(tmp_path)
    _write(source, "phase: <script>alert(1)</script>\n\nBlock\n")

    html = dashboard.generate(source, "<script>evil</script>", storage=storage)

    assert "<script>evil</script>" not in html
    assert "<script>alert(1)</script>" not in html


def test_write_creates_file_under_sandglass_dir(tmp_path, storage):
    source = _source(tmp_path)
    _write(source, "A block\n")

    path = dashboard.write(source, "My Project", storage=storage)

    assert os.path.exists(path)
    assert path.endswith(dashboard.DASHBOARD_FILENAME)
    with open(path, encoding="utf-8") as fh:
        assert "My Project" in fh.read()

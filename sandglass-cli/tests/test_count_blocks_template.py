"""Tests for the bundled `count_blocks.py` template and the Progress.md scaffold.

The script ships to every new project, so its failure modes are everyone's failure modes. The
two that matter are here as tests rather than as prose: a heading quoted inside a fenced code
block must not count as a completion (that is how blocks get marked done while their files are
still placeholders), and a trailing velocity window must report STALLED rather than borrowing an
older rate.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import subprocess
import sys

from sandglass.project_scaffold import TEMPLATES_DIR, new_claude_project

TEMPLATE_SCRIPT = os.path.join(TEMPLATES_DIR, "prompt_tools", "count_blocks.py")


def _load(project_dir):
    """Import the scaffolded copy, rooted at the scaffolded project."""
    spec = importlib.util.spec_from_file_location(
        f"cb_{abs(hash(str(project_dir)))}", os.path.join(project_dir, "prompt_tools", "count_blocks.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _project(tmp_path, queue: str = "", history: str = "", roadmap: str | None = None):
    new_claude_project(str(tmp_path))
    (tmp_path / "prompt_tools" / "future_prompts.md").write_text(queue, encoding="utf-8")
    (tmp_path / "prompt_tools" / "prompt_history.md").write_text(history, encoding="utf-8")
    if roadmap is not None:
        (tmp_path / "master_plan" / "ROADMAP.md").write_text(roadmap, encoding="utf-8")
    return _load(tmp_path)


def _run(project_dir) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, os.path.join("prompt_tools", "count_blocks.py")],
        cwd=str(project_dir), capture_output=True, text=True,
    )


# --- the scaffold delivers something usable ---------------------------------


def test_scaffold_ships_progress_and_counter_with_content(tmp_path):
    new_claude_project(str(tmp_path))

    progress = tmp_path / "master_plan" / "Progress.md"
    script = tmp_path / "prompt_tools" / "count_blocks.py"

    assert progress.is_file() and progress.stat().st_size > 0
    assert script.is_file() and script.stat().st_size > 0
    # The files a project must write in its own words still arrive empty.
    assert (tmp_path / "master_plan" / "SYSTEM_MAP.md").stat().st_size == 0


def test_counter_runs_clean_on_a_brand_new_project(tmp_path):
    """Day one: no blocks, no history, no dates. It must not crash and must not invent a date."""
    new_claude_project(str(tmp_path))

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "queue is empty" in proc.stdout
    assert "0%" in proc.stdout


# --- the trap that motivated the whole script -------------------------------


def test_heading_quoted_inside_a_fence_is_not_a_completion(tmp_path):
    """A `## [Sandglass work]` entry quotes the block it ran. That quotation is not evidence
    the block landed -- counting it is how three blocks were marked done with placeholder files."""
    cb = _project(
        tmp_path,
        queue="====\nmodel: sonnet\n\n### P1.02 — queued still `M`\n",
        history=(
            "## P1.01 — really done `S`  [executed 2026-01-01]\n\n"
            "## [Sandglass work] quoting its prompt\n\n"
            "```\n### P1.02 — queued still `M`\n```\n"
        ),
    )
    executed, _ = cb.scan(cb.HISTORY, cb.HEADING)

    assert executed == ["P1.01"]
    assert "P1.02" not in executed


def test_voided_heading_is_not_a_completion(tmp_path):
    cb = _project(
        tmp_path,
        history="### [VOIDED — HOLLOW CUT] P1.01 — never landed `M`\n",
    )
    executed, voided = cb.scan(cb.HISTORY, cb.HEADING)

    assert executed == []
    assert voided == ["P1.01"]


# --- invariants must fail loudly --------------------------------------------


def test_double_listing_exits_non_zero(tmp_path):
    """One id in both files means a block can execute twice -- in a trading or deploy project
    that is a duplicated side effect, so it must never be a silent green dashboard."""
    _project(
        tmp_path,
        queue="====\nmodel: sonnet\n\n### P1.01 — block `M`\n",
        history="## P1.01 — block `M`  [executed 2026-01-01]\n",
    )

    proc = _run(tmp_path)

    assert proc.returncode == 1
    assert "BOTH files" in proc.stdout and "P1.01" in proc.stdout


def test_forward_dependency_exits_non_zero(tmp_path):
    _project(
        tmp_path,
        queue=(
            "====\nmodel: sonnet\n\n### P1.01 — first `M`\n**Depends on**: P1.02\n"
            "====\nmodel: sonnet\n\n### P1.02 — second `M`\n"
        ),
    )

    proc = _run(tmp_path)

    assert proc.returncode == 1
    assert "not earlier in the file" in proc.stdout


# --- the forecast -----------------------------------------------------------


def test_velocity_uses_a_trailing_window_and_stalls_when_idle(tmp_path):
    """The point of a window over a lifetime average: it can FALL. With nothing cut inside it,
    the honest output is no date at all, not a rate borrowed from last month."""
    cb = _project(
        tmp_path,
        queue="====\nmodel: sonnet\neffort: medium\n\n### P1.02 — pending `M`\n",
        history="## P1.01 — done `M`  [executed 2020-01-01]\n",
    )
    lines = cb.forecast(cb.QUEUE.read_text(encoding="utf-8"),
                        cb.block_meta(cb.HISTORY, cb.HEADING), 1, ["P1.02"])
    text = "\n".join(lines)

    assert "STALLED" in text
    assert "0.0 units/day" in text


def test_a_fresh_cut_produces_a_date(tmp_path):
    today = dt.date.today().isoformat()
    cb = _project(
        tmp_path,
        queue="====\nmodel: sonnet\neffort: medium\n\n### P1.02 — pending `M`\n",
        history=f"## P1.01 — done `M`  [executed {today}]\n",
    )
    text = "\n".join(cb.forecast(cb.QUEUE.read_text(encoding="utf-8"),
                                 cb.block_meta(cb.HISTORY, cb.HEADING), 1, ["P1.02"]))

    assert "STALLED" not in text
    assert "rolling rate" in text


def test_no_dated_completion_offers_no_velocity_date(tmp_path):
    """Undated history is common early on. Say so; do not fabricate a rate."""
    cb = _project(
        tmp_path,
        queue="====\nmodel: sonnet\neffort: medium\n\n### P1.02 — pending `M`\n",
        history="## P1.01 — done, but nobody dated it `M`\n",
    )
    text = "\n".join(cb.forecast(cb.QUEUE.read_text(encoding="utf-8"),
                                 cb.block_meta(cb.HISTORY, cb.HEADING), 1, ["P1.02"]))

    assert "no velocity yet" in text


def test_roadmap_estimate_is_absent_without_a_phase_estimates_block(tmp_path):
    """Estimate C is the plan's own opinion. With no machine-readable source it is simply not
    offered -- a fabricated bottom-up number would be worse than none."""
    cb = _project(
        tmp_path,
        queue="====\nmodel: sonnet\neffort: medium\n\n### P1.02 — pending `M`\n",
        history=f"## P1.01 — done `M`  [executed {dt.date.today().isoformat()}]\n",
        roadmap="# Roadmap\n\nNo machine-readable estimates here.\n",
    )
    text = "\n".join(cb.forecast(cb.QUEUE.read_text(encoding="utf-8"),
                                 cb.block_meta(cb.HISTORY, cb.HEADING), 1, ["P1.02"]))

    assert "ROADMAP's own bottom-up" not in text


def test_roadmap_estimate_appears_when_the_block_is_present(tmp_path):
    cb = _project(
        tmp_path,
        queue="====\nmodel: sonnet\neffort: medium\n\n### P1.02 — pending `M`\n",
        history=f"## P1.01 — done `M`  [executed {dt.date.today().isoformat()}]\n",
        roadmap="# Roadmap\n\n<!-- PHASE-ESTIMATES\nP1: 2-3\n-->\n",
    )
    text = "\n".join(cb.forecast(cb.QUEUE.read_text(encoding="utf-8"),
                                 cb.block_meta(cb.HISTORY, cb.HEADING), 1, ["P1.02"]))

    assert "ROADMAP's own bottom-up" in text


def test_the_bundled_script_and_the_scaffolded_copy_are_identical(tmp_path):
    """The scaffold copies; it must not transform. A drifting copy would make the dashboard's
    'every number comes from this command' claim false in exactly the projects using it."""
    new_claude_project(str(tmp_path))

    with open(TEMPLATE_SCRIPT, encoding="utf-8") as fh:
        bundled = fh.read()
    with open(tmp_path / "prompt_tools" / "count_blocks.py", encoding="utf-8") as fh:
        scaffolded = fh.read()

    assert bundled == scaffolded

import asyncio
import time

import pytest

from sandglass.claude_client import QuotaExceededError
from sandglass.execution_engine import (
    RESET_BUFFER_SECONDS,
    SUMMARY_MAX,
    WORK_LOG_PATH,
    ExecutionEngine,
    _BANNER_FONT,
    _BANNER_GAP,
    _BANNER_SIGNATURE,
    _BANNER_TEXT,
    _BOTTOM_CELLS,
    _CYCLE_TICKS,
    _HOURGLASS_HALF,
    _HOURGLASS_ROWS,
    _HOURGLASS_WIDTH,
    _PROGRESS_UNITS,
    _ROW_CELLS,
    _TICKS_PER_UNIT,
    _TOP_SUBSTEPS,
    _hourglass_frame,
    _pad_to_height,
    _render_banner,
)
from sandglass.models import Response
from sandglass.queue_manager import QueueManager
from sandglass.storage import StorageService


@pytest.fixture
def qm(tmp_path):
    return QueueManager(storage=StorageService(base_path=str(tmp_path / ".sandglass")))


def _task_of(text: str) -> str:
    """The task line out of what the client was handed.

    The engine may prepend a bounded project-state brief to a prompt before
    sending it, so the text a client receives is not always the prompt text.
    A real model answers about the task, not by quoting its whole input, and
    these doubles have to do the same or they'd assert on the brief.
    """
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else text


class _FlakyThenOkClient:
    """Fails a specific prompt with a quota error a fixed number of times, then succeeds.

    ``resets_in`` defaults to a timestamp comfortably past ``RESET_BUFFER_SECONDS``
    (not just a fixed -60s, which the 2-minute buffer would outrun) so
    ``_compute_wait_seconds`` clamps the simulated wait to its 1-second floor
    instead of actually waiting out the real buffer padding -- keeps the test
    suite fast without weakening what's being verified.
    """

    model = "claude-opus-4-8"
    effort = None

    def __init__(self, fail_text: str, times: int, resets_in: float = -RESET_BUFFER_SECONDS - 60):
        self.fail_text = fail_text
        self.times = times
        self.resets_in = resets_in
        self.attempts = 0

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, model=None, **kwargs):
        if text == self.fail_text and self.attempts < self.times:
            self.attempts += 1
            raise QuotaExceededError(
                "quota hit",
                rate_limit_info={"status": "rejected", "resetsAt": time.time() + self.resets_in},
            )
        if on_chunk is not None:
            on_chunk("ok")
        return Response(
            prompt_id="", text=f"done: {_task_of(text)}", tokens_used=10, model=self.model
        )


class _AlwaysQuotaClient:
    """Always raises a quota error -- simulates a misclassified permanent failure."""

    model = "claude-opus-4-8"
    effort = None

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, model=None, **kwargs):
        raise QuotaExceededError(
            "permanently stuck",
            rate_limit_info={"status": "rejected", "resetsAt": time.time() - RESET_BUFFER_SECONDS - 60},
        )


class _AlwaysErrorClient:
    """Always raises a plain, non-quota error."""

    model = "claude-opus-4-8"
    effort = None

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, model=None, **kwargs):
        raise RuntimeError("network is down")


class _SelfLoggingClient:
    """Simulates a headless `claude -p` that writes its own work_log.md entry.

    Real headless runs edit files as a side effect of their own tool calls,
    invisible to ExecutionEngine -- from its perspective all that matters is
    whether WORK_LOG_PATH changed during ``send_prompt``, so touching it here
    is a faithful enough stand-in without needing a real subprocess.
    """

    model = "claude-opus-4-8"
    effort = None

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, model=None, **kwargs):
        with open(WORK_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("\n## a real entry the headless run wrote for itself\n")
        if on_chunk is not None:
            on_chunk("ok")
        return Response(prompt_id="", text=f"done: {text}", tokens_used=5, model=self.model)


def test_run_with_auto_resume_waits_out_quota_then_completes(qm):
    qm.add_prompt("first")
    qm.add_prompt("second")
    client = _FlakyThenOkClient(fail_text="first", times=2)
    engine = ExecutionEngine(qm, client)

    result = asyncio.run(engine.run_with_auto_resume(poll_interval=0.05))

    assert result.stopped_reason is None
    assert result.completed == 2
    assert qm.get_all_prompts() == []
    assert client.attempts == 2


def test_run_with_auto_resume_sends_notifications_for_wait_resume_and_complete(qm, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "sandglass.execution_engine.notify.send",
        lambda message, title="Sandglass", priority="default": sent.append(title),
    )
    qm.add_prompt("first")
    engine = ExecutionEngine(qm, _FlakyThenOkClient(fail_text="first", times=1))

    asyncio.run(engine.run_with_auto_resume(poll_interval=0.05))

    # "token limits hit" comes from execute_queue's detection point, not from
    # the auto-resume loop -- which is why it also fires under `--once`.
    assert sent == [
        "Sandglass: token limits hit",
        "Sandglass: resuming",
        "Sandglass: batch complete",
    ]


def test_quota_hit_notifies_even_without_auto_resume(qm, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "sandglass.execution_engine.notify.send",
        lambda message, title="Sandglass", priority="default": sent.append((title, message)),
    )
    qm.add_prompt("first")
    qm.add_prompt("second")
    engine = ExecutionEngine(qm, _AlwaysQuotaClient())

    asyncio.run(engine.execute_queue())

    assert len(sent) == 1
    title, message = sent[0]
    assert title == "Sandglass: token limits hit"
    # The prompt that hit the limit is still queued, so both are counted.
    assert "2 prompt(s) still queued" in message


def test_error_notifies_even_without_auto_resume(qm, monkeypatch):
    """`--once` never reaches run_with_auto_resume's wrapper, and a plain
    crash mid-block used to reach the phone only through it -- meaning a
    crash under `--once` produced zero notifications at all."""
    sent = []
    monkeypatch.setattr(
        "sandglass.execution_engine.notify.send",
        lambda message, title="Sandglass", priority="default": sent.append((title, priority)),
    )
    qm.add_prompt("will fail")
    engine = ExecutionEngine(qm, _AlwaysErrorClient())

    asyncio.run(engine.execute_queue())

    assert sent == [("Sandglass: batch stopped early", "high")]


def test_error_notifies_exactly_once_through_auto_resume(qm, monkeypatch):
    """Regression guard: the fix above must not turn into a double-fire the
    way no_artifact's did (see test_on_refusal.py) once run_with_auto_resume
    also has a chance to notify for the same stop."""
    sent = []
    monkeypatch.setattr(
        "sandglass.execution_engine.notify.send",
        lambda message, title="Sandglass", priority="default": sent.append(title),
    )
    qm.add_prompt("will fail")
    engine = ExecutionEngine(qm, _AlwaysErrorClient())

    asyncio.run(engine.run_with_auto_resume(poll_interval=0.05))

    assert sent == ["Sandglass: batch stopped early"]


def test_clean_completion_notifies_even_without_auto_resume(qm, monkeypatch):
    """A queue that drains fully under `--once` used to notify nobody at
    all -- "batch complete" only ever fired from run_with_auto_resume's own
    post-loop check, which `--once` never reaches."""
    sent = []
    monkeypatch.setattr(
        "sandglass.execution_engine.notify.send",
        lambda message, title="Sandglass", priority="default": sent.append(title),
    )
    qm.add_prompt("first")
    qm.add_prompt("second")
    engine = ExecutionEngine(qm, _FlakyThenOkClient(fail_text="__never__", times=0))

    asyncio.run(engine.execute_queue())

    assert sent == ["Sandglass: batch complete"]


def test_clean_completion_notifies_exactly_once_through_auto_resume(qm, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "sandglass.execution_engine.notify.send",
        lambda message, title="Sandglass", priority="default": sent.append(title),
    )
    qm.add_prompt("first")
    engine = ExecutionEngine(qm, _FlakyThenOkClient(fail_text="__never__", times=0))

    asyncio.run(engine.run_with_auto_resume(poll_interval=0.05))

    assert sent == ["Sandglass: batch complete"]


def test_run_with_auto_resume_sends_notification_when_stopped_early(qm, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "sandglass.execution_engine.notify.send",
        lambda message, title="Sandglass", priority="default": sent.append((title, priority)),
    )
    qm.add_prompt("will fail")
    engine = ExecutionEngine(qm, _AlwaysErrorClient())

    asyncio.run(engine.run_with_auto_resume(poll_interval=0.05))

    assert sent == [("Sandglass: batch stopped early", "high")]


def test_run_with_auto_resume_gives_up_after_max_stalls(qm):
    qm.add_prompt("stuck prompt")
    engine = ExecutionEngine(qm, _AlwaysQuotaClient())

    result = asyncio.run(engine.run_with_auto_resume(poll_interval=0.05, max_stalls=3))

    assert result.stopped_reason == "quota"
    remaining = qm.get_all_prompts()
    assert len(remaining) == 1
    assert remaining[0].title == "stuck prompt"


def test_run_with_auto_resume_does_not_retry_non_quota_errors(qm):
    qm.add_prompt("will fail")
    engine = ExecutionEngine(qm, _AlwaysErrorClient())

    result = asyncio.run(engine.run_with_auto_resume(poll_interval=0.05))

    assert result.stopped_reason == "error"
    remaining = qm.get_all_prompts()
    assert len(remaining) == 1


def test_compute_wait_seconds_uses_resets_at_when_available(qm):
    engine = ExecutionEngine(qm, _AlwaysQuotaClient())
    resets_at = time.time() + 100
    from sandglass.execution_engine import RESET_BUFFER_SECONDS, _epoch_to_iso

    wait = engine._compute_wait_seconds(_epoch_to_iso(resets_at), fallback=900)

    # ~100s plus the reset buffer, well under the fallback
    expected = 100 + RESET_BUFFER_SECONDS
    assert expected - 10 < wait <= expected + 5


def test_compute_wait_seconds_falls_back_when_no_resets_at(qm):
    engine = ExecutionEngine(qm, _AlwaysQuotaClient())

    wait = engine._compute_wait_seconds(None, fallback=900)

    assert wait == 900


def test_completing_a_markdown_sourced_prompt_cuts_it_into_history(qm, tmp_path):
    source = tmp_path / "future_prompts.md"
    source.write_text(
        "First prompt\n\n====\n\nSecond prompt\n",
        encoding="utf-8",
    )
    qm.import_from_markdown(str(source))
    engine = ExecutionEngine(qm, _FlakyThenOkClient(fail_text="__never__", times=0))

    result = asyncio.run(engine.execute_queue())

    assert result.completed == 2
    assert qm.get_all_prompts() == []
    # Both blocks should be cut from the source file...
    from sandglass import prompt_source
    assert prompt_source.read_blocks(str(source)) == []
    # ...and archived into its sibling history file, newest (second) first.
    history = tmp_path / "prompt_history.md"
    content = history.read_text(encoding="utf-8")
    assert content.index("Second prompt") < content.index("First prompt")


def test_quota_hit_notes_interruption_but_leaves_markdown_sourced_prompt_queued(qm, tmp_path):
    source = tmp_path / "future_prompts.md"
    source.write_text("Only prompt\n", encoding="utf-8")
    qm.import_from_markdown(str(source))
    engine = ExecutionEngine(qm, _AlwaysQuotaClient())

    result = asyncio.run(engine.execute_queue())

    assert result.stopped_reason == "quota"
    assert qm.get_all_prompts()[0].title == "Only prompt"  # still queued, not cut
    from sandglass import prompt_source
    assert len(prompt_source.read_blocks(str(source))) == 1  # block untouched

    history = tmp_path / "prompt_history.md"
    content = history.read_text(encoding="utf-8")
    assert "INTERRUPTED, will auto-resume" in content
    assert "Only prompt" in content


def test_quota_hit_on_a_directly_queued_prompt_does_not_touch_any_history_file(qm, tmp_path):
    qm.add_prompt(text="a manually added prompt")
    engine = ExecutionEngine(qm, _AlwaysQuotaClient())

    asyncio.run(engine.execute_queue())

    # No origin_file -- nothing to note the interruption in.
    assert not (tmp_path / "prompt_history.md").exists()


def test_prompts_added_directly_do_not_touch_any_markdown_file(qm, tmp_path):
    qm.add_prompt(text="a manually added prompt")
    engine = ExecutionEngine(qm, _FlakyThenOkClient(fail_text="__never__", times=0))

    asyncio.run(engine.execute_queue())

    # No prompt_history.md should have been created, since this prompt has
    # no origin_file and _cut_from_source is never invoked for it.
    assert not (tmp_path / "prompt_history.md").exists()


def test_summarize_response_uses_first_non_empty_line(qm):
    engine = ExecutionEngine(qm, _FlakyThenOkClient(fail_text="__never__", times=0))

    assert engine._summarize_response("\n\nDone. Fixed the auth bug.\n\nMore detail below.") == (
        "Done. Fixed the auth bug."
    )


def test_summarize_response_truncates_long_lines(qm):
    engine = ExecutionEngine(qm, _FlakyThenOkClient(fail_text="__never__", times=0))
    long_line = "x" * (SUMMARY_MAX + 20)

    summary = engine._summarize_response(long_line)

    assert len(summary) == SUMMARY_MAX
    assert summary.endswith("…")


def test_summarize_response_handles_blank_text(qm):
    engine = ExecutionEngine(qm, _FlakyThenOkClient(fail_text="__never__", times=0))

    assert engine._summarize_response("   \n\n  ") == "(no summary available)"


def test_execute_queue_prints_what_has_been_done_bullets(qm, capsys):
    qm.add_prompt("first")
    qm.add_prompt("second")
    engine = ExecutionEngine(qm, _FlakyThenOkClient(fail_text="__never__", times=0))

    asyncio.run(engine.execute_queue())

    out = capsys.readouterr().out
    assert "what has been done:" in out
    assert "- done: first" in out
    assert "- done: second" in out


def test_execute_queue_omits_what_has_been_done_when_nothing_completed(qm, capsys):
    engine = ExecutionEngine(qm, _AlwaysErrorClient())
    qm.add_prompt("will fail")

    asyncio.run(engine.execute_queue())

    out = capsys.readouterr().out
    assert "what has been done:" not in out


NECK_ROW = _HOURGLASS_HALF + 1  # +1 for the rounded top cap
SETTLED_TICK = _PROGRESS_UNITS * _TICKS_PER_UNIT  # first poured-out, still frame
TOP_CAP = "╭" + "─" * (_HOURGLASS_WIDTH - 2) + "╮"
BASE_LINE = "╰" + "─" * (_HOURGLASS_WIDTH - 2) + "╯"


def _top_rows(tick):
    """Top chamber rows, widest (nearest the cap) first."""
    return _hourglass_frame(tick).split("\n")[1 : 1 + _HOURGLASS_HALF]


def _bottom_rows(tick):
    """Bottom cone rows, widest (resting on the base line) first."""
    rows = _hourglass_frame(tick).split("\n")[NECK_ROW + 1 : 2 * _HOURGLASS_HALF + 2]
    return rows[::-1]  # drawn narrowest-first; fill order is the reverse


def _sand_columns(row):
    return {i for i, char in enumerate(row) if char in ":."}


def _tick_when_grains(n):
    """First tick at which `n` grains have landed in the bottom cone."""
    # grains == poured * _BOTTOM_CELLS // _PROGRESS_UNITS, so invert that.
    return -(-n * _PROGRESS_UNITS // _BOTTOM_CELLS) * _TICKS_PER_UNIT


def test_hourglass_frame_is_a_compact_rectangle_across_ticks():
    # rounded top cap + top rows + neck + bottom rows + rounded bottom cap
    expected_lines = 2 * _HOURGLASS_HALF + 3
    assert expected_lines <= 9  # compact, spinner-sized -- not a big block
    for tick in range(_CYCLE_TICKS):
        lines = _hourglass_frame(tick).split("\n")
        assert len(lines) == expected_lines
        assert all(len(line) == _HOURGLASS_WIDTH for line in lines)


def test_hourglass_caps_are_glass_and_are_never_broken_by_sand():
    # The rounded caps draw the glass itself: sand piles up *on* the base
    # line, it never replaces part of it, and no falling grain punches
    # through it either.
    for tick in range(_CYCLE_TICKS):
        lines = _hourglass_frame(tick).split("\n")
        assert lines[0] == TOP_CAP
        assert lines[-1] == BASE_LINE


def test_hourglass_frame_starts_full_top_empty_bottom():
    assert all(":" in row for row in _top_rows(0))
    assert all(":" not in row for row in _bottom_rows(0))


def test_hourglass_frame_ends_empty_top_and_full_bottom():
    top = _top_rows(SETTLED_TICK)
    bottom = _bottom_rows(SETTLED_TICK)

    assert all(":" not in row and "." not in row for row in top)
    # Every cell of the cone holds settled sand -- nothing loose, nothing
    # falling; that stillness is what makes the wrap read as a flip.
    for row, cells in zip(bottom, _ROW_CELLS):
        assert row.count(":") == cells
        assert "." not in row


def test_hourglass_top_drains_from_the_surface_down():
    # Halfway through the pour the widest row (under the cap) is the one
    # that cleared -- draining neck-first is what made the old loop read
    # wrong, since the sand surface appeared to rise.
    top = _top_rows(_PROGRESS_UNITS // 2 * _TICKS_PER_UNIT)

    assert ":" not in top[0] and "." not in top[0]
    assert ":" in top[-1]


def test_hourglass_bottom_fills_from_the_bottom_up():
    # The row resting on the base line fills first and completely; the rows
    # above it hold no sand yet -- anything on them is the falling stream
    # passing through the centre channel, never a settled grain.
    center = _HOURGLASS_WIDTH // 2
    bottom = _bottom_rows(_tick_when_grains(_ROW_CELLS[0]))

    assert len(_sand_columns(bottom[0])) == _ROW_CELLS[0]
    for row in bottom[1:]:
        assert ":" not in row
        assert _sand_columns(row) <= {center}


def test_hourglass_bottom_rows_fill_partially_as_a_heap_from_the_centre():
    center = _HOURGLASS_WIDTH // 2
    widths = [len(_sand_columns(_bottom_rows(_tick_when_grains(n))[0])) for n in (1, 2, 3)]

    # A row is not all-or-nothing: it grows a grain at a time...
    assert widths == [1, 2, 3]
    # ...outward from the centre, staying a single contiguous mound.
    for n in range(1, _ROW_CELLS[0] + 1):
        cols = _sand_columns(_bottom_rows(_tick_when_grains(n))[0])
        assert center in cols
        assert cols == set(range(min(cols), min(cols) + n))


def test_hourglass_top_row_thins_before_it_clears():
    # The top's own partial state: a row goes ':' -> '.' -> gone, so the
    # chamber lightens gradually instead of snapping empty.
    seen = {_top_rows(tick * _TICKS_PER_UNIT)[0].strip("\\/") for tick in range(_PROGRESS_UNITS)}

    assert ":" * (_HOURGLASS_WIDTH - 2) in seen
    assert "." * (_HOURGLASS_WIDTH - 2) in seen
    assert " " * (_HOURGLASS_WIDTH - 2) in seen


def test_hourglass_stream_falls_through_the_empty_bottom_cone():
    center = _HOURGLASS_WIDTH // 2
    grain_rows = {
        tick: {i for i, row in enumerate(_bottom_rows(tick)) if row[center] == "."}
        for tick in (0, 1)
    }

    # Every other row carries a grain, and the pattern shifts by one row per
    # tick -- that shift is what reads as sand falling.
    assert grain_rows[0] and grain_rows[1]
    assert grain_rows[0].isdisjoint(grain_rows[1])
    assert grain_rows[0] | grain_rows[1] == set(range(_HOURGLASS_HALF))


def test_hourglass_stream_stops_once_everything_has_poured():
    assert _hourglass_frame(SETTLED_TICK) == _hourglass_frame(SETTLED_TICK + 1)


def test_hourglass_pour_is_conserved_between_the_two_halves():
    # Both halves hold the same amount of sand, so the glass is never
    # visibly emptier or fuller than it started.
    assert sum(_ROW_CELLS) == _BOTTOM_CELLS
    assert _PROGRESS_UNITS % _BOTTOM_CELLS == 0
    assert _PROGRESS_UNITS % _TOP_SUBSTEPS == 0


def test_hourglass_frame_neck_flickers_while_sand_is_flowing():
    frame_a = _hourglass_frame(0).split("\n")[NECK_ROW]
    frame_b = _hourglass_frame(1).split("\n")[NECK_ROW]

    assert frame_a != frame_b
    assert {frame_a.strip(), frame_b.strip()} == {")*(", ").("}


def test_hourglass_frame_neck_is_pinched_with_parens():
    neck = _hourglass_frame(0).split("\n")[NECK_ROW]

    assert neck.strip().startswith(")")
    assert neck.strip()[:3].endswith("(")
    assert neck.strip()[1] in "*."


def test_hourglass_frame_appends_caption_to_the_neck_row_only():
    lines = _hourglass_frame(0, caption="1.2 min remaining").split("\n")

    assert "1.2 min remaining" in lines[NECK_ROW]
    assert all("min remaining" not in line for i, line in enumerate(lines) if i != NECK_ROW)


def test_hourglass_frame_cycle_wraps_around():
    assert _hourglass_frame(0) == _hourglass_frame(_CYCLE_TICKS)
    assert _hourglass_frame(3) == _hourglass_frame(3 + _CYCLE_TICKS)


def test_banner_font_covers_every_letter_sandglass_needs():
    assert set(_BANNER_TEXT) <= set(_BANNER_FONT)


def test_banner_glyphs_are_internally_rectangular():
    """Each letter's own rows must agree on width, or the letters beside it in
    the rendered word go ragged -- checked again here (not just relying on the
    module-level assert) so a future edit to one glyph fails a test, not just
    an import."""
    for letter, rows in _BANNER_FONT.items():
        assert len(rows) == 5, letter
        widths = {len(row) for row in rows}
        assert len(widths) == 1, f"{letter}: {rows}"


def test_render_banner_is_a_five_row_rectangle():
    banner = _render_banner(_BANNER_TEXT)
    lines = banner.split("\n")
    assert len(lines) == 5
    widths = {len(line) for line in lines}
    assert len(widths) == 1, "banner rows must all be the same width"


def test_render_banner_uses_only_the_font_alphabet():
    banner = _render_banner(_BANNER_TEXT)
    glyph_chars = {ch for rows in _BANNER_FONT.values() for row in rows for ch in row}
    assert set(banner) <= (glyph_chars | {"\n"})


def test_render_banner_is_deterministic():
    assert _render_banner(_BANNER_TEXT) == _render_banner(_BANNER_TEXT)


def test_the_signature_names_the_project_and_the_author():
    assert "Sandglass" in _BANNER_SIGNATURE
    assert "Igky Aisa" in _BANNER_SIGNATURE


def test_pad_to_height_centers_a_shorter_block():
    """The banner (5 rows) sits beside the hourglass (9 rows); without
    centering it would hug the top of the row instead of sitting beside the
    glass's visual middle."""
    padded = _pad_to_height(["AB", "CD"], 6)
    assert padded == ["  ", "  ", "AB", "CD", "  ", "  "]


def test_pad_to_height_puts_the_odd_row_on_the_bottom():
    padded = _pad_to_height(["X"], 4)
    assert padded == [" ", "X", " ", " "]


def test_pad_to_height_is_a_noop_at_the_target_height():
    assert _pad_to_height(["AB", "CD"], 2) == ["AB", "CD"]


def test_the_banner_beside_the_hourglass_never_wraps_an_80_column_terminal():
    """Real bug caught before it shipped: appending the countdown caption to
    the hourglass's neck row (as the standalone frame does) pushed the widest
    combined row past 80 columns and wrapped mid-frame in an ordinary
    terminal, breaking the box shape. The composed row -- banner, gap, plain
    hourglass, no caption -- must stay well under that."""
    banner_rows = _pad_to_height(_render_banner(_BANNER_TEXT).split("\n"), _HOURGLASS_ROWS)
    frame_rows = _hourglass_frame(0).split("\n")
    widest = max(len(b) + len(_BANNER_GAP) + len(f) for b, f in zip(banner_rows, frame_rows))
    assert widest < 80


def test_sleep_with_animation_waits_the_full_duration_and_prints_start_end(qm, capsys):
    engine = ExecutionEngine(qm, _AlwaysErrorClient())
    start = time.monotonic()

    asyncio.run(engine._sleep_with_animation(0.8, resume_at=None))

    elapsed = time.monotonic() - start
    assert elapsed >= 0.8
    out = capsys.readouterr().out
    assert "Waiting" in out and "quota to refresh" in out
    assert "Resuming" in out


def test_work_log_snapshot_is_none_without_a_master_plan_dir(qm, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine = ExecutionEngine(qm, _AlwaysErrorClient())

    assert engine._work_log_snapshot() is None


def test_work_log_snapshot_is_a_sentinel_when_dir_exists_but_file_is_missing(qm, tmp_path, monkeypatch):
    (tmp_path / "master_plan").mkdir()
    monkeypatch.chdir(tmp_path)
    engine = ExecutionEngine(qm, _AlwaysErrorClient())

    assert engine._work_log_snapshot() == (0.0, 0)


def test_execute_queue_appends_work_log_fallback_when_prompt_did_not_log_itself(qm, tmp_path, monkeypatch):
    (tmp_path / "master_plan").mkdir()
    (tmp_path / "master_plan" / "work_log.md").write_text("# Work Log\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    qm.add_prompt("a trivial task")
    engine = ExecutionEngine(qm, _FlakyThenOkClient(fail_text="__never__", times=0))

    asyncio.run(engine.execute_queue())

    content = (tmp_path / "master_plan" / "work_log.md").read_text(encoding="utf-8")
    assert "sandglass execute" in content
    assert "a trivial task" in content
    assert "done: a trivial task" in content


def test_execute_queue_skips_work_log_fallback_when_prompt_already_logged_itself(qm, tmp_path, monkeypatch):
    (tmp_path / "master_plan").mkdir()
    (tmp_path / "master_plan" / "work_log.md").write_text("# Work Log\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    qm.add_prompt("a task the agent logs for itself")
    engine = ExecutionEngine(qm, _SelfLoggingClient())

    asyncio.run(engine.execute_queue())

    content = (tmp_path / "master_plan" / "work_log.md").read_text(encoding="utf-8")
    assert "a real entry the headless run wrote for itself" in content
    assert "Auto-logged entry" not in content  # no duplicate fallback entry


def test_execute_queue_never_creates_a_master_plan_dir_when_absent(qm, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    qm.add_prompt("some task")
    engine = ExecutionEngine(qm, _FlakyThenOkClient(fail_text="__never__", times=0))

    asyncio.run(engine.execute_queue())

    assert not (tmp_path / "master_plan").exists()


# --- the artifact gate (sandglass/workspace.py) -------------------------------
#
# Regression tests for a real incident: twelve blocks in the Asymmetry project were
# cut from future_prompts.md into prompt_history.md having landed zero code, two of
# them (P4.03, P5.01) with responses that were verbatim refusals to proceed. The cut
# destroys the only copy of the block text, so those phases became undeliverable.


class _RefusingClient:
    """A run that reads the repo, concludes it cannot proceed, and says so.

    Writes nothing. This is the exact shape of `.sandglass/responses/response_041.json`
    and `response_042.json` from the incident -- a *successful* API call whose content
    is a refusal.
    """

    model = "claude-opus-4-8"
    effort = None

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, model=None, **kwargs):
        if on_chunk is not None:
            on_chunk("blocked")
        return Response(
            prompt_id="",
            text="P5.01 can't be built right now - its dependency was never built.",
            tokens_used=5000,
            model=self.model,
        )


class _WritingClient:
    """A run that actually produces a work product."""

    model = "claude-opus-4-8"
    effort = None

    def __init__(self, target: str):
        self.target = target

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, model=None, **kwargs):
        with open(self.target, "a", encoding="utf-8") as fh:
            fh.write("real work landed here\n")
        if on_chunk is not None:
            on_chunk("ok")
        return Response(prompt_id="", text="done", tokens_used=10, model=self.model)


def _git_repo(tmp_path):
    """Init a real git repo with one commit; returns True if git is usable."""
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


def _markdown_queue(tmp_path, qm, blocks=2):
    source = tmp_path / "future_prompts.md"
    source.write_text(
        "\n====\n".join(f"block {i} body" for i in range(1, blocks + 1)),
        encoding="utf-8",
    )
    qm.import_from_markdown(str(source))
    return source


def test_refusal_that_writes_nothing_is_not_cut_from_the_source(qm, tmp_path, monkeypatch):
    """THE regression test: a response with no work product must not be archived."""
    if not _git_repo(tmp_path):
        pytest.skip("git unavailable")
    monkeypatch.chdir(tmp_path)
    source = _markdown_queue(tmp_path, qm, blocks=2)
    before = source.read_text(encoding="utf-8")

    engine = ExecutionEngine(qm, _RefusingClient())
    result = asyncio.run(engine.execute_queue())

    assert result.stopped_reason == "no_artifact"
    assert result.completed == 0
    # The block is still in the source file -- the whole point.
    assert source.read_text(encoding="utf-8") == before
    # And still in the queue, so nothing is lost.
    assert len(qm.get_all_prompts()) == 2
    # No history file was created for a block that never ran.
    assert not (tmp_path / "prompt_history.md").exists()


def test_refusal_stops_the_run_instead_of_cascading(qm, tmp_path, monkeypatch):
    """A refusal usually means a missing dependency; later blocks depend on it.

    In the real incident P4.03 refused, then P5.01 refused, then P5.03 refused --
    three blocks consumed because the run kept going.
    """
    if not _git_repo(tmp_path):
        pytest.skip("git unavailable")
    monkeypatch.chdir(tmp_path)
    _markdown_queue(tmp_path, qm, blocks=3)

    engine = ExecutionEngine(qm, _RefusingClient())
    result = asyncio.run(engine.execute_queue())

    assert result.stopped_reason == "no_artifact"
    assert len(qm.get_all_prompts()) == 3, "must stop at the first, not burn the rest"


def test_a_prompt_that_writes_a_file_is_still_cut_normally(qm, tmp_path, monkeypatch):
    """The gate must not block real work -- the obvious way to break this fix."""
    if not _git_repo(tmp_path):
        pytest.skip("git unavailable")
    monkeypatch.chdir(tmp_path)
    source = _markdown_queue(tmp_path, qm, blocks=2)

    engine = ExecutionEngine(qm, _WritingClient(str(tmp_path / "out.txt")))
    result = asyncio.run(engine.execute_queue())

    assert result.stopped_reason is None
    assert result.completed == 2
    assert qm.get_all_prompts() == []
    assert source.read_text(encoding="utf-8").strip() == ""
    assert (tmp_path / "prompt_history.md").exists()


def test_no_require_artifact_restores_the_old_permissive_behaviour(qm, tmp_path, monkeypatch):
    if not _git_repo(tmp_path):
        pytest.skip("git unavailable")
    monkeypatch.chdir(tmp_path)
    source = _markdown_queue(tmp_path, qm, blocks=1)

    engine = ExecutionEngine(qm, _RefusingClient(), require_artifact=False)
    result = asyncio.run(engine.execute_queue())

    assert result.stopped_reason is None
    assert result.completed == 1
    assert source.read_text(encoding="utf-8").strip() == ""


def test_gate_is_skipped_outside_a_git_repo_so_non_git_projects_still_work(
    qm, tmp_path, monkeypatch
):
    """Fails open when it cannot measure -- a non-git project must keep working."""
    monkeypatch.chdir(tmp_path)  # deliberately NOT a git repo
    source = _markdown_queue(tmp_path, qm, blocks=1)

    engine = ExecutionEngine(qm, _RefusingClient())
    result = asyncio.run(engine.execute_queue())

    assert result.stopped_reason is None
    assert result.completed == 1
    assert source.read_text(encoding="utf-8").strip() == ""


def test_manually_added_prompts_are_never_gated(qm, tmp_path, monkeypatch):
    """`queue add` prompts have no block to protect, so the gate must not apply."""
    if not _git_repo(tmp_path):
        pytest.skip("git unavailable")
    monkeypatch.chdir(tmp_path)
    qm.add_prompt("just answer a question, write nothing")

    engine = ExecutionEngine(qm, _RefusingClient())
    result = asyncio.run(engine.execute_queue())

    assert result.stopped_reason is None
    assert result.completed == 1

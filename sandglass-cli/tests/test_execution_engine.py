import asyncio
import time

import pytest

from sandglass.claude_client import QuotaExceededError
from sandglass.execution_engine import (
    RESET_BUFFER_SECONDS,
    SUMMARY_MAX,
    WORK_LOG_PATH,
    ExecutionEngine,
    _HOURGLASS_HALF,
    _HOURGLASS_LEVELS,
    _HOURGLASS_WIDTH,
    _TICKS_PER_LEVEL,
    _hourglass_frame,
)
from sandglass.models import Response
from sandglass.queue_manager import QueueManager
from sandglass.storage import StorageService


@pytest.fixture
def qm(tmp_path):
    return QueueManager(storage=StorageService(base_path=str(tmp_path / ".sandglass")))


class _FlakyThenOkClient:
    """Fails a specific prompt with a quota error a fixed number of times, then succeeds.

    ``resets_in`` defaults to a timestamp comfortably past ``RESET_BUFFER_SECONDS``
    (not just a fixed -60s, which the 2-minute buffer would outrun) so
    ``_compute_wait_seconds`` clamps the simulated wait to its 1-second floor
    instead of actually waiting out the real buffer padding -- keeps the test
    suite fast without weakening what's being verified.
    """

    model = "claude-opus-4-8"

    def __init__(self, fail_text: str, times: int, resets_in: float = -RESET_BUFFER_SECONDS - 60):
        self.fail_text = fail_text
        self.times = times
        self.resets_in = resets_in
        self.attempts = 0

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, model=None):
        if text == self.fail_text and self.attempts < self.times:
            self.attempts += 1
            raise QuotaExceededError(
                "quota hit",
                rate_limit_info={"status": "rejected", "resetsAt": time.time() + self.resets_in},
            )
        if on_chunk is not None:
            on_chunk("ok")
        return Response(prompt_id="", text=f"done: {text}", tokens_used=10, model=self.model)


class _AlwaysQuotaClient:
    """Always raises a quota error -- simulates a misclassified permanent failure."""

    model = "claude-opus-4-8"

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, model=None):
        raise QuotaExceededError(
            "permanently stuck",
            rate_limit_info={"status": "rejected", "resetsAt": time.time() - RESET_BUFFER_SECONDS - 60},
        )


class _AlwaysErrorClient:
    """Always raises a plain, non-quota error."""

    model = "claude-opus-4-8"

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, model=None):
        raise RuntimeError("network is down")


class _SelfLoggingClient:
    """Simulates a headless `claude -p` that writes its own work_log.md entry.

    Real headless runs edit files as a side effect of their own tool calls,
    invisible to ExecutionEngine -- from its perspective all that matters is
    whether WORK_LOG_PATH changed during ``send_prompt``, so touching it here
    is a faithful enough stand-in without needing a real subprocess.
    """

    model = "claude-opus-4-8"

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, model=None):
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

    assert sent == [
        "Sandglass: waiting for quota to refresh",
        "Sandglass: resuming",
        "Sandglass: batch complete",
    ]


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


def test_hourglass_frame_is_a_compact_rectangle_across_ticks():
    # rounded top cap + top rows + neck + bottom rows + rounded bottom cap
    expected_lines = 2 * _HOURGLASS_HALF + 3
    assert expected_lines <= 9  # compact, spinner-sized -- not a big block
    for tick in range(_HOURGLASS_LEVELS * _TICKS_PER_LEVEL):
        lines = _hourglass_frame(tick).split("\n")
        assert len(lines) == expected_lines
        assert all(len(line) == _HOURGLASS_WIDTH for line in lines)


def test_hourglass_frame_starts_full_top_empty_bottom():
    lines = _hourglass_frame(0).split("\n")  # index 0 is the rounded top cap
    top_rows = lines[1 : 1 + _HOURGLASS_HALF]
    bottom_rows = lines[_HOURGLASS_HALF + 2 : 2 * _HOURGLASS_HALF + 2]

    assert all(":" in row for row in top_rows)
    assert all(":" not in row for row in bottom_rows)


def test_hourglass_frame_ends_empty_top_full_bottom():
    last_level_tick = _HOURGLASS_HALF * _TICKS_PER_LEVEL
    lines = _hourglass_frame(last_level_tick).split("\n")
    top_rows = lines[1 : 1 + _HOURGLASS_HALF]
    bottom_rows = lines[_HOURGLASS_HALF + 2 : 2 * _HOURGLASS_HALF + 2]

    assert all(":" not in row for row in top_rows)
    assert all(":" in row for row in bottom_rows)


def test_hourglass_frame_neck_flickers_within_a_level():
    neck_row = _HOURGLASS_HALF + 1  # +1 for the rounded top cap

    frame_a = _hourglass_frame(0).split("\n")[neck_row]
    frame_b = _hourglass_frame(1).split("\n")[neck_row]

    assert frame_a != frame_b
    assert {frame_a.strip(), frame_b.strip()} == {")*(", ").("}


def test_hourglass_frame_neck_is_pinched_with_parens():
    neck_row = _HOURGLASS_HALF + 1

    neck = _hourglass_frame(0).split("\n")[neck_row]

    assert neck.strip().startswith(")")
    assert neck.strip()[:3].endswith("(")
    assert neck.strip()[1] in "*."


def test_hourglass_frame_appends_caption_to_the_neck_row_only():
    neck_row = _HOURGLASS_HALF + 1  # +1 for the rounded top cap

    lines = _hourglass_frame(0, caption="1.2 min remaining").split("\n")

    assert "1.2 min remaining" in lines[neck_row]
    assert all("min remaining" not in line for i, line in enumerate(lines) if i != neck_row)


def test_hourglass_frame_cycle_wraps_around():
    cycle_length = _HOURGLASS_LEVELS * _TICKS_PER_LEVEL

    assert _hourglass_frame(0) == _hourglass_frame(cycle_length)
    assert _hourglass_frame(3) == _hourglass_frame(3 + cycle_length)


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

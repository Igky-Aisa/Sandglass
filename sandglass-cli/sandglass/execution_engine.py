"""Execution engine — runs the queue against Claude and persists results."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

from . import notify, prompt_source
from .claude_client import ClaudeClient, QuotaExceededError
from .models import ExecutionResult, PromptObject, Response
from .queue_manager import QueueManager
from .storage import StorageService

logger = logging.getLogger(__name__)

console = Console()

# Fallback cadence when a quota hit doesn't come with an exact reset
# timestamp — matches the interval named in the original project spec.
DEFAULT_POLL_INTERVAL_SECONDS = 15 * 60
# Give the reset timestamp a safety buffer -- clocks/estimates aren't perfect,
# and retrying too close to the exact refresh moment risks hitting the quota
# again before the server-side window has actually rolled over. 2 minutes
# per explicit user request, up from an initial 30s that cut it too close.
RESET_BUFFER_SECONDS = 2 * 60
# Max length of a "what has been done" summary bullet -- mirrors how a
# prompt's own title is truncated in queue_manager.py.
SUMMARY_MAX = 80

# This project's own CLAUDE.md mandates a session report in work_log.md
# after every task -- a mandate a queued prompt's own headless `claude -p`
# run *can* satisfy on its own (it loads the same CLAUDE.md), but doesn't
# reliably: a small, quick prompt routinely judges itself not "substantial"
# enough to bother. Rather than trust that judgment call, execute_queue()
# snapshots this file before each prompt and appends a lightweight fallback
# entry after if the prompt's own execution didn't already touch it -- see
# ExecutionEngine._work_log_snapshot / _append_work_log_entry. Only ever
# activates if a master_plan/ directory already exists in the current
# working directory: Sandglass is a generic tool and shouldn't invent this
# project-specific convention for someone else's repo.
WORK_LOG_PATH = os.path.join("master_plan", "work_log.md")

# --- Quota-wait hourglass animation ---------------------------------------
# A compact, terminal-style ASCII sandglass shown in place of a static
# "waiting..." message during a quota wait, so a multi-hour pause visibly
# keeps moving instead of looking frozen -- sized like a small spinner glyph
# (a handful of lines) rather than a big block, with the live countdown
# folded into the same row instead of a separate caption line. Purely a
# liveness indicator (like any spinner): it loops continuously and isn't
# literally synced to the real remaining time, which the countdown text is.
#
# Motion follows master_plan/animation.md, i.e. how a real hourglass reads:
#   * the TOP chamber empties from its surface DOWN -- the widest row (just
#     under the top cap) clears first, not the row at the neck. The earlier
#     version drained it neck-first, which is what made the loop look wrong.
#   * a THIN STREAM of grains falls through the empty part of the bottom
#     cone, one grain every other row, shifting down each tick, so the glass
#     reads as "flowing" between level changes.
#   * the BOTTOM chamber fills from the BOTTOM UP, one grain at a time, as a
#     heap growing outward from the centre of the lowest row -- so most
#     frames show a PARTIALLY filled row rather than whole rows snapping full.
# The rounded caps are the glass itself, not sand: the bottom line stays an
# unbroken `╰─────╯` in every frame and the heap piles up ON it. (The spec
# drawing fills its own base, `(_________)` -> `(:::::::::)`, only because
# plain ASCII can't draw a rounded cap *and* sand sitting on top of it.)
_HOURGLASS_WIDTH = 7
_HOURGLASS_HALF = 3  # rows per triangle half -- animation is 9 lines total
# (rounded cap + 3 top + neck + 3 bottom + rounded cap), a compact block.
# Interior width of each cone row, widest first: (5, 3, 1) at width 7. The
# top drains and the bottom fills in that same order -- widest row first --
# so the two halves hold exactly the same amount of sand and the pour is
# conserved rather than merely mirrored.
_ROW_CELLS = tuple(_HOURGLASS_WIDTH - 2 - 2 * i for i in range(_HOURGLASS_HALF))
_BOTTOM_CELLS = sum(_ROW_CELLS)  # grains the bottom cone holds, one per cell
_SUBSTEPS_PER_LEVEL = 2  # a top row goes full (':') -> half ('.') -> empty
_TOP_SUBSTEPS = _HOURGLASS_HALF * _SUBSTEPS_PER_LEVEL
# The two halves move at different granularities (6 half-rows up top, 9
# grains down below), so progress is counted in a shared unit that divides
# both; each side then advances on its own beat without drifting out of step
# or running out early.
_PROGRESS_UNITS = math.lcm(_TOP_SUBSTEPS, _BOTTOM_CELLS)
_TICKS_PER_UNIT = 1
# A few still ticks at the end (poured out, nothing falling) so the loop's
# wrap reads as the glass being flipped rather than as a glitch mid-pour.
_SETTLE_TICKS = 3
_CYCLE_TICKS = _PROGRESS_UNITS * _TICKS_PER_UNIT + _SETTLE_TICKS
_FALL_PHASES = 2  # grain every other row; the pattern shifts by one per tick
# Slower than a spinner on purpose: this marks a wait measured in minutes or
# hours, so a full pour takes _CYCLE_TICKS * this (~10s) instead of ~3s.
ANIMATION_TICK_SECONDS = 0.5

_SAND_FULL = ":"
_SAND_LOOSE = "."  # a half-drained top row, or a grain still on the move
_SAND_EMPTY = " "


def _hourglass_frame(tick: int, caption: str = "") -> str:
    """One frame of the looping ASCII hourglass, keyed only by a tick counter.

    `tick` increases once per animation step and wraps forever. The falling
    stream shifts one row per tick; the top chamber thins by half a row every
    third tick, and a grain lands on the bottom heap every second one.

    The neck is rendered as `)*(` / `).(` -- the parens read as the pinched
    waist of the glass, with a flickering grain between them for a "still
    flowing" look even while the level itself is unchanged. `caption`, if
    given, is appended to the neck row (e.g. a live countdown) so the whole
    indicator stays a single compact block, not two.
    """
    step = tick % _CYCLE_TICKS
    poured = min(step // _TICKS_PER_UNIT, _PROGRESS_UNITS)
    # The trailing ticks: everything has poured, nothing is falling.
    settled = step >= _PROGRESS_UNITS * _TICKS_PER_UNIT
    phase = step % _FALL_PHASES

    top_substeps = poured * _TOP_SUBSTEPS // _PROGRESS_UNITS
    rows_drained = top_substeps // _SUBSTEPS_PER_LEVEL
    half_drained = (top_substeps % _SUBSTEPS_PER_LEVEL) == 1
    grains = poured * _BOTTOM_CELLS // _PROGRESS_UNITS

    center = _HOURGLASS_WIDTH // 2

    def bounds(i: int) -> tuple[int, int]:
        return i, _HOURGLASS_WIDTH - 1 - i

    def top_sand(row: int) -> str:
        """Top chamber, row 0 = widest (at the cap) = first to empty."""
        if row < rows_drained:
            return _SAND_EMPTY
        if row == rows_drained and half_drained:
            return _SAND_LOOSE
        return _SAND_FULL

    def heap(row: int) -> tuple[set[int], int | None]:
        """Columns of settled sand in a bottom row, plus the newest grain.

        Rows fill widest-first (row 0 rests on the base line), and within a
        row the heap grows outward from the centre -- so a row in progress
        is drawn as a partial mound instead of all-or-nothing.
        """
        width = _ROW_CELLS[row]
        below = sum(_ROW_CELLS[:row])
        filled = max(0, min(width, grains - below))
        if filled == 0:
            return set(), None
        edge = row + 1  # first interior column of this row
        cols = set(range(edge + (width - filled) // 2, edge + (width + filled) // 2))
        if below + filled != grains:
            return cols, None  # fully settled; a row above holds the newest grain
        previous = edge + (width - filled + 1) // 2
        return cols, next(iter(cols - set(range(previous, previous + filled - 1))), None)

    def has_grain(rows_below_neck: int) -> bool:
        return not settled and (rows_below_neck + phase) % _FALL_PHASES == 0

    lines = ["╭" + "─" * (_HOURGLASS_WIDTH - 2) + "╮"]

    for i in range(_HOURGLASS_HALF):
        left, right = bounds(i)
        row = [" "] * _HOURGLASS_WIDTH
        row[left], row[right] = "\\", "/"
        fill = top_sand(i)
        for c in range(left + 1, right):
            row[c] = fill
        lines.append("".join(row))

    neck = [" "] * _HOURGLASS_WIDTH
    neck[center - 1] = ")"
    neck[center] = "." if settled else ("*" if phase == 0 else ".")
    neck[center + 1] = "("
    neck_line = "".join(neck)
    if caption:
        neck_line += f"  {caption}"
    lines.append(neck_line)

    # Bottom cone, drawn top-down (i counts back from the neck) but filled
    # bottom-up, so this loop runs in reverse of the fill order.
    for i in reversed(range(_HOURGLASS_HALF)):
        left, right = bounds(i)
        row = [" "] * _HOURGLASS_WIDTH
        row[left], row[right] = "/", "\\"
        cols, newest = heap(i)
        for c in cols:
            row[c] = _SAND_FULL
        # The grain that just landed is still loose; by the settle frames
        # everything has come to rest.
        if newest is not None and not settled:
            row[newest] = _SAND_LOOSE
        # The stream only shows where there is still air: once the heap
        # reaches a row's centre it has buried the channel.
        if center not in cols and has_grain(_HOURGLASS_HALF - 1 - i):
            row[center] = _SAND_LOOSE
        lines.append("".join(row))

    lines.append("╰" + "─" * (_HOURGLASS_WIDTH - 2) + "╯")
    return "\n".join(lines)


def _epoch_to_iso(epoch_seconds: int | float | None) -> str | None:
    if epoch_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _local_time_str(iso_str: str | None) -> str:
    """Render a UTC ISO timestamp as a local wall-clock time for humans.

    Notification text only -- a reset time is read off a phone at a glance,
    where "03:15" answers "how long am I waiting" and a UTC ISO string doesn't.
    """
    epoch = _iso_to_epoch(iso_str)
    if epoch is None:
        return "an unknown time"
    try:
        return datetime.fromtimestamp(epoch).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return "an unknown time"


def _iso_to_epoch(iso_str: str | None) -> float | None:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str).timestamp()
    except ValueError:
        return None


class ExecutionEngine:
    """Executes queued prompts sequentially, saving responses and history."""

    def __init__(
        self,
        queue_manager: QueueManager,
        claude_client: ClaudeClient,
        storage: StorageService | None = None,
    ):
        self.queue_manager = queue_manager
        self.claude_client = claude_client
        self.storage = storage or queue_manager.storage

    # --- Public API -------------------------------------------------------

    async def execute_queue(self) -> ExecutionResult:
        """Execute every queued prompt in order.

        Each successful prompt is durably removed from the queue as soon as it
        completes, so an interruption (crash, quota hit, network error) leaves
        the remaining prompts intact for a later re-run rather than losing them.
        """
        prompts = self.queue_manager.load_queue()
        if not prompts:
            console.print("No prompts to execute")
            return ExecutionResult()

        total = len(prompts)
        results: list[Response] = []
        total_tokens = 0
        failed = 0
        stopped_reason: str | None = None
        resume_at: str | None = None
        start = time.monotonic()
        logger.info("Starting queue execution (%d prompt(s))", total)

        for i, prompt in enumerate(prompts):
            console.print(f"[bold][{i + 1}/{total}][/bold] {prompt.title}")
            work_log_before = self._work_log_snapshot()
            try:
                response = await self.execute_prompt(prompt)
            except QuotaExceededError as exc:
                failed += 1
                stopped_reason = "quota"
                resume_at = _epoch_to_iso(exc.resets_at)
                logger.error("Quota hit on prompt %s: %s", prompt.id, exc)
                console.print("  [red]✖ Quota limit reached.[/red]")
                if resume_at:
                    console.print(f"  [yellow]Expected to refresh at {resume_at}.[/yellow]")
                console.print(
                    "  [yellow]Stopping. Remaining prompts stay in the queue "
                    "for a later `sandglass execute`.[/yellow]"
                )
                if prompt.origin_file:
                    self._note_interruption(prompt, resume_at)
                # Fired here, at the point of detection, rather than in
                # run_with_auto_resume -- a quota hit under `--once` is exactly
                # as worth knowing about, and that path never reaches the
                # auto-resume loop at all.
                still_queued = total - i
                when = f" Expected to refresh around {_local_time_str(resume_at)}." if resume_at else ""
                notify.send(
                    f"Token limits hit.{when} {still_queued} prompt(s) still queued.",
                    title="Sandglass: token limits hit",
                )
                break
            except Exception as exc:  # noqa: BLE001 — surface any API/network error gracefully
                failed += 1
                stopped_reason = "error"
                logger.error("Prompt %s failed: %s", prompt.id, exc)
                console.print(f"  [red]✖ Failed: {exc}[/red]")
                console.print(
                    "  [yellow]Stopping. Remaining prompts stay in the queue "
                    "for a later `sandglass execute`.[/yellow]"
                )
                break

            results.append(response)
            total_tokens += response.tokens_used
            self._archive_prompt(prompt, response)
            if work_log_before is not None and self._work_log_snapshot() == work_log_before:
                # The prompt's own headless run didn't touch work_log.md itself
                # (e.g. too small a task to bother) -- log it mechanically so
                # the project's "every task" mandate still holds.
                self._append_work_log_entry(prompt, response)
            # Durably drop this prompt from the front of the persisted queue.
            self.queue_manager.remove_prompt(1)
            if prompt.origin_file:
                self._cut_from_source(prompt, response)

        elapsed = time.monotonic() - start
        logger.info(
            "Queue execution finished (completed=%d, failed=%d, tokens=%d, seconds=%.1f, stopped_reason=%s)",
            len(results), failed, total_tokens, elapsed, stopped_reason,
        )
        self._print_summary(results, failed, total_tokens, elapsed, stopped_reason)

        return ExecutionResult(
            completed=len(results),
            failed=failed,
            total_tokens=total_tokens,
            total_time=elapsed,
            responses=results,
            stopped_reason=stopped_reason,
            resume_at=resume_at,
        )

    async def run_with_auto_resume(
        self,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        max_stalls: int = 5,
    ) -> ExecutionResult:
        """Run the queue to completion, automatically waiting out quota hits.

        This is what makes an unattended `sandglass execute` actually worth
        more than pasting the same prompt into a chat window yourself: instead
        of stopping the moment the subscription's usage limit is hit,
        it sleeps until the reported reset time (falling back to
        ``poll_interval`` if no exact timestamp was reported) and retries —
        repeating until the queue is empty or something other than a quota
        limit stops it. A non-quota error (bad prompt, network failure, etc.)
        is never auto-retried, since blindly looping on those wouldn't help.

        ``max_stalls`` guards against a misclassified, permanently-failing
        "quota" error spinning forever: if the same prompt is still stuck at
        the front of the queue after that many consecutive attempts, this
        gives up and tells the user to look at it manually.
        """
        last_result = ExecutionResult()
        stalls = 0

        while True:
            prompts_before = self.queue_manager.load_queue()
            if not prompts_before:
                return last_result

            last_result = await self.execute_queue()

            remaining = self.queue_manager.load_queue()
            if not remaining:
                notify.send(
                    f"{last_result.completed} prompt(s) executed, "
                    f"{last_result.total_tokens:,} tokens used.",
                    title="Sandglass: batch complete",
                )
                return last_result
            if last_result.stopped_reason != "quota":
                # Non-quota failure -- don't auto-retry, the user needs to look at it.
                notify.send(
                    f"Stopped after a non-quota error. {last_result.completed} prompt(s) "
                    f"executed, {len(remaining)} still queued for a later run.",
                    title="Sandglass: batch stopped early",
                    priority="high",
                )
                return last_result

            made_progress = last_result.completed > 0 or remaining[0].id != prompts_before[0].id
            stalls = 0 if made_progress else stalls + 1
            if stalls >= max_stalls:
                console.print(
                    f"[red]Auto-resume stopped: prompt {remaining[0].id} has hit the same "
                    f"'quota' error {stalls} times in a row without making progress — this "
                    "may not actually be a quota issue. Investigate manually, then re-run "
                    "`sandglass execute`.[/red]"
                )
                notify.send(
                    f"Gave up after prompt {remaining[0].id} hit the same 'quota' error "
                    f"{stalls} times without progress -- may not actually be quota. "
                    f"{len(remaining)} prompt(s) still queued.",
                    title="Sandglass: batch stopped early",
                    priority="high",
                )
                return last_result

            # No notification here: execute_queue already sent "token limits
            # hit" the moment the quota was detected, a second ago. Two pushes
            # for one event is just noise on the phone.
            wait_seconds = self._compute_wait_seconds(last_result.resume_at, poll_interval)
            try:
                await self._sleep_with_animation(wait_seconds, last_result.resume_at)
            except asyncio.CancelledError:
                raise
            notify.send(
                f"Quota refreshed -- resuming with {len(remaining)} prompt(s) still queued.",
                title="Sandglass: resuming",
            )

    @staticmethod
    def _compute_wait_seconds(resume_at_iso: str | None, fallback: float) -> float:
        target_epoch = _iso_to_epoch(resume_at_iso)
        if target_epoch is None:
            return fallback
        remaining = (target_epoch + RESET_BUFFER_SECONDS) - time.time()
        return max(remaining, 1.0)

    @staticmethod
    async def _sleep_with_animation(total_seconds: float, resume_at: str | None) -> None:
        """Sleep out a quota wait behind a live ASCII hourglass animation.

        In a real terminal this redraws in place (`transient=True` clears it
        on exit, matching `execute_prompt`'s progress spinner); piped to a
        file or captured by a test, Rich's `Live` detects the non-terminal
        output and prints nothing per frame -- verified directly rather than
        assumed, since a multi-hour wait ticking every ANIMATION_TICK_SECONDS
        would otherwise flood a redirected log with thousands of frames.
        """
        when = f" (expected around {resume_at})" if resume_at else ""
        console.print(
            f"[yellow]Waiting {total_seconds / 60:.1f} min for quota to refresh{when}. "
            "Press Ctrl-C to stop (queue stays intact).[/yellow]"
        )

        deadline = time.monotonic() + total_seconds
        tick = 0
        with Live(console=console, auto_refresh=False, transient=True) as live:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                caption = f"{remaining / 60:.1f} min remaining"
                live.update(Text(_hourglass_frame(tick, caption), style="yellow"))
                live.refresh()
                tick += 1
                await asyncio.sleep(min(ANIMATION_TICK_SECONDS, remaining))
        console.print("[green]⌛ Resuming — retrying the queue now.[/green]")

    async def execute_prompt(self, prompt: PromptObject) -> Response:
        """Send a single prompt to Claude Code with a live progress spinner."""
        effective_model = prompt.model or self.claude_client.model
        console.print(f"  📤 Sending to Claude ({effective_model})...")

        start = time.monotonic()
        chars_received = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("  {task.description}"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            # Streamed output length isn't known upfront (unlike a fixed
            # max_tokens budget), so this shows a live character count
            # instead of a fake percentage.
            task = progress.add_task("Working...", total=None)

            def on_chunk(piece: str) -> None:
                nonlocal chars_received
                chars_received += len(piece)
                progress.update(task, description=f"{chars_received:,} chars received")

            response = await self.claude_client.send_prompt(
                prompt.text, on_chunk, model=prompt.model
            )

        response.prompt_id = prompt.id
        elapsed_min = (time.monotonic() - start) / 60
        console.print(
            f"  ✅ Done ({response.tokens_used:,} tokens, {elapsed_min:.1f} min)"
        )

        self._save_response(prompt, response)
        return response

    # --- Persistence helpers ---------------------------------------------

    def _save_response(self, prompt: PromptObject, response: Response) -> None:
        """Save a response to .sandglass/responses/response_<id>.json."""
        try:
            self.storage.ensure_dir(self.storage.responses_dir)
            path = os.path.join(
                self.storage.responses_dir, f"response_{prompt.id}.json"
            )
            payload = {
                "prompt_id": prompt.id,
                "prompt_text": prompt.text,
                "response_text": response.text,
                "tokens_used": response.tokens_used,
                "completion_time": response.completion_time,
                "model": response.model,
            }
            self.storage.save_json(path, payload)
        except OSError as exc:
            logger.error("Failed to save response for prompt %s: %s", prompt.id, exc)
            console.print(f"  [yellow]Warning: could not save response file: {exc}[/yellow]")

    def _archive_prompt(self, prompt: PromptObject, response: Response) -> None:
        """Append a completed prompt+response record to history.json."""
        try:
            history = self.storage.load_json(self.storage.history_path)
            completed = history.get("completed", []) if isinstance(history, dict) else []
            completed.append(
                {
                    "prompt": prompt.to_dict(),
                    "response": {
                        "prompt_id": prompt.id,
                        "response_text": response.text,
                        "tokens_used": response.tokens_used,
                        "model": response.model,
                    },
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "tokens_used": response.tokens_used,
                }
            )
            self.storage.save_json(self.storage.history_path, {"completed": completed})
        except OSError as exc:
            logger.error("Failed to archive prompt %s: %s", prompt.id, exc)

    def _cut_from_source(self, prompt: PromptObject, response: Response) -> None:
        """Remove a completed prompt's block from its markdown queue source.

        Mirrors the manual "future prompts" convention: cut the executed
        block out of the source file and archive it into that file's sibling
        prompt_history.md, newest first. Only called for prompts loaded via
        :meth:`QueueManager.import_from_markdown` (``prompt.origin_file`` set)
        — prompts added directly via `queue add` never touch this.
        """
        try:
            cut_text = prompt_source.cut_first_block(prompt.origin_file)
            if cut_text is None:
                logger.warning(
                    "Expected a block to cut from %s for prompt %s, found none",
                    prompt.origin_file, prompt.id,
                )
                return
            history_path = prompt_source.history_path_for(prompt.origin_file)
            prompt_source.prepend_to_history(history_path, prompt.title, response.model, cut_text)
        except OSError as exc:
            logger.error(
                "Failed to cut completed prompt %s out of %s: %s",
                prompt.id, prompt.origin_file, exc,
            )
            console.print(
                f"  [yellow]Warning: could not update {prompt.origin_file}: {exc}[/yellow]"
            )

    def _note_interruption(self, prompt: PromptObject, resume_at: str | None) -> None:
        """Record that a markdown-sourced prompt was interrupted by a quota hit.

        Unlike `_cut_from_source`, this never removes the block from
        ``prompt.origin_file`` -- the prompt stays exactly where it was,
        still at the front of the queue, so `run_with_auto_resume` retries
        it in full once the quota refreshes. This only leaves a visible
        marker in the sibling history file so a long wait between "started"
        and "completed" isn't silent in the audit trail. Only called for
        markdown-sourced prompts (``prompt.origin_file`` set), mirroring
        `_cut_from_source`'s own scope -- prompts added via `queue add` have
        no history file to note this in.
        """
        try:
            history_path = prompt_source.history_path_for(prompt.origin_file)
            prompt_source.append_interruption_note(history_path, prompt.title, resume_at)
        except OSError as exc:
            logger.error(
                "Failed to note interruption of prompt %s in %s: %s",
                prompt.id, prompt.origin_file, exc,
            )

    @staticmethod
    def _work_log_snapshot() -> tuple[float, int] | None:
        """(mtime, size) of WORK_LOG_PATH, or None if master_plan/ isn't present.

        None means "this isn't a project using the work_log.md convention" --
        the caller should skip auto-logging entirely rather than create the
        directory/file. A missing *file* inside an existing master_plan/ dir
        still returns a (sentinel) snapshot, since that's a normal first-entry
        case, not a reason to skip.
        """
        if not os.path.isdir(os.path.dirname(WORK_LOG_PATH)):
            return None
        try:
            stat = os.stat(WORK_LOG_PATH)
            return (stat.st_mtime, stat.st_size)
        except OSError:
            return (0.0, 0)

    def _append_work_log_entry(self, prompt: PromptObject, response: Response) -> None:
        """Mechanically append a fallback work_log.md entry for one completed prompt.

        Deliberately lighter than -- and clearly labeled as distinct from --
        the rich narrative report an interactive agent (or a headless prompt
        that decided the task warranted one) would write by hand: this has
        only the prompt text, a one-line response summary, and basic
        metadata to work with, not real judgment about *why* something was
        done. It exists purely so the project's "every task" mandate holds
        even when nothing richer got written.
        """
        try:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            summary = self._summarize_response(response.text)
            entry = (
                f"\n## {date} - sandglass execute ({response.model}) - {prompt.title}\n\n"
                "### 1. Context Snapshot\n"
                f"- **Goal**: {prompt.title}\n"
                "- **State**: Auto-logged by `sandglass execute` -- this prompt's own "
                "headless run didn't write its own work_log.md entry, so this fallback "
                "stands in for it.\n"
                "- **Previous Blocker**: N/A\n\n"
                "### 2. Work Done\n"
                f"- {summary}\n"
                f"- {response.tokens_used:,} tokens used\n\n"
                "### 3. Next Steps (For the next agent)\n"
                f"- Auto-logged entry, not a full report -- see "
                f"`.sandglass/responses/response_{prompt.id}.json` for the full response "
                "if more detail is needed.\n"
            )
            os.makedirs(os.path.dirname(WORK_LOG_PATH), exist_ok=True)
            with open(WORK_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(entry)
        except OSError as exc:
            logger.error("Failed to append work_log.md entry for prompt %s: %s", prompt.id, exc)
            console.print(f"  [yellow]Warning: could not update {WORK_LOG_PATH}: {exc}[/yellow]")

    # --- Output -----------------------------------------------------------

    @staticmethod
    def _summarize_response(text: str) -> str:
        """Reduce a response to one short "what was done" bullet.

        Takes the first non-empty line, truncated -- Claude's own responses
        already tend to open with a concise statement of what it did
        ("Done. Appended ...", "Fixed ...", "Created ..."), so this needs no
        extra summarization call.
        """
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not first_line:
            return "(no summary available)"
        return first_line if len(first_line) <= SUMMARY_MAX else first_line[: SUMMARY_MAX - 1] + "…"

    def _print_summary(
        self,
        results: list[Response],
        failed: int,
        total_tokens: int,
        elapsed: float,
        stopped_reason: str | None = None,
    ) -> None:
        console.print()
        heading = "Queue Complete!" if not stopped_reason else "Batch Paused"
        console.print(f"[bold]{heading}[/bold]")
        console.print(f"  ✅ {len(results)} prompt(s) executed")
        if failed:
            console.print(f"  ✖ {failed} failed")
        console.print(f"  💰 {total_tokens:,} tokens used")
        console.print(f"  ⏱️ {elapsed / 60:.1f} minutes")
        if results:
            console.print("  what has been done:")
            for response in results:
                console.print(f"    - {self._summarize_response(response.text)}")
        console.print(f"  📁 Responses saved to {self.storage.responses_dir}")

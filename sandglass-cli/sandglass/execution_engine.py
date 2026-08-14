"""Execution engine — runs the queue against Claude and persists results."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
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

from . import notify, project_docs, prompt_source, providers, run_report, workspace
from .accounts import AccountPool
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

# How prompts map onto Claude Code sessions.
#
# `chain` is the default because Sandglass's whole job is running a queue
# unattended, and a queue run as N cold starts pays every fixed cost N times:
# the system prompt, the tool schemas, the project's CLAUDE.md, and whatever
# files the previous block already read and understood. Measured on a real
# project, that floor was ~24k tokens of prefix plus ~100k of mandated reading
# *before any block did work of its own*. Chained, the queue pays it once and
# every later block reads it back from cache at a fraction of the price.
#
# The cost of chaining is that blocks are no longer hermetic: block 6 can see
# what block 5 did, including block 5's mistakes. That is mitigated (a turn
# separator marks each new task, and a single block can opt out with
# `isolate: true`) rather than eliminated, so the other two modes stay
# available for queues where independence matters more than cost.
SESSION_MODE_CHAIN = "chain"      # one session for the whole queue drain
SESSION_MODE_PROMPT = "prompt"    # one session per prompt; retries resume, blocks don't share
SESSION_MODE_ISOLATE = "isolate"  # no persisted session at all (pre-0.10 behaviour)
SESSION_MODES = (SESSION_MODE_CHAIN, SESSION_MODE_PROMPT, SESSION_MODE_ISOLATE)

# How long a chained session may sit IDLE and still be worth rejoining.
#
# Tied to the prompt cache's lifetime (1h at the longest), because that is what
# decides the economics. Resume inside it and the whole conversation is a cache
# *read* at ~0.1x. Resume after it has expired and the same conversation is a
# cache *write* at 1.25-2x -- and it is the accumulated conversation, which
# grows with every block, whereas a cold start's prefix does not. Measured on a
# real run: resuming a session hours old billed 741k cache-write tokens ($4.92)
# to produce 1,908 tokens of output. A cold start would have cost a fraction.
#
# Measured from LAST USE, not from when the chain opened: a queue that has been
# working steadily for eight hours has a hot cache and should stay on it. It is
# the gap that makes a session cold, not its age.
CHAIN_MAX_AGE_SECONDS = 60 * 60

# What to do when a block returns a response but changes no file.
ON_REFUSAL_STOP = "stop"  # stop the run and let a human look (pre-0.11 behaviour)
ON_REFUSAL_ASK = "ask"    # ask the run itself what happened, then decide
ON_REFUSAL_MODES = (ON_REFUSAL_ASK, ON_REFUSAL_STOP)

# One cheap follow-up turn in the session that just refused. It is a question,
# never an instruction to try again: the response already explains itself in
# prose, and prose is exactly what an automated decision cannot act on. Three
# words, because three different situations look identical from outside and
# want opposite responses -- and the run that just refused is the only party
# that already knows which one it is.
REFUSAL_QUERY = (
    "---\n"
    "Sandglass here, not a new task. Your last response changed no file in the "
    "working tree, so the block was NOT cut from the queue file.\n\n"
    "Answer in this exact shape, nothing else:\n"
    "  VERDICT: DONE | BLOCKED | NOOP\n"
    "  WHY: <one line>\n\n"
    "  DONE    — the work was already complete before you started.\n"
    "  BLOCKED — you could not do it; name precisely what is missing.\n"
    "  NOOP    — you did the task and it genuinely required no file changes.\n"
)
_VERDICT_RE = re.compile(r"VERDICT:\s*(DONE|BLOCKED|NOOP)", re.IGNORECASE)
_WHY_RE = re.compile(r"WHY:\s*(.+)", re.IGNORECASE)

# Prepended to every block after the first in a chained session. Without it the
# model reads a new block as a continuation of the last one and keeps working
# on the previous task; with it, blocks stay distinct for ~40 tokens, against
# the several thousand a cold start would cost to achieve the same separation.
CHAIN_TURN_SEPARATOR = (
    "---\n"
    "The previous task is finished. What follows is a **new, independent task** "
    "in the same project. Earlier context in this session is available if it "
    "helps, but do not resume, extend, or redo earlier work unless this task "
    "explicitly asks for it. Treat anything the previous task got wrong as "
    "history, not as your problem to fix.\n"
    "---\n"
)

# Bumped when the meaning of a recorded token count changes. Schema 1 summed
# only input+output, which omitted cache tokens entirely and so undercounted
# any cached run severely; schema 2 counts everything billed and records cost.
# History entries carry this so the two are never compared as if equivalent.
ACCOUNTING_SCHEMA = 2

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
_HOURGLASS_ROWS = 2 * _HOURGLASS_HALF + 3  # cap + half + neck + half + cap
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

# --- "SANDGLASS" banner shown above the hourglass while waiting -----------
# A hand-drawn 5-row block font, deliberately covering only the letters this
# one word needs (S A N D G L) rather than a general alphabet -- there is no
# other caller. Widths are per-letter, not fixed: each glyph's own rows must
# be equal-length (checked once at import time below), but different letters
# may differ from each other.
_BANNER_TEXT = "SANDGLASS"
_BANNER_FONT: dict[str, tuple[str, str, str, str, str]] = {
    "S": (" █████", "█     ", " ████ ", "     █", "█████ "),
    "A": (" ███  ", "█   █ ", "██████", "█   █ ", "█   █ "),
    "N": ("█   █", "██  █", "█ █ █", "█  ██", "█   █"),
    "D": ("████ ", "█   █", "█   █", "█   █", "████ "),
    "G": (" ████", "█    ", "█  ██", "█   █", " ████"),
    "L": ("█    ", "█    ", "█    ", "█    ", "█████"),
}
# The tagline shown under the banner+hourglass row while waiting.
_BANNER_SIGNATURE = "Sandglass is an open-source CLI app for developers, by Igky Aisa."
# Columns between the banner and the hourglass when they sit side by side.
_BANNER_GAP = "   "


def _render_banner(word: str) -> str:
    """The word rendered in :data:`_BANNER_FONT`, one letter beside the next."""
    glyphs = [_BANNER_FONT[ch] for ch in word]
    return "\n".join(" ".join(g[row] for g in glyphs) for row in range(5))


def _pad_to_height(lines: list[str], height: int) -> list[str]:
    """Vertically center a block of equal-width lines inside ``height`` rows.

    Padding is blank lines the same width as the block, split as evenly as
    possible above and below (any odd row goes to the bottom). Exists because
    the banner (5 rows) and the hourglass (:data:`_HOURGLASS_ROWS`, 9) sit side
    by side and don't share a height on their own -- without this the shorter
    block would hug the top of the row and look pinned above the glass rather
    than beside it.
    """
    width = len(lines[0]) if lines else 0
    blank = " " * width
    pad_top = max(0, (height - len(lines)) // 2)
    pad_bottom = max(0, height - len(lines) - pad_top)
    return [blank] * pad_top + list(lines) + [blank] * pad_bottom


# Fails fast at import if a glyph's rows ever drift out of alignment, rather
# than shipping a banner that looks broken only once it's actually printed to
# a terminal mid-wait, hours into an unattended run.
for _ch, _glyph in _BANNER_FONT.items():
    assert len(_glyph) == 5, f"banner glyph {_ch!r} must have 5 rows"
    assert len({len(_row) for _row in _glyph}) == 1, f"banner glyph {_ch!r} rows must be equal width"
del _ch, _glyph


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
        require_artifact: bool = True,
        session_mode: str = SESSION_MODE_CHAIN,
        brief: bool = True,
        skip_executed: bool = True,
        on_refusal: str = ON_REFUSAL_ASK,
        account_pool: "AccountPool | None" = None,
        provider_registry: "providers.ProviderRegistry | None" = None,
        allow_external: bool = True,
    ):
        self.queue_manager = queue_manager
        self.claude_client = claude_client
        self.storage = storage or queue_manager.storage
        # Several subscriptions the run may draw on, one at a time. None means
        # single-account mode: a quota hit waits for the window to refresh,
        # exactly as it did before rotation existed. See accounts.py.
        self.account_pool = account_pool
        if account_pool is not None:
            # Re-apply what an earlier run learned before choosing a starting
            # account, so a restart doesn't spend a block rediscovering that
            # account one is still spent. load_state() advances past any
            # account that is still inside its window.
            account_pool.state_path = self.storage.accounts_state_path
            account_pool.load_state()
            if account_pool.current is not None:
                self.claude_client.auth_token = account_pool.current.token
        # Refuse to cut a markdown block out of its source file when the prompt
        # changed nothing on disk. See sandglass/workspace.py for why this exists
        # and what it cost to learn. Only meaningful for markdown-sourced prompts;
        # `queue add` prompts have no source file to protect.
        self.require_artifact = require_artifact
        # How prompts map onto Claude Code sessions -- see SESSION_MODES.
        if session_mode not in SESSION_MODES:
            raise ValueError(
                f"session_mode must be one of {', '.join(SESSION_MODES)}, got {session_mode!r}"
            )
        self.session_mode = session_mode
        # Prepend a bounded project-state brief instead of letting each cold
        # block re-read the whole work log. See sandglass/project_docs.py.
        self.brief = brief
        # Drop queued blocks that some other runner already executed and cut.
        # See prompt_source.already_executed for what counts as evidence.
        self.skip_executed = skip_executed
        if on_refusal not in ON_REFUSAL_MODES:
            raise ValueError(
                f"on_refusal must be one of {', '.join(ON_REFUSAL_MODES)}, got {on_refusal!r}"
            )
        # Whether a block that changed no file gets asked what happened before
        # the run gives up on the whole queue. See _ask_why_nothing_changed.
        self.on_refusal = on_refusal
        # API keys for non-Anthropic endpoints a block may ask to be routed to.
        # Empty registry == none configured, which simply means every block
        # runs on Anthropic. See providers.py.
        self.provider_registry = provider_registry
        # Kill switch for a run: `--no-external` forces every block onto
        # Anthropic regardless of its marker. For the night you'd rather spend
        # subscription quota than send anything to a third party.
        self.allow_external = allow_external
        # Providers already reported as unusable this run, so a queue with
        # twenty externally-marked blocks says so once instead of twenty times.
        self._provider_warned: set[str] = set()

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
        total_cost = 0.0
        total_cache_read = 0
        total_cache_creation = 0
        failed = 0
        skipped = 0
        # Blocks the run reported as needing no file changes. They stay in the
        # queue and in their source file for a human to confirm.
        left_in_place: list[tuple[PromptObject, str, str]] = []
        stopped_reason: str | None = None
        stopped_detail = ""
        resume_at: str | None = None
        start = time.monotonic()
        logger.info("Starting queue execution (%d prompt(s))", total)

        # Written before any work and updated per prompt, so that a run killed
        # from outside -- closed terminal, sleeping laptop -- still leaves
        # behind what it was doing. See sandglass/run_report.py.
        report = run_report.RunReport(remaining=total)
        run_report.save(self.storage, report)

        for i, prompt in enumerate(prompts):
            console.print(f"[bold][{i + 1}/{total}][/bold] {prompt.title}")
            report.status = run_report.STATUS_RUNNING
            report.prompt_id = prompt.id
            report.prompt_title = prompt.title
            report.completed = len(results)
            report.remaining = total - i
            report.total_tokens = total_tokens
            report.total_cost_usd = total_cost
            run_report.save(self.storage, report)

            # Free pre-flight: has someone already run and cut this block? The
            # queue and the markdown drift (an interactive session cuts a block;
            # a run dies between the cut and the queue update), and dispatching
            # finished work costs a full block to be told it was finished. Only
            # ever fires on positive evidence -- see prompt_source.already_executed.
            if (
                self.skip_executed
                and prompt.origin_file
                and prompt_source.already_executed(prompt.origin_file, prompt.text)
            ):
                console.print(
                    "  [dim]↷ Already executed by someone else — the block is gone "
                    f"from {prompt.origin_file} and recorded in its history file. "
                    "Dropping it from the queue without spending a token.[/dim]"
                )
                logger.info(
                    "Prompt %s was already executed and cut from %s; skipping",
                    prompt.id, prompt.origin_file,
                )
                skipped += 1
                self.queue_manager.remove_prompt_entry(prompt)
                continue

            work_log_before = self._work_log_snapshot()
            # Only measured for markdown-sourced prompts: the cut is the only
            # irreversible step, and `queue add` prompts have no block to cut.
            # Anchored at the directory holding the markdown queue source, not the
            # process CWD: that is the repo whose blocks are at stake, and it keeps
            # the measurement correct even when sandglass is invoked from elsewhere.
            workspace_before = (
                workspace.workspace_fingerprint(
                    cwd=os.path.dirname(os.path.abspath(prompt.origin_file)) or None
                )
                if (self.require_artifact and prompt.origin_file)
                else None
            )
            try:
                response = await self._execute_with_rotation(prompt)
            except QuotaExceededError as exc:
                failed += 1
                stopped_reason = "quota"
                stopped_detail = str(exc)
                # With a pool, every account is spent by the time this is
                # reached -- so the run resumes when the *first* of them comes
                # back, which is rarely the one that raised.
                pool_reset = (
                    self.account_pool.earliest_reset() if self.account_pool else None
                )
                resume_at = _epoch_to_iso(pool_reset or exc.resets_at)
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
                stopped_detail = str(exc)
                logger.error("Prompt %s failed: %s", prompt.id, exc)
                console.print(f"  [red]✖ Failed: {exc}[/red]")
                console.print(
                    "  [yellow]Stopping. Remaining prompts stay in the queue "
                    "for a later `sandglass execute`.[/yellow]"
                )
                # Fired here, at the point of detection, same as the quota and
                # no-artifact stops above -- `--once` never reaches
                # run_with_auto_resume's wrapper, so a plain crash mid-block
                # used to reach the phone only in the default auto-resume mode.
                still_queued = total - i
                notify.send(
                    f"Stopped after an error on '{prompt.title}': {exc}. "
                    f"{still_queued} prompt(s) still queued.",
                    title="Sandglass: batch stopped early",
                    priority="high",
                )
                break

            # --- the artifact gate ---------------------------------------------
            # A response that changed nothing on disk is not a completed block.
            # Cutting it would destroy the only copy of the block text (see
            # sandglass/workspace.py), so stop instead: leave the prompt at the
            # front of the queue AND its block in the markdown, exactly like the
            # quota path does, and let a human decide what happened.
            if workspace_before is not None:
                workspace_after = workspace.workspace_fingerprint(
                    cwd=os.path.dirname(os.path.abspath(prompt.origin_file)) or None
                )
                if not workspace.produced_work_product(workspace_before, workspace_after):
                    console.print(
                        "  [red]✖ No work product: the response changed no file "
                        "in the working tree.[/red]"
                    )
                    verdict, why = await self._ask_why_nothing_changed(prompt)

                    # DONE and NOOP both mean "there was nothing to write here",
                    # so the queue can keep moving. The block is still NOT cut:
                    # cutting destroys the only copy of its text, and a model's
                    # word is not evidence -- that is exactly the mistake the
                    # artifact gate exists to prevent. It stays in the source
                    # file and in the queue for a human to confirm, and the run
                    # carries on with the blocks behind it.
                    if verdict in ("DONE", "NOOP"):
                        console.print(
                            f"  [yellow]↷ Left in place ({verdict}): {why}[/yellow]"
                        )
                        console.print(
                            f"  [dim]Not cut from {prompt.origin_file} — nothing "
                            "verifiable happened. Continuing with the next block.[/dim]"
                        )
                        logger.info(
                            "Prompt %s self-reported %s (%s); leaving it in place "
                            "and continuing", prompt.id, verdict, why,
                        )
                        left_in_place.append((prompt, verdict, why))
                        continue

                    failed += 1
                    stopped_reason = "no_artifact"
                    stopped_detail = (
                        f"{prompt.title} responded but changed no file in the "
                        f"working tree, so its block was not cut from "
                        f"{prompt.origin_file}."
                    )
                    if verdict == "BLOCKED":
                        stopped_detail += f" It reports: {why}"
                    logger.error(
                        "Prompt %s returned a response but changed nothing on disk; "
                        "refusing to cut it from %s",
                        prompt.id, prompt.origin_file,
                    )
                    if verdict == "BLOCKED":
                        console.print(f"  [red]It reports: {why}[/red]")
                    console.print(
                        f"  [yellow]Not cutting this block from {prompt.origin_file}. "
                        f"The response is saved in "
                        f"{os.path.join('.sandglass', 'responses', f'response_{prompt.id}.json')} "
                        f"— read it before re-running.[/yellow]"
                    )
                    console.print(
                        "  [yellow]Stopping: a block that refuses usually means a "
                        "dependency is missing, and the blocks after it depend on "
                        "this one.[/yellow]"
                    )
                    still_queued = total - i
                    notify.send(
                        f"'{prompt.title}' returned a response but changed no files. "
                        + (f"It reports: {why} " if why else "")
                        + f"Not cut. {still_queued} prompt(s) still queued.",
                        title="Sandglass: no work product",
                    )
                    break

            results.append(response)
            if self.account_pool is not None and not response.provider:
                # Only Anthropic traffic is charged to a Claude account. An
                # external block spends someone else's metered credit, and
                # folding it in would misreport both — inflating an account's
                # apparent burn and hiding the third-party spend entirely.
                self.account_pool.record_usage(response.tokens_used, response.cost_usd)
            total_tokens += response.tokens_used
            total_cost += response.cost_usd
            total_cache_read += response.cache_read_tokens
            total_cache_creation += response.cache_creation_tokens
            self._archive_prompt(prompt, response)
            if work_log_before is not None and self._work_log_snapshot() == work_log_before:
                # The prompt's own headless run didn't touch work_log.md itself
                # (e.g. too small a task to bother) -- log it mechanically so
                # the project's "every task" mandate still holds.
                self._append_work_log_entry(prompt, response)
            # Durably drop this prompt from the persisted queue. By id, not by
            # position: the queue file sits inside the project directory that
            # blocks write to with full tool access, so its contents can shift
            # under a long-running block. The work is already done by here --
            # a missing entry is bookkeeping to log, not a run to fail.
            self.queue_manager.remove_prompt_entry(prompt)
            if prompt.origin_file:
                self._cut_from_source(prompt, response)
                self._maybe_notify_progress(prompt)

        # Blocks left in place are still queued, so this run did not finish the
        # queue however smoothly it ran. Saying "complete" here would be the
        # kind of cheerful lie that sends someone looking for work that isn't
        # there -- and `run_with_auto_resume` would otherwise keep re-running
        # blocks that already said they had nothing to do.
        if stopped_reason is None and left_in_place:
            stopped_reason = run_report.REASON_LEFT_IN_PLACE
            stopped_detail = "; ".join(
                f"{p.id}: {verdict} — {why}" for p, verdict, why in left_in_place
            )
            # Fired here, not only from run_with_auto_resume's wrapper, for the
            # same --once-mode reason as every other stop notification above.
            notify.send(
                f"{len(left_in_place)} block(s) reported they needed no file "
                "change, so none were cut. Read them and cut by hand if you agree.",
                title="Sandglass: nothing left to build",
            )

        # The drain is over — end the chain so the next `sandglass execute`
        # opens a fresh conversation rather than appending unrelated work to
        # this one. Only on a clean finish: a run stopped by a quota hit or a
        # refusal is *not* over, and its remaining blocks must rejoin the same
        # session when it picks back up.
        if stopped_reason is None and not self.queue_manager.load_queue():
            self._clear_run_state()
            # Same reasoning: fired here so `--once` gets a "batch complete"
            # push too, not only the default auto-resume path.
            notify.send(
                f"{len(results)} prompt(s) executed, {total_tokens:,} tokens used.",
                title="Sandglass: batch complete",
            )

        elapsed = time.monotonic() - start
        logger.info(
            "Queue execution finished (completed=%d, failed=%d, tokens=%d, cost=$%.4f, "
            "cache_read=%d, cache_write=%d, seconds=%.1f, stopped_reason=%s)",
            len(results), failed, total_tokens, total_cost, total_cache_read,
            total_cache_creation, elapsed, stopped_reason,
        )
        result = ExecutionResult(
            completed=len(results),
            failed=failed,
            skipped=skipped,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            total_cache_read_tokens=total_cache_read,
            total_cache_creation_tokens=total_cache_creation,
            total_time=elapsed,
            responses=results,
            stopped_reason=stopped_reason,
            resume_at=resume_at,
        )
        report.status = (
            run_report.STATUS_STOPPED if stopped_reason else run_report.STATUS_COMPLETE
        )
        report.reason = stopped_reason or run_report.REASON_COMPLETE
        report.detail = stopped_detail
        report.completed = len(results)
        report.remaining = len(self.queue_manager.load_queue())
        report.total_tokens = total_tokens
        report.total_cost_usd = total_cost
        report.resume_at = resume_at
        if self.account_pool is not None:
            report.accounts = self.account_pool.usage_summary()
        report.ended_at = run_report.now_iso()
        run_report.save(self.storage, report)

        self._print_summary(result, elapsed, stopped_reason)
        if left_in_place:
            # Named individually: each one is a block still sitting in the queue
            # and in the source file, waiting for a human to agree it was really
            # nothing to do. A count alone would be too easy to skim past.
            console.print()
            console.print(
                f"[yellow]{len(left_in_place)} block(s) left in place — reported "
                "as needing no file change, so NOT cut:[/yellow]"
            )
            for skipped_prompt, verdict, why in left_in_place:
                console.print(f"  [yellow]· {skipped_prompt.id} ({verdict}): {why}[/yellow]")
            console.print(
                "  [dim]They stay queued. Cut them yourself if you agree, or re-run "
                "with --no-require-artifact if the whole queue is read-only.[/dim]"
            )
        console.print()
        for line in run_report.render(report):
            console.print(line)
        return result

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
                # No notification here -- execute_queue already sent "batch
                # complete" the instant it detected a clean, full drain, which
                # is true on every path that reaches this line. This check is
                # just how the loop notices it's done and can stop.
                return last_result
            if last_result.stopped_reason != "quota":
                # Non-quota failure -- don't auto-retry, the user needs to look
                # at it. No notification here either: execute_queue already
                # sent one for every stop reason it can produce -- quota,
                # no_artifact, left_in_place, error -- at the point of
                # detection. That is what makes it reach the phone under
                # `--once` too, a mode that never calls this function at all.
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
                self.record_stop(
                    run_report.REASON_STALLED,
                    f"Prompt {remaining[0].id} hit the same quota-looking error "
                    f"{stalls} times in a row without progress.",
                )
                return last_result

            # No notification here: execute_queue already sent "token limits
            # hit" the moment the quota was detected, a second ago. Two pushes
            # for one event is just noise on the phone.
            # A quota wait can run for hours, and "why is nothing happening"
            # is a fair question to ask in the middle of one -- so the record
            # says *waiting*, not *stopped*, for as long as that is true.
            waiting = run_report.load(self.storage)
            if waiting is not None:
                waiting.status = run_report.STATUS_WAITING
                waiting.pid = os.getpid()
                waiting.ended_at = None
                run_report.save(self.storage, waiting)

            wait_seconds = self._compute_wait_seconds(last_result.resume_at, poll_interval)
            try:
                await self._sleep_with_animation(wait_seconds, last_result.resume_at)
            except asyncio.CancelledError:
                raise
            # The wait was sized to the first account whose window reopens, so
            # let that one (and any other that has since refreshed) back into
            # the rotation before the next attempt -- otherwise the pool stays
            # marked spent forever and every later block runs single-account.
            if self.account_pool is not None:
                self.account_pool.clear_expired()
                revived = self.account_pool.advance() or self.account_pool.current
                if revived is not None:
                    self.claude_client.auth_token = revived.token
            notify.send(
                f"Quota refreshed -- resuming with {len(remaining)} prompt(s) still queued.",
                title="Sandglass: resuming",
            )

    async def _ask_why_nothing_changed(
        self, prompt: PromptObject
    ) -> tuple[str | None, str]:
        """Ask the run that just wrote nothing which of three things happened.

        Returns ``(verdict, why)`` with verdict in DONE/BLOCKED/NOOP, or
        ``(None, "")`` when it wasn't asked or didn't answer usefully.

        One short turn in the session that just refused, so it is a cache read
        of a context that is already warm — cents, against the price of a whole
        block. Deliberately a *question*, not "try again": the failure modes
        want opposite responses, and only the run itself already knows which
        one it hit.

        Any doubt resolves to ``None``, which stops the run. The optimistic
        branch (keep going) must be earned by an unambiguous answer.
        """
        if self.on_refusal != ON_REFUSAL_ASK or not prompt.origin_file:
            return None, ""

        session_id, persist = self._resolve_session(prompt)
        if not session_id:
            # Nothing to resume: the question would land in a fresh session with
            # no memory of the refusal, so it could only guess. Don't pay for that.
            return None, ""

        console.print("  [dim]Asking why nothing changed…[/dim]")
        try:
            reply = await self.claude_client.send_prompt(
                REFUSAL_QUERY,
                # Never `prompt.model` raw: this question always goes to the
                # Anthropic session that just refused, so a provider's model
                # name would name something that endpoint has never heard of.
                model=self._model_for(prompt, None),
                effort="low",
                resume_session_id=session_id,
                persist_session=persist,
            )
        except Exception as exc:  # noqa: BLE001 — a failed question must not mask the refusal
            logger.warning("Could not ask prompt %s why nothing changed: %s", prompt.id, exc)
            return None, ""

        match = _VERDICT_RE.search(reply.text or "")
        if not match:
            logger.info("Prompt %s gave no parseable verdict; stopping", prompt.id)
            return None, ""
        why_match = _WHY_RE.search(reply.text or "")
        why = why_match.group(1).strip() if why_match else ""
        return match.group(1).upper(), why

    def record_stop(self, reason: str, detail: str = "") -> None:
        """Close the run record with a reason only the caller can know.

        Ctrl-C and an escaping exception both end the process without
        `execute_queue` reaching its own ending, so the CLI closes the record
        on their behalf -- otherwise the most common way a run ends leaves the
        least informative record.
        """
        report = run_report.load(self.storage) or run_report.RunReport()
        report.status = run_report.STATUS_STOPPED
        report.reason = reason
        report.detail = detail or report.detail
        report.remaining = len(self.queue_manager.load_queue())
        if self.account_pool is not None:
            report.accounts = self.account_pool.usage_summary()
        report.ended_at = run_report.now_iso()
        run_report.save(self.storage, report)
        for line in run_report.render(report):
            console.print(line)

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

        # Built once, not per frame -- the banner is static, and it is always
        # centered against the hourglass's fixed height (_HOURGLASS_ROWS never
        # changes tick to tick), so both the render and the centering only
        # need to happen once rather than on every 0.5s redraw.
        banner_rows = _pad_to_height(_render_banner(_BANNER_TEXT).split("\n"), _HOURGLASS_ROWS)

        deadline = time.monotonic() + total_seconds
        tick = 0
        with Live(console=console, auto_refresh=False, transient=True) as live:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # No caption baked into the hourglass here (unlike the
                # standalone frame, which can carry one on its neck row) --
                # banner + gap + hourglass alone is already 63 columns, and
                # appending an 18-character caption to *that* pushes the
                # widest row past 80 and wraps mid-frame in an ordinary
                # terminal, breaking the box shape. Printed as its own line
                # underneath instead, where the extra width costs nothing.
                frame_rows = _hourglass_frame(tick).split("\n")
                display = Text()
                for i, (banner_row, frame_row) in enumerate(zip(banner_rows, frame_rows)):
                    if i:
                        display.append("\n")
                    display.append(banner_row, style="bold yellow")
                    display.append(_BANNER_GAP)
                    display.append(frame_row, style="yellow")
                display.append("\n\n")
                display.append(f"  {remaining / 60:.1f} min remaining", style="yellow")
                display.append("\n\n")
                display.append(_BANNER_SIGNATURE, style="dim italic")
                live.update(display)
                live.refresh()
                tick += 1
                await asyncio.sleep(min(ANIMATION_TICK_SECONDS, remaining))
        console.print("[green]⌛ Resuming — retrying the queue now.[/green]")

    def _resolve_provider(
        self, prompt: PromptObject
    ) -> "tuple[providers.Provider, str] | None":
        """The external endpoint this block asked for, if it can be used.

        ``None`` means "run it on Anthropic", which covers four cases: the
        block never asked, `--no-external` overrode it, the marker named a
        provider Sandglass doesn't know, or no API key is configured for the
        one it named.

        The last two **fall back rather than fail**, loudly. A routing marker
        is a preference about cost, not a correctness requirement: the block
        describes work that Claude can do perfectly well, so refusing to run it
        would stop a queue over a missing config line. The fallback direction
        is also the safe one — toward Anthropic, never silently outward to a
        third party — which is what makes it a defensible default rather than
        a guess.
        """
        if not prompt.provider:
            return None

        name = prompt.provider
        if not self.allow_external:
            if name not in self._provider_warned:
                self._provider_warned.add(name)
                console.print(
                    f"  [yellow]--no-external: ignoring this block's '{name}' "
                    "marker and running it on Anthropic.[/yellow]"
                )
            return None

        provider = providers.get(name)
        if provider is None:
            if name not in self._provider_warned:
                self._provider_warned.add(name)
                console.print(
                    f"  [yellow]Unknown provider {name!r} — running on Anthropic "
                    f"instead. Known: {', '.join(sorted(providers.PROVIDERS))}.[/yellow]"
                )
            return None

        key = self.provider_registry.key_for(name) if self.provider_registry else None
        if not key:
            if name not in self._provider_warned:
                self._provider_warned.add(name)
                console.print(
                    f"  [yellow]This block asked for '{name}', but no API key is "
                    f"configured for it — running it on Anthropic instead.[/yellow]"
                )
                console.print(
                    f"  [dim]Add one with `sandglass providers set {name}`.[/dim]"
                )
            logger.warning(
                "Prompt %s requested provider %s with no key configured; "
                "falling back to Anthropic", prompt.id, name,
            )
            return None
        return provider, key

    def _model_for(
        self, prompt: PromptObject, routed: "tuple[providers.Provider, str] | None"
    ) -> str:
        """The model id to actually send, given where this block is going.

        Routed, this resolves through the provider so `deepseek-pro` becomes
        the id their endpoint expects.

        **Not** routed, it has one job that is easy to miss: a block whose
        model is a *provider's* model — because it said `model: deepseek-pro`,
        or because a `CLINE:` marker set one — has just fallen back to
        Anthropic, and forwarding that name would ask Claude for a model that
        does not exist there. The fallback would turn a missing API key into a
        hard failure on every marked block, which is precisely what falling
        back was meant to avoid. So the name is dropped for the run default and
        the substitution is said out loud, never silently.
        """
        if routed:
            return routed[0].resolve_model(prompt.model)

        owner = providers.provider_for_model(prompt.model)
        if owner is None:
            return prompt.model or self.claude_client.model

        console.print(
            f"  [yellow]'{prompt.model}' is a {owner.name} model and this block "
            f"is running on Anthropic — using {self.claude_client.model} "
            "instead.[/yellow]"
        )
        logger.info(
            "Prompt %s asked for %s but fell back to Anthropic; substituting %s",
            prompt.id, prompt.model, self.claude_client.model,
        )
        return self.claude_client.model

    async def _execute_with_rotation(self, prompt: PromptObject) -> Response:
        """Run one block, moving to the next account when quota runs out.

        The block is *retried*, not skipped: a quota hit interrupts work that
        was never done, so re-sending it under the next account is the whole
        point. Only when every account in the pool is spent does the error
        escape to the caller, which then waits exactly as a single-account run
        always has.

        The session is deliberately left alone across a switch. Transcripts
        are local files that `--resume` replays under any credential, so the
        chain survives; what does not survive is the *server-side* prompt
        cache, which is per-account and starts cold. That costs one cache
        write on the receiving account and nothing after it.
        """
        while True:
            try:
                return await self.execute_prompt(prompt)
            except QuotaExceededError as exc:
                if self.account_pool is None:
                    raise
                if self._resolve_provider(prompt) is not None:
                    # A rate limit from a third-party endpoint says nothing
                    # about any Claude subscription. Rotating here would mark a
                    # perfectly healthy account as spent and park it for an
                    # hour on the strength of someone else's quota.
                    logger.info(
                        "Prompt %s hit a rate limit on provider %s; not rotating "
                        "Claude accounts", prompt.id, prompt.provider,
                    )
                    raise
                spent = self.account_pool.current_name
                self.account_pool.mark_exhausted(exc.resets_at)
                nxt = self.account_pool.advance()
                if nxt is None:
                    console.print(
                        f"  [yellow]Account '{spent}' is out of quota, and so is "
                        "every other account in the pool.[/yellow]"
                    )
                    raise
                self.claude_client.auth_token = nxt.token
                when = (
                    f" (back around {_local_time_str(_epoch_to_iso(exc.resets_at))})"
                    if exc.resets_at else ""
                )
                console.print(
                    f"  [yellow]↻ Account '{spent}' is out of quota{when} — "
                    f"switching to '{nxt.name}' and retrying this block.[/yellow]"
                )
                logger.info(
                    "Rotated account %s -> %s on prompt %s",
                    spent, nxt.name, prompt.id,
                )
                notify.send(
                    f"Quota spent on '{spent}'. Continuing on '{nxt.name}'.",
                    title="Sandglass: switched account",
                )

    async def execute_prompt(self, prompt: PromptObject) -> Response:
        """Send a single prompt to Claude Code with a live progress spinner."""
        routed = self._resolve_provider(prompt)
        effective_model = self._model_for(prompt, routed)
        effective_effort = prompt.effort or self.claude_client.effort

        resume_session_id, persist_session = self._resolve_session(prompt)
        resuming = resume_session_id is not None

        destination = f"Claude ({effective_model}" if not routed else (
            f"{routed[0].name} ({effective_model}"
        )
        label = f"  📤 Sending to {destination}"
        if effective_effort:
            label += f", effort={effective_effort}"
        if resuming:
            label += ", warm session"
        elif not persist_session:
            label += ", isolated"
        console.print(label + ")...")
        if routed:
            console.print(
                "  [dim]↗ external: this block's prompt, the project brief and "
                f"any files it reads go to {routed[0].name}, not Anthropic.[/dim]"
            )

        # A resumed session already carries the project brief in its history,
        # and already knows the previous task ended -- so a warm block needs a
        # turn separator instead, which is two orders of magnitude cheaper than
        # re-sending the brief and is what keeps the new task from being read
        # as a continuation of the last one.
        if resuming:
            text = CHAIN_TURN_SEPARATOR + prompt.text
        else:
            text = project_docs.apply_brief(
                prompt.text,
                project_docs.build_brief(cwd=self._project_dir(prompt))
                if self.brief else None,
            )

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

            try:
                response = await self.claude_client.send_prompt(
                    text,
                    on_chunk,
                    model=effective_model,
                    effort=prompt.effort,
                    resume_session_id=resume_session_id,
                    persist_session=persist_session,
                    provider=routed,
                )
            except QuotaExceededError as exc:
                # A quota hit part-way through real work is the case resuming
                # exists for, so record the session before letting the error
                # propagate -- otherwise the retry starts cold and re-derives
                # everything the interrupted attempt had already worked out.
                if persist_session and exc.session_id:
                    self._record_session(prompt, exc.session_id)
                raise

        # Record the session the CLI actually used, which is not always the one
        # we asked for -- an unusable session makes the client fall back to a
        # fresh one. Recording what happened rather than what was intended is
        # what keeps the chain pointing at a conversation that exists.
        if persist_session and response.session_id:
            self._record_session(prompt, response.session_id)

        response.prompt_id = prompt.id
        elapsed_min = (time.monotonic() - start) / 60
        # On an external block the CLI still prices the run off Anthropic's
        # table, because that is the only table it has -- so the figure is an
        # order-of-magnitude stand-in, not a bill. Said out loud rather than
        # printed as if it were money actually owed.
        cost = (
            f"${response.cost_usd:.2f}" if not routed
            else f"~${response.cost_usd:.2f} at Anthropic rates, not {routed[0].name}'s"
        )
        console.print(
            f"  ✅ Done ({response.tokens_used:,} tokens billed, "
            f"{cost}, {elapsed_min:.1f} min)"
        )

        self._save_response(prompt, response)
        return response

    # How far _project_dir walks up looking for master_plan/ before giving up.
    # 1 covers the standard layout (prompt_tools/future_prompts.md, one level
    # under the project root); a few more is cheap insurance for a `queue
    # import` pointing somewhere deeper, without walking indefinitely.
    _PROJECT_DIR_SEARCH_DEPTH = 4

    @classmethod
    def _project_dir(cls, prompt: PromptObject) -> str | None:
        """Directory whose `master_plan/` describes this prompt's project.

        Anchored on the markdown queue source rather than the process's own
        CWD, for the same reason the artifact gate is: that file lives in the
        repo the prompt is about, which is not necessarily the directory
        Sandglass was invoked from.

        Walks up from the source file's own directory looking for a
        `master_plan/` sibling, rather than assuming one dirname() lands on
        it. The standard layout is `prompt_tools/future_prompts.md` with
        `master_plan/` one level further up (a *sibling* of `prompt_tools/`,
        not its parent) — a single dirname() lands on `prompt_tools/` itself,
        which never contains `master_plan/`. That silently defeated the
        project-state brief for every project using the default queue source:
        confirmed empirically (2026-08-14) that `build_brief` returned `None`
        for exactly this layout, meaning every markdown-sourced block was
        paying to re-read `work_log.md` in full — the cost this brief exists
        to eliminate. The artifact gate escaped the same bug by accident: it
        shells out to `git`, which resolves its own repo root via `git
        rev-parse --show-toplevel` regardless of the `cwd` it's given.
        """
        if not prompt.origin_file:
            return None
        directory = os.path.dirname(os.path.abspath(prompt.origin_file))
        current = directory
        for _ in range(cls._PROJECT_DIR_SEARCH_DEPTH):
            if project_docs.uses_convention(current):
                return current
            parent = os.path.dirname(current)
            if parent == current:  # reached a filesystem root
                break
            current = parent
        # Not found within the search depth -- return the immediate directory
        # as before. Every caller already goes through `uses_convention` (or
        # tolerates a brief-less/no-op result) for a project that doesn't use
        # the convention at all, so this isn't a new failure mode.
        return directory or None

    # --- Session continuity ------------------------------------------------

    def _resolve_session(self, prompt: PromptObject) -> tuple[str | None, bool]:
        """How this prompt should be run, as ``(resume_session_id, persist)``.

        ``(None, True)`` means "a normal run whose session the CLI will create
        and name" — nothing is ever named up front. Sandglass learns the id
        from the response and resumes *that*, which is the only way the
        recorded session is guaranteed to be one that actually exists.
        """
        if prompt.provider and self.allow_external:
            # An externally-routed block never joins the chain, in either
            # direction. Resuming would replay the whole accumulated Claude
            # conversation — every file the queue has read so far — to a
            # third-party endpoint, which is a far larger disclosure than the
            # block's author agreed to. Letting the external turn *into* the
            # chain is no better: subsequent Claude blocks would inherit
            # another vendor's output as their own history. It costs a cold
            # start, which is precisely what the block is being routed away
            # from Anthropic to make cheap.
            return None, False

        if self.session_mode == SESSION_MODE_ISOLATE or prompt.isolate:
            # `isolate: true` on a single block is the escape hatch for work
            # that must not inherit context: a review that shouldn't see the
            # implementation's own rationalisations, a from-scratch second
            # opinion. The chain simply skips it and continues after.
            return None, False

        if self.session_mode == SESSION_MODE_PROMPT:
            # One session per prompt: a retry resumes where the interrupted
            # attempt stopped, but blocks never see each other.
            return prompt.session_id, True

        return self._chained_session_id(), True

    def _chained_session_id(self) -> str | None:
        """The conversation this queue drain is running in, if one exists yet.

        ``None`` means the chain hasn't started — the next prompt opens it. A
        stored session older than :data:`CHAIN_MAX_AGE_SECONDS` is discarded
        rather than rejoined: an old conversation's context is stale, its cache
        has long expired, and carrying it forward is pure overhead.
        """
        state = self.storage.load_json(self.storage.run_state_path)
        if not isinstance(state, dict):
            return None
        session_id = state.get("session_id")
        # `last_used_at` is the one that matters; `started_at` is the fallback
        # for state written before it existed.
        touched_at = state.get("last_used_at") or state.get("started_at")
        if not session_id or not touched_at:
            return None
        age = time.time() - float(touched_at)
        if age > CHAIN_MAX_AGE_SECONDS:
            logger.info(
                "Chained session %s has been idle %.1fh; its cache has expired, "
                "so a cold start is cheaper than resuming it.",
                session_id, age / 3600,
            )
            self._clear_run_state()
            return None
        return session_id

    def _record_session(self, prompt: PromptObject, session_id: str) -> None:
        """Remember the session a prompt just ran in, so the next one joins it.

        Only ever called with an id the CLI reported, i.e. a conversation that
        demonstrably exists. Writing a *hoped-for* id here is what previously
        turned a single failed attempt into a permanently stuck queue.
        """
        if self.session_mode == SESSION_MODE_PROMPT:
            if prompt.session_id != session_id:
                prompt.session_id = session_id
                self._persist_prompt_session(prompt)
            return
        if self.session_mode != SESSION_MODE_CHAIN:
            return
        now = time.time()
        state = self.storage.load_json(self.storage.run_state_path)
        if isinstance(state, dict) and state.get("session_id") == session_id:
            # Same conversation, used again: refresh the clock. Without this the
            # timestamp is frozen at the moment the chain opened, so a batch that
            # works steadily for hours would abandon a perfectly warm session,
            # while an overnight gap on a young chain would rejoin a cold one.
            state["last_used_at"] = now
            self._save_run_state(state)
            return
        self._save_run_state(
            {"session_id": session_id, "started_at": now, "last_used_at": now}
        )

    def _save_run_state(self, state: dict) -> None:
        try:
            self.storage.save_json(self.storage.run_state_path, state)
        except OSError as exc:
            # Losing this costs a cold start, which is the old behaviour --
            # never a reason to abort a run that is otherwise fine.
            logger.warning("Could not persist chained session state: %s", exc)

    def _clear_run_state(self) -> None:
        """End the chain. The next `sandglass execute` starts a new session."""
        try:
            if os.path.exists(self.storage.run_state_path):
                os.remove(self.storage.run_state_path)
        except OSError as exc:
            logger.warning("Could not clear chained session state: %s", exc)

    def _persist_prompt_session(self, prompt: PromptObject) -> None:
        """Write a per-prompt session id back to the queue file.

        Best-effort: failing to record it costs a restart on the next attempt,
        which is exactly the old behaviour, so it must never abort the run.
        """
        try:
            queue = self.queue_manager.load_queue()
            for entry in queue:
                if entry.id == prompt.id:
                    entry.session_id = prompt.session_id
                    break
            else:
                return
            self.queue_manager.save_queue(queue)
        except (OSError, ValueError) as exc:
            logger.warning(
                "Could not persist session id for prompt %s (%s); a retry will "
                "start it fresh instead of resuming.", prompt.id, exc,
            )

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
                "usage": {
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "cache_creation_tokens": response.cache_creation_tokens,
                    "cache_read_tokens": response.cache_read_tokens,
                },
                "cost_usd": response.cost_usd,
                "session_id": prompt.session_id,
                "provider": response.provider,
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
                    "cost_usd": response.cost_usd,
                    "usage": {
                        "input_tokens": response.input_tokens,
                        "output_tokens": response.output_tokens,
                        "cache_creation_tokens": response.cache_creation_tokens,
                        "cache_read_tokens": response.cache_read_tokens,
                    },
                    # Entries written before full-usage accounting counted only
                    # input+output and are therefore large undercounts. Stamped
                    # so `sandglass history` can say so instead of quietly
                    # averaging two incompatible numbers together.
                    "accounting": ACCOUNTING_SCHEMA,
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
            # Cut the block that just ran, identified by its own heading -- not
            # whichever block happens to sit at the top of the file. Those are
            # the same block only while nothing is ever skipped or left behind,
            # and cutting the wrong one destroys unbuilt work.
            cut_text = prompt_source.cut_block(prompt.origin_file, prompt.text)
            if cut_text is None:
                logger.warning(
                    "No block matching prompt %s found in %s; cutting nothing "
                    "(re-running a block is recoverable, cutting the wrong one is not)",
                    prompt.id, prompt.origin_file,
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

    def _maybe_notify_progress(self, prompt: PromptObject) -> None:
        """Push a phone notification when prompt throughput crosses a new 5%
        milestone, and keep `Progress.md`'s own throughput numbers live
        rather than stale until someone next runs "check progress" by hand.

        Only for markdown-sourced blocks (``prompt.origin_file`` set — a
        `queue add` prompt has no source/history pair to count) in a project
        that already uses the `master_plan/` convention. See
        `prompt_source.throughput` for the count itself and why it is a
        separate, simpler definition than
        `templates/prompt_tools/count_blocks.py`'s project-specific one.

        A status ping must never break a block that otherwise completed
        successfully -- the whole body runs under one broad try/except for
        that reason (not just OSError: `os.path.relpath` raises `ValueError`
        on Windows when two paths resolve to different drives, which is a
        real possibility here, not a hypothetical one).
        """
        try:
            self._notify_progress_milestone(prompt)
        except Exception as exc:  # noqa: BLE001 — see docstring
            logger.warning(
                "Could not update/notify prompt-throughput progress for %s: %s",
                prompt.id, exc,
            )

    def _notify_progress_milestone(self, prompt: PromptObject) -> None:
        project_dir = self._project_dir(prompt)
        if not project_docs.uses_convention(project_dir):
            return
        counted = prompt_source.throughput(prompt.origin_file)
        if counted is None:
            return
        done, remaining, total, pct = counted

        # Resolved to absolute paths before diffing against project_dir (also
        # absolute) so the label is correct regardless of the process's own
        # CWD at call time, rather than relying on it implicitly matching.
        history_path = os.path.abspath(prompt_source.history_path_for(prompt.origin_file))
        source_path = os.path.abspath(prompt.origin_file)
        project_docs.update_progress_throughput(
            project_dir,
            done=done, remaining=remaining, total=total, pct=pct,
            done_label=f"`{os.path.relpath(history_path, project_dir).replace(os.sep, '/')}`",
            remaining_label=f"`{os.path.relpath(source_path, project_dir).replace(os.sep, '/')}`",
        )

        milestone = (pct // 5) * 5
        if milestone <= 0:
            return
        state_path = self.storage.progress_notify_path
        state = self.storage.load_json(state_path)
        last = state.get("last_notified_pct", -1) if isinstance(state, dict) else -1
        if milestone <= last:
            return
        notify.send(
            f"{pct}% complete ({done}/{total} blocks). {remaining} remaining.",
            title=f"Sandglass: {milestone}% complete",
        )
        # Persisted only once the notification is actually sent, so the
        # stored value always means "the highest milestone the phone has
        # already been told about" -- never a milestone that was computed but
        # never announced.
        self.storage.save_json(state_path, {"last_notified_pct": milestone})

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
                f"- {response.tokens_used:,} tokens billed "
                f"({response.cache_read_tokens:,} read from cache), "
                f"${response.cost_usd:.2f}\n\n"
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
        result: ExecutionResult,
        elapsed: float,
        stopped_reason: str | None = None,
    ) -> None:
        results = result.responses
        console.print()
        heading = "Queue Complete!" if not stopped_reason else "Batch Paused"
        console.print(f"[bold]{heading}[/bold]")
        console.print(f"  ✅ {len(results)} prompt(s) executed")
        if result.failed:
            console.print(f"  ✖ {result.failed} failed")
        if result.skipped:
            console.print(
                f"  ↷ {result.skipped} skipped (already executed and cut elsewhere)"
            )
        console.print(
            f"  💰 {result.total_tokens:,} tokens billed  ·  "
            f"${result.total_cost_usd:.2f}"
        )
        cached = result.total_cache_read_tokens + result.total_cache_creation_tokens
        if cached:
            # The split, not the total, is the number worth watching: reads are
            # a fraction of base price while writes are a multiple of it, so a
            # run that is mostly writes is re-creating a prefix it never reuses.
            share = result.total_cache_read_tokens / cached * 100
            console.print(
                f"  ♻️ cache: {result.total_cache_read_tokens:,} read / "
                f"{result.total_cache_creation_tokens:,} written ({share:.0f}% reused)"
            )
        console.print(f"  ⏱️ {elapsed / 60:.1f} minutes")
        external = [r for r in results if r.provider]
        if external:
            # The totals above mix two things the CLI cannot tell apart: real
            # Anthropic cost, and an Anthropic-priced guess at what another
            # vendor charged. Naming the split is the difference between a
            # number someone can act on and one that just looks precise.
            vendors = ", ".join(sorted({r.provider for r in external if r.provider}))
            ext_tokens = sum(r.tokens_used for r in external)
            console.print(
                f"  ↗ {len(external)} block(s) ran on {vendors} "
                f"({ext_tokens:,} tokens) — their share of the cost above is "
                "estimated at Anthropic rates, not billed by Anthropic."
            )
        if results:
            console.print("  what has been done:")
            for response in results:
                console.print(f"    - {self._summarize_response(response.text)}")
        console.print(f"  📁 Responses saved to {self.storage.responses_dir}")

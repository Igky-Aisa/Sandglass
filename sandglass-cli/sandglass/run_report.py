"""Why the last run stopped — recorded on disk, not just printed once.

A queue run is unattended by design, which means the moment it stops is
almost never a moment anyone is watching. By the time the answer is wanted,
the explanation has scrolled out of the terminal, or the terminal is closed,
or the machine has slept — and reconstructing it from `history.json` and file
timestamps is guesswork that gets the interesting cases wrong.

So every run writes its own state to ``.sandglass/last_run.json`` as it goes:
what it is doing now, and, the moment it ends, why it ended. `sandglass why`
prints that back at any time.

The case this was built for is the one where nothing gets written at all. A
run killed with the terminal, or by a sleeping laptop, never reaches its own
"I stopped because…" line. That is why the record is written *while running*
and carries a PID: a report still marked ``running`` whose process is gone is
itself the answer — the run was killed, and it names the prompt that was in
flight when it happened.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# What the run is doing / did. `waiting` is a live state, not a stop: a quota
# wait can last hours and "why isn't it doing anything" is a fair question to
# ask in the middle of one.
STATUS_RUNNING = "running"
STATUS_WAITING = "waiting"
STATUS_STOPPED = "stopped"
STATUS_COMPLETE = "complete"

# Terminal reasons. The first three mirror `ExecutionResult.stopped_reason`;
# the rest are conditions only the caller can see.
REASON_QUOTA = "quota"
REASON_ERROR = "error"
REASON_NO_ARTIFACT = "no_artifact"
REASON_STALLED = "stalled"
REASON_INTERRUPTED = "interrupted"
REASON_CRASHED = "crashed"
REASON_COMPLETE = "complete"
REASON_LEFT_IN_PLACE = "left_in_place"
# Not written by anyone: inferred when a `running` report's process is gone.
REASON_VANISHED = "vanished"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunReport:
    """The state of the most recent `sandglass execute`."""

    status: str = STATUS_RUNNING
    reason: Optional[str] = None
    # The CLI's own words for what went wrong, verbatim. Never paraphrased --
    # "You've hit your session limit · resets 6:20pm" carries the reset time,
    # and a summary of it would not.
    detail: str = ""
    prompt_id: Optional[str] = None
    prompt_title: str = ""
    completed: int = 0
    remaining: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    resume_at: Optional[str] = None
    # Per-account totals when several subscriptions were rotated through.
    # Names, block counts and cost only -- a token must never reach this file,
    # which is written to `.sandglass/` inside the repo where any block with
    # bypassPermissions could read it back. See accounts.py.
    accounts: list[dict] = field(default_factory=list)
    started_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    ended_at: Optional[str] = None
    pid: int = field(default_factory=os.getpid)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunReport":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


def _pid_alive(pid: int) -> bool:
    """Whether the process that owned a run is still around.

    Best-effort: a recycled PID can make a dead run look alive. That is an
    acceptable failure here because the consequence is a slightly wrong
    sentence in a diagnostic, and the alternative (no liveness check) makes
    the killed-run case indistinguishable from the running one, which is the
    case this whole module exists for.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --- persistence -------------------------------------------------------------


def save(storage, report: RunReport) -> None:
    """Persist the report. Never raises — a diagnostic must not break a run."""
    report.updated_at = now_iso()
    try:
        storage.ensure_sandglass_dir()
        storage.save_json(storage.last_run_path, asdict(report))
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("Could not record run state: %s", exc)


def load(storage) -> Optional[RunReport]:
    try:
        data = storage.load_json(storage.last_run_path)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read run state: %s", exc)
        return None
    if not isinstance(data, dict) or not data:
        return None
    try:
        return RunReport.from_dict(data)
    except TypeError:
        return None


def clear(storage) -> None:
    try:
        path = storage.last_run_path
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.warning("Could not clear run state: %s", exc)


# --- explanation -------------------------------------------------------------


def effective_reason(report: RunReport) -> str:
    """The reason to report, correcting a record its own run never closed.

    A report left at ``running`` by a process that no longer exists was not
    written by a run that chose to stop — it is the fingerprint of one that
    was killed.
    """
    if report.status in (STATUS_RUNNING, STATUS_WAITING) and not _pid_alive(report.pid):
        return REASON_VANISHED
    if report.status == STATUS_RUNNING:
        return STATUS_RUNNING
    if report.status == STATUS_WAITING:
        return STATUS_WAITING
    return report.reason or REASON_ERROR


def explain(report: RunReport) -> tuple[str, str, str]:
    """Return ``(headline, what happened, what to do)`` for a report."""
    reason = effective_reason(report)
    where = f"prompt {report.prompt_id}" if report.prompt_id else "the queue"
    queued = f"{report.remaining} prompt(s) still queued."

    if reason == STATUS_RUNNING:
        return (
            "Still running",
            f"Working on {where} right now ({report.prompt_title}).",
            "Nothing to do — leave it, or Ctrl-C to stop (the queue stays intact).",
        )
    if reason == STATUS_WAITING:
        when = f" until around {report.resume_at}" if report.resume_at else ""
        return (
            "Waiting out a quota limit",
            f"Token limits were hit on {where}, so the run is sleeping{when} "
            f"and will retry by itself. {queued}",
            "Nothing to do — it resumes on its own. Ctrl-C stops it without losing the queue.",
        )
    if reason == REASON_VANISHED:
        return (
            "Killed before it could say why",
            f"The run was working on {where} ({report.prompt_title}) and its "
            f"process (PID {report.pid}) is gone without recording an ending. "
            "That means it was stopped from outside: terminal closed, machine "
            "slept or shut down, or the process was killed. It is NOT a quota "
            f"limit and NOT a failure of the prompt. {queued}",
            "Re-run `sandglass execute` — the in-flight prompt was never cut "
            "from the queue, so it starts again from the top of that block.",
        )
    if reason == REASON_QUOTA:
        when = f" Expected to refresh around {report.resume_at}." if report.resume_at else ""
        return (
            "Token limits hit",
            f"Claude reported the subscription's usage limit while running "
            f"{where}.{when} {queued}",
            "Re-run `sandglass execute` after the reset; it auto-waits and "
            "resumes on its own unless you passed --once.",
        )
    if reason == REASON_NO_ARTIFACT:
        return (
            "A prompt produced no work product",
            f"{where.capitalize()} returned a response but changed no files, so "
            "Sandglass refused to cut its block from the source file — almost "
            "always an unbuilt dependency the block needed. The blocks behind it "
            f"usually depend on it, so the run stopped rather than burning them. {queued}",
            "Read the saved response in .sandglass/responses, fix or reorder the "
            "dependency, then re-run. `--no-require-artifact` skips the check for "
            "genuinely read-only queues.",
        )
    if reason == REASON_STALLED:
        return (
            "Gave up retrying a 'quota' error",
            f"{where.capitalize()} hit the same quota-looking error repeatedly "
            f"without making progress, so it may not be quota at all. {queued}",
            "Read the error below and run that one prompt by hand to see what it "
            "really is.",
        )
    if reason == REASON_INTERRUPTED:
        return (
            "Stopped by Ctrl-C",
            f"You interrupted the run while it was on {where}. {queued}",
            "Re-run `sandglass execute` whenever you like — nothing was lost.",
        )
    if reason == REASON_CRASHED:
        return (
            "Sandglass itself crashed",
            f"An unexpected error escaped while running {where}. This is a bug in "
            f"Sandglass, not in the prompt. {queued}",
            "Report the traceback below; the queue is intact, so re-running is safe.",
        )
    if reason == REASON_LEFT_IN_PLACE:
        return (
            "Ran out of blocks it could finish",
            f"Every remaining block reported that it needed no file change — "
            f"already done, or nothing to write. None of them were cut, because "
            f"nothing verifiable happened and a block's own word is not evidence. "
            f"{queued}",
            "Read the saved responses; if you agree the work is done, cut those "
            "blocks from the queue file yourself. They will otherwise be retried.",
        )
    if reason == REASON_COMPLETE:
        return (
            "Finished the queue",
            f"All prompts executed ({report.completed} this run). Nothing left queued.",
            "Nothing to do.",
        )
    return (
        "Stopped after an error",
        f"{where.capitalize()} failed with the error below. Errors that aren't "
        f"quota limits are never auto-retried, because looping on them doesn't "
        f"help. {queued}",
        "Read the error, fix the cause, then re-run `sandglass execute`.",
    )


def render(report: RunReport) -> list[str]:
    """Rich-markup lines explaining the report, ready to print."""
    headline, what, todo = explain(report)
    reason = effective_reason(report)
    colour = {
        STATUS_RUNNING: "cyan",
        STATUS_WAITING: "cyan",
        REASON_COMPLETE: "green",
    }.get(reason, "yellow" if reason in (REASON_QUOTA, REASON_INTERRUPTED) else "red")

    lines = [f"[bold {colour}]Why it stopped: {headline}[/bold {colour}]", f"  {what}"]
    if report.detail:
        # Indented verbatim, never reflowed: this is the CLI's own text and it
        # is what someone will search for.
        for line in report.detail.strip().splitlines():
            lines.append(f"  [dim]│[/dim] {line}")
    lines.append(f"  [bold]Next:[/bold] {todo}")
    if report.accounts:
        # Only rendered for rotated runs. One merged total would hide the fact
        # that the queue crossed accounts at all, which is the thing worth
        # knowing when reading back why a run cost what it did.
        lines.append("  [bold]Accounts used:[/bold]")
        for entry in report.accounts:
            lines.append(
                f"    [dim]·[/dim] {entry.get('name', '?')}: "
                f"{entry.get('blocks', 0)} block(s), "
                f"{entry.get('tokens', 0):,} tokens, "
                f"${entry.get('cost_usd', 0.0):.4f}"
            )
    when = report.ended_at or report.updated_at
    if when:
        lines.append(f"  [dim]Recorded {when}[/dim]")
    return lines

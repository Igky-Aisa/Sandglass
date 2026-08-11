"""Reads and prunes the markdown state files a Sandglass project keeps
(`master_plan/work_log.md`, `master_plan/Progress.md`).

Why this module exists — the measurement that motivated it:

A project's CLAUDE.md typically tells every agent to read `work_log.md` before
doing substantial work. That is good practice for one interactive session and
ruinous for a queue: `sandglass execute` runs each block through a *cold*
`claude -p`, so the mandate is re-paid, in full, at full price, once per block.
Those files are append-only, so the bill grows with every session that ever
ran. Measured on a real project: a 313 KB work_log plus an 85 KB system map is
~100k tokens of mandated reading before a block does any work of its own, on
top of a ~24k fixed prefix.

Two fixes live here, and they are meant to be used together:

- :func:`build_brief` turns the unbounded, agent-driven read into a bounded,
  Sandglass-controlled injection: the last couple of work-log entries and the
  short status file, prepended to the block with an explicit instruction not to
  re-read the full files. Bounded is the whole point — the brief has a hard
  character cap so it can never quietly become the thing it replaced.
- :func:`rotate_log` keeps the source files from growing without limit, so the
  brief stays cheap to build and a human (or an agent that legitimately needs
  history) still has it, one directory over.

Neither is specific to Sandglass's own repo, but both are no-ops when the
project doesn't use the convention — a missing `master_plan/` means "not this
kind of project", not an error.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MASTER_PLAN_DIR = "master_plan"
WORK_LOG_NAME = "work_log.md"
PROGRESS_NAME = "Progress.md"
ARCHIVE_DIR = "archive"

# How many trailing work-log entries the brief carries. Two is the smallest
# number that still answers "what happened last, and what did it leave open" --
# the previous session plus the one before it, which is usually where a
# still-unresolved blocker was first written down.
DEFAULT_BRIEF_ENTRIES = 2
# Hard ceiling on the assembled brief. A brief that grows without bound is the
# original problem wearing a different hat, so it is capped rather than
# trusted: entries are dropped oldest-first, then the remainder is truncated.
BRIEF_MAX_CHARS = 6000
# Entries kept in place by `rotate-logs`; everything older is archived.
DEFAULT_KEEP_ENTRIES = 5

# A log entry starts at a level-2 heading in column 0. Both the work_log format
# (`## [Date] - [Agent] - [Title]`) and prompt_history's (`## [label] Title`)
# follow this, so one splitter serves both.
_ENTRY_RE = re.compile(r"^## ", re.MULTILINE)


def master_plan_path(*parts: str, cwd: str | None = None) -> str:
    """Path inside the project's `master_plan/` directory."""
    base = os.path.join(cwd, MASTER_PLAN_DIR) if cwd else MASTER_PLAN_DIR
    return os.path.join(base, *parts)


def uses_convention(cwd: str | None = None) -> bool:
    """Whether this project keeps a `master_plan/` directory at all.

    False means "not a project that uses these conventions" — every function
    here degrades to a no-op rather than creating the directory. Sandglass is a
    general-purpose runner and has no business imposing one project's filing
    system on someone else's repo.
    """
    base = os.path.join(cwd, MASTER_PLAN_DIR) if cwd else MASTER_PLAN_DIR
    return os.path.isdir(base)


def split_entries(text: str) -> list[str]:
    """Split a markdown log into its `## `-headed entries, in file order.

    Any preamble before the first heading is returned as the first element so
    that re-joining the result reproduces the file byte for byte — callers that
    only want real entries should skip a leading chunk that doesn't start with
    `## `.
    """
    if not text:
        return []
    starts = [m.start() for m in _ENTRY_RE.finditer(text)]
    if not starts:
        return [text]
    chunks = []
    if starts[0] > 0:
        chunks.append(text[: starts[0]])
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        chunks.append(text[start:end])
    return chunks


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def tail_entries(path: str, keep: int) -> str:
    """The last ``keep`` entries of a markdown log, as one string.

    Returns "" if the file is missing or empty. Deliberately reads the whole
    file: these are hundreds of kilobytes at worst, which is nothing to Python
    and everything to a language model — the cost this module exists to avoid
    is tokens sent to Claude, not bytes read from disk.
    """
    text = _read(path)
    if not text.strip():
        return ""
    entries = [c for c in split_entries(text) if c.startswith("## ")]
    if not entries:
        return text.strip()
    return "".join(entries[-keep:]).strip()


def build_brief(
    cwd: str | None = None,
    entries: int = DEFAULT_BRIEF_ENTRIES,
    max_chars: int = BRIEF_MAX_CHARS,
) -> str | None:
    """Assemble the bounded project-state brief prepended to a queued prompt.

    Returns ``None`` when there is nothing useful to say (no `master_plan/`, or
    the files are empty) — the caller should then send the prompt unchanged
    rather than prepending an empty header that only wastes tokens and implies
    state was checked when it wasn't.

    The instruction line matters as much as the content. Without it the agent
    reads the brief *and then* reads `work_log.md` anyway, which is strictly
    worse than not injecting anything, so the brief explicitly names the files
    it stands in for and tells the agent to open them only if it needs history
    the brief doesn't cover.
    """
    if not uses_convention(cwd):
        return None

    work_log = tail_entries(master_plan_path(WORK_LOG_NAME, cwd=cwd), entries)
    progress = _read(master_plan_path(PROGRESS_NAME, cwd=cwd)).strip()
    if not work_log and not progress:
        return None

    sections: list[str] = [
        "<project_state>",
        "Current project state, supplied by Sandglass. This replaces reading "
        f"`{MASTER_PLAN_DIR}/{WORK_LOG_NAME}` and `{MASTER_PLAN_DIR}/{PROGRESS_NAME}` "
        "yourself: both are append-only and now large, and re-reading them in full "
        "costs more than the task usually does. Work from what is below. Open the "
        "full files only if you need history this brief genuinely doesn't cover, "
        "and read the tail rather than the whole file when you do.",
    ]
    if progress:
        sections.append(f"\n## Status ({MASTER_PLAN_DIR}/{PROGRESS_NAME})\n\n{progress}")
    if work_log:
        sections.append(
            f"\n## Last {entries} work-log entr{'y' if entries == 1 else 'ies'} "
            f"({MASTER_PLAN_DIR}/{WORK_LOG_NAME})\n\n{work_log}"
        )
    sections.append("</project_state>")

    brief = "\n".join(sections)
    if len(brief) > max_chars:
        # Keep the head (status + the instruction that makes the brief work)
        # and cut the older tail of the work log, which is the least valuable
        # part of an over-long brief.
        brief = brief[: max_chars - 120].rstrip() + (
            "\n\n[brief truncated at "
            f"{max_chars:,} characters — read the tail of "
            f"{MASTER_PLAN_DIR}/{WORK_LOG_NAME} if you need more]\n</project_state>"
        )
    return brief


def apply_brief(prompt_text: str, brief: str | None) -> str:
    """Prepend ``brief`` to a prompt, or return the prompt unchanged."""
    if not brief:
        return prompt_text
    return f"{brief}\n\n{prompt_text}"


def rotate_log(path: str, keep: int = DEFAULT_KEEP_ENTRIES) -> tuple[int, str] | None:
    """Move all but the last ``keep`` entries of ``path`` into a dated archive.

    Returns ``(entries_archived, archive_path)``, or ``None`` if there was
    nothing to do (missing file, or already at/below ``keep``).

    The archive is a sibling file under `master_plan/archive/`, and a pointer
    line is left at the top of the live file so the history is one link away
    rather than gone. Nothing is deleted — this is a move, and it has to stay a
    move: an agent that reads only the live file must still be able to find out
    that earlier sessions existed.
    """
    text = _read(path)
    if not text.strip():
        return None

    chunks = split_entries(text)
    preamble = "".join(c for c in chunks if not c.startswith("## "))
    entries = [c for c in chunks if c.startswith("## ")]
    if len(entries) <= keep:
        return None

    old, kept = entries[:-keep], entries[-keep:]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive_dir = os.path.join(os.path.dirname(path) or ".", ARCHIVE_DIR)
    os.makedirs(archive_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    archive_path = os.path.join(archive_dir, f"{base}_archive_{stamp}.md")

    header = (
        f"# Archived entries from {os.path.basename(path)}\n\n"
        f"Moved here by `sandglass rotate-logs` on {stamp}. Newest of these "
        f"entries is immediately followed, chronologically, by the oldest entry "
        f"still in the live file.\n"
    )
    existing = _read(archive_path)
    with open(archive_path, "a" if existing else "w", encoding="utf-8") as fh:
        if not existing:
            fh.write(header)
        fh.write("\n" + "".join(old).rstrip() + "\n")

    pointer = (
        f"> Older entries live in `{MASTER_PLAN_DIR}/{ARCHIVE_DIR}/"
        f"{os.path.basename(archive_path)}`. This file keeps the most recent "
        f"{keep} so reading it stays cheap; consult the archive only when you "
        f"need history older than that.\n"
    )
    # Replace any pointer a previous rotation left, so they don't stack up.
    preamble = re.sub(
        r"^> Older entries live in .*\n(?:\n)?", "", preamble, flags=re.MULTILINE
    )
    new_text = f"{preamble.rstrip()}\n\n{pointer}\n{''.join(kept).rstrip()}\n" if preamble.strip() \
        else f"{pointer}\n{''.join(kept).rstrip()}\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)

    logger.info("Rotated %d entr(ies) out of %s into %s", len(old), path, archive_path)
    return len(old), archive_path

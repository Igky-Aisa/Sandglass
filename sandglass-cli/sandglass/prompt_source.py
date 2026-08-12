"""Parses `====`-delimited prompt blocks out of a markdown queue source file
(default: prompt_tools/future_prompts.md) so `sandglass execute` can treat
that file as the default queue when nothing has been queued via `queue add`.

This mirrors -- and is meant to coexist with, not replace -- the manual
"future prompts" chat workflow already documented in the project's CLAUDE.md:
same file, same `====` delimiter convention, same "cut into prompt_history.md
on completion" behavior. The difference is *who* executes the prompt: the
manual workflow runs it in an interactive Claude Code chat session, this
module feeds it to `sandglass execute` -> `claude -p` (headless).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

DEFAULT_QUEUE_SOURCE = os.path.join("prompt_tools", "future_prompts.md")

# A line containing only '=' characters (whitespace aside) marks a block edge.
# Matches the literal rule already in CLAUDE.md: "prompts are separated by
# lines containing ==== at start and end".
_DELIMITER_RE = re.compile(r"^[ \t]*=+[ \t]*$", re.MULTILINE)


def parse_blocks(raw_text: str) -> list[dict]:
    """Split ``raw_text`` into prompt blocks separated by '====' lines.

    Matches the literal CLAUDE.md convention: N prompts are separated by
    N-1 delimiter lines, e.g. ``prompt A\\n====\\nprompt B``. There's no
    requirement for a delimiter before the first block or after the last —
    a delimiter only ever marks the boundary *between* two blocks, so a file
    holding a single prompt needs no delimiter at all (this also covers the
    last prompt left in the file after every earlier one has been cut out).
    A blank/whitespace-only file yields no blocks.

    Returns a list of ``{"text": str, "start": int, "end": int}`` dicts in
    file order. ``start``/``end`` span the block's own content plus the
    delimiter line that follows it (if any), so a caller can excise exactly
    that region and leave the next block sitting at the top of the file with
    no stray leading delimiter — matching how a completed prompt is meant to
    be cut out.
    """
    matches = list(_DELIMITER_RE.finditer(raw_text))
    content_starts = [0] + [m.end() for m in matches]
    content_ends = [m.start() for m in matches] + [len(raw_text)]
    cut_ends = [m.end() for m in matches] + [len(raw_text)]

    blocks: list[dict] = []
    for content_start, content_end, cut_end in zip(content_starts, content_ends, cut_ends):
        text = raw_text[content_start:content_end].strip("\n")
        if text.strip():
            blocks.append({"text": text, "start": content_start, "end": cut_end})
    return blocks


def read_blocks(file_path: str) -> list[dict]:
    """Read and parse blocks from ``file_path``; ``[]`` if it doesn't exist."""
    if not os.path.exists(file_path):
        return []
    with open(file_path, "r", encoding="utf-8") as fh:
        return parse_blocks(fh.read())


def cut_first_block(file_path: str) -> str | None:
    """Remove the first delimited block from ``file_path``, returning its text.

    Returns ``None`` if the file is missing or has no blocks left — callers
    should treat that as "nothing to cut", not an error, since a prompt may
    have been added to the queue by other means.
    """
    if not os.path.exists(file_path):
        return None
    with open(file_path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    blocks = parse_blocks(raw)
    if not blocks:
        return None
    block = blocks[0]
    new_raw = raw[: block["start"]] + raw[block["end"] :]
    new_raw = re.sub(r"\n{3,}", "\n\n", new_raw)  # tidy up the resulting gap
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(new_raw)
    return block["text"]


# A block's identity is its own first markdown heading -- the one thing that
# survives a block being re-authored, because the heading names the deliverable
# ("### P6.04 — MT5 polling → diffed events") while the body around it changes.
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)
# Below this length a heading is something like "## Notes" and would prefix-match
# half the file. Not enough to identify anything, so it isn't used as evidence.
_MIN_IDENTITY_CHARS = 8


def _normalize_heading(text: str) -> str:
    text = text.strip().lower().replace("—", "-").replace("–", "-")
    text = re.sub(r"[`*_]", "", text)
    return re.sub(r"\s+", " ", text)


def block_identity(prompt_text: str) -> str | None:
    """The normalized first heading of a block, or ``None`` if it has none."""
    match = _HEADING_RE.search(prompt_text)
    if not match:
        return None
    identity = _normalize_heading(match.group(1))
    return identity if len(identity) >= _MIN_IDENTITY_CHARS else None


def _has_heading(file_path: str, identity: str) -> bool:
    """Whether any heading in ``file_path`` starts with ``identity``.

    Prefix, not equality: a completed block's heading picks up a marker on the
    way into the history file (``### P6.04 — … [executed 2026-08-11 …]``), and
    it is still the same block.
    """
    if not os.path.exists(file_path):
        return False
    with open(file_path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    return any(
        _normalize_heading(m.group(1)).startswith(identity)
        for m in _HEADING_RE.finditer(raw)
    )


def already_executed(source_file: str, prompt_text: str) -> bool:
    """Whether this block has already been run and cut, by someone else.

    The queue in `.sandglass/queue.json` and the blocks in `future_prompts.md`
    are two copies of the same intent, and they drift: an interactive session
    can execute a block and cut it from the markdown, or a run can be killed
    after the cut but before the queue is updated. Sandglass then re-dispatches
    finished work, the model correctly declines to redo it, nothing changes on
    disk, and the artifact gate stops the whole queue -- a full block's cost to
    be told the block was already done. (Measured once: $4.92 for one minute.)

    Deliberately requires **positive evidence**, never inferring from absence.
    A block missing from the source could just as easily have been re-authored,
    and skipping real work would be far worse than re-running finished work. So
    this returns True only when the block is gone from the source *and* present
    in the sibling history file, which together mean exactly one thing: it ran.
    """
    identity = block_identity(prompt_text)
    if not identity:
        return False
    if _has_heading(source_file, identity):
        return False  # still pending -- this is the copy waiting to be run
    return _has_heading(history_path_for(source_file), identity)


def cut_block(file_path: str, prompt_text: str) -> str | None:
    """Remove the block matching ``prompt_text``, returning its text.

    `cut_first_block` assumes the block being completed is the one at the top
    of the file, which holds only while the queue is drained strictly in order
    and nothing is ever left behind. The moment a block is skipped or left in
    place, "first" and "the one that just ran" are different blocks — and
    cutting the wrong one destroys the only copy of unbuilt work.

    Matches on the block's heading identity first, then on exact text. Returns
    ``None`` and cuts **nothing** when neither matches: re-running a block is
    recoverable, deleting the wrong one is not.
    """
    if not os.path.exists(file_path):
        return None
    with open(file_path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    blocks = parse_blocks(raw)
    if not blocks:
        return None

    identity = block_identity(prompt_text)
    target = None
    if identity:
        target = next(
            (b for b in blocks if block_identity(b["text"]) == identity), None
        )
    if target is None:
        wanted = prompt_text.strip()
        target = next((b for b in blocks if b["text"].strip() == wanted), None)
    if target is None:
        return None

    new_raw = raw[: target["start"]] + raw[target["end"] :]
    new_raw = re.sub(r"\n{3,}", "\n\n", new_raw)
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(new_raw)
    return target["text"]


def history_path_for(source_file: str) -> str:
    """The sibling history file a source file's completed blocks get archived to."""
    return os.path.join(os.path.dirname(source_file) or ".", "prompt_history.md")


def _insert_entry(history_path: str, entry: str) -> None:
    """Insert ``entry`` right after the file's intro `---` marker (newest
    first), creating the file with the standard intro if it doesn't exist yet.
    Shared by `prepend_to_history` (a completed prompt) and
    `append_interruption_note` (a prompt still in progress, interrupted by a
    quota hit) -- same file, same newest-first convention, different content.
    """
    intro = (
        "# Executed Prompts (History)\n\n"
        "Prompts moved here after execution, most recent first.\n\n---\n"
    )
    if os.path.exists(history_path):
        with open(history_path, "r", encoding="utf-8") as fh:
            existing = fh.read()
    else:
        existing = intro

    marker = "---\n"
    idx = existing.find(marker)
    if idx == -1:
        new_content = existing + entry
    else:
        insert_at = idx + len(marker)
        new_content = existing[:insert_at] + entry + existing[insert_at:]

    os.makedirs(os.path.dirname(history_path) or ".", exist_ok=True)
    with open(history_path, "w", encoding="utf-8") as fh:
        fh.write(new_content)


def prepend_to_history(
    history_path: str,
    title: str,
    model: str,
    block_text: str,
    *,
    via: str = "sandglass execute",
    label: str | None = "Sandglass work",
) -> None:
    """Record a completed block in ``history_path``, newest first.

    Matches the manual "future prompts" convention's own history file layout
    exactly, so entries from `sandglass execute` and from the interactive
    chat workflow read the same way in the same file. ``via``/``label``
    default to the real headless `sandglass execute` path (the only actual
    caller of this via `ExecutionEngine._cut_from_source`) -- pass
    ``label=None`` and a different ``via`` when archiving a prompt that was
    instead run by hand in an interactive chat session, so the two paths
    stay visually distinguishable in a file they both write to, per
    CLAUDE.md's "these are two different execution paths, not duplicates"
    note.
    """
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    heading = f"## [{label}] {title}" if label else f"## {title}"
    entry = (
        f"\n{heading}\n\n"
        f"**Executed:** {date} — {model} (via `{via}`)\n\n"
        "```\n"
        "============================\n\n"
        f"{block_text}\n\n"
        "============================\n"
        "```\n"
    )
    _insert_entry(history_path, entry)


def append_interruption_note(history_path: str, title: str, resume_at: str | None) -> None:
    """Record that a queued prompt was interrupted by a quota hit, without
    cutting its block out of the source file -- only a successful completion
    does that (`cut_first_block`, called from `prepend_to_history`'s call
    site). This is purely an audit-trail marker so a long gap between a
    prompt starting and its eventual completion entry isn't silent; the
    prompt itself stays queued and `run_with_auto_resume` retries it in full
    once the quota refreshes -- nothing about the retry depends on this note.
    """
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    when = f" (expected around {resume_at})" if resume_at else ""
    entry = (
        f"\n## [Sandglass work] {title} — INTERRUPTED, will auto-resume\n\n"
        f"**Interrupted:** {date} — hit the subscription's usage limit{when}. "
        "Not removed from the queue; `sandglass execute`'s auto-resume will "
        "retry it in full once the quota refreshes (see the eventual "
        "completion entry above this one, once it lands).\n"
    )
    _insert_entry(history_path, entry)

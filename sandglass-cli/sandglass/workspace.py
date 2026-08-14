"""Did the prompt actually produce a work product?

Sandglass's cut-on-completion is a *lock*: when a markdown-sourced block finishes,
its text is removed from `future_prompts.md` and archived into `prompt_history.md`
so no other runner can execute it twice. That lock is only sound if "finished"
means "produced the artifact the block asked for".

It did not. Until this module existed, `execute_queue()` cut a block whenever
`execute_prompt()` returned without raising — and a model that reads the repo,
correctly concludes it *cannot* build the block (a missing dependency, an
unbuilt foundation), and says so in prose, returns exactly like one that wrote a
thousand lines. Both got cut.

That is not hypothetical. In the Asymmetry project, twelve blocks were cut having
landed zero code; two of them (P4.03, P5.01) had responses that are *verbatim
refusals to proceed*, and the blocks were archived as executed anyway. The queue
then could not deliver those phases at all, because the only copy of the block
text had been moved into a history file that nobody re-runs.

The check here is deliberately about the **working tree**, not about the response
text. Trying to classify prose as a refusal is a losing game — models phrase it a
hundred ways, and a confident wrong classification is worse than none. "Did any
tracked file change?" is a fact, cheaply obtained, with no false confidence.
"""

from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# A prompt can legitimately take a while; git status on a large repo is still fast,
# but never let a hung git block the queue.
_GIT_TIMEOUT_SECONDS = 30


def _run_git(args: list[str], cwd: str | None = None) -> str | None:
    """Run a git command, returning stdout, or None if git can't answer.

    None is "I don't know", never "nothing changed" — every caller has to treat
    the two differently, which is the whole reason this returns an optional.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("git %s unavailable: %s", " ".join(args), exc)
        return None
    if completed.returncode != 0:
        logger.debug(
            "git %s exited %d: %s",
            " ".join(args), completed.returncode, completed.stderr.strip()[:200],
        )
        return None
    return completed.stdout


# Sandglass's own bookkeeping, which changes on EVERY prompt regardless of whether
# the prompt did anything: the saved response JSON, the queue, the history archive.
# Counting these would make the whole gate a no-op — every run would look productive
# because sandglass itself had just written a response file. This is not a
# hypothetical: `.sandglass/responses/response_041.json` and `response_042.json` are
# precisely the files the refusing runs left behind in the incident this module fixes.
_IGNORED_PREFIXES = (".sandglass/",)

# The project's OWN bookkeeping files — the ones its CLAUDE.md tells every task
# to write. These are excluded for the same reason as `.sandglass/`: a file the
# process mandates cannot be evidence that the process produced anything.
#
# This is not a hypothetical either, and it is worse than the `.sandglass/` case
# because it inverts the gate on exactly the runs it exists to catch. A block
# that cannot do its work writes a work-log entry explaining why it is blocked —
# obeying the convention — and that write made the gate see a "work product", so
# the block was cut from `future_prompts.md` and archived into
# `prompt_history.md` as executed. Observed live: a P6.08 block whose own
# response opens "P6.08 is **BLOCKED**. I made no code changes" was archived as
# complete, with a matching work-log entry, having built nothing.
#
# That is self-reinforcing. Every refusal deletes a block from the plan and adds
# a false "done" to the history, so the blocks that depend on it later arrive to
# find their dependency missing, refuse in turn, and are archived the same way.
# The queue drifts further from the repo with every run.
#
# Matched as whole paths, not prefixes: `master_plan/` also holds architecture
# docs, and a block whose whole job is editing one of those has genuinely
# produced its deliverable.
_IGNORED_FILES = (
    "master_plan/work_log.md",
    "master_plan/progress.md",
    "prompt_tools/prompt_history.md",
    "prompt_tools/future_prompts.md",
)


def _normalize(path: str) -> str:
    # NB: a leading "./" is stripped as a *prefix*, not with lstrip("./") — that
    # strips characters, turning ".sandglass/" into "sandglass/" and quietly
    # defeating the whole exclusion. Git reports an untracked directory as
    # ".sandglass/" with no "./", so this is the exact path that matters.
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_ignored(path: str) -> bool:
    normalized = _normalize(path)
    if normalized.startswith(_IGNORED_PREFIXES):
        return True
    # Case-insensitive on the filename: `Progress.md` and `progress.md` are the
    # same file on Windows and both spellings appear in real projects.
    return normalized.lower() in _IGNORED_FILES


def _status_entries(status: str) -> list[tuple[str, str]]:
    """`(status_code, repo_relative_path)` pairs, with sandglass's own files dropped.

    Handles rename entries (`R  old -> new`) by taking the destination, and strips
    the quoting git applies to paths with unusual characters.
    """
    entries: list[tuple[str, str]] = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        raw = line[3:].strip()
        if " -> " in raw:  # rename/copy: the destination is what exists now
            raw = raw.split(" -> ", 1)[1]
        path = raw.strip().strip('"')
        if _is_ignored(path):
            continue
        entries.append((code, path))
    return entries


def _content_stamp(root: str, rel_path: str) -> str:
    """`(size, mtime_ns)` for one status entry, recursing into untracked dirs.

    Why this exists: `git status --porcelain` reports an untracked file by **name
    only**, so a prompt that edits a file an earlier prompt already created (and
    which is still untracked) produces byte-identical status output. Without this,
    the gate would report "no work product" for real work — blocking a legitimate
    run, which is a worse failure than the one this module fixes.

    Stat rather than a content hash: a prompt that rewrites a file to exactly the
    same size *and* the same nanosecond mtime has not meaningfully happened, and
    hashing every changed file on a large repo costs real time on every prompt.
    """
    abs_path = os.path.join(root, rel_path)
    try:
        if os.path.isdir(abs_path):
            # An untracked directory is reported as `dir/`; walk it so a change to
            # any file inside registers.
            entries: list[str] = []
            for dirpath, _dirnames, filenames in os.walk(abs_path):
                for name in sorted(filenames):
                    full = os.path.join(dirpath, name)
                    try:
                        st = os.stat(full)
                        entries.append(f"{os.path.relpath(full, root)}:{st.st_size}:{st.st_mtime_ns}")
                    except OSError:
                        entries.append(f"{os.path.relpath(full, root)}:?")
            return "|".join(entries)
        st = os.stat(abs_path)
        return f"{rel_path}:{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        # Deleted between status and stat, or unreadable — the path being listed at
        # all is itself the signal.
        return f"{rel_path}:absent"


def workspace_fingerprint(cwd: str | None = None) -> str | None:
    """A string that changes whenever the working tree or HEAD changes.

    Returns None when this isn't a git working tree, or git isn't installed, or
    git failed for any reason. **None means "cannot tell"** and callers must fall
    back to permissive behaviour: a project that doesn't use git still has to be
    able to run its queue.

    Combines three sources so every shape of work product registers:
      - the prompt committed its own work            -> HEAD changes
      - a file appeared, vanished or changed state   -> status changes
      - an already-untracked file was edited again   -> its stat stamp changes
        (status alone would be byte-identical — see :func:`_content_stamp`)
    """
    head = _run_git(["rev-parse", "HEAD"], cwd=cwd)
    # A repo with no commits yet has no HEAD; that's fine, status alone still works.
    if head is None:
        head = ""

    status = _run_git(["status", "--porcelain"], cwd=cwd)
    if status is None:
        # No usable status => not a git worktree (or git is broken). Cannot tell.
        return None

    root = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    root = root.strip() if root else (cwd or ".")

    # Rebuilt from the FILTERED entries rather than the raw status text, so
    # sandglass's own `.sandglass/` writes cannot register as a work product.
    lines = [f"{code} {path} {_content_stamp(root, path)}" for code, path in _status_entries(status)]
    return f"{head.strip()}\n" + "\n".join(lines)


def produced_work_product(
    before: str | None,
    after: str | None,
) -> bool:
    """True when the workspace changed between the two fingerprints.

    Fails **open** (returns True) when either fingerprint is None — i.e. when we
    could not measure. A non-git project must keep working exactly as it did, and
    silently refusing to ever cut a block would be a worse bug than the one this
    module fixes. The caller is responsible for saying out loud that the check was
    skipped, so "unverified" never reads as "verified".
    """
    if before is None or after is None:
        return True
    return before != after

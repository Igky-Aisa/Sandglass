"""Count the build queue exactly. The one definition of every number in `master_plan/Progress.md`.

Run from the repository root:

    python prompt_tools/count_blocks.py

Exits non-zero, naming the problem, when an invariant is broken -- so it can gate a commit.

Why this exists rather than a grep pipeline
-------------------------------------------
`grep -oE "^### P[0-9]+\\.[0-9]+"` over `prompt_history.md` overcounts. A `## [Sandglass work]`
log entry legitimately quotes the block it ran inside a fenced ``` block, and that quoted
`### P3.05` heading is indistinguishable from a completion record to any grep. In the project
this was first built for, three blocks were counted as executed for four days while their
files were still 3-line placeholders -- and they fell out of the queue entirely, because the
dashboard said they were done.

So this script:
  * ignores every heading inside a fenced code block -- a quoted prompt is not a completion;
  * ignores `### [VOIDED ...] P<id>` headings -- positive evidence a block did NOT land;
  * counts UNIQUE ids, because history legitimately contains re-pastes;
  * reports blocks listed in BOTH files, which must always be empty: with several runners
    sharing one queue file the cut is the only lock, and a block that executes twice is, in a
    trading or deployment project, a duplicated side effect.

Conventions it parses (keep `CLAUDE.md` and this file in agreement)
-------------------------------------------------------------------
  queue     `prompt_tools/future_prompts.md`   blocks separated by lines of `====`
            `### P<phase>.<n> -- <title> `S|M|L``     heading, size label optional
            `**Depends on**: P1.02, P1.03 ...`        dependencies, must point backwards
            `model:` / `effort:` front matter          routing, one blank line after
  history   `prompt_tools/prompt_history.md`
            `## P<id> -- ... [executed YYYY-MM-DD]`   completion record (## or ### both count)
  roadmap   `master_plan/ROADMAP.md`, optional
            a `PHASE-ESTIMATES` comment block -- see `_roadmap_weeks` below

Nothing here is specific to one project, and it is stdlib-only on purpose: it must run with a
bare `python` in a project whose dependencies are not installed yet.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "prompt_tools" / "future_prompts.md"
HISTORY = ROOT / "prompt_tools" / "prompt_history.md"
ROADMAP = ROOT / "master_plan" / "ROADMAP.md"

# `## P3.04 - ... [executed ...]` and `### P6.01 - ... [executed ...]` are both real completion
# records; conventions drift mid-project, so this is level-agnostic on purpose.
HEADING = re.compile(r"^#{2,3} (?:\[(?P<voided>VOIDED[^\]]*)\]\s*)?(?P<id>P\d+\.\d+[a-z]?)\b")
QUEUE_HEADING = re.compile(r"^### (?:\[(?P<voided>VOIDED[^\]]*)\]\s*)?(?P<id>P\d+\.\d+[a-z]?)\b")
DEPENDS = re.compile(r"^\*\*Depends on\*\*: ([^·]+)")
BLOCK_ID = re.compile(r"P\d+\.\d+[a-z]?")

# Ids executed before the completion-heading convention existed, whose records a parser cannot
# find. Add one ONLY when the code it names is verified on disk -- never to make a number look
# better. Most projects leave this empty forever.
LEGACY_EXECUTED: frozenset[str] = frozenset()

# --- forecasting inputs -------------------------------------------------------------------
# A block's `S|M|L` label is the only size signal the queue carries, so it is the unit of work.
# An unlabelled block counts as M: assuming S would flatter the forecast, and the conservative
# reading should win in a status file people plan against.
SIZE_UNITS = {"S": 1, "M": 2, "L": 3}
DEFAULT_UNITS = 2

# `effort:` is usually set by blast radius rather than duration, but it is the best proxy
# available for how long a block takes, and it is present on every queued block. Medium = 1.0 is
# the baseline. These are judgement, not measurement -- the softest numbers on the dashboard,
# and the panel says so out loud.
EFFORT_WEIGHT = {"low": 0.5, "medium": 1.0, "high": 1.5, "xhigh": 2.0, "max": 3.0}

# Velocity is measured over a TRAILING WINDOW ending today, never over the whole project. A
# lifetime average keeps crediting week-one scaffolding speed against week-three hard work, and
# it is monotonic -- it cannot fall, so a week of silence still reads as progress and the finish
# date is flattered forever. A trailing window rises on a good run and decays to zero when the
# work stops, which is the only behaviour worth putting on a dashboard. Three days is short
# enough to react within a session and long enough to survive one quiet day. The cost is sample
# noise, so the panel always prints the per-day tally beside the derived date. Widen this if the
# project's cadence is burstier than daily.
WINDOW_DAYS = 3


def scan(path: Path, pattern: re.Pattern[str]) -> tuple[list[str], list[str]]:
    """Return (live_ids_in_order, voided_ids). Fenced headings are ignored."""
    live: list[str] = []
    voided: list[str] = []
    if not path.is_file():
        return live, voided
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = pattern.match(line)
        if not m:
            continue
        (voided if m.group("voided") else live).append(m.group("id"))
    return live, voided


def block_meta(path: Path, pattern: re.Pattern[str]) -> dict[str, tuple[str | None, str | None]]:
    """Map each live block id to (size letter, execution date) read off its heading."""
    out: dict[str, tuple[str | None, str | None]] = {}
    if not path.is_file():
        return out
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = pattern.match(line)
        if not m or m.group("voided"):
            continue
        tail = line[m.end():]
        size = re.search(r"`([SML])`", tail)
        date = re.search(r"(\d{4}-\d{2}-\d{2})", tail)
        out.setdefault(
            m.group("id"), (size.group(1) if size else None, date.group(1) if date else None)
        )
    return out


def phase_of(block_id: str) -> str:
    return block_id.split(".")[0]


def units_of(size: str | None) -> int:
    return SIZE_UNITS.get(size or "", DEFAULT_UNITS)


def _roadmap_weeks() -> dict[str, tuple[float, float]]:
    """Read the plan's own per-phase week estimates from `master_plan/ROADMAP.md`.

    The contract is an explicit machine-readable block, not a scraped prose table -- a scraped
    table breaks the first time someone reformats it, and silently:

        <!-- PHASE-ESTIMATES
        P1: 2-3
        P2: 1
        -->

    `P<n>: <low>-<high>` in weeks, or `P<n>: <weeks>` when there is no range. Absent or
    unparseable means estimate C is simply not offered -- better than a fabricated one.
    """
    if not ROADMAP.is_file():
        return {}
    text = ROADMAP.read_text(encoding="utf-8")
    block = re.search(r"<!--\s*PHASE-ESTIMATES(.*?)-->", text, re.S)
    if not block:
        return {}
    out: dict[str, tuple[float, float]] = {}
    for line in block.group(1).splitlines():
        m = re.match(r"\s*(P\d+)\s*:\s*([\d.]+)\s*(?:[-–]\s*([\d.]+))?\s*$", line)
        if m:
            lo = float(m.group(2))
            hi = float(m.group(3)) if m.group(3) else lo
            out[m.group(1)] = (lo, hi)
    return out


def forecast(queue_text: str, executed_meta: dict[str, tuple[str | None, str | None]],
             n_executed: int, queued_ids: list[str]) -> list[str]:
    """Return the forecast panel's lines. Independent estimates, never averaged."""
    today = dt.date.today()
    lines: list[str] = []

    if not queued_ids:
        return ["  queue is empty - nothing left to forecast."]

    done_units = sum(units_of(s) for s, _ in executed_meta.values())
    done_units += DEFAULT_UNITS * max(n_executed - len(executed_meta), 0)

    efforts = re.findall(r"(?m)^effort: (\w+)", queue_text)
    sizes = re.findall(r"(?m)^### P[\d.]+[a-z]? .*?`([SML])`", queue_text)
    left_units = sum(units_of(s) for s in sizes)
    left_units += DEFAULT_UNITS * max(len(queued_ids) - len(sizes), 0)
    effort_avg = (
        sum(EFFORT_WEIGHT.get(e, 1.0) for e in efforts) / len(efforts) if efforts else 1.0
    )
    lines.append(f"  remaining   {left_units:>4} units across {len(queued_ids)} blocks, "
                 f"avg effort {effort_avg:.2f} (medium = 1.00)")

    weeks = _roadmap_weeks()
    roadmap_line: str | None = None
    if weeks:
        lo = hi = 0.0
        for ph, left in Counter(phase_of(b) for b in queued_ids).items():
            total = left + sum(1 for b in executed_meta if phase_of(b) == ph)
            share = left / total if total else 1.0
            w_lo, w_hi = weeks.get(ph, (0.0, 0.0))
            lo += w_lo * share
            hi += w_hi * share
        if lo or hi:
            roadmap_line = (
                f"  C - ROADMAP's own bottom-up estimate ....... "
                f"{today + dt.timedelta(weeks=lo):%Y-%m-%d} to "
                f"{today + dt.timedelta(weeks=hi):%Y-%m-%d}   ({lo:.1f}-{hi:.1f} weeks)"
            )

    dated = [(dt.date.fromisoformat(d), units_of(s)) for s, d in executed_meta.values() if d]
    if not dated:
        lines.append("")
        lines.append("  A/B - no velocity yet: no completion heading carries an [executed")
        lines.append("        YYYY-MM-DD] date. Date them as blocks are cut and the rolling")
        lines.append("        estimate appears here on its own.")
        if roadmap_line:
            lines.append(roadmap_line)
        return lines

    dates = sorted(d for d, _ in dated)
    first, last = dates[0], dates[-1]
    win_start = today - dt.timedelta(days=WINDOW_DAYS - 1)
    in_win = [(d, u) for d, u in dated if win_start <= d <= today]
    win_units = sum(u for _, u in in_win)
    per_day = Counter(d for d, _ in in_win)
    tally = " ".join(
        f"{(win_start + dt.timedelta(days=i)):%m-%d}:"
        f"{per_day.get(win_start + dt.timedelta(days=i), 0)}"
        for i in range(WINDOW_DAYS)
    )
    all_time = done_units / max((today - first).days, 1)

    lines.insert(0, f"  velocity    {win_units:>4} units / {WINDOW_DAYS} days = "
                    f"{win_units / WINDOW_DAYS:.1f} units/day   "
                    f"(all-time {all_time:.1f}, from {len(in_win)} block(s) in window)")
    lines.insert(0, f"  window      last {WINDOW_DAYS} days "
                    f"({win_start:%Y-%m-%d} .. {today:%Y-%m-%d})   blocks/day  {tally}")
    lines.append("")

    if win_units == 0:
        lines.append(f"  A - STALLED: nothing cut in the last {WINDOW_DAYS} days. A trailing")
        lines.append("      window reports no velocity rather than borrowing an older one,")
        lines.append("      so there is no date here until something lands.")
        lines.append("  B - STALLED, same reason.")
    else:
        rate = win_units / WINDOW_DAYS
        raw_days = left_units / rate
        lines.append(f"  A - at the {WINDOW_DAYS}-day rolling rate .............. "
                     f"{today + dt.timedelta(days=round(raw_days)):%Y-%m-%d}"
                     f"   ({round(raw_days)} days)")
        adj = raw_days * effort_avg
        lines.append(f"  B - same rate x remaining effort mix ....... "
                     f"{today + dt.timedelta(days=round(adj)):%Y-%m-%d}   ({round(adj)} days)")

    if roadmap_line:
        lines.append(roadmap_line)
    lines.append("")
    lines.append(f"  last cut {last:%Y-%m-%d}. A is a FLOOR, not a forecast: a {WINDOW_DAYS}-day")
    lines.append("  window is a few samples wide, and it cannot see a gate it has no data for.")
    if roadmap_line:
        lines.append("  C is the only estimate built bottom-up from the work itself.")
    lines.append("  Trust the SPREAD; never average these and never quote one as THE date.")
    return lines


def main() -> int:
    queued, queued_voided = scan(QUEUE, QUEUE_HEADING)
    executed, executed_voided = scan(HISTORY, HEADING)

    uniq_queued = sorted(set(queued))
    uniq_executed = sorted(set(executed) | LEGACY_EXECUTED)
    problems: list[str] = []

    if not QUEUE.is_file():
        problems.append(f"no queue file at {QUEUE}")
    if len(queued) != len(uniq_queued):
        dupes = [i for i, n in Counter(queued).items() if n > 1]
        problems.append(f"duplicate headings inside the queue: {dupes}")
    if queued_voided:
        problems.append(f"VOIDED heading in the QUEUE (it belongs in history): {queued_voided}")

    both = sorted(set(uniq_queued) & set(uniq_executed))
    if both:
        problems.append(f"listed in BOTH files - can execute twice: {both}")

    position = {bid: i for i, bid in enumerate(queued)}
    current: str | None = None
    if QUEUE.is_file():
        for line in QUEUE.read_text(encoding="utf-8").splitlines():
            m = QUEUE_HEADING.match(line)
            if m:
                current = m.group("id")
                continue
            d = DEPENDS.match(line)
            if d and current:
                for dep in BLOCK_ID.findall(d.group(1)):
                    if dep in position and position[dep] >= position[current]:
                        problems.append(
                            f"{current} depends on {dep}, which is not earlier in the file"
                        )

    total = len(uniq_executed) + len(uniq_queued)
    pct = (len(uniq_executed) * 100) // total if total else 0

    print(f"executed (unique, non-fenced, non-voided) : {len(uniq_executed)}")
    print(f"queued   (unique)                         : {len(uniq_queued)}")
    print(f"total                                     : {total}")
    print(f"overall                                   : {pct}%  (rounds DOWN)")
    print(f"voided headings in history                : {len(set(executed_voided))}")
    print(f"next block                                : {queued[0] if queued else '(queue empty)'}")
    print()

    phases = sorted(
        {phase_of(b) for b in uniq_executed} | {phase_of(b) for b in uniq_queued},
        key=lambda p: int(p[1:]),
    )
    if phases:
        print(f"{'PHASE':>6}  {'DONE':>4}  {'LEFT':>4}")
        for ph in phases:
            done = sum(1 for b in uniq_executed if phase_of(b) == ph)
            left = sum(1 for b in uniq_queued if phase_of(b) == ph)
            print(f"{ph:>6}  {done:>4}  {left:>4}")
        print()

    queue_text = QUEUE.read_text(encoding="utf-8") if QUEUE.is_file() else ""
    if queued:
        print("tier :", dict(Counter(re.findall(r"(?m)^\*\*TIER: ([A-Z]+)", queue_text))))
        print("model:", dict(Counter(re.findall(r"(?m)^model: (\w+)", queue_text))))
        for label, pattern in (("model:", r"(?m)^model: "),):
            found = len(re.findall(pattern, queue_text))
            if found and found != len(queued):
                problems.append(f"{found} `{label}` lines for {len(queued)} blocks - one is missing")
        print()

    print("FORECAST")
    for line in forecast(queue_text, block_meta(HISTORY, HEADING), len(uniq_executed), queued):
        print(line)
    print()

    if problems:
        print("PROBLEMS (these belong in the dashboard's WARNINGS panel, and in your response):")
        for p in problems:
            print(f"  ! {p}")
        return 1
    print("invariants OK: no double-listing, no forward dependency.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# <PROJECT> — PROGRESS

> **Derived file. Never a source of truth.** Every number here comes from
> `python prompt_tools/count_blocks.py`, which reads
> [../prompt_tools/future_prompts.md](../prompt_tools/future_prompts.md) and
> [../prompt_tools/prompt_history.md](../prompt_tools/prompt_history.md); phase names come from
> [ROADMAP.md](ROADMAP.md). If this file disagrees with any of them, **they win and this file is
> the defect.** Never fix a disagreement by editing the queue to match the dashboard.

```
╔════════════════════════════════════════════════════════════════════════╗
║   < P R O J E C T >   ·   B U I L D   S T A T U S                      ║
║   <one line: what this project is>                                     ║
╠════════════════════════════════════════════════════════════════════════╣
║   as of   <YYYY-MM-DD>   branch  main     head  <sha>                  ║
║   phase   <the one sentence someone needs if they read nothing else>   ║
║   next    <top block id — and whether it is runnable RIGHT NOW>        ║
║   eta     <A floor> · <C plan of record> — see FORECAST. The spread    ║
║           IS the estimate; there is no single date.                    ║
╚════════════════════════════════════════════════════════════════════════╝

  OVERALL   ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░    0%    0 / 0 blocks
            └───── executed 0 ─────┘└──── queued 0 ────┘  0 orphaned

┌─ PHASES ───────────────────────────────────────────────────────────────┐

  ID   PHASE                          DONE  LEFT   BAR            STATUS
  ───  ─────────────────────────────  ────  ────   ────────────   ───────
  F1   <phase name from ROADMAP.md>      0     0   ░░░░░░░░░░░░   QUEUED
  ───  ─────────────────────────────  ────  ────
       TOTAL                             0     0   (0 orphaned = 0)
└────────────────────────────────────────────────────────────────────────┘


┌─ FORECAST — when does the queue empty? ────────────────────────────────┐

  Nothing has been executed yet, so there is no velocity to measure and no
  date to give. Paste `count_blocks.py`'s FORECAST panel here once blocks
  start landing; it fills in on its own.

  When it does, it carries **three estimates that are never averaged**:

  A  a TRAILING 3-DAY rolling velocity — work units cut in the last three
     days ÷ 3. **Not** a project-lifetime average: a lifetime average keeps
     crediting week-one scaffolding speed against week-three hard work, and
     it can never fall, so a quiet week still reads as progress and the
     finish date is flattered forever. The window rises on a good run and
     decays when work stops. If nothing is cut for three days it prints
     **STALLED and no date** — no recent throughput is no basis for a date.
  B  A, scaled by the remaining queue's average `effort:` weight.
  C  ROADMAP.md's own per-phase week ranges, pro-rated by blocks remaining
     ÷ blocks total. Fill in the `PHASE-ESTIMATES` block there or C is
     simply not offered.

  **Trust the spread, not any single date.** A extrapolates a three-day
  sample and cannot see a gate it has no data for — an unavailable
  credential, a device you do not own, a review you do not control. C was
  written before any code existed and is naive in the other direction.
  Read A as a floor and C as the plan of record. B is the softest of the
  three: the effort multipliers are judgement, not measurement.

  A three-day window is a handful of samples, so always keep the per-day
  tally next to the date — it is what stops one good afternoon reading as
  a trend.
└────────────────────────────────────────────────────────────────────────┘


┌─ WHAT ACTUALLY WORKS TODAY ────────────────────────────────────────────┐

  Nothing yet.

  Write this section in **plain language, for someone deciding whether to
  trust the thing** — not a changelog. The rule that keeps it honest:
  describe only what you have SEEN work, and say explicitly what is
  code-complete but never run against the real world. "The tests pass" and
  "it works" are different claims, and this is the file where conflating
  them does the most damage.
└────────────────────────────────────────────────────────────────────────┘


┌─ WARNINGS ─────────────────────────────────────────────────────────────┐

  Nothing yet.

  This panel exists for things that are true and unwelcome: a block cut
  with no code behind it, a dependency claimed done that is not on disk, a
  gate nobody has run, a count that stopped being trustworthy. **If
  `count_blocks.py` exits non-zero, its output goes here and gets said out
  loud** — a silent green dashboard over a broken invariant is worse than
  no dashboard.
└────────────────────────────────────────────────────────────────────────┘


---

## How to keep this file honest

**The first screen is the dashboard.** Status box, OVERALL bar, PHASES table and FORECAST, in
that order and **adjacent**, so the shape of the project is legible without scrolling. Narrative,
counters and warnings live below. Prose inserted between the bars and the forecast is the single
edit that breaks this file's only job.

**Regenerate, never hand-count:**

```bash
python prompt_tools/count_blocks.py     # exits non-zero if an invariant is broken
```

It prints the counts, the overall percentage (rounded **down**), the per-phase split, routing
counters, the next block, and the FORECAST panel. Paste what it prints. It refuses to be fooled by
the two things that fool a grep: a `### P<id>` heading quoted inside a fenced code block (a
`## [Sandglass work]` entry does this legitimately, and it is how blocks get counted as done while
their files are still placeholders) and a `[VOIDED]` heading, which is positive evidence a block
did *not* land.

**Update it in the same task whenever:**

- a block is cut from `future_prompts.md` to `prompt_history.md` (the counts moved);
- blocks are added, split, removed or re-routed;
- a phase reaches its Definition of Done in `ROADMAP.md`;
- something "WHAT ACTUALLY WORKS TODAY" claims stops being true.

Skip it for prose-only edits that touch neither the queue nor shipped behaviour.

**Percentages round down.** A phase is done when its ROADMAP DoD is met and zero of its blocks
remain queued — never because it "basically works".

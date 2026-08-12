# QuotaShield CLI — Work Log

> Older entries live in `master_plan/archive/work_log_archive_2026-08-11.md`. This file keeps the most recent 5 so reading it stays cheap; consult the archive only when you need history older than that.

## 2026-08-08 - Opus 5 - Quota-wait hourglass: real pouring motion, slower pace

### 1. Context Snapshot
- **Goal**: The animation shown while `sandglass execute` waits out a quota limit looked "too fast and incoherent with the usual process" — rework it to match the frame-by-frame spec the user drew in `master_plan/animation.md`: slower, with a visible line of falling sand and the bottom chamber filling from the bottom up.
- **State**: `sandglass-cli/sandglass/execution_engine.py` `_hourglass_frame()` rewritten; tests updated (93/93 pass, 12 hourglass tests). No behavioral change outside the animation.
- **Previous Blocker**: None carried over.

### 2. Work Done
- **Diagnosed why it read as incoherent rather than just fast** — three separate direction bugs, all visible once compared against the drawn spec: `drained = i >= (HALF - level)` emptied the top chamber **neck-first** (the sand *surface rose* instead of dropping), the mirrored condition filled the bottom chamber **top-down** (sand piling in mid-air under the neck rather than on the base), and nothing at all was drawn between the chambers — the only motion between level changes was the neck grain flicker. Fast pace made all three harder to read but wasn't the root cause.
- **Rewrote the frame model around conserved sand**: top row *n* empties exactly as bottom slot *n* fills, where bottom slot 0 is the base cap, slot 1 the widest cone row above it, and so on upward. With `_HOURGLASS_LEVELS = _HOURGLASS_HALF` (3), that leaves the bottom cone's narrowest row (at the neck) permanently clear — which is precisely what the spec drawing does, and it's what makes room for the falling stream to be visible all the way down.
- **Falling stream**: a grain at the centre column of every *other* empty row below the neck, the pattern shifting one row per tick (`(rows_below_neck + phase) % 2`). Verified frame-for-frame against `animation.md` — ticks 0 and 1 reproduce its first two frames character-for-character, including the `╰──.──╯` grain landing in the not-yet-filled base.
- **Slower via structure, not just a bigger sleep**: each level now passes through a half-full `.` state before the full `:` (`_SUBSTEPS_PER_LEVEL = 2`, `_TICKS_PER_SUBSTEP = 3`), and `ANIMATION_TICK_SECONDS` went 0.35 → 0.5. ~2.8s → ~10.5s per pour. Added one trailing still substep (fully poured, no stream) so the loop wrap reads as *the glass being flipped* instead of a glitch mid-pour — a pure-modulo loop with no rest beat is a large part of why the old one felt frantic.
- **Kept deliberately**: the 9-line compact size, the rounded `╭─╮`/`╰─╯` caps (the user noted they couldn't draw those in the spec and drew `(_________)` instead — so the bottom cap now *fills with sand* the way that drawn base does), the pinched `)*(` / `).(` neck, and the countdown folded into the neck row.
- Docs updated in the same task per the user-manual rule: `master_plan/user_manual.md` §6 (new description + a mid-pour frame replacing the stale one), `sandglass-cli/README.md` (said "5 compact lines"; it has been 9 for a while), and a CHANGELOG `[Unreleased]` entry.

### 3. Next Steps (For the next agent)
- `_hourglass_frame` is parameterised by `_HOURGLASS_WIDTH`/`_HOURGLASS_HALF`, but the *sand budget* assumes `_HOURGLASS_LEVELS == _HOURGLASS_HALF`. If the glass is ever made taller, the top gains a row of sand and the bottom gains a slot at the same time, so conservation still holds — but the "narrow row at the neck stays clear" property is what the falling-stream tests assert, so check those first.
- The animation is a liveness indicator, not a progress bar: it loops on a fixed ~10.5s cycle and is *not* scaled to the real remaining wait (the countdown text is the only truthful part). Making one full pour equal the whole wait was considered and rejected — a 3-hour wait would advance a level every 30 minutes and look frozen, which is the exact problem the animation exists to solve.

## 2026-08-08 - Opus 5 - Hourglass: unbroken base line + grain-by-grain heap fill

### 1. Context Snapshot
- **Goal**: Follow-up on the same task — user confirmed the new pouring motion was right, but asked for two corrections: **don't break the bottom line**, and **add partial filling lines**.
- **State**: `sandglass-cli/sandglass/execution_engine.py` `_hourglass_frame()` reworked again; hourglass tests rewritten around the new invariants (96/96 pass, 15 hourglass tests).
- **Previous Blocker**: None.

### 2. Work Done
- **The base line is glass, not sand.** The previous pass had read `animation.md`'s `(_________)` → `(:::::::::)` literally and filled the rounded bottom cap, producing `╰──.──╯` / `╰.....╯` — which visibly *breaks the box outline*. The drawing fills its base only because plain ASCII cannot draw a rounded cap *and* sand resting on it; with `╰─────╯` available there is no reason to. Both caps are now byte-identical in every frame, asserted directly (`test_hourglass_caps_are_glass_and_are_never_broken_by_sand` iterates the whole cycle) rather than spot-checked — this is exactly the kind of thing that silently regresses.
- **"Partial filling lines" → the bottom fills a grain at a time, as a heap.** A row is no longer all-or-nothing: it grows outward from its centre column (`{3}` → `{2,3}` → `{2,3,4}` → …), so most frames show a partial mound. The grain that just landed is drawn loose (`.`) and settles (`:`) when the next arrives — the same glyph as the falling stream, so a grain visually falls, lands, and settles as one continuous object.
- **Conservation is now exact instead of by construction.** Losing the base cap as a fill slot freed the cone to hold the whole pour: 9 interior cells (5+3+1) below, 9 above. The two halves still tick at different granularities (6 half-rows vs 9 grains), so progress is counted in `_PROGRESS_UNITS = lcm(6, 9) = 18` shared units and each side advances on its own beat — neither can drift or finish early, whatever `_HOURGLASS_WIDTH`/`_HOURGLASS_HALF` are set to. The cone now ends *completely* full, which also removed the previous "narrow row at the neck stays permanently empty" asymmetry.
- Settle frames (3 ticks) render the last grain settled rather than loose, so the still moment before the flip really is still. Cycle stays 21 ticks × 0.5s ≈ 10.5s.
- Docs updated in the same task: `master_plan/user_manual.md` §6 (description + a fresh mid-pour frame that no longer shows a broken base), `sandglass-cli/README.md`, and the CHANGELOG `[Unreleased]` entry rewritten to describe the final shape, including why the earlier base-filling reading was wrong.

### 3. Next Steps (For the next agent)
- Two invariants the tests pin, and the reason each exists: **caps never change** (an earlier version broke the base line), and **`_PROGRESS_UNITS` is divisible by both `_TOP_SUBSTEPS` and `_BOTTOM_CELLS`** (otherwise integer division makes one half finish before the other and the pour stops looking conserved). If the glass is ever resized, both still hold — `_ROW_CELLS` is derived from the width/height constants — but re-run the hourglass tests, they are cheap.
- `.` is deliberately overloaded: falling grain, just-landed grain, and half-drained top row. That is a feature (one continuous material), not an oversight — a test asserting "no `.` anywhere in row X" will be ambiguous, so assert on *columns* (`_sand_columns`) and on the centre channel instead, as the current tests do.

## 2026-08-09 - Opus 4.8 - "check progress" rule + Progress.md

### 1. Context Snapshot
- **Goal**: Add a CLAUDE.md rule that reports project progress (against `MASTER_ARQ_SYSTEM_MAP.md` + prompt done/remaining counts), writes it to `master_plan/Progress.md`, and ships in the `new-claude-project` scaffold.
- **State**: `CLAUDE.md` (working + bundled template), `master_plan/Progress.md` (new), `templates/master_plan/Progress.md` (new).
- **Previous Blocker**: None.

### 2. Work Done
- Added a **"check progress" trigger** section to `CLAUDE.md` (inserted before the shutdown ritual). It does three things: (1) qualitative standing vs `MASTER_ARQ_SYSTEM_MAP.md`'s components/Implementation Phases, (2) quantitative done/remaining/percent from `====` blocks in `prompt_tools/{future_prompts,prompt_history}.md` — the two ends of the same pipeline the "future prompts" trigger already moves blocks along, (3) overwrite the live snapshot in `master_plan/Progress.md`. Explicitly scoped it *against* `work_log.md` so the two files don't collapse into one: Progress.md is the at-a-glance status, work_log stays the per-task narrative.
- **Scaffold propagation** was the real point of the request. `new-claude-project` reproduces `master_plan/` filenames as *empty* files (`open(path,"a").close()` — template content is never copied for those dirs), so making it scaffold `Progress.md` meant adding the *filename* to `templates/master_plan/`, not seeding content there. Created `templates/master_plan/Progress.md` as a 0-byte file to match the other template master_plan files.
- CLAUDE.md is copied verbatim by the scaffold and was byte-identical to the bundled template, so synced the rule by `cp CLAUDE.md templates/CLAUDE.md` (verified identical) rather than editing twice and risking drift. `claude-md-update` would do the same, but a direct copy needs no installed CLI.
- Seeded the working `master_plan/Progress.md` with a real first snapshot (Phases 1–2 done, Phase 3 pending; 16 done / 1 remaining / 94%) so the format is visible and the file isn't empty.

### 3. Next Steps (For the next agent)
- The prompt counts in the seeded `Progress.md` are a point-in-time snapshot; re-run "check progress" to refresh them — don't trust the numbers as they age.
- No user-manual entry: this is an agent/dev convention with no operator-facing UI surface.

## 2026-08-09 - Opus 5 - Cut 0.9.0 and refresh stale editable-install metadata

### 1. Context Snapshot
- **Goal**: `pip show sandglass` reported 0.7.0 while the source said 0.8.0. Diagnose and fix, and give the unreleased work a version.
- **State**: 0.8.0 → 0.9.0 in `setup.py` + `sandglass/__init__.py`, CHANGELOG `[Unreleased]` cut as `[0.9.0] - 2026-08-09`, editable install refreshed. 96/96 tests pass.
- **Previous Blocker**: None.

### 2. Work Done
- **The version was not drifting in source — this was stale install metadata.** `setup.py` and `__init__.py` both already said 0.8.0 and `sandglass version` (which reads `__version__`) reported it correctly; only `pip show` said 0.7.0, because an editable install's `dist-info` is written **once, at install time** and is never refreshed by a `git pull`. Worth remembering before "fixing" a version number that isn't wrong: the two numbers answer different questions — `sandglass version` is what the code says *now*, `pip show` is what the code said when `pip install -e .` last ran.
- Bumped to **0.9.0** (minor, not patch: `[Unreleased]` contained a new command, `sandglass commands`, alongside the hourglass rework) and cut the CHANGELOG section with today's date.
- Re-ran `pip install -e .` so the metadata matches; verified `sandglass version`, `pip show` **and** the editable path all agree afterwards, rather than assuming the reinstall preserved the editable link.

### 3. Next Steps (For the next agent)
- Version lives in **two** places (`setup.py`, `sandglass/__init__.py`) with nothing keeping them in sync — they happened to agree here, but a future bump that touches only one will produce a *real* drift that looks exactly like this session's fake one. If it bites once, make `setup.py` read `__version__` from the package instead of hardcoding it.
- A `git pull` never needs a reinstall for code changes (editable install points at the working tree), but it **does** if `entry_points` or `install_requires` change in `setup.py`, and the pip metadata goes stale on any version bump. Cheap rule: re-run `pip install -e .` after pulling a change to `setup.py`.

## 2026-08-09 - Opus 5 - Token-limit notification + sleep-time quiet hours

### 1. Context Snapshot
- **Goal**: Push an explicit "token limits hit" ntfy on a quota hit, and add a command to silence notifications overnight (default 22:00–06:00) so an unattended run can't wake the user.
- **State**: `sandglass-cli/` — new `quiet_hours.py`, touched `notify.py`, `execution_engine.py`, `cli.py`.
- **Previous Blocker**: None.

### 2. Work Done
- **New `sandglass/quiet_hours.py`.** Window as minutes-since-local-midnight in a frozen `QuietHours` dataclass; `contains()` handles the wrap past midnight explicitly (22:00→06:00 is two ranges, not one — the bug this would otherwise have shipped with). Parsing accepts bare hours because that is how the window is spoken about ("22 to 6").
- **Stored globally in `~/.sandglass/settings.json`, deliberately breaking the per-directory pattern.** `queue_source` describes a project so it belongs in the local `.sandglass/`; sleep hours describe the *person* and are identical whichever directory a run was launched from. Per-project would guarantee that the one run that wakes you is the one in the directory you forgot to configure. `SANDGLASS_QUIET_HOURS` overrides the file, matching how the ntfy topic itself is configured, so CI/headless can opt out without a writable home dir.
- **Enforced inside `notify.send()`, not at call sites.** One choke point means all five existing notifications obey it by construction and so will any added later. Suppressed pushes are *dropped, not queued*: by morning the batch has moved on, and a backlog of stale 3am alerts is worse than silence. `is_quiet()` fails open (notify rather than crash) — consistent with this module's standing rule that a notification never breaks a run.
- **"Token limits hit" fires from the detection point in `execute_queue`**, not from `run_with_auto_resume`. That is what makes it reach `--once`, which stops on quota and never enters the auto-resume loop — it was silent before. Removed the loop's old "waiting for quota to refresh" push rather than retitling it: it fired ~1s later for the same event, and two pushes per hit is noise. New `_local_time_str()` renders the reset time as local wall-clock, since the message is read off a phone where `03:15` answers the question and a UTC ISO string doesn't.
- **Tests**: new `tests/test_quiet_hours.py` (20 tests — parsing, midnight wrap, persistence round-trip, corrupt config, env override, fail-open) + a suppression test in `test_notify.py` and a `--once` notification test in `test_execution_engine.py`. `test_notify.py`'s autouse fixture now forces quiet hours off, or the suite would pass or fail according to what time of day it ran at. 124/124 pass.
- Docs: `CHANGELOG.md`, `sandglass-cli/README.md` (Features, CLI table, new "Sleep time" subsection), `master_plan/user_manual.md` §9 + §13.

### 3. Next Steps (For the next agent)
- **Quiet hours silence bad news too** — "batch stopped early" is held like everything else. That is intentional (you can't act on it at 3am) and documented as a gotcha, but it is the design decision most likely to be revisited; a `priority="high"` bypass is the obvious lever if the user ever asks.
- `notify.py`'s docstring used to claim "no persisted settings". That is no longer true and the docstring was corrected — don't restore the old wording.
- Sleep hours read the machine's local clock. No timezone handling: a laptop that travels reports the local time wherever it is, which is the intended behaviour, not an oversight.
- **Landmine, hit during this session: run pytest from `sandglass-cli/`, never from the repo root.** `WORK_LOG_PATH` is the relative path `master_plan/work_log.md`, resolved against the CWD, and the tests don't chdir to a tmp dir — so a full-suite run launched from the repo root makes the work-log backfill append ~8 fake "sandglass execute (claude-opus-4-8)" entries to *this file*, and they get committed if nobody looks. They were stripped by hand here. The real fix is an autouse `monkeypatch.chdir(tmp_path)` fixture in `tests/`, or making the path injectable on `ExecutionEngine` — not yet done.

## 2026-08-11 - Opus 5 - Token-efficiency pass across the CLI and the doc conventions

### 1. Context Snapshot
- **Goal**: Cut what a queued block costs, without cutting output quality.
- **State**: `sandglass-cli/` (client, engine, queue, new `project_docs.py`), plus the CLAUDE.md template and the work_log / future_prompts conventions.
- **Previous Blocker**: None. Trigger was a cost review whose numbers turned out to be a floor, not a total.

### 2. Work Done
- **Accounting was wrong, and everything else depended on it.** `input_tokens` from the CLI is the *uncached remainder*, so the old `input + output` sum dropped the cache buckets. Measured: a do-nothing prompt in Azymetrix billed 23,788 tokens / $0.024 and was recorded as 53. Fixed first, on purpose — no other change here is verifiable without it. Old history rows are stamped `accounting: 1` and excluded from totals rather than silently averaged in.
- **The per-block `work_log.md` re-read was the largest real cost** (~100k tokens/block on a mature project). Replaced with a capped `<project_state>` brief built by `project_docs.py` and an explicit "don't re-read these" instruction — the instruction is load-bearing; without it the agent reads the brief *and* the file.
- **The queue now runs as one warm chained session** (`--session-mode chain`, default). I first shipped per-prompt sessions and deferred chaining over contamination risk; corrected in-session after the user pointed out that not starting cold is the actual goal. Contamination is mitigated rather than avoided: a ~40-token turn separator marks each new task, and `isolate: true` opts a single block out. Chain state lives in `.sandglass/run_state.json`, not memory — the thing it has to survive is the process dying on a quota hit. Also fixes the artifact-gate false positive, since a resumed run knows what it already wrote.
- **`TIER:` markers are honoured rather than ignored.** Treated as a fix, not a new convention: authors were already writing them and every block was running on Opus regardless. Made visible (printed, logged, `--no-tiers`) because it changes which model runs.
- Docs: the CLAUDE.md template now bounds its own read mandate, caps work_log entries at ~20 lines, and teaches the `future_prompts.md` block format. Rotated this repo's log as dogfooding: 117 KB → 16.5 KB.

### 3. Next Steps (For the next agent)
- **Measure a real chained batch.** Everything above is validated by unit tests and a scratch-repo dry run, not by a live multi-block run. Watch the cache read/write split in the summary: chaining should push it heavily toward *read*. If it doesn't, the prefix is still being invalidated somewhere and `--stable-prefix` isn't covering it.
- **Watch for context exhaustion on long queues.** A 20-block chain is one long conversation; Claude Code auto-compacts, but nobody has run a queue that long warm yet. If output quality degrades late in a batch, that's the first suspect — `--session-mode prompt` is the fallback.
- `sandglass queue lint` only checks backticked paths. Dependency-ordering checks between blocks are the obvious next addition.

## 2026-08-11 - Opus 5 - Chained queues died on block 2: prompt parsed as a CLI flag

### 1. Context Snapshot
- **Goal**: Explain and fix a live Azymetrix run that completed block 1 of 34 and failed block 2 in 1.4s with `error: unknown option '---…'`.
- **State**: `sandglass-cli/sandglass/claude_client.py` (command construction only).
- **Previous Blocker**: Resolved. Same batch's earlier "session already in use" wedge was the prior fix; this is a different fault it uncovered.

### 2. Work Done
- **`-p/--print` takes no value.** `claude [options] [prompt]` — the prompt is positional. Sandglass built `["-p", text]`, which happened to work only because prompts had never started with a dash. Chaining introduced one: `CHAIN_TURN_SEPARATOR` opens with a `---` markdown rule, so every block after the first was handed to the option parser as a flag. Block 1 (no separator) always passed; block 2 always died. Verified against the real CLI, not inferred — `claude --print=<text>` also fails, confirming it is not an option at all.
- **Fixed at the parser boundary, not the separator.** Moved the text to the end behind `--`. Renaming the separator would have hidden the bug and left every block opening with a `- ` bullet still broken.
- Prompt 046 stayed queued and the chain session survived, so the batch resumes with `sandglass execute` — no state repair needed.

### 3. Next Steps (For the next agent)
- The Azymetrix run is still the first real chained batch; the "measure a real chained batch" note in the entry above is unanswered. Block 1 billed 8.8M tokens / $7.40 with 98% cache reuse over 15.8 min — the reuse is right, the absolute size is not yet explained. Check whether one block really needs ~150 model turns or whether the artifact gate is provoking rework.
- That account is at 75% of its seven-day quota (`allowed_warning` on the rate-limit event). A 33-block queue at block 1's cost will not fit; consider `--budget-usd` before the next `sandglass execute`.

## 2026-08-11 - Opus 5 - `sandglass why`: a run that stops must say why, on disk

### 1. Context Snapshot
- **Goal**: A live Azymetrix batch stopped overnight and the only copy of the reason was terminal scrollback nobody read. Make the reason survive the terminal.
- **State**: New `sandglass-cli/sandglass/run_report.py`; wired through `execution_engine.py`, `cli.py`, `storage.py`.
- **Previous Blocker**: Resolved — the dash-prefix parse bug from the previous entry; blocks 046/047 then ran warm.

### 2. Work Done
- **Diagnosis had to be done by archaeology** (history.json rows, file mtimes, PID checks) because nothing recorded *why*. That's the bug: the tool is built to run unattended, so the moment it stops is by definition unwatched. Every run now writes `.sandglass/last_run.json` continuously and prints a "Why it stopped" panel.
- **Kept separate from `run_state.json`.** That file is machinery the next run consumes; this one is prose a person reads. Merging them would tempt future code to branch on a diagnostic.
- **The PID is the load-bearing part.** A run killed from outside never reaches its own ending, so a record stuck at `running` with a dead PID *is* the diagnosis. It reports "killed", names the in-flight prompt, and explicitly rules out quota and prompt failure — the two things a reader assumes otherwise.
- Ctrl-C and unhandled exceptions are closed by `cli.py` via `engine.record_stop()`; a quota wait reports `waiting`, not `stopped`, while it is genuinely sleeping.
- Detail text is stored verbatim, never paraphrased: the quota message carries its own reset time.

### 3. Next Steps (For the next agent)
- **Chained-session cost is the open question.** Three blocks billed $38.30 (045 $7.40 → 046 $13.00 → 047 $17.90) with cache_read tracking the same curve (8.5M → 29.4M → 45.3M). Suspicion: in a chained session every API call re-reads the whole accumulated conversation, so per-block cost grows with queue position. If true, `chain` is cheaper only for short queues and should reset every N blocks. Measure before changing the default.
- ntfy silently no-ops when `SANDGLASS_NTFY_TOPIC` is unset and prints no hint at run start — that gap cost this session an hour. Consider a one-line notice in `execute`.

# Changelog

## [Unreleased]

### Added

- **`sandglass commands`.** Prints a table of every command — top-level and nested (`queue *`, `responses *`) — with its one-line description. Introspects the live Typer/Click app tree rather than a hardcoded list, so it can't drift out of sync as commands are added. `sandglass <command> --help` still gives full per-command option help.

### Changed

- **Quota-wait hourglass animation reworked to pour like a real hourglass**, per `master_plan/animation.md`. Three separate things were wrong with the old loop, all of which made it read as "incoherent" rather than as sand falling: the top chamber drained **neck-first** (so the sand surface rose instead of dropping), the bottom chamber filled **top-down** (sand piling up in mid-air below the neck instead of on the base), and there was no stream between the two — only the neck grain flickered. Now: the top empties from its surface **down** (widest row, under the cap, clears first, thinning `:` → `.` before it goes); a thin **stream of grains falls** through the empty part of the bottom cone, one grain every other row shifting down a row per tick; and the bottom fills **from the base up, one grain at a time**, each row growing as a heap outward from its centre so most frames show a *partially* filled row rather than whole rows snapping full. The last grain to land is drawn loose (`.`) and settles (`:`) once the next one arrives, so the falling grain, the landing grain and the settled pile are visually one continuous thing.
- **The rounded caps are glass, not sand.** `╭─────╮` and `╰─────╯` are now byte-identical in every frame — sand piles up *on* the base line and never replaces part of it, and no falling grain punches through it. (The spec drawing fills its own base, `(_________)` → `(:::::::::)`, only because plain ASCII has no way to draw a rounded cap *and* sand resting on it; an earlier pass in this same batch took that literally and broke the line.) The bottom cone therefore holds the sand, and it now fills completely: 9 cells (5 + 3 + 1 interior columns) against 9 in the top, so the pour is conserved rather than merely mirrored.
- **Slowed down, ~2.8s → ~10.5s per pour.** `ANIMATION_TICK_SECONDS` 0.35 → 0.5, and the per-grain/per-half-row granularity above replaces the old four-state whole-row loop — that finer granularity, not just the longer sleep, is what makes it gradual rather than steppy. A cycle ends with three still ticks (fully poured, nothing falling) so the wrap reads as the glass being flipped rather than as a glitch mid-pour. The two halves move at different granularities (6 half-rows up top, 9 grains below), so progress is counted in `_PROGRESS_UNITS = lcm(6, 9) = 18` shared units; `_TICKS_PER_LEVEL`/`_HOURGLASS_LEVELS` are gone, replaced by `_ROW_CELLS`/`_BOTTOM_CELLS`/`_TOP_SUBSTEPS`/`_PROGRESS_UNITS`.
- Unchanged on purpose: the 9-line compact size, the rounded caps, the pinched `)*(` / `).(` neck, and the countdown folded into the neck row.

## [0.8.0] - 2026-07-31

### Added

- **`sandglass new-claude-project [PATH]`.** Scaffolds a new project's Claude Code setup from a bundled template: copies `CLAUDE.md` with content, and creates `master_plan/` and `prompt_tools/` with the same filenames as this repo's own (`SYSTEM_MAP.md`, `work_log.md`, `future_prompts.md`, etc.) but empty — only the filenames are templated, not their content. Defaults to the current directory; anything already present at the target is left untouched (reported as skipped) so re-running on a partially-set-up project is safe. New `sandglass/project_scaffold.py`.
- **`sandglass claude-md-update [SOURCE]`.** Overwrites the bundled `CLAUDE.md` template (`sandglass/templates/CLAUDE.md`) with `SOURCE` (default: `./CLAUDE.md`), so the next `new-claude-project` run picks up whatever the current project's CLAUDE.md now says. The two commands are meant to be used together: edit a project's CLAUDE.md, run `claude-md-update` to adopt it as the new baseline, then `new-claude-project` elsewhere to propagate it.
- Bundled `sandglass/templates/` (CLAUDE.md + empty `master_plan/`/`prompt_tools/` placeholder files), wired into `setup.py` via `package_data`/`include_package_data`, plus a `MANIFEST.in` so the templates ship in sdists too.

## [0.7.0] - 2026-07-23

Five related changes from one queued batch in `prompt_tools/future_prompts.md`.

### Added

- **ntfy.sh push notifications.** New `sandglass/notify.py` (stdlib `urllib` only, no new dependency): `notify.send(message, title, priority)` posts to `{SANDGLASS_NTFY_SERVER:-https://ntfy.sh}/{SANDGLASS_NTFY_TOPIC}`, a silent no-op if `SANDGLASS_NTFY_TOPIC` isn't set, and never raises on a network/server failure. `ExecutionEngine.run_with_auto_resume` now sends one at each real lifecycle event: a quota hit starting a wait, the wait ending and resuming, the whole queue completing, and the batch stopping early (non-quota error, or giving up after too many stalls — `priority="high"`). New `_load_dotenv()` in `sandglass/__init__.py` (also stdlib-only) loads `KEY=VALUE` lines from a `.env` file in the CWD on import, without overriding an already-set env var — the natural way to keep `SANDGLASS_NTFY_TOPIC` around without exporting it by hand every run.
- **Quota-interruption audit marker.** When a markdown-sourced prompt (`origin_file` set) hits a `QuotaExceededError`, `ExecutionEngine._note_interruption` now appends an "INTERRUPTED, will auto-resume" note to the sibling `prompt_history.md` — the block itself is *not* cut (only a successful completion does that), so the retry picks the exact same prompt back up in full once the quota refreshes, and the gap between "started" and "eventually completed" is now visible in the log instead of silent. New `prompt_source.append_interruption_note()`; the shared "insert after the intro `---` marker" logic used by both this and `prepend_to_history` was factored into a new private `_insert_entry()`.

### Changed

- **`RESET_BUFFER_SECONDS` 30s → 2 minutes.** Retrying right at the exact reported quota-refresh timestamp risks hitting the same limit again before the server-side window has actually rolled over; a 2-minute safety margin (up from 30s) per explicit user request. (Test doubles that hardcoded a `-60s`-in-the-past `resetsAt` to stay under the old buffer's floor were updated to scale with `RESET_BUFFER_SECONDS`, or they'd have started actually sleeping ~60s per quota-retry test.)
- **`prompt_source.prepend_to_history()` gained `via=`/`label=` keyword params** (defaulting to the existing `"sandglass execute"` / `"Sandglass work"` behavior). Fixes a real mislabeling bug: the function unconditionally tagged every entry as a headless `sandglass execute` run regardless of caller, so an interactive chat session processing `future_prompts.md` by hand (calling this same function directly, as the manual "future prompts" workflow does) got its own entries mislabeled as headless ones — exactly the distinction CLAUDE.md says must stay clear between the two execution paths. Pass `label=None` and a different `via` for a manually-run entry.
- **Hourglass neck now renders as `)*(` / `).(`** (parens as the pinched waist, flickering grain between them) instead of a single `*`/`.` character, per explicit request. Caught mid-session: an earlier pass had verified the line-count and rounded-cap parts of this same request but missed that its own notes said "drain/fill/neck-flicker logic untouched" — the neck motif had never actually been implemented despite looking done at a glance.

### Fixed

- Nothing else — the `prompt_history.md` routing and the 9-line/rounded-cap hourglass sizing from the two queued prompts checked in this batch were confirmed already correct from an earlier session; not redone.

## [0.6.4] - 2026-07-23

### Added

- **`work_log.md` backfill for projects that keep one.** This project's own CLAUDE.md mandates a `master_plan/work_log.md` session report after every task, and a queued prompt's headless `claude -p` run genuinely can (and, confirmed in practice, sometimes does) satisfy that on its own — but small/quick prompts routinely skip it. `sandglass execute` now snapshots `work_log.md`'s mtime+size before each prompt and compares again after; if unchanged (the prompt didn't self-log), it appends a short, clearly-labeled fallback entry (prompt as goal, `_summarize_response()`'s one-line summary as work done, a pointer to the full saved response) — if changed, it leaves the prompt's own real entry alone, avoiding a duplicate. Only activates if `master_plan/` already exists in the CWD; Sandglass is a generic tool and won't invent this convention elsewhere. New `ExecutionEngine._work_log_snapshot()` / `_append_work_log_entry()`, `WORK_LOG_PATH`.

## [0.6.3] - 2026-07-23

### Changed

- **Shrank the quota-wait hourglass animation to a compact 5-line glyph**, per explicit user feedback that the previous 11-line version (with separate top/bottom border rows and a caption line below) was too big — asked for something closer to the scale of Claude Code's own terminal spinner. `_HOURGLASS_WIDTH`/`_HOURGLASS_HALF` shrunk (9→7, 4→2), the flat border rows were dropped (the widest sand row now doubles as the visual edge), and the live countdown is folded directly into the neck row (`_hourglass_frame(tick, caption)`) instead of a separate caption line below — the whole indicator is now exactly 5 lines, redrawn in place.

## [0.6.2] - 2026-07-22

### Added

- **Animated ASCII sandglass during a quota wait.** Replaces the old periodic "still waiting (N min left)" text lines with a live, terminal-style hourglass — sand visibly drains from the top chamber into the bottom one, with a flickering grain in the neck, redrawn in place (`rich.live.Live(transient=True)`, matching the existing progress spinner's pattern) alongside an accurate live countdown of the actual remaining time. The animation itself loops continuously as a pure liveness indicator (like any spinner) rather than being literally synced to the real wait duration. Verified that piping/redirecting output prints only the start ("Waiting...") and end ("Resuming...") lines — Rich detects the non-terminal output and emits nothing per animation frame, so an unattended multi-hour run never floods a log file. New `_hourglass_frame()`, `ANIMATION_TICK_SECONDS`; `ExecutionEngine._sleep_with_heartbeat` renamed to `_sleep_with_animation`; `HEARTBEAT_INTERVAL_SECONDS` removed (superseded by the animation itself).

## [0.6.1] - 2026-07-22

### Added

- **`execute`'s completion summary now includes a "what has been done" bullet list.** Previously the summary only showed counts (executed/failed/tokens/time); now, whenever at least one prompt completed, it also prints one short line per completed prompt — the first non-empty line of that prompt's response (Claude's own responses tend to open with a concise statement of what happened, e.g. "Done. Fixed the auth bug.", so no extra summarization call is needed), truncated to 80 chars. Omitted entirely when nothing completed (e.g. every prompt failed). New `ExecutionEngine._summarize_response` + `SUMMARY_MAX`; `_print_summary` now takes the full `results: list[Response]` instead of just a count.

## [0.6.0] - 2026-07-22

### Changed — renamed QuotaShield → Sandglass

- **Project, package, and command renamed end-to-end.** `quotashield-cli/` → `sandglass-cli/`, the `quotashield` package → `sandglass`, the `quotashield` console script → `sandglass` (`sandglass queue add`, `sandglass execute`, etc.), and the on-disk storage directory `.quotashield/` → `.sandglass/` (existing `queue.json`/`history.json`/`responses/` migrated in place — nothing was lost). `StorageService.ensure_quotashield_dir()` renamed to `ensure_sandglass_dir()`.
- Reinstalled as an editable package under the new name (`pip install -e sandglass-cli/`); the old `quotashield` distribution was uninstalled.
- Updated all living docs (`README.md`, `master_plan/MASTER_ARQ_SYSTEM_MAP.md`, `master_plan/human_idea.md`, `master_plan/user_manual.md`, the project's `CLAUDE.md`) to the new name. Historical records — this changelog's older entries, `master_plan/work_log.md`'s past sessions, `prompt_tools/history_prompts.md` — intentionally still say "QuotaShield"/"quotashield execute", since that's what the tool was actually called when those events happened.
- No behavior changed — this is a pure rename, not a functional release.

## [0.5.2] - 2026-07-22

### Fixed

- **`quotashield execute` silently dropped prompts instead of running the whole queue.** `prompt_source.parse_blocks` treated `====` lines as open/close *pairs* (1st+2nd delimiter form a block, 3rd+4th form the next, ...), but the real convention — `N` prompts separated by `N-1` `====` lines, as documented in the project's own `CLAUDE.md` and used in the actual `prompt_tools/future_prompts.md` — never wraps each block in its own pair. With 2 prompts and 1 separator, the old pairing logic found zero complete pairs and imported nothing; with 3 prompts and 2 separators, it only picked up the middle one. `parse_blocks` now treats each `====` line purely as a boundary *between* two blocks — a file holding a single prompt (including the last one left after every earlier prompt has been cut out) needs no delimiter at all.
- Updated `tests/test_prompt_source.py`, `test_queue_manager.py`, and `test_execution_engine.py` fixtures off the old wrapped-pair format onto the real separator format; all 52 tests pass.

## [0.5.1] - 2026-07-22

### Fixed

- **`✖ Failed: Separator is found, but chunk is longer than limit`** — `claude -p --output-format stream-json` emits one JSON object per line, and a `bypassPermissions` run reading/writing a large file can easily produce a single line over asyncio's default 64KB-per-line buffer (`asyncio.LimitOverrunError`), aborting the prompt entirely. `claude_client.py`'s subprocess now opens with `limit=STREAM_BUFFER_LIMIT` (10 MB), and the read loop catches `LimitOverrunError`/`ValueError` specifically — killing and reaping the subprocess cleanly and raising a clear message ("this is a QuotaShield buffer limit, not a quota or auth issue") instead of letting an opaque asyncio internal error surface as an unexplained failure.

## [0.5.0] - 2026-07-22

### Added — default queue source (`prompt_tools/future_prompts.md`)

- **`quotashield execute` no longer requires `queue add` for every prompt.** If the queue is empty, it automatically loads every `====`-delimited block from a markdown file — `prompt_tools/future_prompts.md` by default, the same file and delimiter convention the project's manual "future prompts" chat workflow already uses — and runs those instead.
- New module `quotashield/prompt_source.py`: `parse_blocks`/`read_blocks` (parse `====`-delimited blocks, pairing consecutive delimiter lines as open/close), `cut_first_block` (remove just the first block, leaving everything else in the file untouched), `history_path_for` + `prepend_to_history` (archive a completed block into a sibling `history_prompts.md`, newest first — matching the manual workflow's own convention exactly).
- `PromptObject` gained `origin_file`: set when a prompt came from `import_from_markdown` rather than `queue add`. On successful completion, `ExecutionEngine._cut_from_source` cuts that prompt's block out of `origin_file` and archives it — mirroring `queue.json`'s existing "only remove on success" durability guarantee, so a failed or in-flight prompt's block is never lost from the source file.
- New `QueueManager.import_from_markdown(source_file)`: loads every block via `add_prompt(text=block, origin_file=source_file)`, reusing the existing `model:` header parsing automatically.
- New commands: `quotashield queue import FILE` (persists which file `execute` falls back to, in `.quotashield/settings.json["queue_source"]` — doesn't load anything immediately) and `quotashield queue source` (shows the current default). Prompts added directly via `queue add` always take priority — the markdown source is only consulted when the queue is completely empty.
- **This is a distinct execution path from the manual "future prompts" chat trigger**, not a replacement for it — both read the same file and delimiter convention, but the manual workflow runs a block interactively in a Claude Code chat session, while `quotashield execute` runs it headlessly via `claude -p`. Whichever processes a block first cuts it out, so they never double-process the same one. Documented in the project's `CLAUDE.md`.
- 14 new unit tests (`tests/test_prompt_source.py`, plus additions to `test_queue_manager.py` and `test_execution_engine.py`) — 50/50 total pass.

## [0.4.0] - 2026-07-22

### Added — auto-resume through quota hits (Phase 2, shipped early)

- **This is the change that makes the tool actually worth using unattended.** Previously, `quotashield execute` stopped the whole batch the moment a prompt hit the subscription's usage limit — no better than pasting the prompt into a chat window and waiting yourself. Now, `quotashield execute` (the new default; `--once` restores the old behavior) automatically waits out a quota hit and retries, repeating until the queue is empty or something *other* than quota stops it.
- `QuotaExceededError` now carries the raw `rate_limit_event` payload (`resets_at` property) so callers know the exact timestamp the quota is expected to refresh, instead of just knowing "it failed."
- `ExecutionResult` gained `stopped_reason` (`None`/`"quota"`/`"error"`) and `resume_at` (ISO timestamp), so a caller can tell *why* a run stopped and *when* to retry.
- New `ExecutionEngine.run_with_auto_resume(poll_interval=900, max_stalls=5)`: waits until the exact reported reset time (printing a heartbeat every 5 minutes so a multi-hour wait doesn't look frozen), falls back to polling every `poll_interval` seconds if no exact time is known, and never auto-retries a non-quota failure. Gives up after `max_stalls` consecutive attempts make no progress on the same prompt, in case an error was misclassified as quota-related — prevents an infinite loop on a genuinely broken prompt.
- `quotashield execute` gained `--once` (old single-pass behavior) and `--poll-interval` (fallback retry cadence, default 900s / 15 min, matching the interval originally named in `human_idea.md`'s Phase 2 plan).
- 5 new unit tests in `tests/test_execution_engine.py` covering: waiting out a quota hit and completing, giving up after max stalls, not auto-retrying non-quota errors, and the wait-time computation with/without an exact reset timestamp.

## [0.3.0] - 2026-07-22

### Added

- **Per-prompt model override.** `PromptObject` gained a `model` field. Set it explicitly with `quotashield queue add TEXT --model opus`, or via a `model: <name>` header (name, then a blank line, then the prompt body) on prompt text or `--file` content — parsed and stripped by `QueueManager.add_prompt`/`_extract_model_header` before the title is derived. `--model` always wins over a header if both are present. Accepts short aliases (`opus`/`sonnet`/`haiku`, case-insensitive) or a full model ID unchanged — `ClaudeClient._normalize_model` only lowercases values without a hyphen, since full IDs always contain one. `ExecutionEngine`/`ClaudeClient.send_prompt` pass the per-prompt model through to `claude -p --model ...`, falling back to the CLI's default model when a prompt doesn't set one. `queue list` and `execute --dry-run` both show the effective model per prompt.
- The manual `future_prompts.md` "future prompts" workflow adopted the same `model: <name>` header convention (documented in the project `CLAUDE.md`) — since I can't switch my own model, encountering one means asking you to run `/model <name>` first, not automating anything.

## [0.2.2] - 2026-07-22

### Changed

- `quotashield execute`'s default `--permission-mode` changed from `default` to **`bypassPermissions`**. Queued prompts run unattended — there's no one available to answer an interactive tool-permission prompt — so full tool access (file writes, shell commands) with no confirmation step is now the standing default, not an opt-in. A warning is printed before every run regardless of whether the mode was chosen explicitly or left at default. Pass `--permission-mode default` for the previous, stricter behavior.

## [0.2.1] - 2026-07-22

### Removed

- Removed the `ANTHROPIC_API_KEY` environment-variable check/warning from `quotashield execute` entirely — no code in QuotaShield reads or reacts to that variable anymore. Auth visibility is now solely `client.get_auth_status()` (`claude auth status`), which reports the actual logged-in account/plan regardless of what's in the environment.

## [0.2.0] - 2026-07-21

### Fixed — critical architecture correction

- **Execution now goes through the `claude` CLI (`claude -p`, headless mode) instead of the Anthropic Messages API.** The 0.1.0 MVP used `AsyncAnthropic(api_key=...)`, which bills pay-per-token API credits — a different Anthropic product from a Claude Pro/Max subscription's rolling, refreshing quota. Since this tool's entire premise is running batches of prompts against a subscription's quota (not paying separately per token), that was a real bug, not a style choice. Shelling out to `claude -p` runs against whatever account `claude` is logged into, so usage counts against the subscription when logged in via `claude auth login`.
- `ANTHROPIC_API_KEY` is no longer required or read directly by QuotaShield. `quotashield execute` now prints which account/plan will be used before running.
- Added `--permission-mode` on `execute`, since unattended queued prompts can't answer an interactive tool-permission prompt; defaults to `default` (same rules an interactive session would use), with a printed warning if a looser mode is chosen.
- Removed the `anthropic` SDK dependency entirely (`requirements.txt`, `setup.py`).
- Progress display switched from a fake percentage bar (based on a `max_tokens` budget the CLI backend doesn't expose) to a live streamed-character count with an elapsed-time spinner.
- Discovered the CLI backend emits `rate_limit_event`s with real quota status and a `resetsAt` timestamp during execution — a better foundation for the Phase 2 auto-resume feature than anything the API-based approach could have offered.

## [0.1.0] - 2026-07-21

### Features

- Queue management: `queue add` (text/file), `queue list`, `queue remove`, `queue clear`, `queue stats`
- Sequential prompt execution against the Claude API (`execute`, `execute --dry-run`)
- Live progress display (streamed characters, elapsed time)
- Response saving to `.quotashield/responses/response_<id>.json`
- Completed-prompt archive in `.quotashield/history.json`, browsable via `history`, `responses list`, `responses show`
- Atomic, corruption-resistant local JSON storage (no cloud, no database)
- Graceful handling of missing API keys, rate limits, and file errors — a failed run leaves the remaining queue intact for a later retry

### Known limitations

- No auto-polling — execution is manual only (`quotashield execute`)
- No graceful pause/auto-resume on quota hit (the run stops; retry later) — planned for Phase 2
- No background execution — runs in the foreground
- No response export (Markdown/CSV) — planned for Phase 2

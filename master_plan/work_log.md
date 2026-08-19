# QuotaShield CLI — Work Log

> Older entries live in `master_plan/archive/work_log_archive_2026-08-15.md`. This file keeps the most recent 5 so reading it stays cheap; consult the archive only when you need history older than that.

## 2026-08-18 - Opus 5 - A DeepSeek 402 killed a run that had somewhere else to go

### 1. Context Snapshot
- **Goal**: `API Error: 402 Insufficient Balance` should rotate, then fall back, then wait — not stop the batch.
- **State**: `sandglass/providers.py`, `claude_client.py`, `execution_engine.py`, `cli.py`; tests in `test_providers.py` / `test_external_routing.py`.
- **Previous Blocker**: Resolved. A live run stopped at block 5/8 after 82 minutes with three blocks queued and the night still ahead.

### 2. Work Done
- **Credit exhaustion is a different failure from a quota hit, so it gets its own exception.** `ProviderCreditExhaustedError` is deliberately not a `QuotaExceededError`: a quota returns on a clock and waiting is correct; a zero balance returns when a human pays, so waiting is doing nothing all night. `_looks_like_quota_error` never matched the 402 string, which is why it fell through to the generic-error stop.
- **Fallback chain in `_execute_with_rotation`**: the vendor's next key → Anthropic (via `_switch_to_available_account`, which also skips a parked account) → and only if the pool is empty too, a `QuotaExceededError` so the run enters the *existing* hourglass wait instead of dying. That last conversion is the whole reason the wait path didn't need duplicating.
- **A provider may now hold several keys** (`api_keys: [...]`, `providers set --add`), drained in file order. Without that, one empty wallet puts every cheap block back on the subscription quota the routing existed to protect.
- **Credit state is per-run only**, unlike `accounts_state.json`: persisting it would bench a topped-up key on the next run. Same reasoning drives re-offering retired keys after a quota wait — the push went to a phone hours earlier, and a refusal costs nothing.

### 3. Next Steps (For the next agent)
- **Not verified against a live 402** — detection is string-matched (`claude_client._looks_like_credit_error`) because the CLI hands us only its one-line error text. If DeepSeek rewords it, the run reverts to the old generic-error stop. Worth confirming the exact wording next time a key runs dry.
- `_looks_like_credit_error` treats an Anthropic credit refusal as a quota hit, which parks that account for the 1h default cooldown. If a pay-per-token account ever runs a queue, that cooldown is wrong (it never refreshes) — decide then whether a spent account should leave the pool entirely.

## 2026-08-14 - Opus 5 - The artifact gate was archiving refusals as successes

### 1. Context Snapshot
- **Goal**: Explain why one class of stop keeps recurring in Azymetrix, and fix the cause.
- **State**: `sandglass-cli/sandglass/workspace.py` (`_IGNORED_FILES`), `execution_engine.py` (`_ask_why_nothing_changed`), `CLAUDE.md` + template.
- **Previous Blocker**: Resolved. The recursion the user suspected is real and now evidenced.

### 2. Work Done
- **Root cause: a mandated `work_log.md` entry counted as a work product.** CLAUDE.md tells every task to log a session report — including a task that refuses. The gate ignored only `.sandglass/`, so the refusal's own log entry proved "work happened", and the block was cut and archived as executed. Evidence: Azymetrix `response_003.json` opens "P6.08 is **BLOCKED**. I made no code changes", yet P6.08 is in `prompt_history.md` and gone from `future_prompts.md`, with the incriminating log write at `work_log.md:6007`.
- **Why it compounds**: every refusal deletes a block from the plan and adds a false "done", so dependents refuse later and are archived the same way. That project had already lost and restored P1.17/P2.06/P2.07 to this.
- **Excluded four named files, not `master_plan/` wholesale** — that directory also holds architecture docs, and a block whose job is editing one has genuinely delivered. The distinction has its own test.
- **The DONE/BLOCKED/NOOP question never fired on that queue**: 28 of 30 blocks were externally routed, which runs cold by design, and the recovery required a session to resume. Cold blocks now get the question with block text and response attached, without opening a session of their own.
- Wrote `C:\Codes\Azymetrix\prompt_tools\audit_orphaned_blocks.md` — a standalone (deliberately un-queued) block that audits `prompt_history.md` against the repo and reports blocks archived as done but never built.

### 3. Next Steps (For the next agent)
- **The already-orphaned blocks are still orphaned.** The fix stops new ones; run the audit prompt above to find the existing ones. Restore dependencies before dependents.
- Block 002 of that run billed **53M tokens / 30 min** for one SONNET-tier block, 100% cache read, zero cache write. Nothing explains that yet and it dwarfs every other cost in the project. Worth a look before the next long batch.

## 2026-08-14 - Claude (Sonnet 5) - ntfy "arriving all at once in the evening" — diagnosed, not a code bug

### 1. Context Snapshot
- **Goal**: User reported ntfy pushes seem to batch up and land all at once in the evening instead of in the moment.
- **State**: Read-only investigation — `sandglass/notify.py`, `sandglass/quiet_hours.py`, `sandglass/execution_engine.py`, `~/.sandglass/settings.json`, env vars.
- **Previous Blocker**: none.

### 2. Work Done
- **Ruled out the codebase.** Every `notify.send()` call (quota hit, error, no-artifact, stalled, batch complete, and the new 5%-milestone push) fires synchronously at the point the event is detected — nothing buffers or schedules sends. `quiet_hours` is configured 23:00–06:00 (`~/.sandglass/settings.json`; no `SANDGLASS_QUIET_HOURS` override anywhere), which is overnight, not evening, and suppressed pushes are *dropped, not queued* — so there's no release-at-once mechanism even at 06:00.
- **Likely cause is phone-side FCM batching**, not Sandglass: the free `ntfy.sh` path delivers Android pushes via Firebase Cloud Messaging unless "Instant delivery" is enabled in the ntfy app, and Android can defer normal-priority FCM pushes to a maintenance window (often triggered when the phone is next unlocked — commonly evening). User was pointed at two app-side settings (Instant delivery, battery-optimization exemption) rather than a code change.

### 3. Next Steps (For the next agent)
- No code change made or needed. If the evening-batching complaint recurs after the phone-side settings are fixed, revisit whether `notify.send()`'s default priority (only two of ~8 call sites use `"high"`) is worth raising across the board — declined this time as a first step since it doesn't address FCM batching directly.

## 2026-08-14 - Claude (Sonnet 5) - `CLINE: STOP` was inverting a Claude-only safety marker into DeepSeek routing

### 1. Context Snapshot
- **Goal**: User asked why a block explicitly labeled `**TIER: OPUS**` in a live Azymetrix run was showing "Sending to deepseek" in the console.
- **State**: `sandglass-cli/sandglass/queue_manager.py` (`_CLINE_NEGATIONS`, `_external_defaults`); `sandglass-cli/tests/test_providers.py`.
- **Previous Blocker**: none.

### 2. Work Done
- **Root cause: `_CLINE_RE` matches any word after `CLINE:`/`EXTERNAL:`, with no concept of "no."** Azymetrix's queue used `**CLINE: STOP** — OPUS tier: money path... Claude only` as a plain-English warning against external routing. Sandglass parsed it as the opposite: `CLINE: <value>` unconditionally means "route externally," and "STOP" isn't a known tier, so `_external_defaults` fell through to the default provider (deepseek) anyway. `model: opus` front matter survived (model precedence beats the tier marker), but `provider` is decided independently — so the block still shipped to DeepSeek, where "opus" resolved via DeepSeek's own tier map to `deepseek-v4-pro`. Confirmed live: block 7/21 (unattended order placement) had already run this way and billed $7.55; block 8/21 (crash-resume, money path) was mid-run on DeepSeek when caught. 8 more `CLINE: STOP` blocks were still queued.
- **Fix: `_CLINE_NEGATIONS`**, a denylist (`stop`, `no`, `none`, `off`, `never`, `claude-only`, etc.) checked before the value is resolved to a provider — a negation never routes, full stop, rather than being "forwarded to the provider" like a genuinely unrecognised tier name would be. Scoped to the parser so every project sharing this CLI is protected, not just Azymetrix's queue file.
- Regression tests pin the exact failing phrase plus a parametrized sweep of negation words (`test_providers.py`).

### 3. Next Steps (For the next agent)
- **Not yet deployed to wherever Azymetrix's `sandglass execute` was actually running from** — this fix lives in this repo's `sandglass-cli/`; confirm which installed copy that Azymetrix run uses (editable install vs. a separate checkout) before assuming the live run is safe now.
- The 8 `CLINE: STOP` blocks still in Azymetrix's `future_prompts.md` were left as-is (user chose the code fix, not a reword) — they're safe now under the patched parser, but the wording itself still reads like a routing marker to a human; worth flagging to whoever owns that project.

## 2026-08-14 - Claude (Sonnet 5) - `sandglass dashboard` + `phase:` front matter

### 1. Context Snapshot
- **Goal**: A visual, "modern dashboard" alternative to `Progress.md`'s text summary — overall progress, title, and a per-phase breakdown where phases exist — installed alongside every project like the rest of the `master_plan/`/`prompt_tools/` scaffolding, kept simple (no server, no bat-script trickery a browser button can't run anyway).
- **State**: New `sandglass-cli/sandglass/dashboard.py`; `prompt_source.py` (`phase_breakdown`), `queue_manager.py`/`models.py` (`phase` front-matter field), `execution_engine.py` (auto-regenerate hook), `cli.py` (`dashboard` command). New `tests/test_dashboard.py`; `phase_breakdown` tests added to `tests/test_prompt_source.py`.
- **Previous Blocker**: none.

### 2. Work Done
- **Static file, not a server** — settled after discussing the "button that closes/reopens via a .bat" idea: a browser button can't spawn a local process regardless (sandboxing), and a live server is one more background thing to manage. Instead the file is rewritten after every completed block (same hook `Progress.md`'s update already uses) and the page itself carries `<meta http-equiv="refresh">`, so a tab left open catches up with zero clicks and zero extra processes.
- **`phase:` reuses the existing front-matter mechanism** (`_HEADER_KEYS` in `queue_manager.py`, alongside `model:`/`effort:`/`isolate:`/`provider:`) rather than inventing a second convention. The done-count trick: a cut block's raw text (front matter included) survives verbatim inside the fenced quote `prepend_to_history` writes, so `phase_breakdown()` finds a completed block's phase with one regex pass over the whole history file — no need to re-derive per-entry boundaries the way `_count_top_level_headings`'s fence-awareness does for a different problem (telling two `## ` headings apart).
- **Documented in three places, matching precedent** ("here, in the bundled template, and in Azymetrix" — same pattern as the `.sandglass/` ownership fix): this repo's `CLAUDE.md` *and* `sandglass/templates/CLAUDE.md` (an exact mirror, block-writing section included) both got the `phase:` bullet; `README.md` and `master_plan/user_manual.md` got the CLI-level version for projects that don't read this repo's own `CLAUDE.md`.
- Status badge reuses `run_report.effective_reason()` — caught before shipping that it returns the *specific* stop reason (`quota`, `error`, ...) for a stopped run, not the generic `STATUS_STOPPED`, so the color map keys on every `REASON_*` constant, not the status one.

### 3. Next Steps (For the next agent)
- Not yet exercised against a real multi-hour `sandglass execute` run — only unit tests and one manual smoke test (fixture data, `--no-open`). Worth watching the first real run for whether the 8s refresh is intrusive or right, and whether phase names drift (case-sensitive, exact-match only — `Phase 1` and `phase 1` count as different phases, called out in the docs but not enforced in code).
- Left version at 0.10.0 / CHANGELOG `[Unreleased]` rather than cutting a release — this project's own convention (seen at 0.9.0→0.10.0) is to batch several changes before bumping, and nothing forced an immediate release this time.

## 2026-08-14 - Claude (Sonnet 5) - Hero hourglass animation on the dashboard, using the real brand logo

### 1. Context Snapshot
- **Goal**: A "hypnotizing and mesmerizing," centered hourglass animation on `sandglass dashboard`, per the user's ask right after they dropped a logo file (`sandglass-cli/sandglass logo.jfif`) into the repo.
- **State**: `sandglass-cli/sandglass/dashboard.py` (`_icon_data_uri`, `.hero`/`.medallion` markup+CSS); new `sandglass-cli/sandglass/assets/` (`hourglass_icon.jpg`, `logo_full.jfif`); `setup.py`/`MANIFEST.in` (package the new asset); `tests/test_dashboard.py`.
- **Previous Blocker**: none — direct continuation of the same session's dashboard work.

### 2. Work Done
- **Used the real logo instead of hand-rolling an SVG hourglass.** The supplied artwork has soft organic curves and gradient sand, not geometric triangles — cropping/animating a from-scratch CSS shape risked looking like a crude knockoff next to it. Cropped the icon out of the full logo (excluding the wordmark), saved as `assets/hourglass_icon.jpg` (~10KB, JPEG beats PNG badly here since it's gradient-heavy photographic-ish content, not flat color) and kept the full original at `assets/logo_full.jfif` as source. Verified the crop bounds and the final composed page by actually rendering it headlessly (`chrome.exe --headless=new --screenshot`) rather than trusting the HTML source alone — caught nothing wrong, but this is the first dashboard change actually rendered and eyeballed rather than just read as markup.
- **Animation is glow/motion around the static image, not motion baked into the artwork**: a spinning blurred conic-gradient halo (`14s linear infinite`), a breathing radial-gradient glow (`4s ease-in-out`), and a slow pendulum sway on the whole medallion (`6s ease-in-out`) — three simple, well-supported CSS properties (`transform`, `opacity`, `filter: blur`) layered together read as far more "alive" than the sum of their parts, and none of them risk misaligning with the image content the way overlaying fake falling-sand particles on top of the real artwork would have.
- **Icon embedded as a base64 data: URI baked in at generation time**, not a linked file — the dashboard has to stay a single self-contained HTML file (that's the whole point of it being static/served-nowhere), so `write()` couldn't just point `<img src>` at a sibling path. Cached via `functools.lru_cache` since a long `execute` run regenerates the page after every block and it's the same ~10KB file every time.
- **A missing/corrupt icon degrades to no hero section, not a broken page** — `_icon_data_uri()` catches `OSError` and returns `None`; `generate()` skips the whole `<section class="hero">` block rather than emitting a broken `<img>`. Matters because, unlike the auto-regenerate hook (already inside `execution_engine`'s broad try/except), the plain `sandglass dashboard` CLI command has no such safety net of its own.
- Packaged the new `assets/*.jpg` in both `setup.py`'s `package_data` and `MANIFEST.in` — the editable pipx install used for dev doesn't need this (it reads straight off the source tree), but a real `pip install` elsewhere would silently ship a dashboard with no hero without it. Also caught and fixed `setup.py`'s `version="0.9.0"`, stale since the `__init__.py` bump to `0.10.0` a few commits back — two sources of truth for the same version number, now back in sync.
- Deleted the loose `sandglass-cli/sandglass logo.jfif` (space in the filename, sitting at the package root) after confirming its checksum matched the copy now at `assets/logo_full.jfif` — same asset, one clean location.
- **Found and repaired real corruption in this file, caused by an earlier edit this same session.** Inserting the `CLINE: STOP` entry above dropped the ntfy entry's own `## ` heading line, silently merging its body into the entry above (invisible in a normal read — the content was all still there, just missing its own boundary). Compounded by inserting every new entry at the *top* of the file all session instead of appending at the bottom, which is this file's actual convention — `project_docs.rotate_log`/`tail_entries`/`build_brief` all read `entries[-keep:]`, i.e. newest-last. That inversion is why `sandglass rotate-logs`, run immediately after, archived the four newest real entries (this one, dashboard+phase, CLINE:STOP+the-glued-ntfy-body, and the oldest 08-12 entry) while keeping older 08-13/08-14 entries live — it trusted file order to mean chronological order, which by then it no longer did. Rebuilt both this file and `master_plan/archive/work_log_archive_2026-08-15.md` by hand into true oldest-top/newest-bottom order, heading restored.

### 3. Next Steps (For the next agent)
- Static-frame verified only (headless screenshot). The three infinite CSS animations were checked for syntax validity and standard-property support, not frame-to-frame motion in a live browser — worth a real visual look the first time someone actually watches it for more than a screenshot's worth of time.
- If `setup.py`'s version ever drifts from `__init__.py`'s again, that's the second time — might be worth having one read from the other instead of hand-syncing two literals.
- **Append new entries at the bottom of this file, not the top.** This session got it backwards four times in a row before the mistake was caught. `project_docs.split_entries`/`_ENTRY_RE` also has no fence-awareness (unlike `prompt_source._count_top_level_headings`, which was built specifically to avoid this class of bug) — a future entry that quotes a `## `-prefixed line inside a fenced code block would silently split there too. Worth hardening if it's touched again, but wasn't the cause this time.

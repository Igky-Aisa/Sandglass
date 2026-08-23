# Archived entries from work_log.md

Moved here by `sandglass rotate-logs` on 2026-08-23. Newest of these entries is immediately followed, chronologically, by the oldest entry still in the live file.

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

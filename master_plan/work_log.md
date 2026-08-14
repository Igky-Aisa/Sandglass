# QuotaShield CLI — Work Log

> Older entries live in `master_plan/archive/work_log_archive_2026-08-14.md`. This file keeps the most recent 5 so reading it stays cheap; consult the archive only when you need history older than that.

## 2026-08-12 - Opus 5 - Stop re-dispatching blocks another runner already finished

### 1. Context Snapshot
- **Goal**: A live block cost $4.92 and one minute to reply "P6.04 is already complete, I'm not redoing it", then tripped the artifact gate and stopped the queue.
- **State**: `sandglass-cli/sandglass/prompt_source.py` (`already_executed`, `block_identity`), wired into `execution_engine.execute_queue`.
- **Previous Blocker**: Resolved — the run that vanished mid-block had already done P6.04 and cut it from the markdown before dying.

### 2. Work Done
- **The queue and the markdown are two copies of one intent, and they drift.** An interactive session can execute a block and cut it; a run can die between the cut and the queue update. Sandglass then re-sends finished work. Fixed with a free pre-flight rather than a smarter refusal parser — no model involved, no tokens spent.
- **Positive evidence only.** A skip requires the block to be *gone from the source* AND *present in the history file*. Absence alone is not evidence: a re-authored block is also absent, and silently skipping real work is far worse than paying to re-run finished work. The asymmetry drives the whole design.
- **Identity is the block's first markdown heading**, prefix-matched — it names the deliverable and survives re-authoring, and the history copy gains an `[executed …]` suffix, which is why prefix and not equality.
- Deliberately did NOT auto-retry the refusal. The gate exists because auto-cutting once destroyed twelve blocks; feeding "solve it" back to the model would re-introduce that on the model's word.

### 3. Next Steps (For the next agent)
- **Chained-session staleness is expensive.** Block 048 resumed a session hours old and paid 741k of cache *write* for 1,908 output tokens. `CHAIN_MAX_AGE_SECONDS` is 24h but the cache TTL is minutes; a resume past the TTL re-writes the entire accumulated context at 1.25-2x. Consider lowering it to ~1h so a cold start (cheap, bounded) beats a stale resume (unbounded, grows with the conversation).
- Genuine dependency refusals still stop the run. A bounded "classify then act" recovery is designed but unbuilt — see the chat for the DONE/BLOCKED/NOOP shape.

## 2026-08-12 - Opus 5 - Refusals no longer end the batch; stale chains no longer cost a fortune

### 1. Context Snapshot
- **Goal**: Two fixes the last live run demanded — a no-artifact block killing a 31-block queue, and a resumed session billing $4.92 for one minute of work.
- **State**: `execution_engine.py` (`_ask_why_nothing_changed`, `CHAIN_MAX_AGE_SECONDS`), `prompt_source.py` (`cut_block`), `run_report.py`, `cli.py`.
- **Previous Blocker**: Resolved — the already-executed block 048 is now dropped pre-flight.

### 2. Work Done
- **`--on-refusal ask` (default): one cheap turn, then decide.** DONE/NOOP keep the queue moving, BLOCKED stops with the cause named. It is a *question*, never "try again": the three cases want opposite responses and only the run knows which it hit. Unparseable answers stop — continuing is the optimistic branch and has to be earned.
- **A self-report never authorizes a cut.** Left-in-place blocks stay in the queue AND the source file; the run ends `left_in_place`, not `complete`, listing each one. Trusting prose here is precisely the mistake that once destroyed twelve blocks.
- **`cut_block` replaces `cut_first_block` at the call site.** Once a block can be left behind, "first in the file" and "the one that just ran" diverge, and cutting the wrong one destroys unbuilt work. Matches on heading identity; cuts nothing when unsure.
- **Chain staleness was two bugs.** The 24h cutoff ignored the cache's 1h lifetime (a stale resume re-writes the whole accumulated conversation at 1.25-2x — the measured $4.92), and the timestamp was frozen at chain open, so age meant "time since the chain started" rather than idle time. Now 1h from last use.

### 3. Next Steps (For the next agent)
- **Nobody has watched `--on-refusal ask` decide on a real refusal yet.** Watch the first one: if a block talks itself into NOOP when it was really BLOCKED, the queue advances past a missing dependency. `--on-refusal stop` is the fallback.
- Per-block cost across a chain is still unexplained (045 $7.40 → 047 $17.90 with cache_read tracking the same curve). The 1h cutoff addresses stale *resumes*, not growth *within* a warm chain.

## 2026-08-12 - Claude (Opus 5) - Account rotation for sandglass execute

### 1. Context Snapshot
- **Goal**: Let a run continue on the next Claude subscription when one hits its quota, instead of sleeping until the window refreshes.
- **State**: `sandglass-cli/` — new `sandglass/accounts.py`; touched `claude_client.py`, `execution_engine.py`, `cli.py`, `run_report.py`, README, CHANGELOG.
- **Previous Blocker**: Prompt 050 stopped the queue (self-deadlock on `.sandglass/last_run.json` — still unfixed, see Next Steps).

### 2. Work Done
- **Rotation retries the block, it does not skip it.** A quota hit interrupts work that was never done, so `_execute_with_rotation` re-sends the same prompt under the next account. The error only escapes once the pool is drained, and the existing wait path then sleeps until the **earliest** account refreshes — not the one that failed last, which is rarely the same.
- **The token file lives outside the repo by design.** Blocks run `bypassPermissions` in the project tree and responses persist verbatim to `.sandglass/responses/`, so a credential stored inside it can be read by a block and written into a response that outlives the run. Observed precedent: prompt 050 read `.sandglass/last_run.json` unprompted. `Account.__repr__` redacts; the run report carries names and cost, never tokens.
- **`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` are stripped** from the rotated subprocess env — either outranks the subscription token and would silently bill API credits, which announces itself only on an invoice.
- **Sessions are deliberately untouched across a switch.** Transcripts are local and `--resume` replays under any credential (confirmed against 5 days of manual practice). Only Anthropic's server-side cache is per-account, so accounts drain one at a time in file order rather than balancing — one deeply-cached account beats three shallow ones.
- Two claims I asserted and had to retract after checking: that Anthropic's terms prohibit holding multiple accounts (they prohibit *sharing* one — a different thing), and that `--resume` breaks across accounts. Verify before asserting; both cost a round-trip.

### 3. Next Steps (For the next agent)
- **The 050 self-deadlock is still open** and is what actually stopped the queue: a block read `.sandglass/last_run.json`, saw its own runner's PID marked `running` on its own `prompt_id`, and refused to write, believing a competing process held the files. Fix in `project_docs.build_brief` — tell the block its own runner's PID — or stop shaping `last_run.json` like an advisory lock.
- **Live crash fixed: a block cleared `.sandglass/queue.json` mid-run and the engine died removing it.** `IndexError: Index 1 out of range (1-0)` after a completed block costing $10.21 / 14.1M tokens. Not rotation's fault — the block was following the project's own `CLAUDE.md`, which tells blocks to cut their executed prompt, so it cut itself *and* cleared the queue while Sandglass was still holding an index into it. `remove_prompt_entry` now matches id **and text** (ids are per-insert sequence numbers, not identities — a rebuilt queue reissues `001`, and id-only matching would delete an unrelated block; the regression test caught that, not production) and returns None instead of raising. Two root-cause fixes alongside: the injected brief now tells blocks `.sandglass/` is the runner's, not project state, and that a `running` entry is *this* run rather than a lock; and the "future prompts" section of `CLAUDE.md` (here, in the bundled template, and in Azymetrix) now says the cut step applies only to interactive runs.
- Rotation is untested against a *real* second account; only fakes so far (`tests/test_accounts.py`, 17 cases: the 1→2→3→1 cycle, drain-then-wait, no-pool, earliest-reset, revival, restart persistence, redaction).
- **Exhaustion times persist** to `.sandglass/accounts_state.json` (names + epochs, no tokens) — added because "save the timestamp" only pays off across a restart, which is exactly when a crashed overnight run would otherwise burn one block per dead account rediscovering what it already knew.
- **The token pre-flight was wrong and is rewritten.** It inferred validity from `claude auth status` returning an email, so it flagged all three of the user's working tokens as broken. `auth status` does no validation whatsoever — `loggedIn: true` for the literal string `x` — and never carries an email under `oauth_token` auth. Free checks are now limited to what is actually checkable (empty/truncated/whitespace/placeholder, duplicate tokens); real verification is `sandglass accounts --probe`, one minimal request per account, run from an empty temp dir so `CLAUDE.md` isn't discovered. **Third time this session that asserting instead of verifying cost a round-trip** — the pattern is one-sided testing: the bogus-token case was checked, the valid-token case was not.

## 2026-08-13 - Claude (Opus 5) - External provider routing (DeepSeek)

### 1. Context Snapshot
- **Goal**: Let a block opt out of Claude quota entirely and run on DeepSeek, so mechanical work stops competing with the work that actually needs Opus.
- **State**: `sandglass-cli/` — new `sandglass/providers.py`, `tests/test_providers.py`, `tests/test_external_routing.py`; touched `queue_manager.py`, `claude_client.py`, `execution_engine.py`, `models.py`, `cli.py`, both CLAUDE.mds, README, CHANGELOG, user_manual.
- **Previous Blocker**: none new; the 050 self-deadlock is still open (see the previous entry).

### 2. Work Done
- **This is not a second account pool, it is the opposite trade.** The pool spreads a queue over subscriptions already paid for — free at the margin, finite. An external provider is metered and never free, but spends no quota. That framing decided every default below.
- **`**TIER: CHEAP — EXTERNAL-OK**` deliberately does NOT route.** It sits on blocks written months before this feature and means "would be fine externally"; the new `**CLINE: pro**` means "do it". Treating them as one marker would have retroactively shipped a pile of old blocks to a third party on the strength of a comment written without that consequence in mind. Separate regex, separate meaning, test pinning it.
- **`CLAUDE_CODE_OAUTH_TOKEN` is popped from the external subprocess env** (`Provider.subprocess_env`). Left set alongside a redirected `ANTHROPIC_BASE_URL`, a live subscription credential would go to DeepSeek as a bearer token and nothing in the output would look wrong. Single most consequential line in the module; has its own test.
- **A routed block is forced out of the chain in both directions** (`_resolve_session` returns `(None, False)` first). Resuming would replay the accumulated Claude transcript — every file the queue has read — to a third party; letting its turn into the chain would give later Claude blocks another vendor's output as their history. Costs a cold start, which is what the block was routed away to make cheap anyway.
- **External runs are excluded from `AccountPool.record_usage`, and an external 429 no longer rotates accounts.** Both would misreport: the first inflates an account's burn while hiding third-party spend, the second parks a healthy subscription for an hour on someone else's quota.
- **Missing key / unknown provider / `--no-external` fall back to Claude with a warning rather than failing.** A routing marker is a cost preference, not a correctness requirement — and the fallback direction is always *toward* Anthropic, never silently outward.
- Model ids are resolved through the provider (`pro`→`deepseek-v4-pro`), never `self.model`: a Claude id sent to DeepSeek is silently remapped to their equivalent, so the run would quietly get a model nobody chose.

### 3. Next Steps (For the next agent)
- **Never exercised against the live DeepSeek endpoint** — all 12 routing tests use fakes. Before trusting it to a batch, run one real marked block and check three things: that `--model deepseek-v4-pro` is accepted, that tool use (file writes) actually works over their endpoint, and that a real 429 from them is still classified by `_looks_like_quota_error`.
- Cost for external blocks is priced off Anthropic's table because that is the only one the CLI has. If it turns out worth having real numbers, the place to add them is a per-provider rate table in `providers.py` applied in `_response_from_usage`.
- `providers.py` is written for N providers but wired for one. Adding another is a `Provider(...)` entry plus its tier map; nothing else should need to change.

## 2026-08-13 - Claude (Sonnet 5) - SANDGLASS banner in the quota-wait animation

### 1. Context Snapshot
- **Goal**: Show a big "SANDGLASS" block-letter banner and an author signature inside the existing hourglass wait animation.
- **State**: `sandglass-cli/sandglass/execution_engine.py` — `_render_banner`, `_BANNER_FONT`, `_BANNER_SIGNATURE`; `_sleep_with_animation` now composes banner + hourglass + signature into one `Text`.
- **Previous Blocker**: none — cosmetic addition, no dependency on open items.

### 2. Work Done
- **Hand-drawn 5-row block font**, only for the letters SANDGLASS needs (S A N D G L) — no general alphabet, since there's exactly one caller. A module-level `assert` checks every glyph's rows are equal-width at import time, so a future typo in the font fails on `import sandglass`, not silently in a terminal hours into an unattended run. Verified the render both as a raw string (row alignment) and through actual Rich `Text`/`Console` output before wiring it in, since a font that looks right as plain strings can still misrender through Rich's styling.
- **Banner + signature live inside the same `Live` transient region as the hourglass**, not printed separately beforehand. Rebuilt every 0.5s tick alongside the hourglass despite being static, which is cheap (string concat) — the alternative (print once, outside `Live`) would leave a permanent banner in scrollback on every quota-wait, which gets noisy across a multi-account overnight run with several rotations.
- Existing hourglass shape tests (`_hourglass_frame`) untouched and still pass — they exercise the frame function directly, which the banner change doesn't touch.

### 3. Next Steps (For the next agent)
- Purely cosmetic; nothing pending. If the banner ever needs new letters (e.g. a rebrand), extend `_BANNER_FONT` and the import-time assert will catch any row-width mistake immediately.

## 2026-08-14 - Claude (Sonnet 5) - Banner beside the hourglass, and a test-pollution bug closed for real

### 1. Context Snapshot
- **Goal**: Put the SANDGLASS banner beside the hourglass instead of above it; separately, stop the test suite from writing fake entries into this file.
- **State**: `sandglass-cli/sandglass/execution_engine.py` (`_pad_to_height`, `_BANNER_GAP`, `_HOURGLASS_ROWS`); new `sandglass-cli/tests/conftest.py`.
- **Previous Blocker**: none for the layout; the pollution bug was flagged 2026-08-09 as "not yet done" and had recurred at least twice since, including twice in this session.

### 2. Work Done
- **Side-by-side needed vertical centering, not just concatenation.** The banner is 5 rows, the hourglass 9 (`_HOURGLASS_ROWS = 2*_HOURGLASS_HALF+3`); `_pad_to_height` centers the shorter block against the taller one so it sits beside the glass's visual middle rather than pinned to the top.
- **Caught and fixed a real width bug before shipping it.** Appending the live countdown to the hourglass's neck row (as the standalone frame already does) pushed the combined row to ~83 columns, which wraps mid-frame in an ordinary 80-column terminal and breaks the box shape — visible only by actually rendering it through Rich at `Console(width=80)`, not from the raw strings. Fixed by leaving `_hourglass_frame` uncalled with a caption here and printing the countdown as its own line underneath instead; widest composed row is now 68. Test pins width < 80 explicitly so this can't silently regress.
- **The 2026-08-09 test-pollution landmine finally got its real fix.** `_append_work_log_entry` resolves `master_plan/work_log.md` against the process CWD, not any test's `tmp_path`; several tests in `test_execution_engine.py` never chdir, so running the suite with CWD at the repo root — an easy mistake, and one I made twice this session via `cd c:\Codes\sandglass; python -m pytest sandglass-cli/tests -q` — appends real, fake entries to this exact file. New `tests/conftest.py` adds an autouse `monkeypatch.chdir(tmp_path)` for every test; verified by running the full suite from the repo root and diffing this file's size before/after (unchanged). Autouse rather than per-test, because per-test opt-in is exactly what let this recur three times.
- **Stripped 19 already-accumulated junk entries** (the "first"/"second"/"First prompt"/etc. batches dated 2026-08-12 through 2026-08-14) programmatically, matched on the `sandglass execute (claude-opus-4-8) - ` heading pattern rather than by hand, since manual multi-line string matching kept missing near-duplicate blocks. Then ran `sandglass rotate-logs` (file was at 13 real entries, over the ~10 threshold) — 8 older entries archived to `master_plan/archive/work_log_archive_2026-08-14.md`, nothing deleted.

### 3. Next Steps (For the next agent)
- The pollution bug should not recur, but if a work-log entry ever shows up dated to a session that didn't ask for one, check whether a test bypassed `conftest.py`'s fixture (e.g. via `os.chdir` after the fixture already ran, or a subprocess that inherits a different CWD) rather than assuming the fixture failed outright.
- Purely cosmetic on the banner side; nothing pending there.

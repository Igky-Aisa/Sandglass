# Context — cold-start window

The cheapest useful read in this repo. If you arrived **cold** — a block routed
to a non-Claude model, a fresh CLI, a new machine — start here, not with
`work_log.md`. Maintenance rules: see `CLAUDE.md` / `AGENTS.md` → "the
cold-start window". **Newest entry first** in this file (the opposite of
`work_log.md`, which is newest-last — do not carry the habit across).

Open these only when this file isn't enough: `CLAUDE.md`/`AGENTS.md` (rules) ·
`master_plan/work_log.md` (last 2 entries) · `master_plan/Progress.md` (status)
· `master_plan/MASTER_ARQ_SYSTEM_MAP.md` (the plan).

---

## What this repo is

**sandglass** — a Python CLI that runs a queue of prompt "blocks" unattended
through `claude -p` (Claude Code headless), so a night's work executes with no
human present. Blocks live in `prompt_tools/future_prompts.md` separated by
`====` lines and move to `prompt_history.md` when done. Package:
`sandglass-cli/sandglass/`, version 0.10.0. Windows dev machine, PowerShell.

⚠️ **`CLAUDE.md`/`AGENTS.md` also carry rules for an unrelated ECSP/E-Drafter
Flutter platform** — backend deploys, `flutter analyze`, API SPEC comments,
Cloud Run. None of that code exists in this repo (there is no Flutter app, no
backend, no database here). The *protocol* sections do apply: future prompts,
the work_log entry, the reading budget, `.sandglass/` ownership.

## Where things are

- `cli.py` — every command (Typer). `execution_engine.py` — the run loop,
  account rotation, external-provider fallback.
- `claude_client.py` — the `claude -p` subprocess wrapper; owns model/effort
  resolution and stream-json parsing.
- `accounts.py` — the pooled Claude subscriptions. `providers.py` — non-Anthropic
  endpoints reached through the same CLI (DeepSeek today).
- `queue_manager.py` / `prompt_source.py` — blocks, `====` parsing, front matter
  (`model:`, `effort:`, `isolate:`, `phase:`, `provider:`).
- `dashboard.py`, `run_report.py`, `project_docs.py`, `project_scaffold.py`.

## Invariants — violating one of these is a real bug

- `CLAUDE_CODE_OAUTH_TOKEN` is **stripped** from any external-provider
  subprocess (`providers.Provider.subprocess_env`). It is a live subscription
  credential; leaking it to a third-party endpoint would look like nothing.
- External routing is **opt-in per block**, never global-by-default.
- A block running under `sandglass execute` must **not** cut its own entry from
  `future_prompts.md` and must **not** touch `.sandglass/`. The runner does that
  after the block returns. (Ignoring this once cost $10.21 / 14.1M tokens.)
- `work_log.md` entries are appended at the **bottom**. `project_docs` reads
  `entries[-keep:]` and trusts file order to be chronological.
- `CLAUDE.md` ≡ `AGENTS.md`, byte for byte. `diff` them before finishing.

## Now — 2026-08-21

- **Why anything is changing:** Anthropic halved the weekly subscription limit
  this week. Three pooled accounts no longer carry a full month, so the queue
  would stall mid-month. Grok (SuperGrok/X Premium+ *subscription*, OAuth — not
  the metered xAI API) is being evaluated as a second flat-rate executor.
- **Built so far:** the `CLAUDE.md`/`AGENTS.md` mirror pair, this file,
  `sandglass accounts --enable/--disable`, and `sandglass ui` (the dashboard
  with working Run/Stop buttons).
- **Not built:** the second executor itself. A Grok block cannot run yet.
- **Known gap:** `master_plan/SYSTEM_MAP.md` is **empty (0 bytes)** even though
  `CLAUDE.md` names it the first thing to read before grepping the tree.

## Window — last 5 tasks, newest first

### 2026-08-21 — `sandglass ui`: the dashboard with working buttons
A `file://` page cannot spawn a process, so buttons needed a server — foreground,
dies with Ctrl-C, takes its child run with it. Run spawns `sandglass execute`
rather than reimplementing it; Stop interrupts rather than terminates so the
queue survives. Key-in-URL + `Origin` check: `localhost` is not private.

### 2026-08-20 — Grok groundwork: mirror pair, cold-start window, account toggles
Added `AGENTS.md` as a byte-identical twin of `CLAUDE.md` so non-Claude agents
get the same rules; a copy, not a symlink, because git-on-Windows checks
symlinks out as text. Added `sandglass accounts --enable/--disable` so an
account past its weekly cap can be parked without editing the tokens file.

### 2026-08-14 — Hero hourglass animation on the dashboard
Used the real logo asset rather than a hand-rolled SVG; animation is glow and
sway *around* a static image. Also repaired `work_log.md`, which an earlier edit
in that same session had corrupted by inserting entries at the top — that
inversion made `sandglass rotate-logs` archive the four newest entries.

### 2026-08-14 — `sandglass dashboard` + `phase:` front matter
A static self-contained HTML file regenerated after each block, with
`<meta http-equiv="refresh">`, instead of a local server. `phase:` reuses the
existing front-matter mechanism; phase names are exact-match and
case-sensitive, so `Phase 1` and `phase 1` count as two phases.

### 2026-08-13 — Keep the queue moving when an external provider runs out of credit
A metered key hits a zero balance, which waiting never fixes — so the run
rotates to the next key for that vendor and falls back to Anthropic only when
every one is spent. Credit state is per-run and deliberately never persisted.


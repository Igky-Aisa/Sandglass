# Sandglass CLI — Batch Prompt Queue Executor

Queue multiple Claude prompts and execute them sequentially from the command line —
against your **Claude Pro/Max subscription's rolling quota**, not pay-per-token API credits.

## Why this exists

Anthropic has two separate products with two separate billing systems:

| | Claude Pro/Max subscription | Anthropic API |
|---|---|---|
| Auth | `claude auth login` (your claude.ai account) | `ANTHROPIC_API_KEY` |
| Billing | Flat $20–$200/month, usage window that **refreshes** on a rolling schedule | Pay-per-token credits — a running balance, no refresh |

Sandglass runs queued prompts through the **`claude` CLI itself** (`claude -p`, headless
mode), so execution draws on whatever account `claude` is logged into. If that's your
Pro/Max subscription, usage counts against its refreshing quota — not a separate API bill.
`sandglass execute` prints which account/plan it's about to use before running.

## Features

- Queue management: add (text or file), list, remove, clear, stats
- **Default queue source**: if nothing's queued via `queue add`, `execute` automatically loads
  `====`-delimited prompt blocks from `prompt_tools/future_prompts.md` (or wherever `queue import`
  points), cutting each into its sibling `prompt_history.md` (labeled `[Sandglass work]`) as it completes
- Per-prompt model override — mix Opus/Sonnet/Haiku prompts in one queue
- Sequential execution through the Claude Code CLI, with a live progress spinner
- Pre-flight auth check — shows which account/plan will be billed before running
- Responses saved locally as JSON (`.sandglass/responses/`)
- Completed prompts archived to `.sandglass/history.json`
- Dry-run mode to preview a queue before spending any usage
- All data stored locally — no cloud, no telemetry
- A failed or interrupted run leaves the remaining prompts safely queued for a later `sandglass execute`
- If run inside a project using a `master_plan/work_log.md` convention, backfills a lightweight session-log
  entry for any completed prompt that didn't already write its own — see "Work log backfill" below
- Optional ntfy.sh push notifications (quota wait started/resumed, batch complete/stopped early) — see
  "Push notifications" below

**`sandglass execute` auto-resumes through quota hits.** There's still no scheduler or
background daemon — you run it once and it keeps running in that terminal — but a quota
limit no longer just stops the batch. It waits until the subscription's usage window is
expected to refresh (using the exact timestamp Claude Code reports) and automatically
retries, until the queue is empty or something other than a quota limit goes wrong. Pass
`--once` for the old single-pass behavior. See "Auto-resume" below.

## Installation

```bash
# 1. Install Claude Code (if not already) and Sandglass
#    https://claude.com/claude-code
git clone <repo>
cd sandglass-cli
pip install -e .

# 2. Log in with your Pro/Max subscription (skip if already logged in)
claude auth login

# 3. Add prompts
sandglass queue add "Your prompt"

# 4. Execute
sandglass execute
```

## Quick Start

```bash
$ sandglass queue add "Fix auth bug in auth.dart"
✓ Added prompt 1 to queue

$ sandglass queue add --file prompts/tests.txt
✓ Added prompt 2 to queue

$ sandglass queue list
Queued Prompts (2)
  1  Fix auth bug in auth.dart  text  (default)  2026-07-21T14:32:00+00:00
  2  Write integration tests    file  (default)  2026-07-21T14:33:10+00:00

$ sandglass execute
Using you@example.com — pro plan (subscription quota).
[1/2] Fix auth bug in auth.dart
  📤 Sending to Claude (claude-opus-4-8)...
  ✅ Done (8,234 tokens, 1.2 min)
...
Queue Complete!
  ✅ 2 prompt(s) executed
  💰 Total tokens: 15,900
```

## CLI Commands

### Project scaffolding

| Command | Description |
|---|---|
| `sandglass new-claude-project [PATH]` | Scaffold `CLAUDE.md`, `master_plan/`, and `prompt_tools/` from the bundled template into `PATH` (default: current directory). `CLAUDE.md` is copied with content; files under `master_plan/`/`prompt_tools/` are created empty — only their names are templated. Anything already present at the target is left untouched. |
| `sandglass claude-md-update [SOURCE]` | Adopt `SOURCE` (default: `./CLAUDE.md`) as the bundled template, so future `new-claude-project` runs copy its content. |

### Queue management

| Command | Description |
|---|---|
| `sandglass queue add TEXT` | Add a prompt from raw text |
| `sandglass queue add --file FILE` | Add a prompt from a file |
| `sandglass queue add TEXT --model MODEL` | Add a prompt pinned to a specific model (e.g. `opus`, `sonnet`, `haiku`, or a full model ID) |
| `sandglass queue list` | List all queued prompts (including per-prompt model) |
| `sandglass queue remove INDEX` | Remove a prompt (1-based index) |
| `sandglass queue clear [--yes]` | Clear the entire queue |
| `sandglass queue stats` | Show queue size and estimated token usage |
| `sandglass queue import FILE` | Set the default queue source `execute` falls back to (default: `prompt_tools/future_prompts.md`) |
| `sandglass queue source` | Show the current default queue source |

### Execution

| Command | Description |
|---|---|
| `sandglass execute` | Run the queue, auto-resuming through quota hits until it's empty (see Auto-resume below) |
| `sandglass execute --once` | Old behavior: single pass, stop at the first failure (including a quota hit) |
| `sandglass execute --dry-run` | Preview what would run without calling Claude |
| `sandglass execute --poll-interval SECONDS` | Fallback wait between retries when no exact reset time is reported (default: 900 = 15 min) |
| `sandglass execute --permission-mode MODE` | Passed through to `claude -p` (default: `bypassPermissions`) — see Security below |

### History & responses

| Command | Description |
|---|---|
| `sandglass history` | Show completed prompts (archive) |
| `sandglass responses list` | List saved response files |
| `sandglass responses show INDEX` | View a specific saved response |

## Default queue source

You don't have to run `queue add` for every prompt. If the queue is empty when you run
`sandglass execute`, it automatically loads every `====`-delimited block out of
`prompt_tools/future_prompts.md` — the same file and delimiter convention this project's
manual "future prompts" chat workflow already uses (see the project's `CLAUDE.md`). A
`model: <name>` header works here too, same as `queue add`.

```
====

model: opus

Refactor the auth module to use the new token format.

====

====

Write integration tests for the auth module.

====
```

As each block completes successfully, it's cut out of `future_prompts.md` and archived
into its sibling `prompt_history.md` (labeled `[Sandglass work]`) — newest first, mirroring the
manual workflow's own convention exactly. A block that fails (or is still running when the process stops) stays
in `future_prompts.md` untouched, so nothing is lost.

To point at a different file instead — e.g. to keep a separate queue for a different
project or purpose — set it once:

```bash
sandglass queue import path/to/other-queue.md   # persists; overrides the default
sandglass queue source                          # shows the current default
```

This only changes *which file* `execute` checks — it doesn't load anything immediately.
Prompts added directly via `queue add` always take priority: the markdown source is only
consulted when the queue is completely empty.

## Work log backfill

Some projects keep a `master_plan/work_log.md` — a running log of what was done, session
by session. Since `sandglass execute` runs each prompt through a full headless `claude -p`,
that prompt *can* write its own entry there on its own initiative — and often does for
substantial changes — but a small, quick prompt routinely doesn't judge itself worth a
session report.

`sandglass execute` doesn't rely on that judgment call. Before each prompt, if
`master_plan/` already exists in the current directory, it snapshots `work_log.md`'s
modification time and size; after the prompt completes, it checks again. If the prompt's
own run already changed the file, nothing more happens — no duplicate entry. If it didn't,
Sandglass appends a short, clearly-labeled fallback entry itself (the prompt as the goal,
a one-line summary of the response, a pointer to the full saved response) so the log stays
complete either way.

This only ever activates if `master_plan/` already exists — Sandglass won't invent this
convention in a project that doesn't use it.

## Per-prompt model

Every prompt in the queue can use a different model. Two ways to set one:

```bash
# 1. Explicitly, at add time
sandglass queue add "Refactor the auth module" --model opus

# 2. A `model: <name>` header on the prompt itself (text or --file), followed
#    by a blank line — handy when adding prompts from files:
```

```
model: sonnet

Write integration tests for the auth module.
```

`--model` always wins if both are given. Accepts short aliases (`opus`,
`sonnet`, `haiku` — same as `claude --model`) or a full model ID (e.g.
`claude-opus-4-8`). Prompts with no model set use the CLI's default
(`claude-opus-4-8`). `queue list` and `execute --dry-run` both show which
model each prompt will use.

## Auto-resume

Without this, `sandglass execute` would just stop the first time you hit your
subscription's usage limit — no better than pasting the prompt into a chat window
yourself and waiting. Instead, by default:

1. A prompt fails because the quota is exhausted. If it came from a markdown queue
   source (§ Default queue source), this is noted in `prompt_history.md` as
   **interrupted** — the block itself stays put in `future_prompts.md`/wherever it
   came from, so the gap between "started" and "eventually completed" is never silent.
2. Sandglass reads the exact refresh time Claude Code reports (`rate_limit_event` /
   `resetsAt`) and waits until then — showing a small live animated ASCII sandglass in
   your terminal (5 compact lines, spinner-sized, with a live countdown folded in) so a
   multi-hour wait never looks frozen. (Piped or redirected output shows just the
   start/resume messages, no animation spam.) A 2-minute safety buffer is added on top
   of the reported refresh time, since retrying right at the exact boundary risks
   hitting the same quota again before the server-side window has actually rolled over.
3. It automatically retries — since completed prompts are already durably removed
   from the queue, this just continues where it left off, from zero, on the same
   prompt (nothing partial is cached across a quota hit — the full prompt text is
   resent as a fresh request).
4. Repeats until the queue is empty, or something *other* than a quota limit fails
   (a bad prompt, a network error) — those are never auto-retried; the batch stops
   and tells you to look at it.

If Claude Code doesn't report an exact reset time, it falls back to checking every
`--poll-interval` seconds (default 15 min) instead. As a safety net against a
misclassified, permanently-failing error spinning forever, it gives up and asks you
to investigate manually if the same prompt fails the same way five times in a row
without making progress.

```bash
sandglass execute              # default: waits out quota hits, auto-resumes
sandglass execute --once       # old behavior: stop at the first failure
```

### Push notifications (ntfy.sh)

If you're not watching the terminal, set an [ntfy.sh](https://ntfy.sh) topic and
`sandglass execute` (in auto-resume mode) will push a notification at each of these
points: a quota hit starts a wait, the wait ends and it resumes, the whole queue
finishes, or the batch stops early for a non-quota reason.

```bash
export SANDGLASS_NTFY_TOPIC=your-topic-name     # pick something private/hard to guess
# optional: export SANDGLASS_NTFY_SERVER=https://your-self-hosted-ntfy.example.com
sandglass execute
```

Or put the same variable in a `.env` file in the directory you run `sandglass` from —
it's loaded automatically on import (no `python-dotenv` dependency; a plain `KEY=VALUE`
parser, gitignored by convention). If `SANDGLASS_NTFY_TOPIC` isn't set, notifications are
silently skipped — nothing fails or slows down because of it.

Ctrl-C at any point stops the wait cleanly — the queue stays exactly as it was.

## Security: unattended tool access

Queued prompts run through Claude Code non-interactively — there's no one there to click
"allow" on a tool permission prompt. Because of that, `sandglass execute` **defaults to
`--permission-mode bypassPermissions`**: every queued prompt gets full tool access (file
writes, shell commands) with no confirmation step at all. `sandglass execute` prints a
warning before every run as a reminder — **only queue prompts you actually trust.**

If you want a run to respect the same permission rules an interactive `claude` session in
that directory would use instead, pass a stricter mode explicitly:

```bash
sandglass execute --permission-mode default
```

Note that a queued prompt needing a tool that stricter mode doesn't already allow may stall
or get silently denied rather than erroring — `bypassPermissions` is the mode this tool is
actually designed and tested to run unattended.

## Architecture

See [`master_plan/MASTER_ARQ_SYSTEM_MAP.md`](../master_plan/MASTER_ARQ_SYSTEM_MAP.md) for the full technical architecture, and [`master_plan/human_idea.md`](../master_plan/human_idea.md) for the original concept.

## Troubleshooting

| Problem | Fix |
|---|---|
| `sandglass: command not found` | Run `pip install -e .` from `sandglass-cli/` |
| `the 'claude' CLI was not found on PATH` | Install Claude Code: https://claude.com/claude-code |
| `claude is not logged in` | Run `claude auth login` |
| Execution seems to be billing API credits, not my subscription | Run `claude auth status` to confirm `authMethod` is `claude.ai` |
| `.sandglass/ permission denied` | Check folder permissions in the current directory |
| Queue file looks corrupted | It's auto-backed-up to `queue.json.<timestamp>.bak` and a fresh empty queue is created — nothing is silently lost |
| A run stopped partway through (non-quota error) | The prompt that failed and everything after it stay in the queue — fix the issue and run `sandglass execute` again. (A quota hit auto-resumes on its own; see Auto-resume.) |
| Auto-resume gave up ("may not actually be a quota issue") | The same prompt failed the same way 5 times in a row — investigate what's actually wrong before re-running |

## Development

```bash
pip install -r requirements-dev.txt
pytest tests/
```

## Phase 1 MVP (what works)

- Queue add/list/remove/clear/stats, with per-prompt model override
- Default queue source (`prompt_tools/future_prompts.md`, or wherever `queue import` points)
- Sequential execution through the Claude Code CLI, with live progress
- Auto-resume through quota hits (waits for the exact refresh time, retries automatically)
- Pre-flight auth check (shows which account/plan will be used)
- Response saving + history archive
- Dry-run mode
- Local JSON storage only
- ntfy.sh notifications (quota wait started/resumed, batch complete/stopped early)

## Phase 2 Roadmap

- Batch add (multiple files at once)
- Response export (Markdown, CSV)
- Background execution (run detached, without holding a terminal open)
- Scheduled execution (start a run at a specific time, not just on quota refresh)

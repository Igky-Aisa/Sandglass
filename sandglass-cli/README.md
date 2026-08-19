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
- Optional ntfy.sh push notifications (token limits hit, resumed, batch complete/stopped early), with
  quiet hours so an overnight run doesn't wake you — see "Push notifications" below

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

Run `sandglass commands` at any time for a live, auto-generated table of every
command (top-level and nested) with its description — handy when this README
drifts out of sync. `sandglass <command> --help` still gives full option help
for any single command.

### General

| Command | Description |
|---|---|
| `sandglass commands` | List every command, grouped by top-level and subcommand |
| `sandglass sleeptime [START] [END]` | Show or set the hours when notifications are held (default 22:00–06:00) |
| `sandglass version` | Show the installed Sandglass version |

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
| `sandglass queue add TEXT --effort LEVEL` | Add a prompt at a specific reasoning depth (`low`…`max`) |
| `sandglass queue list` | List all queued prompts (including per-prompt model and effort) |
| `sandglass queue lint` | Free static pre-flight: missing referenced paths, empty blocks, bad effort values |
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
| `sandglass execute --no-brief` | Don't prepend the project-state brief; let each block read the full work log itself |
| `sandglass execute --session-mode MODE` | `chain` (default, one warm session for the queue), `prompt` (one per block), `isolate` (all cold) |
| `sandglass execute --effort LEVEL` | Default reasoning depth for prompts that don't set their own |
| `sandglass execute --budget-usd N` | Hard per-prompt spend cap (`claude --max-budget-usd`) |
| `sandglass execute --no-tiers` | Ignore `TIER:` markers in block text |
| `sandglass execute --on-refusal MODE` | `ask` (default) asks a block that wrote nothing whether it was DONE / BLOCKED / NOOP, and keeps going unless blocked; `stop` always stops |
| `sandglass execute --no-skip-executed` | Send blocks even when they were already executed and cut by someone else |
| `sandglass why` | Why the last run stopped — or what it's doing right now (see below) |
| `sandglass dashboard [--source FILE] [--no-open]` | Write a visual status page (progress, phases, run state) to `.sandglass/dashboard.html` and open it (see below) |
| `sandglass rotate-logs [--keep N]` | Archive old `work_log.md` / `prompt_history.md` entries into `master_plan/archive/` |
| `sandglass update [--check]` | Update Sandglass itself from the git repo (see Updating below) |

#### `sandglass why`

A queue runs unattended, so it usually stops while nobody is looking. Every run
writes `.sandglass/last_run.json` as it goes, and prints a **Why it stopped**
block when it ends; `sandglass why` prints that back later, after the terminal
has scrolled or closed:

```
Why it stopped: Token limits hit
  Claude reported the subscription's usage limit while running prompt 048.
  Expected to refresh around 2026-08-11T22:20:00+00:00. 31 prompt(s) still queued.
  │ You've hit your session limit · resets 6:20pm (America/Santiago)
  Next: Re-run `sandglass execute` after the reset; it auto-waits and resumes
  on its own unless you passed --once.
```

It distinguishes the endings that look alike from the outside: a quota **wait**
in progress, a Ctrl-C, a crash, a block that produced no work product, and — the
one nothing else can tell you — a run **killed from outside** (closed terminal,
sleeping machine) that never got to write down why it vanished.

#### `sandglass dashboard`

A nicer view of the same data `Progress.md` and the 5%-milestone push already
have — a self-contained HTML page (progress ring, overall done/remaining/
total, run status, and a per-phase breakdown when blocks declare a `phase:`
front-matter field) written to `.sandglass/dashboard.html` and opened in your
browser. It's static, not served — no port, no background process — and stays
current because `sandglass execute` rewrites the file after every completed
block and the page itself reloads on a short timer, so a tab left open during
a run keeps catching up on its own.

The phase breakdown only appears once at least one block in the project sets
`phase:` (see [How to write a block](#per-prompt-model-and-effort) below) —
projects with no notion of phases just get the overall ring.

### History & responses

| Command | Description |
|---|---|
| `sandglass history` | Show completed prompts with billed tokens, cached tokens and cost |
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

**"Completes successfully" means it changed something.** The cut is a lock: it removes the
only copy of the block text from the queue so no other runner runs it twice. That is only
sound if the block actually produced what it asked for — and a run that reads the repo,
concludes it *cannot* proceed (a dependency was never built), and says so returns exactly
like a run that wrote a thousand lines. So before cutting, Sandglass fingerprints the git
working tree and compares it to a fingerprint taken before the prompt ran. If **nothing
changed**, it refuses the cut, leaves the block where it is, and stops the run:

```
  ✖ No work product: the response changed no file in the working tree.
  Not cutting this block from prompt_tools/future_prompts.md. The response is
  saved in .sandglass/responses/response_042.json — read it before re-running.
  Stopping: a block that refuses usually means a dependency is missing, and the
  blocks after it depend on this one.
```

It stops rather than skipping ahead because a refusal is almost always a missing
dependency, and the blocks queued behind it depend on the one that just refused —
continuing simply burns them the same way.

Sandglass's own `.sandglass/` writes never count as a work product, and the check is
skipped entirely outside a git repo (and for prompts added with `queue add`, which have no
block to protect). For a queue of genuinely read-only prompts — reviews, questions,
summaries that write nothing — pass `--no-require-artifact`.

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

## Per-prompt model and effort

Every prompt in the queue can use a different model and reasoning depth.
Three ways to set them, in order of precedence:

```bash
# 1. Explicitly, at add time
sandglass queue add "Refactor the auth module" --model opus --effort high
```

```
# 2. Front matter on the prompt itself (text or --file), followed by a
#    blank line — handy when adding prompts from files:
model: sonnet
effort: low

Write integration tests for the auth module.
```

```
# 3. A TIER marker near the top of the block, which many queue files already
#    carry as a note to the human reader:
**TIER: CHEAP — EXTERNAL-OK** — add a dirty-flag badge to the editor
```

Models accept short aliases (`opus`, `sonnet`, `haiku` — same as
`claude --model`) or a full model ID. Effort is `low` | `medium` | `high` |
`xhigh` | `max`; lower means fewer, more-consolidated tool calls and less
preamble, which is the cheapest dial available that doesn't change the model.
Tier markers map to a model+effort pair (`SONNET` → `sonnet`/`medium`,
`CHEAP` → `haiku`/`low`); `--no-tiers` ignores them. Prompts with nothing set
use the CLI defaults. `queue list` and `execute --dry-run` both show what each
prompt will actually run as.

A block can also declare `phase:` in the same front matter, in whatever words
the project's own plan uses (`phase: 8`, `phase: Causal stage automation`).
It changes nothing about execution — it's purely a label `sandglass dashboard`
groups done/remaining by, so a project can chart progress per stage instead
of only one overall total. Every block in the same stage needs to spell it
identically, and a project with no notion of phases can just skip it.

## Sending a block somewhere other than Anthropic

Claude Code talks to any endpoint that speaks the Anthropic wire format, and
DeepSeek publishes one for exactly this purpose. A block that says so runs
there instead of spending Claude quota — with every tool it normally has, since
file edits and shell commands never leave your machine.

```bash
sandglass providers set deepseek --key sk-...   # prompted, hidden, if omitted
sandglass providers list                        # what's configured
```

Then say so in the block. Naming the model is enough — you don't need a
separate marker as well:

```
model: deepseek-pro

Rename every `foo_bar` to `fooBar` across `lib/models/`.
```

`deepseek-pro` → `deepseek-v4-pro`, `deepseek-flash` → `deepseek-v4-flash`;
literal ids work too, and so does `--model deepseek-pro` on `queue add`. Two
other spellings mean the same thing: a `**CLINE: pro**` marker near the top of
the block, and `provider: deepseek` front matter (which wins over everything).
Everything else is unchanged: the same tools, the same artifact gate, the same
cut into `prompt_history.md`.

Routing only ever triggers on a **vendor-prefixed** name, so `model: opus`
stays on Claude. That is why the tier is spelled `deepseek-pro` rather than
plain `pro` — a bare `pro` couldn't say which vendor it meant.

**What this is for.** The account pool spreads a queue across subscriptions
you already pay a flat rate for — free at the margin, but finite. An external
provider is the opposite trade: metered, so never free, but it doesn't consume
quota you were saving for the work that actually needs Opus. Mechanical
blocks are the ones worth spending pennies on.

**Four things worth knowing before you use it:**

- **Only marked blocks ever leave Anthropic.** An unmarked block never routes
  out, whatever is configured. Routing sends the prompt, the injected project
  brief, and any file the agent reads to that vendor under *their* retention
  policy — a decision only the block's author can make.
- **`**TIER: CHEAP — EXTERNAL-OK**` does not route.** It predates this feature
  and appears on blocks written long before it existed; "EXTERNAL-OK" is the
  author granting permission, `CLINE:` is exercising it. Old blocks keep
  running on Claude.
- **A routed block runs cold, outside the chain.** Resuming would replay the
  whole accumulated Claude conversation to a third party, and letting its
  output into the chain would give later Claude blocks another vendor's work as
  their own history.
- **Its reported cost is an estimate.** The CLI prices every run off Anthropic's
  table because that is the only table it has. The run summary says which
  blocks those were, and they are never charged to a Claude account's totals.

**Out of credit is not out of quota.** A prepaid balance hitting zero
(`API Error: 402 Insufficient Balance`) doesn't come back on a clock, so
waiting for it is waiting for nothing. The run moves instead: to that vendor's
next key, then off the vendor entirely — every later block marked for it goes
to Claude without re-probing — and it only becomes a wait when Claude has no
quota left either, which is the one case where waiting is the right answer.
A push notification fires at the moment of the refusal, and a run that does end
up sitting out a quota wait gives the vendor one more chance afterwards, since
a top-up during those hours is exactly what the notification asks for. Store a
second key with `sandglass providers set deepseek --add` (without `--add` the
new key replaces the stored one); `providers list` shows how many are held.
Credit state is per-run and never written to disk — an empty balance is undone
by paying, not by time, so persisting it would bench a funded key for nothing.

No key configured, or `--no-external`? The block runs on Claude, with a warning
— the fallback direction is always *toward* Anthropic, never silently outward.
Its `deepseek-…` model name is swapped for the run default at the same time,
since Claude has never heard of it; that substitution is printed too.
Keys live in `~/.sandglass/providers.json` (or `$DEEPSEEK_API_KEY`), outside
the repo, for the same reason account tokens do.

## Installing and updating on another machine

Sandglass is **not on PyPI**. The name `sandglass` there belongs to an
unrelated project, so `pip install sandglass` installs something else
entirely. Always install from the git URL:

```bash
# Recommended — isolated, and `sandglass` lands on your PATH
pipx install "git+https://github.com/Igky-Aisa/Sandglass.git#subdirectory=sandglass-cli"

# Or, if you want to hack on it: clone and install editable
git clone https://github.com/Igky-Aisa/Sandglass.git
pipx install -e ./Sandglass/sandglass-cli
```

Then, on any machine:

```bash
sandglass update --check    # what would change, without changing anything
sandglass update            # do it
```

`update` works out how this machine got Sandglass and acts accordingly:

| Install shape | What `update` does |
|---|---|
| Editable clone (`pipx install -e`, `pip install -e`) | `git pull --ff-only` in the source tree. The code is live immediately — nothing is reinstalled. |
| Copy install (`pipx install git+…`, `pip install git+…`) | Reinstalls from the remote with `pipx` / `uv tool` / `pip`, whichever owns the install. |

**It will not pull over uncommitted changes.** A clone is a working tree, and
a pull across half-finished work is how an afternoon disappears — commit or
stash first. `--force` skips that check but still never discards anything: the
pull is `--ff-only` and git refuses on conflict.

**Checking whether you have a given fix** — `sandglass version` is not
reliable, since the version string only moves on a release. Use:

```bash
sandglass commands | grep rotate-logs   # present ⇒ you have this release
```

## Keeping a long queue affordable

Sandglass exists to run a queue unattended, one block after another, and that
hasn't changed. What changed is that the queue no longer starts from scratch at
every block.

Blocks used to run as **N cold `claude -p` sessions**. Each one re-paid every
fixed cost from zero, and those costs are larger than they look:

- **A do-nothing prompt is not free.** Measured in a project with a large
  `CLAUDE.md`: `"Reply with exactly: OK"` billed **23,788 tokens** before the
  model did anything, because the system prompt, tool schemas and project
  memory are re-sent every time.
- **A `read work_log.md` mandate is paid per block.** On a mature project the
  work log and system map are ~100k tokens, re-read from cold for every block,
  forever, and growing.
- **Repeated runs re-*create* the prompt cache rather than reading it**, unless
  the prefix is byte-identical — and git status lives in that prefix, so any
  block that writes a file invalidates it for the next one.

Sandglass now addresses all three by default, and you can switch each off:

| Default | What it does | Off switch |
|---|---|---|
| `--session-mode chain` | Runs the whole queue as **one warm session**, so only the first block pays for the system prompt, `CLAUDE.md` and the files the queue has already read | `--session-mode isolate` |
| `--brief` | Injects the last two work-log entries + `Progress.md` as a bounded `<project_state>` block and tells the run not to re-read those files | `--no-brief` |
| `--stable-prefix` | Moves cwd/env/git-status out of the system prompt so the cached prefix survives between blocks | `--no-stable-prefix` |
| `--tiers` | Honours `TIER:` markers so cheap blocks run on cheap models | `--no-tiers` |

### What chaining does and doesn't change

**Unchanged:** blocks still run strictly one after another, unattended;
`execute` still auto-resumes through quota hits; each block is still cut from
`future_prompts.md` into `prompt_history.md` as it completes; the artifact gate
still stops a run that produced nothing.

**Changed:** block 6 can see what block 5 did. That's the point — it's why
block 6 doesn't have to re-read the same files — but it means a mistake in
block 5 is visible to block 6 too. Three things keep that manageable:

- Every warm block is prefixed with a short **turn separator** telling the
  model the previous task is finished and this is a new, independent one.
- **A single block can opt out** with `isolate: true` in its front matter. The
  chain resumes on the block after it. Use it for a review that shouldn't see
  the author's reasoning, or a genuine second opinion.
- **The chain ends when the queue drains** (and on `queue clear`, and after 24
  hours). An interrupted run rejoins its session; an unrelated batch tomorrow
  gets a fresh one.

If a queue really needs hermetic blocks, `--session-mode prompt` keeps retries
resumable while giving each block its own session, and `--session-mode isolate`
is the fully cold behaviour.

Two things worth doing by hand:

```bash
sandglass rotate-logs        # archive old work_log entries once past ~10
sandglass queue lint         # free; catches blocks that would refuse anyway
```

`sandglass execute` reports billed tokens, cost, and the **cache read/write
split** at the end of a run. The split is the number to watch: reads are a
fraction of base price and writes a multiple of it, so a run that is mostly
writes is paying a premium for a prefix it never reuses.

> **Note on historical numbers.** Before this accounting fix, Sandglass summed
> `input_tokens + output_tokens` — which omits cache tokens entirely and so
> undercounts a cached run badly (53 reported against 23,788 billed, in the
> measurement above). `sandglass history` marks those older rows with `*` and
> leaves them out of its totals rather than mixing two incompatible numbers.

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
   your terminal (9 compact lines, spinner-sized, with a live countdown folded in) so a
   multi-hour wait never looks frozen. It pours like the real thing: top chamber drains
   from the top down, a thin stream of grains falls through the neck, and the bottom
   fills from the base up a grain at a time, heaping outward from the middle, ~10s per
   pour. (Piped or redirected output shows just the
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

### Several subscriptions: rotating accounts instead of waiting

If you hold more than one Claude subscription, a quota hit does not have to mean
an idle night. Give Sandglass a pool of accounts and it moves to the next one and
**retries the same block**, only falling back to the wait above once every account
in the pool is spent — at which point it sleeps until the *earliest* of them
refreshes, not the last one to fail.

> **These must be accounts you hold yourself.** Anthropic's Consumer Terms forbid
> sharing account credentials or making an account available to anyone else. One
> person with several subscriptions is fine; a pool assembled from other people's
> logins is the thing that is not.

**This is not the Anthropic API.** A `setup-token` token authenticates the same
subscription you log into interactively — `claude auth status` reports
`authMethod: oauth_token` with `apiProvider: firstParty`, where a plain login
reports `authMethod: claude.ai` with the same `firstParty`. The variable that
*would* switch you to pay-per-token billing is `ANTHROPIC_API_KEY`, and
Sandglass strips it (along with `ANTHROPIC_AUTH_TOKEN`) from every rotated
subprocess. How you sign in to claude.ai — Google, email, whatever — makes no
difference; the token represents the account, not the identity provider.

Mint one token per account. `setup-token` mints for whichever account you are
currently signed in as, so it's one pass per account:

```bash
claude auth login      # sign in as account 1
claude setup-token     # copy the token
claude auth logout
claude auth login      # sign in as account 2 …
```

Your day-to-day interactive login is untouched by any of this — tokens are
injected per-subprocess, so you can keep using Claude Code by hand under one
account while a batch runs under another.

Then write them to `~/.sandglass/accounts.json`:

```json
{
  "accounts": [
    { "name": "personal", "token": "sk-ant-oat01-…" },
    { "name": "work",     "token": "sk-ant-oat01-…" }
  ]
}
```

```bash
chmod 600 ~/.sandglass/accounts.json    # POSIX; Sandglass warns if others can read it
sandglass execute                       # --accounts auto (default): rotates if the file exists
sandglass execute --accounts off        # ignore the pool, wait out the quota as before
sandglass execute --accounts rotate     # require the pool; error if the file is missing
```

Set `SANDGLASS_ACCOUNTS` to read the file from somewhere else.

Check the pool any time with `sandglass accounts`:

```
$ sandglass accounts
Accounts (C:\Users\you\.sandglass\accounts.json)
  ● quota available — personal
  ○ spent, expected back around 21:17 — work
```

That much is free, and it is also the limit of what free can tell you:
**`claude auth status` reports `loggedIn: true` for any non-empty string**, so
nothing offline can distinguish a working token from an expired one. To check
for real, spend one tiny request per account:

```
$ sandglass accounts --probe
Probing each token with one small request…
  ✔ personal: works
  ✔ work: works
```

The probe runs from an empty directory so your `CLAUDE.md` isn't
auto-discovered — in a large project that context alone measures ~23,000
billed tokens, which would make checking three tokens cost more than the work
it protects.

`sandglass execute` checks the cheap things at startup — a token that is empty,
truncated, whitespace-wrapped, or still the placeholder from these docs, and
the same token pasted under two names (which makes the pool look bigger than
it is). It does not claim a token is valid.

The cycle, with three accounts:

```
quota hit on account-1  → switch to account-2   (account-1 back at 21:17)
quota hit on account-2  → switch to account-3   (account-2 back at 23:41)
quota hit on account-3  → nothing has quota     (account-3 back at 02:05)
                        → wait until 21:17, the earliest of the three
21:17                   → resume on account-1, keep going until it hits again
```

Each account's refresh time is written to `.sandglass/accounts_state.json`
(names and epochs only, never a token) as it's learned, so a restarted or
crashed run doesn't spend a block rediscovering a quota the previous process
already found — it picks up on an account that actually has capacity. Times
already in the past are ignored, so the file self-clears.

**Keep that file outside your project directory** — the default location already
is. Queued blocks run with `bypassPermissions` in the project tree and their
output is persisted verbatim to `.sandglass/responses/`, so a credential stored
*inside* the repo is one incurious `cat` away from being written into a response
file that outlives the run. Nothing Sandglass writes — logs, run report,
history — ever contains a token; only the account's `name`.

The switch deliberately leaves your sessions alone. Transcripts are local files
that `--resume` replays under any credential, so a chained queue keeps its
context across a rotation. What does not carry over is Anthropic's *server-side*
prompt cache, which is per-account: the first block under a new account pays a
cache write instead of a cache read, and every block after it is warm again.
That's why the rotation drains one account fully before touching the next rather
than balancing across them.

`sandglass why` reports the split when a run used more than one:

```
Accounts used:
  · personal: 7 block(s), 412,331 tokens, $3.9812
  · work: 5 block(s), 288,104 tokens, $2.7710
```

### Push notifications (ntfy.sh)

If you're not watching the terminal, set an [ntfy.sh](https://ntfy.sh) topic and
`sandglass execute` will push a notification (to the free ntfy app on iOS/Android, or
any topic subscriber) at every point a run stops or reaches a milestone: **token limits
hit** (with the expected refresh time), the wait ends and it resumes, **5% prompt-throughput
milestones** as markdown-sourced blocks land, the whole queue finishes, or the batch stops
early for any reason — a crash, a refused block, or one left in place. Every one of these
fires under `--once` too, not only the default auto-resume mode — each is sent exactly once,
at the point it's detected, rather than relying on a wrapper that `--once` never reaches.

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

#### Progress milestones

For a markdown-sourced queue in a project using the `master_plan/` convention, completing
a block pushes a notification every time the project's overall prompt-throughput crosses a
new 5% line (55%, 60%, 65%, ...) — the same done/remaining/total definition CLAUDE.md's
"check progress" trigger already uses, computed fresh from `future_prompts.md` and
`prompt_history.md` rather than waiting for someone to run that trigger by hand. The same
numbers are written straight into `master_plan/Progress.md`'s "Prompt throughput" section,
so it stays live between manual "check progress" runs instead of going stale. Only that one
section is touched — the qualitative "Against the main idea" judgment above it is a call only
a human or an agent reading the architecture doc can make, and this mechanical hook never
overwrites it. Milestones only move forward: a restarted run won't re-announce 5%, 10%,
15%... from scratch, and several blocks landing in the same 5% band notify once.

#### Sleep time (quiet hours)

An unattended run routinely waits out a quota limit overnight, so notifications are
**held between 22:00 and 06:00 by default** — no 3am buzz about something you can't
act on until morning. The run itself is unaffected: it keeps waiting and resuming
either way, and you just won't hear about it until the window ends.

```bash
sandglass sleeptime            # show the window, and whether it's active right now
sandglass sleeptime 22 6       # set it (bare hours, or 22:30 / 06:15)
sandglass sleeptime 23 7 --off # save a window but leave it disabled
sandglass sleeptime --off      # notify at any hour
sandglass sleeptime --on       # re-enable the saved window
```

Notifications inside the window are **dropped, not queued** — by morning the batch has
moved on, and a backlog of stale 3am alerts is worse than none. This includes the
"stopped early" alert, so a failure at 1am is only visible when you next look.

Unlike `queue import`, this is stored **globally** in `~/.sandglass/settings.json`, not
per-directory: you sleep the same hours whichever project you launched a run from.
`SANDGLASS_QUIET_HOURS=22:00-06:00` (or `=off`) overrides the saved value for one run or
one machine, which is the easiest way to opt a CI/headless environment out entirely.

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

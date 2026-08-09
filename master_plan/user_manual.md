# Sandglass CLI — User Manual

A plain-language guide to installing and using Sandglass day-to-day. For the
technical architecture, see `MASTER_ARQ_SYSTEM_MAP.md`; for the original idea,
see `human_idea.md`; for a terser command reference, see
`sandglass-cli/README.md`.

---

## 1. What Sandglass is for

You have a Claude Pro or Max subscription. It gives you a **rolling usage
quota** that refreshes on a schedule — it is not a pay-per-token bill. If you
have a stack of prompts to run (a multi-step build, a batch of refactors, a
pile of one-off questions), running them one at a time and babysitting each
one wastes the plan you're already paying for.

Sandglass lets you:

1. Queue up prompts (as raw text or from files) ahead of time.
2. Walk away.
3. Run `sandglass execute` and let them run sequentially — and if a prompt
   hits your subscription's usage limit partway through, Sandglass waits
   for it to refresh and keeps going on its own, instead of just stopping.
4. Come back to saved responses, a history log, and a queue that only ever
   contains the prompts that *haven't* run yet.

This last point is the actual point of the tool: without auto-resuming past
a quota hit, running a queue is no better than pasting the same prompt into
a chat window yourself and waiting. §6 covers exactly how this works.

**Important:** Sandglass runs prompts through the `claude` command-line tool
itself, in headless mode — it does **not** use a separate Anthropic API key
and does **not** bill pay-per-token API credits. It uses whatever account
`claude` is already logged into. If that's your Pro/Max subscription, that's
what gets used.

---

## 2. First-time setup

You need two things installed before Sandglass is useful:

1. **Claude Code** (the `claude` command). If you don't have it yet:
   https://claude.com/claude-code
2. **Sandglass itself**, from this repo:

   ```bash
   cd sandglass-cli
   pip install -e .
   ```

Then make sure `claude` is logged into your subscription:

```bash
claude auth login
```

You can check this worked at any time with:

```bash
claude auth status
```

which prints something like:

```json
{
  "loggedIn": true,
  "authMethod": "claude.ai",
  "subscriptionType": "pro"
}
```

`"authMethod": "claude.ai"` is what you want to see — it means usage is going
against your subscription, not a separate API bill.

That's it. There is nothing else to configure. Sandglass stores all of its
own data in a `.sandglass/` folder that it creates automatically in
whatever directory you run it from.

---

## 3. Everyday workflow

### Step 1 — Build a queue

Add prompts one at a time, either as text or from a file:

```bash
sandglass queue add "Refactor the auth module to use the new token format"
sandglass queue add --file prompts/write-tests.txt
sandglass queue add "Update the README with the new install steps"
```

Each one gets appended to the queue with a sequential ID (`001`, `002`, `003`,
...) and a title derived from its first line.

**Or skip this step entirely** and just write prompts directly into
`prompt_tools/future_prompts.md` instead — `sandglass execute` picks them
up automatically when nothing's been added via `queue add`. See §5.

Check what's queued at any time:

```bash
sandglass queue list
```

If you change your mind about one:

```bash
sandglass queue remove 2        # 1-based index, matches `queue list`
sandglass queue clear           # asks for confirmation first
sandglass queue clear --yes     # skip the confirmation
```

Before committing to a run, get a rough sense of size:

```bash
sandglass queue stats
```

### Step 2 — Preview before spending anything

```bash
sandglass execute --dry-run
```

This lists every prompt that *would* run, with a rough token estimate, and
sends nothing. Use this to sanity-check a queue before walking away from it.

### Step 3 — Run it

```bash
sandglass execute
```

Before anything runs, Sandglass tells you which account and plan it's about
to use:

```
Using you@example.com — pro plan (subscription quota).
```

Then it works through the queue one prompt at a time:

```
[1/3] Refactor the auth module to use the new token format
  📤 Sending to Claude (claude-opus-4-8)...
  ✅ Done (8,234 tokens, 1.2 min)
[2/3] Write integration tests
  ...
```

Each completed prompt is:
- removed from the queue immediately (so a later crash or interruption never
  re-runs or loses work),
- saved to `.sandglass/responses/response_<id>.json`,
- archived into `.sandglass/history.json`.

If a prompt hits your subscription's usage limit, `sandglass execute`
doesn't just stop — it waits and retries on its own. See §6 for exactly how.

When everything finishes (or the run stops early for a non-quota reason —
see §8), you get a summary:

```
Queue Complete!
  ✅ 3 prompt(s) executed
  💰 Total tokens: 15,900
  ⏱️ 2.4 minutes
  📁 Responses saved to .sandglass/responses
```

### Step 4 — Look back at what ran

```bash
sandglass history                    # everything completed, ever
sandglass responses list             # every saved response file
sandglass responses show 2           # read one specific response in full
```

---

## 4. Assigning a model per prompt

Different prompts often need different models — a quick "read a config
file" prompt doesn't need Opus, but "redesign this module" might. Every
queued prompt can be pinned to its own model, two ways:

**At add time**, with `--model`:

```bash
sandglass queue add "Refactor the auth module" --model opus
sandglass queue add "Write a one-line changelog entry" --model haiku
```

**Or as a header on the prompt itself** — a `model: <name>` line, then a
blank line, then the prompt. This works whether the prompt comes from
`--file` or as typed text, so it's the natural fit if you're writing prompts
out in a file ahead of time:

```
model: sonnet

Write integration tests for the auth module.
```

If both are present, `--model` wins. Accepted values are the same short
aliases `claude --model` already understands (`opus`, `sonnet`, `haiku`) or
a full model ID (e.g. `claude-opus-4-8`). A prompt with no model set falls
back to the CLI's default. `sandglass queue list` and
`sandglass execute --dry-run` both show which model each queued prompt
will use, so you can double-check a mixed-model queue before running it.

---

## 5. Using future_prompts.md as your default queue

You don't have to run `queue add` for every prompt. If the queue is empty
when you run `sandglass execute`, it automatically loads every
`====`-delimited block out of `prompt_tools/future_prompts.md` — the exact
same file and delimiter convention the manual "future prompts" chat workflow
already uses (see the project's `CLAUDE.md`). A `model: <name>` header (§4)
works here too.

```
====

model: opus

Refactor the auth module to use the new token format.

====

====

Write integration tests for the auth module.

====
```

As each block completes successfully, Sandglass cuts it out of
`future_prompts.md` and archives it into the sibling `prompt_history.md`
(labeled `[Sandglass work]`) — newest first, exactly mirroring what the manual
workflow already does by hand. A block that fails, or is still in progress when a run stops, is left
alone in `future_prompts.md` — nothing is lost.

**These two paths — the interactive chat workflow ("future prompts") and
`sandglass execute` — read the same file, but they're not the same thing.**
Saying "future prompts" to me in chat still means *I* read the top block and
run it myself, interactively, in this conversation. Running
`sandglass execute` means the block runs headlessly through `claude -p`,
unattended, with whatever permission mode is configured. Whichever one
processes a block first cuts it out, so the other won't see it — they don't
double-process the same block, but you should be intentional about which one
you're asking for.

**Prompts added directly via `queue add` always take priority** — the
markdown file is only consulted when the queue is completely empty, so
mixing the two workflows never causes a surprising merge.

To keep a separate queue instead of the shared default — for a different
project, or just to keep things split — point Sandglass at a different
file:

```bash
sandglass queue import path/to/other-queue.md   # persists; overrides the default
sandglass queue source                          # shows the current default
```

`queue import` only changes *which file* gets checked — it doesn't load
anything right away. The next time the queue is empty and you run
`sandglass execute`, that's the file it reads.

---

## 6. Auto-resume: what happens when you hit your usage limit

This is the part that makes the tool worth using instead of just typing the
prompt into chat yourself. By default, `sandglass execute`:

1. Runs prompts one at a time, same as always. If the interrupted prompt
   came from a markdown queue source (§5), it's noted as **interrupted** in
   `prompt_history.md` — the block itself is *not* cut out (only a
   successful completion does that), so the eventual retry picks it up
   exactly where it was, and the gap between "started" and "actually
   finished" is visible in the log rather than silent.
2. If a prompt fails because your subscription's usage window is exhausted,
   it doesn't give up — it reads the **exact time Claude Code says the quota
   refreshes**, adds a 2-minute safety buffer (retrying right at the exact
   boundary risks hitting the same limit again before the window has really
   rolled over), and waits until then, showing a small live animated ASCII
   sandglass in your terminal — nine compact lines, sized like a spinner
   rather than a big block, with a live countdown folded right in — so a
   multi-hour wait never looks like it froze. It pours the way a real
   hourglass does: the **top chamber empties from the top down**, thinning
   from `:` to `.` before a row clears; a thin **stream of grains falls**
   through the neck; and the **bottom chamber fills from the base up**, one
   grain at a time, piling into a heap that spreads out from the middle of
   the lowest row. When it has all poured it holds still for a moment, then
   flips and starts over. A full pour takes about ten seconds —
   deliberately unhurried, since what you are waiting on is measured in
   minutes or hours. If output is piped
   or redirected to a file instead of a real terminal, the animation doesn't
   print at all — just the start and resume messages — so it never floods a
   log with thousands of frames.
3. Once that time passes, it automatically retries — and because completed
   prompts are already durably removed from the queue, it just continues
   exactly where it left off, from zero, on the same prompt (nothing partial
   carries over across a quota hit — the prompt is resent in full).
4. Repeats until the queue is empty, or something *other* than a quota limit
   fails (a bad prompt, a network error). Those are never auto-retried — the
   run stops and tells you to look at it, since blindly retrying wouldn't fix
   a broken prompt.

```
✖ Quota limit reached.
Expected to refresh at 2026-07-22T21:15:00+00:00.
Waiting 42.0 min for quota to refresh (expected around 2026-07-22T21:15:00+00:00). Press Ctrl-C to stop (queue stays intact).

╭─────╮
\     /
 \:::/
  \:/
  )*(    41.6 min remaining
  /.\
 /   \
/ ::. \
╰─────╯

⌛ Resuming — retrying the queue now.
```

(That is one frame mid-pour: the top row has emptied, a grain is falling
through the cone, and three have piled up at the bottom — the last one still
loose. The rounded top and bottom lines are the glass itself, so they stay
unbroken; the sand piles up *on* the base, never through it.)

If Claude Code doesn't report an exact refresh time, it falls back to
checking every 15 minutes instead (`--poll-interval` to change that). See
§13 if you'd rather get a push notification than watch the terminal.

As a safety net: if the *same* prompt fails the *same* way five times in a
row without any progress, Sandglass stops trying and tells you to
investigate — that pattern usually means something other than quota is
actually wrong, and looping on it forever wouldn't help.

**To get the old, non-waiting behavior** (stop at the first failure,
including a quota hit, and don't retry):

```bash
sandglass execute --once
```

Ctrl-C at any point during a wait stops cleanly — the queue is exactly as it
was, nothing is lost.

---

## 7. Understanding permission mode

Because queued prompts run unattended, there's no one available to click
"allow" if Claude wants to use a tool (edit a file, run a shell command,
etc.). So **Sandglass defaults `sandglass execute` to
`--permission-mode bypassPermissions`** — every queued prompt gets full tool
access (file writes, shell commands) with no confirmation step at all. This
is the mode the tool is actually designed to run unattended in, so a queued
prompt never stalls or gets silently denied partway through a batch.

Sandglass prints a warning before every run as a reminder of this — **only
queue prompts you actually trust.**

If you'd rather a run respect the same permission rules an interactive
`claude` session in that directory would use, ask for the stricter mode
explicitly:

```bash
sandglass execute --permission-mode default
```

---

## 8. What happens when something goes wrong

Sandglass is built so a bad run never loses your queue:

- **You hit your subscription's usage limit**: covered in §6 — by default
  this auto-resumes on its own, it doesn't just stop.
- **A prompt fails for a non-quota reason** (bad prompt, network error,
  auto-resume gave up after too many stalls): the run stops right there.
  That prompt and everything after it stay in the queue untouched. Fix
  whatever went wrong, then run `sandglass execute` again.
- **The queue file gets corrupted** (rare, e.g. a disk error mid-write): it's
  automatically backed up to a timestamped `.bak` file and replaced with a
  fresh empty one — nothing is silently thrown away without a trace.
- **You press Ctrl-C** (mid-run, or mid-wait during auto-resume): whatever
  was in flight is abandoned, but everything not yet completed stays queued.

---

## 9. Command reference

| Command | What it does |
|---|---|
| `sandglass queue add TEXT` | Queue a prompt from raw text |
| `sandglass queue add --file FILE` | Queue a prompt from a file |
| `sandglass queue add TEXT --model MODEL` | Queue a prompt pinned to a specific model (see §4) |
| `sandglass queue list` | Show everything currently queued (including per-prompt model) |
| `sandglass queue remove INDEX` | Remove one prompt (1-based index) |
| `sandglass queue clear [--yes]` | Empty the whole queue |
| `sandglass queue stats` | Queue size + rough token estimate |
| `sandglass queue import FILE` | Set the default queue source (see §5) |
| `sandglass queue source` | Show the current default queue source |
| `sandglass execute` | Run the queue, auto-resuming through quota hits (see §6); falls back to the default queue source (§5) if nothing's queued |
| `sandglass execute --once` | Old behavior: single pass, stop at the first failure |
| `sandglass execute --dry-run` | Preview a run without sending anything |
| `sandglass execute --poll-interval SECONDS` | Fallback retry cadence when no exact reset time is known (default 900 = 15 min) |
| `sandglass execute --permission-mode MODE` | Loosen/tighten unattended tool access (see §7) |
| `sandglass history` | Show everything ever completed |
| `sandglass responses list` | List saved response files |
| `sandglass responses show INDEX` | Read one saved response in full |
| `sandglass version` | Print the installed version |
| `sandglass new-claude-project [PATH]` | Scaffold `CLAUDE.md` + `master_plan/` + `prompt_tools/` into a new project (see §14) |
| `sandglass claude-md-update [SOURCE]` | Adopt `SOURCE` as the template `new-claude-project` will use next (see §14) |

---

## 10. FAQ / troubleshooting

**"sandglass: command not found"**
Run `pip install -e .` from inside `sandglass-cli/`.

**"the 'claude' CLI was not found on PATH"**
Install Claude Code: https://claude.com/claude-code

**"claude is not logged in"**
Run `claude auth login`.

**I'm not sure whether this is using my subscription or billing something
else.**
Run `claude auth status` — you want to see `"authMethod": "claude.ai"`.
`sandglass execute` also prints the account/plan it's about to use every
time, before it sends anything.

**".sandglass/ permission denied"**
Check folder permissions in whatever directory you're running Sandglass
from — it needs to create a `.sandglass/` subfolder there.

**A run stopped partway through — did I lose my queue?**
No. See §8 — anything not yet completed is still in the queue. Just run
`sandglass execute` again.

**What actually kicks off execution — do I need to schedule something, or
does it sit there checking for quota?**
`sandglass execute` is still the only trigger — there's no separate
scheduler or background daemon, and you still need to run the command
yourself once. But once it's running, it **does** sit there and wait out a
quota hit on its own (see §6) — you don't have to notice and manually re-run
it anymore. What's still missing (Phase 2) is something that starts that
first `sandglass execute` for you on a schedule, and running fully
detached without holding a terminal window open.

**Do I still need `queue add` for every prompt?**
No — see §5. Write prompts straight into `prompt_tools/future_prompts.md`
and `sandglass execute` picks them up automatically whenever the queue is
otherwise empty.

**Does `sandglass execute` update `master_plan/work_log.md` itself?**
Not directly — but see §12. Each queued prompt runs as a full headless Claude
session that can (and for substantial changes, does) write its own entry
there on its own initiative. `sandglass execute` backstops that: if a
completed prompt's own run didn't touch `work_log.md`, it appends a short
fallback entry itself, so the log stays complete either way.

**Can I get a phone/desktop notification instead of watching the terminal?**
Yes — see §13. Set `SANDGLASS_NTFY_TOPIC` (env var or `.env` file) and
`sandglass execute` pushes via ntfy.sh at each quota wait/resume and at the
end of the batch. Unset by default; nothing is sent until you configure it.

---

## 11. What's not built yet (Phase 2)

- Something to *start* `sandglass execute` on a schedule (auto-resume once
  it's already running is built — see §6; this is about not having to run
  the initial command yourself)
- Background execution — running fully detached, without holding a terminal
  window open
- Adding multiple files to the queue in one command
- Exporting responses to Markdown/CSV

---

## 12. Work log backfill (for projects that keep a work_log.md)

Some projects — this one included — keep a `master_plan/work_log.md`: a running,
append-only log of what was done, session by session, in a specific narrative format
(goal, state, work done, next steps). If that project also has a CLAUDE.md mandating a
log entry after every task, a queued prompt run through `sandglass execute` is a full
headless `claude -p` session that reads that same CLAUDE.md — so it genuinely can, and
often does, write its own proper entry there for anything substantial.

The catch: that's a judgment call the prompt makes for itself, and a small, quick prompt
routinely decides it isn't worth a full report. Rather than leave that gap, `sandglass
execute` backstops it:

1. If `master_plan/` already exists in the directory you're running from, it notes
   `work_log.md`'s modification time and size right before sending the prompt.
2. Right after the prompt completes, it checks again.
3. **Unchanged** → the prompt didn't log itself, so Sandglass appends a short fallback
   entry — the prompt as the goal, a one-line summary of the response (the same kind
   used for the "what has been done" bullets in §3), and a pointer to the full saved
   response in `.sandglass/responses/`.
4. **Changed** → the prompt already wrote something of its own. Sandglass leaves it
   alone — no duplicate, redundant entry gets appended on top of a real one.

This only ever kicks in if `master_plan/` is already there. In a directory that doesn't
use this convention, Sandglass doesn't create it — it's a generic prompt-queue tool, not
something that should impose one project's documentation convention on every project it
runs in.

A fallback entry is clearly labeled as such (`Auto-logged by sandglass execute...`) so
it's never mistaken for a real narrative report someone (or some other headless run)
wrote by hand.

---

## 13. Push notifications (ntfy.sh)

If you're not watching the terminal during a long run, `sandglass execute` (in its
default auto-resume mode) can push a notification via [ntfy.sh](https://ntfy.sh) at
four points:

- A quota hit starts a wait ("waiting for quota to refresh")
- The wait ends and the queue resumes ("resuming")
- The whole queue finishes ("batch complete")
- The batch stops early for a non-quota reason, or gives up after too many stalls
  ("batch stopped early")

**Setup** — pick a topic name (something private/hard to guess, since anyone who knows
it can read your notifications — ntfy topics aren't access-controlled by default) and
set it as an environment variable:

```bash
export SANDGLASS_NTFY_TOPIC=your-topic-name
sandglass execute
```

Or keep it in a `.env` file in the directory you run `sandglass` from instead of
exporting it every time — Sandglass loads `KEY=VALUE` lines from `.env` automatically on
startup (no extra dependency; a plain parser, gitignored by convention so it never ends
up committed):

```
SANDGLASS_NTFY_TOPIC=your-topic-name
```

Then subscribe to that same topic in the [ntfy Android/iOS app](https://ntfy.sh) or at
`https://ntfy.sh/your-topic-name` in a browser to receive the pushes.

**If `SANDGLASS_NTFY_TOPIC` isn't set, nothing happens** — no error, no delay, every
notification call is a silent no-op. A failed or unreachable ntfy server is handled the
same way (logged as a warning, never raised) — a notification is a nice-to-have, not
something that should ever break or stall a queue run.

---

## 14. Scaffolding a new Claude Code project

Two commands set up a new project with the same `CLAUDE.md` + `master_plan/` +
`prompt_tools/` convention this project itself uses:

```bash
sandglass new-claude-project            # scaffold into the current directory
sandglass new-claude-project ./my-app   # or into a given path
```

This creates:
- `CLAUDE.md` — a real copy of the bundled template's content
- `master_plan/` — the same filenames this project's own `master_plan/` uses
  (`SYSTEM_MAP.md`, `work_log.md`, `human_idea.md`, `user_manual.md`, `MASTER_ARQ_SYSTEM_MAP.md`),
  created **empty** — only the filenames are templated, not their content
- `prompt_tools/` — `future_prompts.md` and `prompt_history.md`, also empty

**Anything that already exists at the target is left alone** (reported as "Skipped") —
safe to re-run on a project that's already partly set up; it only fills in what's missing.

**Keeping the template current** — the bundled `CLAUDE.md` doesn't update itself when you
edit a project's `CLAUDE.md`. After making changes you want future scaffolds to carry
forward, adopt them explicitly:

```bash
sandglass claude-md-update             # adopts ./CLAUDE.md as the new template
sandglass claude-md-update path/to/CLAUDE.md   # or from a specific file
```

The next `sandglass new-claude-project` run anywhere will copy that updated content.

To point at a self-hosted ntfy server instead of the public `ntfy.sh`:

```bash
export SANDGLASS_NTFY_SERVER=https://your-ntfy.example.com
```


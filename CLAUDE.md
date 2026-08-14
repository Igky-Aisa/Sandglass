# Project Context

> Full rules and protocol: see [master_plan/rules_for_every_promt.md](master_plan/rules_for_every_promt.md)
> Full rules and protocol for backend and database: see [master_plan/rules_backend.md]

> Architecture source of truth: see [master_plan/ECSP MASTER ARCHITECTURE V4.txt](<master_plan/ECSP MASTER ARCHITECTURE V4.txt>)
> Current work state: see [master_plan/work_log.md](master_plan/work_log.md)

## MANDATORY — Read before acting

This is a regulation-safe domain platform (Flutter). Philosophy: **Determinism, Auditability, "Not-CRUD"** — non-negotiable. For any substantial code task, read first:

1. `master_plan/SYSTEM_MAP.md` — **agent quick-reference**: current-state map of commands/handlers/routes/services/models/pages, deploy topology, and gotchas (read before grepping the tree). Keep it updated when connections change.
2. `master_plan/ECSP MASTER ARCHITECTURE V4.txt` — authoritative architecture
3. `master_plan/work_log.md` — **the last two entries only** (see the reading budget below)
4. `master_plan/rules_for_every_promt.md` — long-form protocol, rationale, examples

(The essentials from those files are inlined below so they apply every prompt; read the source files for detail/rationale.)

### Reading budget — read the tail, not the file

`work_log.md` and `prompt_tools/prompt_history.md` are **append-only**: they
only ever grow, and they are already far larger than the architecture doc. A
full read of the work log costs more than most tasks do, and it buys almost
nothing — the part that matters is the last session or two.

- **Default to the last two entries** of `work_log.md`, not the whole file.
  Read further back only when you're chasing something specific, and say what.
- **`prompt_history.md` is a record, not a working document.** Don't read it to
  find out what's going on; that's what `work_log.md` and `Progress.md` are for.
- **If a `<project_state>` block appears at the top of your prompt, that IS the
  state** — `sandglass execute` put it there precisely so you don't re-read
  these files. Trust it and get to work; open the full files only if you need
  history it genuinely doesn't cover.
- Run **`sandglass rotate-logs`** when the work log passes ~10 entries. It moves
  the older ones to `master_plan/archive/` and leaves a pointer, so history
  stays available without being re-read on every task.

## Non-negotiables

- **Backend is the ONLY DB client** — the FastAPI backend is the single component that talks to the database. Every other piece (Flutter operator console, public intake form, the local `esaas_declarator` SEC bot) reads/writes **only through the backend HTTP API**. No direct DB connections or DB credentials anywhere else. (Project-wide core rule; see `master_plan/PRODUCTION_DEPLOYMENT.md` §5/§8. The dev-only leads-sync job is the sole documented exception — do not generalize it.)
- **E-Drafter is dual-target (standalone + ESaaS) from one codebase** — the Flutter app talks to exactly ONE seam (`ApiClient` / `/api/v1` HTTP); it must never import DB code, assume a DB, or contain ESaaS-specific branches. Drafting logic (scene model, tools, generator, DXF/PDF/XLSX/cuadro exporters) stays backend-agnostic and shared. Integration features are gated by `GET /capabilities`, not build forks. **Before touching the app↔backend boundary, persistence, auth, exporters, or integration, read [master_plan/EDRAFTER_DUAL_TARGET.md](master_plan/EDRAFTER_DUAL_TARGET.md).**
  - **Current focus is the ESaaS-integrated E-Drafter.** We may ship features that assume the ESaaS backend (e.g. live geo/staticmap) *without* building the standalone equivalent yet — BUT every time you rely on something the standalone backend doesn't have, or take an ESaaS-only shortcut, you MUST record it in [master_plan/standalone_edrafter.md](master_plan/standalone_edrafter.md): what the feature needs, which standalone endpoint/capability is missing, and how it should be gated (`GET /capabilities`) so the standalone build degrades gracefully instead of breaking. Prefer capability-gating in code; when you skip it for speed, the gap goes in that file. Goal: build a fully functional ESaaS now without foreclosing a fully standalone E-Drafter later.
- **No Silent Mutations** — no logic/design changes without explicit reasoning
- **Command-Driven state** — all state changes follow Master Architecture
- **Visual Excellence** — gradients, micro-animations, use `AppColors` and `Ico` classes
- **Fix lint errors before claiming completion**
- **Append a session report to `master_plan/work_log.md` at the end of every task** (format below)

## Verify WHAT IS RUNNING before debugging logic

When app behaviour contradicts the code you just read, **the code you read is
often not the code that served the request.** Check these BEFORE re-reading
logic, adding logging, or "fixing" anything — three separate bugs in one week
(2026-07-26/28) were each misdiagnosed as a logic error and were all this:

1. **Which backend is the frontend actually hitting?** `esaas_frontend` defaults
   to `http://localhost:8000`; it only uses Cloud Run when launched with
   `--dart-define=API_BASE_URL=<url>`. Confirm it, never assume it — a local
   backend and the deployed one routinely run different code.
2. **Is a local backend serving stale code?** A `uvicorn` process started before
   your edit keeps the old route table and old logic until it is restarted.
   Always run it with `--reload`.
3. **Is your fix actually deployed?** `esaas_backend` **and `edrafter_core`**
   reach Cloud Run only via `./deploy.sh <gcp-project>` (deploy.sh copies the
   `edrafter_core` *working tree* into the image). Diff `git show HEAD:<file>`
   against the working tree: **an uncommitted fix is not deployed — even if you
   already applied its DB migration.** A half-applied change (migration live,
   code not) looks exactly like a logic bug.

**Prove which version answered; do not infer it.** `curl <base>/openapi.json`
shows the live route table (params and methods included). For logic, run the
case through the *installed* module. A test that passes against your working
tree proves nothing about what production or a running local server is doing.

## work_log.md session report — required format

Append at the end of every task:

```markdown
## [Date] - [Agent Name] - [Task Title]

### 1. Context Snapshot
- **Goal**: [1-sentence goal]
- **State**: [Current branch/module/file focus]
- **Previous Blocker**: [What was resolved or remains]

### 2. Work Done
- [Key implementation details / architectural decisions / files modified]

### 3. Next Steps (For the next agent)
- [Specific point of resumption / known bugs / pending UX]
```
Focus on **why** a decision was made relative to the Architecture, not the obvious.

### Keep entries short — every future task pays for this one

This file is read by every agent that comes after you, and it never shrinks.
An entry that's twice as long is a cost charged to every future session, so
length here is not free the way it is in a chat reply.

**Target ~20 lines. Hard ceiling 40.** To stay there:

- **Never paste code, diffs, file contents, or command output.** Name the file
  and the function — `sandglass/claude_client.py:_response_from_usage` — and
  let the next agent open it. The code is in git; the log is for what git
  can't tell them.
- **Write what wasn't obvious**: the decision and its reason, the thing that
  looked right and wasn't, the constraint that forced an ugly shape. Skip
  anything a `git log` or a glance at the diff already says.
- **One bullet per real decision**, not per file touched.
- **"Next Steps" is the highest-value section** — it's the part the next agent
  actually acts on. Be specific enough to resume from: a file and a question,
  not "continue the work".
- If a task genuinely needs a long write-up, put it in its own document under
  `master_plan/` and link to it in one line from here.

## API SPEC comment rule

Whenever you create a Button, GestureDetector, or any Widget that triggers a backend interaction, add this block immediately above the `onPressed`/`onTap` handler:

```dart
/* [API SPEC]
- NAME: {Descriptive name of the action}
- FUNCTIONALITY: {What happens in the UI}
- ENDPOINT: {HTTP Method} {Proposed endpoint, e.g. POST /api/v1/resource}
- REQUEST_BODY: {JSON sent to backend, or 'null'}
- RESPONSE_BODY: {JSON of expected success response}
*/
```
JSON keys should be based on the current widget's variables.

## User manual rule — document what the OPERATOR can do

Whenever you add or change anything the operator can **use or configure** — a
button, menu item, dialog, toggle, settings field, keyboard shortcut, or a
default that changes visible behaviour — document it in
[master_plan/user_manual.md](master_plan/user_manual.md) **in the same task**,
alongside the `work_log.md` entry. The work log explains *why* to the next
agent; the user manual explains *how* to the person using the app.

Write it for the OPERATOR, never for a developer:
- **Where it is** — app → screen → panel/tab, using the **exact Spanish label**
  shown in the UI, so it can be found without hunting.
- **What it does**, in plain language. No file paths, class names, or endpoints.
- **Gotchas / when NOT to use it** — the non-obvious part that would otherwise
  turn into a support question later.

Skip it for pure refactors, internal fixes, and backend changes with no visible
surface. But if a fix **changes visible behaviour** (a new default, a moved or
renamed control, a rule the operator must now follow), that IS user-facing —
update the entry rather than staying silent.

## How to write a block in `future_prompts.md`

Blocks run one after another, unattended, sharing **one warm session** — so a
block can see what earlier blocks did, but it is still a separate task and is
told so explicitly. Write each block as if the reader knows the project but not
your intentions: state the job, don't assume it will infer it from the block
before.

**Optional front matter**, first lines of the block, followed by a blank line:

```
model: sonnet
effort: low

Add the dirty-flag badge to the strategy editor...
```

- `model:` — `opus` / `sonnet` / `haiku`, or a full model ID.
- `effort:` — `low` / `medium` / `high` / `xhigh` / `max`. Lower means fewer,
  more-consolidated tool calls and less preamble. Most blocks are not `max`.
- `isolate: true` — run this block with no memory of the earlier ones. Blocks
  normally share one warm session (that's what stops each one re-reading the
  whole project), so use this only when prior context would actively harm the
  work: an independent review, a genuine second opinion.
- `model: deepseek-pro` (or `deepseek-flash`) — run this block on DeepSeek
  instead of Claude, so it doesn't eat subscription quota. Naming the model is
  all it takes; `provider: deepseek` and a `**CLINE: pro**` marker mean the
  same thing. **Only use it for mechanical work you'd be comfortable sending to
  a third party** — the block's prompt, the injected project brief and every
  file it reads go to that vendor, not Anthropic. Needs a key (`sandglass
  providers set deepseek`); without one the block runs on Claude instead, with
  a warning. Note `**TIER: CHEAP — EXTERNAL-OK**` does *not* route — that
  marker means "would be fine externally", this one means "do it".
- A `**TIER: SONNET**`-style marker near the top works too and maps to a
  sensible model+effort pair, but explicit front matter is clearer and wins.

**Then write the block itself:**

- **One block, one deliverable.** If it needs "and then also", it's two blocks.
- **Name every file you expect it to touch, in backticks** — `lib/foo/bar.dart`.
  This is what makes the work findable without a repo-wide grep, and
  `sandglass queue lint` uses it to warn you before a run that a path the block
  depends on doesn't exist yet.
- **State the acceptance check.** "Done when `flutter analyze` is clean and the
  badge appears on an edited strategy." A block with no definition of done
  tends to produce either half a feature or three times as much as you wanted.
- **Declare dependencies explicitly**: "assumes block 7 created the
  `StrategyRepository`." A block whose dependency was never built burns a full
  run and delivers nothing, and saying so out loud also survives the block
  being run on its own later.
- **Don't restate project context.** Architecture, conventions, and the current
  state already reach the run via `CLAUDE.md` and the injected
  `<project_state>` brief. Repeating them costs tokens on every block and adds
  nothing.
- **Prefer "change X to do Y" over "investigate X".** Open-ended exploration is
  the most expensive shape a block can have and the least likely to end in a
  diff.

Before queueing a batch, run **`sandglass queue lint`** — it's free and catches
missing paths, empty blocks, and bad effort values before they cost a run.

## "future prompts" trigger

When I say **"future prompts"**: prompts are separated by lines containing `====` at start and end.

1. Read the prompt at the **top** of `prompt_tools/future_prompts.md` and execute it.
2. Then cut that executed prompt out of `future_prompts.md` and paste it into `prompt_tools/prompt_history.md`, leaving the next prompt at the top.
3. When you start you excution , quote the prompt that you are working on it , cus the current display only shows "future prompts"

When I say **"future prompts all"**
1. execute all the prompts one by one in the `future_prompts.md` file, following the instructions above.

**Per-prompt model:** if the top prompt's block starts with a `model: <name>` line followed by a blank line (e.g. `model: Opus`), that names the model it should run under. An `effort:` line may follow it; when I run the block myself in chat there's no equivalent switch, so just note it and match the depth it asks for.if you can switch your own model do it , just remind at the end of the response in the chat the model switch,and if you can't switch your own model — tell me to run `/model <name>` first if it doesn't match what I'm currently on. wait for 10 second for the switch and if it doesn't happend just keep using the model available for you Strip the header before treating the rest as the prompt body.

**Relationship to `sandglass execute`:** the Sandglass CLI (`sandglass-cli/`) can also read `future_prompts.md` directly — if its queue is empty, `sandglass execute` auto-loads every `====` block from this same file and runs each one headlessly via `claude -p`, cutting completed ones into `history_prompts.md` the same way. These are two different execution paths over the same file, not duplicates of each other: saying "future prompts" to me means *I* run the top block myself, interactively, in this chat; running `sandglass execute` means it runs unattended through the CLI instead. Whichever processes a block first cuts it out, so a block is never run twice — but be intentional about which one you're asking for.

> ### ⚠️ If you are running under `sandglass execute`, do NOT cut, and do NOT touch `.sandglass/`
>
> **How to tell:** a `<project_state>` block at the top of your prompt means you are a headless
> block inside a queue run. Step 2 above ("cut that executed prompt out") applies **only** when a
> human runs the block interactively in chat.
>
> In a queue run the runner owns the bookkeeping — it cuts the block from `future_prompts.md`,
> archives it, and maintains `.sandglass/`. Doing any of that yourself is not a harmless
> duplicate: the runner does it **after** your response returns, and finds the state already
> changed underneath it.
>
> Real incident (2026-08-13): a block cut its own entry and cleared `.sandglass/queue.json` while
> it was still running. The runner then crashed trying to remove a block that had just cost
> **$10.21 and 14.1M tokens**. Separately, a block read `.sandglass/last_run.json`, saw its own
> runner's PID marked `running`, concluded a rival process held the files, and refused to write
> anything at all — that entry is *this* run, not a lock.
>
> If a task genuinely requires changing the queue, say so in your response and leave the files
> alone.

### The two queues — `future_prompts.md` and `.sandglass/queue.json`

`sandglass` keeps its own **snapshot** of the queue in `.sandglass/queue.json`, taken when it
first imports the markdown. Editing `future_prompts.md` does **not** update that snapshot, so
the two drift apart and the run executes block text that no longer exists.

- **Whoever edits `future_prompts.md` runs `sandglass queue clear` in the same task**, so the
  next `sandglass execute` re-imports the current text. Clearing loses nothing: the blocks live
  in the markdown, and the queue is only ever a copy of them.
- Sandglass drops a queued block that is **gone from `future_prompts.md` and present in
  `prompt_history.md`** — someone else already ran it. That is a repair for one specific
  divergence, not a substitute for clearing: a stale snapshot still holds *old text* for blocks
  that are still listed.
- **`sandglass why`** explains why the last run stopped (or what it is doing now). Read it
  before re-running anything.

### If your block changes no file, you will be asked why

A block that returns a response but changes nothing in the working tree gets one follow-up turn.
Answer in exactly this shape:

```
VERDICT: DONE | BLOCKED | NOOP
WHY: <one line>
```

`DONE` = the work was already complete before you started. `BLOCKED` = you could not do it;
name precisely what is missing. `NOOP` = the task genuinely required no file change.

`DONE` and `NOOP` let the queue move on to the next block; `BLOCKED` stops it. **Answer
accurately rather than agreeably** — a block that says `NOOP` to be helpful sends the queue past
a real missing dependency, and the blocks behind it will fail for reasons nobody can see.
Nothing is ever cut on the strength of that answer; it decides whether the run continues, never
whether a block is destroyed.

**Writing a `work_log.md` entry is not a work product.** The log, `Progress.md` and both
`prompt_tools/` files are excluded from that check on purpose: they are what this process
mandates, so they cannot be evidence that the process produced anything. Recording *why* you
were blocked is still correct and still expected — it just won't be mistaken for having built
the block.


## "check progress" trigger — measure against the master plan

When I say **"check progress"** (or **"progress"**), report where the project
stands against its own plan, then record it. Do all three:

1. **Against the main idea.** Read
   [master_plan/MASTER_ARQ_SYSTEM_MAP.md](<master_plan/MASTER_ARQ_SYSTEM_MAP.md>)
   — the architecture / system map that defines what the finished system is
   supposed to be (its components and Implementation Phases). Judge how much of
   that is actually built vs still pending. This is the qualitative half: *are
   we building the right thing, and how far along the plan are we?*
2. **Prompt throughput.** Count the `====`-delimited blocks still queued in
   `prompt_tools/future_prompts.md` (remaining) and the executed entries in
   `prompt_tools/prompt_history.md` (done). Report done, remaining, total, and a
   percentage — e.g. `16 done / 1 remaining (17 total, 94%)`. These are the two
   ends of one pipeline: a block leaves `future_prompts.md` and lands in
   `prompt_history.md` when it completes (see the "future prompts" trigger).
3. **Check the reading budget.** If `work_log.md` holds more than ~10 entries,
   run `sandglass rotate-logs` — every future task is paying to read whatever
   is in there. Mention it in the report either way.
4. **Write it down.** Update
   [master_plan/Progress.md](<master_plan/Progress.md>) with the result —
   overwrite the live snapshot at the top (date + who), don't just pile on
   noise. Keep it short: the master-plan standing, the prompt counts, and any
   blocker worth flagging. `Progress.md` is the at-a-glance status file — it is
   NOT the work_log (that stays the per-task narrative in `work_log.md`).

## Session shutdown ritual — "may the force be with you"

**Triggers** (any of): **"may the force be with you"** · **"live long and
prosper"** · **"hasta la vista baby"** · **"good work good bye"**.

Run the whole checklist, then report what actually happened at each step —
including anything skipped and why. Never claim a step succeeded without
checking; several bugs in this repo came from assuming a deploy landed.

1. **Commit + push to `main`.** Stage everything, then **verify no secrets are
   staged** (`.env`, `secrets/*`, `*-sa.json`, `*.pem` — check, don't assume),
   commit with a real message, push the branch, then fast-forward `main` to it
   with `git push origin <branch>:main`. Prefer that over checking `main` out:
   a checkout of a branch many commits behind rewrites the whole working tree
   for nothing.
2. **Sync backend → Cloud Run.** `cd esaas_backend && ./deploy.sh ecsp-sheets-ingest`.
   ⚠️ **This is a PRODUCTION deploy and it ships the WORKING TREE, committed or
   not.** Say so before running it, and stop and ask if the tree holds anything
   this session did not intend to ship. Afterwards **verify the new code is
   live** (`curl <url>/openapi.json`) — an exit code of 0 is not proof.
3. **Sync the database — SCHEMA ONLY.** `alembic upgrade head`, then confirm
   `alembic current` == head. **"Sync" here NEVER means data**: do not copy,
   dump, restore or overwrite rows between environments. If the chain contains
   pending migrations this session did not author, name them and confirm before
   applying. Note: `esaas_backend/.env` points at **Supabase prod**, so "local"
   and "Supabase" are the same database today.
4. **Docs current.** `master_plan/SYSTEM_MAP.md` (did any connection/topology
   change?) and `master_plan/user_manual.md` (any new button, setting, shortcut
   or changed default? — see the User manual rule), plus the usual
   `work_log.md` session report.
5. **Suggest a chat title** — one short line naming the one or two main pieces
   of work. The auto-title comes from the first message and is almost always
   wrong by the end of a session.
6. **Sign off with a quote.** Cool, funny, intellectual or historical — **always
   attributed** (film, book, person, event). **Vary it every single time**;
   never reuse one, and let it echo how the session actually went. The trigger
   phrases are Star Wars / Star Trek / Terminator — answering in kind is fair
   game, so is going somewhere completely unexpected.

## Prompt history

Every prompt I input is logged to `prompt_tools/prompt_history.md` automatically by a `UserPromptSubmit` hook (see `.claude/`).

## Flutter Development Rules

### NO long test 
- dont run dart analyze
- dont run flutter test
- dont run flutter analyze --no-fatal-infos
- dont run flutter test --no-pub  
- dont run flutter test --no-pub --coverage
- dont run flutter test --no-pub --coverage --no-build-failures
- dont run flutter test --no-pub --coverage --no-build-failures --no-build-failures
- dont run flutter test --no-pub --coverage --no-build-failures --no-build-failures --no-build-failures 

### Testing
- Always use `flutter test --no-pub` — never bare `flutter test`
- Run only affected test files unless full suite is explicitly requested

## Token Efficiency Rules

### Communication
- No affirmations — skip "Great!", "Sure!", "Of course!", "Absolutely!"
- No preamble — start responses with the answer or action directly
- No summaries at the end unless explicitly asked
- No restating what you're about to do — just do it


### Code Output
- When editing a file, show only the changed lines plus minimal context (3 lines max around the change)
- Do not reprint entire files unless the change affects >70% of the file
- For multi-file changes, show a single unified diff summary, not full file dumps
- No inline comments explaining what standard Flutter/Dart patterns do

### Planning & Reasoning
- Reason silently — only surface conclusions and decisions, not the full reasoning chain
- If a task has sub-steps, list them once briefly, then execute without narrating each one
- Ask only one clarifying question at a time, only when genuinely blocked

### ⚡ EFFICIENT WORKFLOW & TESTING RULES
- **No Global Testing:** DO NOT run full test suites or exhaustive `flutter test` commands after code changes unless explicitly requested.
- **Incremental Validation:** Only run tests specifically related to the modified files. If no unit test exists for the specific change, prioritize successful compilation over runtime testing.
- **Flutter UI Focus:** For frontend/Flutter tasks, rely on static analysis (`flutter analyze`) rather than execution. Assume the developer will perform visual validation via Hot Reload.
- **Fast Feedback Loop:** If a task requires verification, suggest a specific, targeted command rather than executing a broad one. 
- **Time Boxing:** Never initiate any process (indexing, testing, or cleaning) that is expected to take more than 120 seconds without asking for confirmation.

### English correction and prompt education
- When I misspell or misphrase something, correct me — be direct, no need to soften it. Put the correction at the **end** of the response so it never blocks the actual answer.
- Show how a native speaker would say what I meant: correct and natural, but everyday register (not overly formal — average, fluent speaker).
- When my phrasing could be more natural, show **both**: what I wrote and your recommended version, so I can compare.
- If a rephrasing would get a more specific or accurate result from you, tell me that too (prompt-writing tips, not just grammar).
- Keep it brief. For a minor fix, just restate my prompt with the corrections in **bold** — no explanation needed.
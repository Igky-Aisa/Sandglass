# Sandglass CLI: Batch Prompt Queue System
## Concept Document (Human-Oriented)

---

## The Problem

You have multiple Claude prompts to run, but:
- **Token quota**: You hit your limit mid-project
- **Multi-part tasks**: Prompt 1 → Prompt 2 → Prompt 3 (sequential, dependent)
- **Manual execution**: Currently you run each prompt separately, wait, then run the next
- **Time-consuming**: No way to queue them all and let Claude work through them

**Example workflow today**:
```
1. Send Prompt 1 (Fix auth) → Wait 30 min → Finish
2. Manually copy Prompt 2 (Write tests)
3. Send Prompt 2 → Wait 30 min → Finish
4. Manually copy Prompt 3 (Deploy)
5. Send Prompt 3 → Wait...
```

**The goal**: Queue them all at once, execute sequentially, one command.

---

## The Solution: Sandglass CLI (Batch Queue)

**Sandglass** is a command-line tool that manages a prompt queue and executes them automatically, one after another.

### Scenario 1: Queue & Execute Sequentially
```bash
$ sandglass queue add "Fix auth bug in auth.dart"
✓ Added prompt 1

$ sandglass queue add "Write unit tests for auth module"
✓ Added prompt 2

$ sandglass queue add "Deploy to staging environment"
✓ Added prompt 3

$ sandglass queue list
Queue (3 prompts):
  1. Fix auth bug in auth.dart
  2. Write unit tests for auth module
  3. Deploy to staging environment

$ sandglass execute
[Starting queue execution...]
[1/3] Fix auth bug in auth.dart
  ▶ Sending to Claude...
  ▶ ███████░░░░ 58% complete
  ✅ Done (8,234 tokens used)

[2/3] Write unit tests for auth module
  ▶ Sending to Claude...
  ▶ ████░░░░░░░ 35% complete
  ...
```

### Scenario 2: Add Prompts from File
```bash
$ sandglass queue add prompts/task1.txt
$ sandglass queue add prompts/task2.txt
$ sandglass execute  # Runs both in order
```

### Scenario 3: Stage 2 — Auto-Resume (Future)
```bash
$ sandglass execute
[Executing queue...]
[1/3] Prompt 1... ✅ Done
[2/3] Prompt 2...
[!] Quota hit. Pausing. Will auto-resume when quota refreshes.
[Polling for quota... next check: 3:15 PM]

[At 3:15 PM - auto-resumes]
[2/3] Prompt 2... ▶ Resuming from 45% complete
       ███████░░░░ 78% complete
       ✅ Done

[3/3] Prompt 3... ✅ Done

[Complete] All 3 prompts executed successfully.
```

---

## Architecture Overview

### Three Components

#### 1. **CLI Tool** (sandglass command)
- User interface: type `sandglass queue add`, `sandglass execute`, etc.
- Manages queue (add, list, remove, clear)
- Triggers execution
- Shows progress in terminal

#### 2. **Execution Engine** (Python backend)
- Reads queue from JSON storage
- Sends each prompt to Claude API sequentially
- Tracks token usage
- Handles quota limits (pauses gracefully in Phase 2)
- Saves responses to `.sandglass/responses/`

#### 3. **Data Storage** (JSON files)
- `.sandglass/queue.json` - list of prompts to execute
- `.sandglass/responses/` - folder with response files (one per prompt)
- `.sandglass/history.json` - archive of completed prompts

---

## How It Works: User Journey

### Stage 1 MVP (Manual Queue Execution)

**Day 1 - Build the queue:**
```bash
# Monday morning at work
$ sandglass queue add "Refactor authentication module"
$ sandglass queue add "Write integration tests"
$ sandglass queue add "Update API documentation"
$ sandglass queue list
Queue (3 prompts):
  1. Refactor authentication module
  2. Write integration tests
  3. Update API documentation
```

**Day 1 - Run the queue:**
```bash
# Monday evening, ready to go
$ sandglass execute

[Stage 1 MVP] Executing queue (3 prompts)...

[1/3] Refactor authentication module
  📤 Sending to Claude (claude-opus-4-8)...
  ▶ ████████░░░░░░░░░░░░░░░░ 32% complete (18 min elapsed)
  ▶ ███████████░░░░░░░░░░░░░░░░ 45% complete (27 min elapsed)
  ▶ ████████████████░░░░░░░░░░░░ 65% complete (38 min elapsed)
  ✅ Done (23,450 tokens used, 45 minutes)

[2/3] Write integration tests
  📤 Sending to Claude...
  ▶ ███████░░░░░░░░░░░░ 35% complete
  ...

[Queue complete!]
  ✅ Prompt 1: 23,450 tokens
  ✅ Prompt 2: 18,720 tokens
  ✅ Prompt 3: 15,980 tokens
  💰 Total: 58,150 tokens used

Responses saved to: .sandglass/responses/
  - response_001_refactor_auth.json
  - response_002_tests.json
  - response_003_docs.json
```

### Stage 2: Auto-Resume (Phase 2)

Same as above, but if quota hits:

```bash
$ sandglass execute

[1/3] Refactor auth... ✅ Done
[2/3] Write tests...
  ▶ ████████░░░░░░░░ 48% complete
  [!] QUOTA HIT. Pausing queue execution.
  [Auto-resume enabled] Will check quota every 15 minutes.

[Polling for quota...]
  [3:15 PM] Not available yet
  [3:30 PM] Not available yet
  [3:45 PM] ✅ Quota refreshed!

[Resuming from 48%...]
  ▶ ████████████████░░░░░░ 72% complete
  ✅ Done (18,720 tokens used)

[3/3] Deploy... ✅ Done

[Queue complete!]
```

---

## Key Features

| Feature | MVP | Phase 2 |
|---------|-----|---------|
| **Queue Management** | Add, list, remove, clear | Reorder, batch add |
| **Sequential Execution** | ✅ Yes | ✅ Yes |
| **Manual Start** | ✅ Yes (type `sandglass execute`) | ✅ Yes |
| **Live Progress** | ✅ Show % complete, tokens used | ✅ Yes + ETA |
| **Quota Pausing** | ❌ Fails on quota hit | ✅ Pauses gracefully |
| **Auto-Resume** | ❌ | ✅ When quota refreshes |
| **Auto-Check Quota** | ❌ | ✅ Every 15 min |
| **Notifications** | ❌ | ✅ ntfy.sh alerts |
| **Response Saving** | ✅ JSON files | ✅ + Markdown export |

---

## CLI Commands (MVP)

### Queue Management

```bash
# Add a prompt (text)
$ sandglass queue add "Your prompt here"

# Add a prompt (from file)
$ sandglass queue add prompts/task1.txt

# List queued prompts
$ sandglass queue list

# Remove a prompt
$ sandglass queue remove 2

# Clear entire queue
$ sandglass queue clear

# Show queue stats
$ sandglass queue stats
Queue: 5 prompts, ~120,000 estimated tokens
```

### Execution

```bash
# Execute all queued prompts sequentially (blocking)
$ sandglass execute

# Execute with custom model (Phase 2)
$ sandglass execute --model claude-sonnet-5

# Execute dry-run (show what would run, don't execute)
$ sandglass execute --dry-run
```

### History & Responses

```bash
# Show completed prompts
$ sandglass history

# Show responses from last execution
$ sandglass responses list

# View a specific response
$ sandglass responses show 1

# Export response as markdown
$ sandglass responses export 1 --format markdown
```

---

## Data Storage (Local Only)

**All data stays on your machine** — stored as JSON in `.sandglass/`:

```
.sandglass/
├── queue.json              # Current queue
├── responses/
│   ├── response_001.json   # Claude response for prompt 1
│   ├── response_002.json
│   └── response_003.json
├── history.json            # Archive of completed prompts
└── settings.json           # User config (model, token limits)
```

**Example: queue.json**
```json
{
  "prompts": [
    {
      "id": "001",
      "title": "Fix authentication bug",
      "text": "I have an auth flow issue in auth.dart where...",
      "added_at": "2026-07-21T14:32:00Z",
      "source": "text"  // or "file"
    },
    {
      "id": "002",
      "title": "Write integration tests",
      "file_path": "prompts/tests.txt",
      "added_at": "2026-07-21T14:45:00Z",
      "source": "file"
    }
  ]
}
```

---

## Technology Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| **CLI** | Python (Typer or Click) | Easy CLI, fast to write |
| **Execution** | `claude` CLI, headless mode (`claude -p`) — **not** the Anthropic Python SDK | The Messages API bills pay-per-token API credits with no "refresh." That's a different product from a Pro/Max subscription's rolling quota, which is the entire point of this tool. Shelling out to `claude -p` runs against whatever account `claude` is logged into. *(Corrected 2026-07-21 — MVP build originally implemented the raw API by mistake; see `master_plan/work_log.md`.)* |
| **Storage** | JSON files | Simple, portable, no DB |
| **Model** | claude-opus-4-8 (MVP), configurable later | Best quality for complex tasks |

---

## Installation & Quick Start (MVP)

```bash
# 1. Install
cd sandglass-cli
pip install -e .

# 2. Add prompts
sandglass queue add "Your prompt here"

# 3. Run
sandglass execute

# Done! Responses in .sandglass/responses/
```

---

## Scope: MVP vs Phase 2

### MVP (This sprint) ✅
- ✅ CLI: queue add/list/remove/clear
- ✅ Sequential execution of prompts
- ✅ Save responses to JSON
- ✅ Manual start (`sandglass execute`)
- ✅ Progress display (% complete, tokens)
- ✅ Local storage only

### Phase 2 (Future)
- ✅ Auto quota checking — done 2026-07-22, and better than originally scoped:
  watches the CLI backend's real `rate_limit_event`/`resetsAt` instead of blind polling
- ✅ Graceful pause on quota hit — done 2026-07-22
- ✅ Auto-resume when quota refreshes — done 2026-07-22 (`sandglass execute`'s default
  behavior now; `--once` opts back into the old stop-on-first-failure behavior)
- ✅ ntfy.sh notifications — done 2026-07-23 (quota wait started/resumed, batch
  complete/stopped early; opt-in via `SANDGLASS_NTFY_TOPIC`, silently skipped if unset)
- ⏳ Reorder prompts in queue
- ⏳ Batch add (all from folder)
- ⏳ Markdown export of responses
- ⏳ Settings UI (choose model, token limits) — per-prompt model override shipped
  2026-07-22 via `--model`/a `model:` header, though there's still no persistent
  settings file for a global default

---

## Success Criteria (MVP)

✅ **You'll know it works when:**

1. Type `sandglass queue add "test prompt"` → saved to queue.json
2. Type `sandglass queue list` → shows prompt in list
3. Type `sandglass execute` → 
   - Sends prompt to Claude
   - Shows live progress (% complete)
   - Saves response to `.sandglass/responses/response_001.json`
4. Type `sandglass queue list` → queue is empty (completed)
5. Type `sandglass responses list` → shows completed response

**That's it.** Core functionality works. Phase 2 adds the "magic" of auto-resume.

---

## Estimated Build Time

- **Prompts**: 4 (not 5)
- **Tokens**: ~40-45k (cheaper than VS Code extension)
- **Time**: ~4-5 hours work
- **Timeline**: 1 week part-time (1 hour/day) OR 1-2 days full-time

**After MVP**, you'll have ~155-160k tokens left for Phase 2.

---

## Next Steps

1. Read `MASTER_ARQ_SYSTEM_MAP.md` (technical architecture)
2. Follow `prompt_tools/future_prompts.md` (4 step-by-step prompts)
3. Execute Prompt 1 → Prompt 2 → Prompt 3 → Prompt 4
4. Ship Stage 1 ✅
5. Build Phase 2 (auto-quota, auto-resume) with remaining budget

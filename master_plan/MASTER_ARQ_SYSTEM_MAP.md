# Sandglass CLI: Technical Architecture & System Map
## Master Architecture Document (Claude Reference)

---

## System Overview

**Sandglass CLI** is a command-line batch prompt queue system. It manages prompts, executes them sequentially against Claude API, and saves responses locally.

```
User Terminal
     ↓
  [CLI Commands]  ← sandglass queue add/list/execute
     ↓
  sandglass-cli/
  ├── sandglass/
  │   ├── cli.py                    (Command handlers: add, list, execute, etc.)
  │   ├── queue_manager.py          (Queue: load, save, add, remove, clear, import_from_markdown)
  │   ├── execution_engine.py       (Execute queue, send to Claude, track progress)
  │   ├── claude_client.py          (Claude API wrapper)
  │   ├── prompt_source.py          (ADDED 2026-07-22: parse/cut ==== blocks from a markdown queue source)
  │   ├── notify.py                 (ADDED 2026-07-23: optional ntfy.sh push notifications)
  │   ├── storage.py                (JSON file I/O)
  │   └── models.py                 (Prompt, Response data classes)
  ├── setup.py                      (Package installer)
  └── requirements.txt              (Dependencies)
     ↓
  .sandglass/
  ├── queue.json                    (List of prompts to execute)
  ├── responses/                    (Saved Claude responses)
  ├── history.json                  (Archive of completed prompts)
  └── settings.json                 (User config -- currently just `queue_source`,
                                      the default markdown file `execute` falls back
                                      to when the queue is empty; see prompt_source.py)
```

---

## Component 1: CLI Interface (sandglass/cli.py)

**Entry point**: `sandglass` command (installed via `pip install -e .`)

### Commands

#### Queue Management

```python
@app.command()
def queue_add(prompt_text: str = None, file: str = None, model: str = None):
    """Add prompt to queue (text or file), optionally pinned to a model"""
    # If --file: read file content
    # If text: use directly
    # If content starts with "model: <name>\n\n", strip header, use as model
    #   (--model flag always wins over a header if both given)
    # Save to .sandglass/queue.json
    # Print: "✓ Added prompt N" (+ "(model: X)" if set)

@app.command()
def queue_list():
    """List all queued prompts"""
    # Load from .sandglass/queue.json
    # Print table:
    #   1. Fix auth bug...
    #   2. Write tests...
    #   3. Deploy...

@app.command()
def queue_remove(index: int):
    """Remove prompt from queue by index"""
    # Remove from queue.json
    # Print: "✓ Removed prompt 2"

@app.command()
def queue_clear():
    """Clear entire queue"""
    # Confirm: "Clear 3 prompts? (y/n)"
    # If yes: clear queue.json
    # Print: "✓ Queue cleared"

@app.command()
def queue_stats():
    """Show queue statistics"""
    # Print:
    #   Queue: 3 prompts, ~120,000 estimated tokens
    #   Model: claude-opus-4-8
    #   Responses saved: .sandglass/responses/
```

#### Execution

```python
@app.command()
def execute(dry_run: bool = False, once: bool = False, poll_interval: int = 900, permission_mode: str = "bypassPermissions"):
    """Execute all queued prompts sequentially"""
    # Load queue from .sandglass/queue.json
    # ADDED 2026-07-22: if queue.json is empty, import_from_markdown() the
    #   current queue source (settings.json's "queue_source", default
    #   prompt_tools/future_prompts.md) before proceeding -- see Component 7
    # For each prompt:
    #   - Send to Claude Code CLI (claude -p)
    #   - Track progress (streamed char count)
    #   - Save response to .sandglass/responses/
    #   - Remove from queue
    #   - If prompt.origin_file set: cut its block from that file into
    #     the sibling prompt_history.md, labeled [Sandglass work] (Component 7)
    # Print summary: tokens used, time taken, and (ADDED 2026-07-22) a short
    #   "what has been done" bullet list -- one line per completed prompt,
    #   the first non-empty line of its response (see Component 3)
    #
    # If dry_run: show what would run (including markdown-sourced prompts), don't execute
    # Unless --once: auto-resume through quota hits (see run_with_auto_resume, Component 3)
```

```python
@app.command()
def queue_import(file: str):
    """Set the default queue source `execute` falls back to when queue.json is
    empty. Persists file path to .sandglass/settings.json["queue_source"] --
    does not load anything immediately."""

@app.command()
def queue_source():
    """Print the current default queue source (settings.json, or
    prompt_tools/future_prompts.md if never set)."""
```

#### History & Responses

```python
@app.command()
def history():
    """Show completed prompts (archive)"""
    # Load from .sandglass/history.json
    # Print table with timestamps, token usage

@app.command()
def responses_list():
    """List saved responses"""
    # Show files in .sandglass/responses/
    # Print: response_001.json, response_002.json, etc.

@app.command()
def responses_show(index: int):
    """View a specific response"""
    # Load response JSON
    # Pretty-print Claude's response text

@app.command()
def responses_export(index: int, format: str = "json"):
    """Export response (json or markdown)"""
    # If markdown: convert to .md file
    # Save to .sandglass/responses/response_001.md
```

---

## Component 2: Queue Manager (sandglass/queue_manager.py)

```python
class QueueManager:
    """Manage prompt queue (load/save/add/remove)"""
    
    def __init__(self, queue_path: str = ".sandglass/queue.json"):
        self.queue_path = queue_path
        self._ensure_sandglass_dir()
    
    def _ensure_sandglass_dir(self):
        """Create .sandglass/ if missing"""
        # os.makedirs(".sandglass", exist_ok=True)
    
    def load_queue(self) -> list[PromptObject]:
        """Load queue from JSON"""
        # Read .sandglass/queue.json
        # If missing: return empty list
        # Parse JSON → list of PromptObject
    
    def save_queue(self, prompts: list[PromptObject]):
        """Save queue to JSON (atomic write)"""
        # Write to temp file first
        # Verify valid JSON
        # Rename temp → queue.json
        # Handle disk errors gracefully
    
    def add_prompt(self, text: str = None, file_path: str = None, model: str = None) -> str:
        """Add prompt to queue"""
        # Generate ID (timestamp-based)
        # If text/file content starts with "model: <name>\n\n", strip that
        # header and use it as the model (explicit `model` param wins if given)
        # Create PromptObject:
        #   {
        #     id: "001",
        #     title: derive from first line of text (post-header-strip),
        #     text: actual prompt text (post-header-strip),
        #     file_path: if from file, else None,
        #     added_at: ISO timestamp,
        #     source: "text" or "file",
        #     model: per-prompt model override, or None -> CLI default
        #   }
        # Append to queue
        # Return ID
    
    def remove_prompt(self, index: int):
        """Remove prompt by index (1-based)"""
        # Load queue
        # Remove at index-1
        # Save queue
    
    def clear_queue(self):
        """Clear entire queue"""
        # Save empty list to queue.json
    
    def get_prompt(self, index: int) -> PromptObject:
        """Get prompt by index (1-based)"""
```

---

## Component 3: Execution Engine (sandglass/execution_engine.py)

```python
class ExecutionEngine:
    """Execute queued prompts sequentially"""
    
    def __init__(self, claude_client: ClaudeClient, queue_manager: QueueManager):
        self.claude_client = claude_client
        self.queue_manager = queue_manager
        self.responses_dir = ".sandglass/responses"
    
    async def execute_queue(self, dry_run: bool = False) -> ExecutionResult:
        """Execute all prompts in queue"""
        # Load queue
        # If empty: print "No prompts to execute"
        # For each prompt (enumerate):
        #   print(f"[{i}/{total}] {prompt.title}")
        #   if dry_run: print "  [DRY RUN] Would send this")
        #   else: execute_prompt(prompt)
        # After all: print summary
        # Move completed prompts to history.json
        # Clear queue
        # Return ExecutionResult (tokens used, time, etc.)
    
    async def execute_prompt(self, prompt: PromptObject) -> Response:
        """Send single prompt to Claude, track progress"""
        # print "  📤 Sending to Claude..."
        # Send to Claude API via claude_client
        # Stream response, show progress:
        #   - Track tokens in real-time
        #   - Print: "▶ ████░░░░░░░ 35% complete"
        # Save response to .sandglass/responses/response_{id}.json
        # Return Response object
    
    def _get_next_response_id(self) -> str:
        """Get next response ID (001, 002, etc.)"""
        # Count existing files in responses/
        # Return f"{count+1:03d}"
    
    def _save_response(self, prompt_id: str, response: Response):
        """Save response to JSON file"""
        # Create responses/ dir if missing
        # Write to responses/response_{id}.json
        # Atomic write (temp file + rename)
    
    def _archive_prompt(self, prompt: PromptObject, result: Response):
        """Move completed prompt to history.json"""
        # Load history.json
        # Append: {prompt, response, completed_at, tokens_used}
        # Save history.json

    @staticmethod
    def _work_log_snapshot() -> tuple[float, int] | None:
        """ADDED 2026-07-23: (mtime, size) of master_plan/work_log.md, or
        None if master_plan/ doesn't exist in the CWD. None means "this repo
        doesn't use the work_log.md convention" -- Sandglass is a generic
        tool and shouldn't invent it for someone else's project. A missing
        *file* inside an existing master_plan/ dir returns a sentinel
        (0.0, 0), not None -- that's a normal first-entry case.
        """

    def _append_work_log_entry(self, prompt: PromptObject, response: Response) -> None:
        """ADDED 2026-07-23: this project's own CLAUDE.md mandates a
        work_log.md session report after every task. A queued prompt's own
        headless `claude -p` run loads that same CLAUDE.md and *can* satisfy
        the mandate itself (confirmed in practice -- two real headless runs
        this same day each wrote their own full narrative entry for
        substantial changes), but doesn't reliably for small/quick prompts
        that judge themselves not worth a report. execute_queue() snapshots
        WORK_LOG_PATH via `_work_log_snapshot()` right before each prompt and
        compares again right after; only if it's unchanged (nothing was
        self-logged) does it call this to append a clearly-labeled,
        lightweight fallback entry -- prompt title as Goal, the same
        `_summarize_response()` used for the CLI's own "what has been done"
        bullets as Work Done, a pointer to the full saved response. This
        "log only if not already logged" design is what avoids a duplicate,
        redundant entry for the prompts that already self-report properly.
        """

    async def run_with_auto_resume(self, poll_interval: float = 900, max_stalls: int = 5) -> ExecutionResult:
        """ADDED 2026-07-22 -- the actual point of the tool.

        Without this, `execute_queue()` stopping on the first quota hit made
        the CLI no better than pasting a prompt into chat yourself. This loops
        execute_queue(): on a QuotaExceededError, it reads the exact refresh
        time from `rate_limit_event.resetsAt` (falling back to poll_interval
        if unknown), sleeps until then via `_sleep_with_animation` (see
        below), then retries -- durable per-prompt removal means the retry
        just continues where it stopped. Repeats until the queue is empty or
        ExecutionResult.stopped_reason != "quota" (a non-quota failure is
        never auto-retried). Gives up after max_stalls consecutive attempts
        make no progress on the same prompt, in case a non-quota error got
        misclassified as a quota one.
        """

    @staticmethod
    async def _sleep_with_animation(total_seconds: float, resume_at: str | None) -> None:
        """ADDED 2026-07-22, made compact same day: replaces the old "print a
        heartbeat line every few minutes" wait with a live, terminal-style
        ASCII sandglass (`_hourglass_frame(tick, caption)`, module-level,
        pure function) rendered via `rich.live.Live(transient=True)` -- same
        pattern `execute_prompt`'s progress spinner already uses. Sized like
        a compact spinner glyph (5 lines: 2 top + neck + 2 bottom, no border
        rows) rather than a big block, per explicit user request -- the live
        countdown is folded into the neck row as `caption` instead of a
        separate line. Ticks every ANIMATION_TICK_SECONDS (0.35s); the sand
        level and a flickering neck grain animate purely as a liveness
        indicator (like any spinner), not literally synced to the real
        remaining time -- the countdown text is what's accurate. Verified
        directly (not assumed) that Rich's Live prints nothing per-frame when
        stdout isn't a real terminal (piped/redirected/captured) -- only the
        start ("Waiting...") and end ("Resuming...") console.print lines show
        up there, so a multi-hour wait never floods a redirected log with
        thousands of frames.
        """

    def _print_summary(self, results: list[Response], failed: int, total_tokens: int,
                        elapsed: float, stopped_reason: str | None = None) -> None:
        """ADDED 2026-07-22: prints the post-run summary, now including a
        "what has been done" bullet list (one line per completed prompt,
        via `_summarize_response` -- the first non-empty line of that
        prompt's response, truncated to SUMMARY_MAX). Omitted entirely when
        nothing completed. Runs once per `execute_queue()` call, so a
        `run_with_auto_resume` batch that pauses for a quota wait and
        resumes prints one such block per pass, not one for the whole run.
        """
```

---

## Component 4: Claude Client Wrapper (sandglass/claude_client.py)

> **CORRECTED 2026-07-21** — this component originally specified the raw Anthropic
> Messages API (`Anthropic(api_key=...)`). That's a different product from a Claude
> Pro/Max subscription: the Messages API bills pay-per-token API credits with no
> "refresh," while `human_idea.md`'s whole premise ("optimized for $20/month Claude
> account", "quota refresh") is about the subscription's rolling usage window. Those
> only overlap if you separately buy API credits — which defeats the tool's purpose.
> Fixed by shelling out to the already-installed `claude` CLI in headless mode
> (`claude -p`) instead, so execution draws on whatever account `claude` is logged
> into (`claude auth login` → subscription, not API key).

```python
class ClaudeClient:
    """Wraps `claude -p` (Claude Code headless mode), not the Messages API."""

    def __init__(self, model: str = "claude-opus-4-8", permission_mode: str = "bypassPermissions"):
        # self._cli_path = shutil.which("claude")
        # permission_mode is passed through to `claude -p --permission-mode ...`.
        # Defaults to bypassPermissions: unattended prompts can't answer an
        # interactive permission dialog, so Sandglass always runs with full
        # tool access unless the caller explicitly asks for a stricter mode
        # (e.g. "default"). `sandglass execute` prints a warning every run.

    async def send_prompt(self, text: str, on_chunk: Callable = None, model: str = None) -> Response:
        """Run `claude -p TEXT --output-format stream-json --include-partial-messages
        --verbose --no-session-persistence --permission-mode ... --model ...` as a
        subprocess (opened with limit=STREAM_BUFFER_LIMIT=10MB -- FIXED 2026-07-22:
        asyncio's 64KB default per-line buffer raised LimitOverrunError on a single
        stream-json line embedding a large tool result/file read under bypassPermissions;
        LimitOverrunError/ValueError are also caught explicitly to kill+reap the
        subprocess and raise a clear message instead of an opaque asyncio error),
        parse the newline-delimited JSON event stream:
          - "stream_event" / content_block_delta / text_delta -> on_chunk(piece), accumulate
          - "rate_limit_event" -> rate_limit_info {status, resetsAt, rateLimitType, ...}
            (the live quota signal `run_with_auto_resume` watches -- see Component 3)
          - "result" -> final text (`result`), `usage` (token counts), `is_error`
        Raises QuotaExceededError (carrying rate_limit_info) if rate_limit_info/result
        indicates the account's usage limit is hit; RuntimeError for any other failure.
        Return Response(text=..., tokens_used=..., model=effective_model)
        """

    def get_auth_status(self) -> dict | None:
        """Runs `claude auth status` (JSON output) so the CLI can warn the user
        up front if execution would bill API credits instead of a subscription."""

    def estimate_tokens(self, text: str) -> int:
        """Rough estimate: len(text) / 4"""
```

---

## Component 5: Data Models (sandglass/models.py)

```python
from dataclasses import dataclass
from datetime import datetime

@dataclass
class PromptObject:
    """A prompt in the queue"""
    id: str
    title: str
    text: str
    source: str = "text"  # "text" or "file"
    file_path: str = None
    added_at: str = None  # ISO timestamp
    model: str = None  # per-prompt model override; None = use the CLI default
    origin_file: str = None  # if set, cut this prompt's block from here on success (Component 7)

@dataclass
class Response:
    """Claude's response to a prompt"""
    prompt_id: str
    text: str
    tokens_used: int
    completion_time: str  # ISO timestamp
    model: str

@dataclass
class ExecutionResult:
    """Result of executing entire queue"""
    completed: int
    failed: int
    total_tokens: int
    total_time: float  # seconds
    responses: list[Response]
    stopped_reason: str = None  # None (fully done), "quota", or "error"
    resume_at: str = None  # ISO timestamp quota is expected to refresh, if known
```

---

## Component 6: Storage (sandglass/storage.py)

```python
class StorageService:
    """JSON file I/O"""
    
    def __init__(self, base_path: str = ".sandglass"):
        self.base_path = base_path
    
    @property
    def queue_path(self) -> str:
        return f"{self.base_path}/queue.json"
    
    @property
    def responses_dir(self) -> str:
        return f"{self.base_path}/responses"
    
    @property
    def history_path(self) -> str:
        return f"{self.base_path}/history.json"
    
    @property
    def settings_path(self) -> str:
        return f"{self.base_path}/settings.json"
    
    def load_json(self, path: str) -> dict:
        """Load JSON from file"""
        # If missing: return {}
        # If invalid: backup old, return {}
        # Read and parse
    
    def save_json(self, path: str, data: dict):
        """Save JSON to file (atomic)"""
        # Write to temp file
        # Verify valid JSON
        # Rename temp → target
        # Handle errors gracefully
    
    def ensure_dir(self, path: str):
        """Create directory if missing"""
        # os.makedirs(path, exist_ok=True)
```

---

## Component 7: Prompt Source (sandglass/prompt_source.py) — ADDED 2026-07-22

Lets `sandglass execute` use a markdown file as the default queue when nothing's
been added via `queue add` — specifically `prompt_tools/future_prompts.md`, the same
file and `====` delimiter convention the manual "future prompts" chat workflow already
uses (see the project's `CLAUDE.md`). Two independent execution paths can consume this
file — an interactive chat session reading it by hand, or `sandglass execute` reading
it headlessly — but whichever processes a block first cuts it out, so they never
double-process the same one.

```python
DEFAULT_QUEUE_SOURCE = "prompt_tools/future_prompts.md"

def parse_blocks(raw_text: str) -> list[dict]:
    """FIXED 2026-07-22: each '====' line is a boundary *between* two
    blocks (N prompts, N-1 delimiters), not an open/close pair -- a lone
    prompt (including the last one left after every earlier cut) needs no
    delimiter at all. Returns [{"text": ..., "start": ..., "end": ...}] in
    file order -- start/end span the block's content plus the delimiter
    that follows it, so a block can be excised precisely, leaving the next
    one at the top with no stray leading delimiter."""

def read_blocks(file_path: str) -> list[dict]:
    """parse_blocks() on file_path's contents; [] if the file is missing."""

def cut_first_block(file_path: str) -> str | None:
    """Remove the first block from file_path, return its text (None if
    there are no blocks left). Called only on successful completion --
    a failed or in-flight prompt's block stays put, mirroring queue.json's
    own per-prompt durable-removal-only-on-success semantics."""

def history_path_for(source_file: str) -> str:
    """Sibling prompt_history.md next to source_file (ROUTED here 2026-07-23,
    was history_prompts.md)."""

def prepend_to_history(history_path: str, title: str, model: str, block_text: str,
                        *, via: str = "sandglass execute", label: str | None = "Sandglass work"):
    """Archive a completed block, newest first -- same layout the manual
    "future prompts" workflow already writes by hand. ADDED 2026-07-23:
    via/label distinguish a genuine headless sandglass execute run (the
    default -- ExecutionEngine._cut_from_source's only real call site) from
    a prompt processed by hand in an interactive chat session (pass
    label=None and a different via)."""

def append_interruption_note(history_path: str, title: str, resume_at: str | None):
    """ADDED 2026-07-23: records that a queued prompt was interrupted by a
    quota hit, without cutting its block -- purely an audit-trail marker so
    the gap before its eventual completion entry isn't silent."""
```

**Integration points:**
- `QueueManager.import_from_markdown(source_file)`: reads all current blocks,
  adds each via `add_prompt(text=block, origin_file=source_file)` (reusing the
  existing `model:` header parsing) — called by `cli.py:execute()` only when
  `queue.json` is empty.
- `ExecutionEngine._cut_from_source(prompt, response)`: called right after a
  prompt with `origin_file` set completes successfully — cuts its block and
  archives it, alongside the existing `.sandglass/history.json` archiving.
- `ExecutionEngine._note_interruption(prompt, resume_at)`: ADDED 2026-07-23 —
  called right after a `QuotaExceededError` for a prompt with `origin_file`
  set, before `execute_queue()` breaks out of its loop. Never cuts the block.
- `cli.py`'s `_get_queue_source()`: reads `settings.json["queue_source"]`,
  defaulting to `DEFAULT_QUEUE_SOURCE` — `queue import FILE` is the only thing
  that writes this key.

---

## Component 8: Notifications (sandglass/notify.py) — ADDED 2026-07-23

Optional ntfy.sh push notifications for `run_with_auto_resume`'s lifecycle events
(quota wait started, resumed, batch complete, batch stopped early) — for when nobody's
watching the terminal during a long unattended run. Configured entirely through
environment variables (`SANDGLASS_NTFY_TOPIC`, optionally `SANDGLASS_NTFY_SERVER`),
loadable from a `.env` file via a small stdlib-only parser in `sandglass/__init__.py`'s
`_load_dotenv()` (no `python-dotenv` dependency, run once at import time, doesn't
override an already-set env var).

```python
NTFY_TOPIC_ENV = "SANDGLASS_NTFY_TOPIC"
NTFY_SERVER_ENV = "SANDGLASS_NTFY_SERVER"
DEFAULT_NTFY_SERVER = "https://ntfy.sh"

def is_configured() -> bool:
    """Whether a topic is set."""

def send(message: str, title: str = "Sandglass", priority: str = "default") -> bool:
    """POST to {server}/{topic} via stdlib urllib (no new dependency).
    Returns False (never raises) if unconfigured or the request fails --
    a notification is a nice-to-have, never a reason to break or stall
    a queue run over a flaky network."""
```

**Integration point:** `ExecutionEngine.run_with_auto_resume` calls `notify.send(...)`
at exactly the branches described above: right before `_sleep_with_animation` (quota
hit -> waiting), right after it returns (resumed), and at each of its `return
last_result` points that represent a genuine stop (queue empty -> complete; non-quota
error or max-stalls give-up -> stopped early, `priority="high"`). The queue-empty-at-
the-very-start return (nothing was ever queued) sends nothing, since nothing happened.

---

## File Structure

```
sandglass-cli/
├── sandglass/
│   ├── __init__.py
│   ├── __main__.py               (Entry point: python -m sandglass)
│   ├── cli.py                    (Command definitions)
│   ├── queue_manager.py          (Queue operations)
│   ├── execution_engine.py       (Execute prompts)
│   ├── claude_client.py          (Claude API wrapper)
│   ├── prompt_source.py          (Markdown queue source parsing/cutting)
│   ├── storage.py                (JSON I/O)
│   └── models.py                 (Data classes)
├── tests/
│   ├── test_prompt_source.py
│   ├── test_queue_manager.py
│   ├── test_execution_engine.py
│   └── test_storage.py
├── setup.py                      (Package config, entry point)
├── requirements.txt
├── README.md
└── CHANGELOG.md
```

---

## setup.py (Package Installation)

```python
from setuptools import setup, find_packages

setup(
    name="sandglass",
    version="0.1.0",
    description="Batch prompt queue executor for Claude",
    packages=find_packages(),
    install_requires=[
        "typer>=0.9.0",
        "rich>=13.0.0",  # Pretty terminal output
    ],
    # No Anthropic SDK dependency — execution shells out to the `claude` CLI
    # (a separate, already-installed binary), not the Messages API.
    entry_points={
        "console_scripts": [
            "sandglass=sandglass.cli:app",  # Makes 'sandglass' command
        ],
    },
    python_requires=">=3.9",
)
```

---

## CLI Usage Flow

### Scenario: Queue 3 Prompts & Execute

```
$ sandglass queue add "Fix auth bug"
✓ Added prompt 1 to queue

$ sandglass queue add prompts/tests.txt
✓ Added prompt 2 to queue

$ sandglass queue add "Deploy to staging"
✓ Added prompt 3 to queue

$ sandglass queue list
Queued Prompts (3):
  1. Fix auth bug
  2. [From file] tests.txt
  3. Deploy to staging

$ sandglass execute

[Executing queue (3 prompts)...]

[1/3] Fix auth bug
  📤 Sending to Claude (claude-opus-4-8)...
  ▶ ████░░░░░░░░░░░░░░░░ 20% complete (5 min)
  ▶ ████████░░░░░░░░░░░░ 38% complete (10 min)
  ▶ ████████████░░░░░░░░ 62% complete (15 min)
  ✅ Done (22,450 tokens, 18 min)

[2/3] [From file] tests.txt
  📤 Sending to Claude...
  ▶ ███░░░░░░░░░░░░░░░░░ 15% complete
  ...

[Queue Complete!]
Summary:
  ✅ 3 prompts executed
  💰 Total tokens: 58,150
  ⏱️ Total time: 54 minutes
  📁 Responses: .sandglass/responses/
```

---

## Data Flow

### Add Prompt

```
$ sandglass queue add "Fix auth bug"
     ↓
  cli.py: queue_add()
     ↓
  queue_manager.add_prompt(text="Fix auth bug")
     ↓
  Generate ID: "001"
  Create PromptObject: {id: "001", text: "Fix auth bug", ...}
     ↓
  storage.save_json(.sandglass/queue.json, [...])
     ↓
  Print: "✓ Added prompt 1"
```

### Execute Queue

```
$ sandglass execute
     ↓
  cli.py: execute()
     ↓
  execution_engine.execute_queue()
     ↓
  For each prompt in queue:
    - Fetch from queue
    - Call execution_engine.execute_prompt(prompt)
      ↓
      - Call claude_client.send_prompt(text)
        ↓ (streams response)
        - Accumulate response text
        - Update progress display
      - Save to .sandglass/responses/response_001.json
      - Append to .sandglass/history.json
    - Remove from queue
  ↓
  Print summary (tokens, time)
```

---

## Error Handling

### Missing .sandglass/
- Auto-create on first command
- Create queue.json, responses/, etc.

### Invalid queue.json
- Backup to queue.json.bak
- Create fresh queue.json
- Warn user: "Queue was corrupted, backed up to .bak"

### Claude API Error
- Pause execution
- Show error message
- Option to retry or skip

### Quota Limit Hit (DONE 2026-07-22, not Phase 2 anymore)
- Read the exact refresh time from the CLI backend's `rate_limit_event.resetsAt`
- Sleep until then behind a live animated ASCII sandglass (updated 2026-07-22 --
  was a periodic text heartbeat, now `_sleep_with_animation`, see Component 3),
  falling back to a 15-min poll only if no exact time was reported
- Auto-retry from where the queue stopped (`ExecutionEngine.run_with_auto_resume`)
- Give up after 5 consecutive stalls on the same prompt (likely misclassified,
  not actually quota) rather than looping forever
- `--once` opts back into the old stop-immediately behavior

---

## Testing Strategy

### Unit Tests
- QueueManager: add, remove, clear, load/save
- StorageService: JSON read/write, atomic writes
- Models: serialization/deserialization

### Integration Tests
- Full flow: add → list → execute → check responses

### Manual Testing
- Add 3 prompts (text + file)
- Execute and verify responses saved
- Check .sandglass/ folder structure

---

## Dependencies

```
typer>=0.9.0        # CLI framework
rich>=13.0.0        # Terminal output (colors, tables)
```

Plus the external `claude` CLI (Claude Code) on PATH — execution shells out to it
(`claude -p`) rather than calling the Anthropic Messages API directly, so usage draws
on whatever account `claude` is logged into (Pro/Max subscription, via `claude auth
login`) instead of separate pay-per-token API credits.

**No heavy Python dependencies** — keeps installation fast and lightweight.

---

## Implementation Phases

### Phase 1: MVP (This Sprint)
- ✅ CLI commands (queue add/list/execute)
- ✅ Sequential execution
- ✅ Response saving
- ✅ Progress display
- ✅ Local storage

### Phase 2: Auto-Resume — ✅ DONE 2026-07-22 (see Quota Limit Hit above)
- ✅ Auto quota checking (via `rate_limit_event`, not blind polling)
- ✅ Graceful pause on quota hit
- ✅ Auto-resume when quota available
- ⏳ Notifications (ntfy.sh) — still not built
- ⏳ Batch operations (add multiple files) — still not built

### Phase 3: Polish
- Settings UI
- Response export (markdown, CSV)
- Scheduled execution (run at specific time)
- Cloud backup (optional)


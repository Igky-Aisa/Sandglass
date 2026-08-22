"""CLI entry point: queue management and execution commands."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
from datetime import datetime
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__  # noqa: E402 — UTF-8 stdout reconfig happens on package import
from . import accounts as accounts_mod
from .accounts import AccountPool, AccountsError
from .claude_client import DEFAULT_PERMISSION_MODE, ClaudeClient
from .execution_engine import (
    ACCOUNTING_SCHEMA,
    DEFAULT_POLL_INTERVAL_SECONDS,
    ON_REFUSAL_MODES,
    SESSION_MODE_CHAIN,
    SESSION_MODES,
    ExecutionEngine,
)
from . import dashboard as dashboard_mod
from . import project_docs, providers as providers_mod, quiet_hours, run_report, updater
from .project_scaffold import new_claude_project, update_claude_md_template
from .prompt_source import DEFAULT_QUEUE_SOURCE
from .queue_manager import QueueManager
from .storage import StorageService

logger = logging.getLogger(__name__)

console = Console()

app = typer.Typer(
    add_completion=False,
    help="Sandglass — batch prompt queue executor for Claude.",
)

queue_app = typer.Typer(help="Manage the prompt queue.")
app.add_typer(queue_app, name="queue")

responses_app = typer.Typer(help="Inspect saved responses.")
app.add_typer(responses_app, name="responses")


@app.callback()
def main() -> None:
    """Initialize the CLI. Ensures the .sandglass/ workspace exists."""
    try:
        StorageService().ensure_sandglass_dir()
    except OSError as exc:
        console.print(f"[red]Error: could not initialize .sandglass/ workspace: {exc}[/red]")
        raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """Show the Sandglass version."""
    typer.echo(f"sandglass {__version__}")


@app.command()
def commands() -> None:
    """List every available command, grouped by top-level and subcommand."""
    root = typer.main.get_command(app)

    table = Table(title="Sandglass Commands")
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Description")

    def add_rows(cmd, prefix: str) -> None:
        sub_commands = getattr(cmd, "commands", None)
        if sub_commands:
            for name, sub in sorted(sub_commands.items()):
                add_rows(sub, f"{prefix} {name}".strip())
        else:
            help_text = (cmd.get_short_help_str() or "").strip()
            table.add_row(f"sandglass {prefix}", help_text)

    for name, cmd in sorted(root.commands.items()):
        add_rows(cmd, name)

    console.print(table)
    console.print("[dim]Run `sandglass <command> --help` for options on any command.[/dim]")


@app.command()
def sleeptime(
    start: Optional[str] = typer.Argument(None, help="Start of quiet hours, e.g. 22 or 22:30."),
    end: Optional[str] = typer.Argument(None, help="End of quiet hours, e.g. 6 or 06:30."),
    off: bool = typer.Option(False, "--off", help="Disable quiet hours (notify at any hour)."),
    on: bool = typer.Option(False, "--on", help="Re-enable quiet hours with the saved window."),
) -> None:
    """Set the hours when push notifications are held back (default 22:00-06:00).

    Sandglass runs unattended for hours, so a quota wait routinely spans the
    night. Within this window every ntfy notification is dropped instead of
    buzzing your phone -- the run itself keeps waiting and resuming normally.

    Run with no arguments to show the current window.
    """
    if off and on:
        console.print("[red]Error: pass either --off or --on, not both.[/red]")
        raise typer.Exit(code=1)

    current = quiet_hours.load()

    if start is None and end is None and not off and not on:
        state = "on" if current.enabled else "off"
        console.print(f"Sleep time: {current.start_text} → {current.end_text} ([bold]{state}[/bold])")
        if current.enabled:
            if quiet_hours.is_quiet():
                console.print("[dim]Active right now — notifications are being held.[/dim]")
            else:
                console.print("[dim]Not active right now — notifications will go through.[/dim]")
        console.print(f"[dim]Stored in {quiet_hours.global_settings_path()} (applies to every project).[/dim]")
        if os.environ.get(quiet_hours.QUIET_HOURS_ENV):
            console.print(
                f"[yellow]Note: {quiet_hours.QUIET_HOURS_ENV} is set and overrides the saved value.[/yellow]"
            )
        return

    if start is None and (off or on):
        # Toggling only -- keep whatever window is already saved.
        saved = quiet_hours.save(current.start, current.end, enabled=on)
        console.print(
            f"[green]✓ Sleep time {'enabled' if on else 'disabled'}"
            f" ({saved.start_text} → {saved.end_text})[/green]"
        )
        return

    if start is None or end is None:
        console.print("[red]Error: give both a start and an end, e.g. `sandglass sleeptime 22 6`.[/red]")
        raise typer.Exit(code=1)

    try:
        start_minutes = quiet_hours.parse_time(start)
        end_minutes = quiet_hours.parse_time(end)
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    if start_minutes == end_minutes:
        console.print("[red]Error: start and end are the same — that window is empty.[/red]")
        raise typer.Exit(code=1)

    saved = quiet_hours.save(start_minutes, end_minutes, enabled=not off)
    console.print(f"[green]✓ Sleep time set to {saved.start_text} → {saved.end_text}[/green]")
    console.print("[dim]Notifications are held during this window on every project.[/dim]")
    if os.environ.get(quiet_hours.QUIET_HOURS_ENV):
        console.print(
            f"[yellow]Note: {quiet_hours.QUIET_HOURS_ENV} is set and overrides what you just saved.[/yellow]"
        )


# --- project scaffolding ----------------------------------------------------


@app.command("new-claude-project")
def new_claude_project_cmd(
    path: str = typer.Argument(".", help="Directory to scaffold into (defaults to the current directory)."),
) -> None:
    """Scaffold CLAUDE.md, master_plan/, and prompt_tools/ from the bundled template.

    CLAUDE.md is copied with its template content; files under master_plan/
    and prompt_tools/ are created empty (only the filenames are templated).
    Anything that already exists at the target is left untouched. Run
    `sandglass claude-md-update` first if you want this to use a CLAUDE.md
    other than the one currently bundled.
    """
    try:
        result = new_claude_project(path)
    except OSError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    for created in result.created:
        console.print(f"[green]✓ Created {created}[/green]")
    for skipped in result.skipped:
        console.print(f"[dim]  Skipped (already exists): {skipped}[/dim]")

    if not result.created:
        console.print("[yellow]Nothing to do — every templated file already exists.[/yellow]")


@app.command("claude-md-update")
def claude_md_update_cmd(
    source: str = typer.Argument("CLAUDE.md", help="CLAUDE.md to adopt as the new template."),
) -> None:
    """Adopt SOURCE as the bundled CLAUDE.md template for future `new-claude-project` runs."""
    try:
        template_path = update_claude_md_template(source)
    except OSError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Template updated from {source} → {template_path}[/green]")


# --- queue add/list/remove/clear/stats -------------------------------------


@queue_app.command("add")
def queue_add(
    prompt: Optional[str] = typer.Argument(None, help="Prompt text to queue."),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Path to a file with the prompt text."),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help=(
            "Model for this prompt only (e.g. opus, sonnet, or a full model ID). "
            "Overrides any `model: <name>` header in --file content. If omitted, "
            "falls back to the header if present, else the CLI's default model."
        ),
    ),
    effort: Optional[str] = typer.Option(
        None,
        "--effort",
        "-e",
        help=(
            "Reasoning depth for this prompt only (low|medium|high|xhigh|max). "
            "Lower effort means fewer, more-consolidated tool calls and less "
            "preamble — the cheapest dial that doesn't change the model. "
            "Overrides an `effort:` header or a TIER marker in the text."
        ),
    ),
) -> None:
    """Add a prompt to the queue (text or --file).

    A prompt (from --file, or typed text) may start with `model:` / `effort:`
    header lines followed by a blank line — those apply to this prompt unless
    the matching option is also given, e.g.:

        model: opus
        effort: high

        Refactor the auth module...

    Failing both, a `TIER: SONNET`-style marker near the top of the text
    supplies defaults.
    """
    if bool(prompt) == bool(file):
        console.print("[red]Error: provide either prompt text or --file, not both/neither.[/red]")
        raise typer.Exit(code=1)

    qm = QueueManager()
    try:
        prompt_id = qm.add_prompt(text=prompt, file_path=file, model=model, effort=effort)
    except FileNotFoundError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    added = qm.get_prompt(int(prompt_id))
    bits = []
    if added.model:
        bits.append(f"model: {added.model}")
    if added.effort:
        bits.append(f"effort: {added.effort}")
    if added.provider:
        # Never let this one be inferred from the model name alone — "it runs
        # somewhere other than Anthropic" is the fact worth seeing at the
        # moment the prompt is queued, not at 3am in a log.
        bits.append(f"provider: {added.provider}")
    suffix = f" [dim]({', '.join(bits)})[/dim]" if bits else ""
    console.print(f"[green]✓ Added prompt {int(prompt_id)} to queue[/green]{suffix}")


@queue_app.command("list")
def queue_list() -> None:
    """List all queued prompts."""
    qm = QueueManager()
    prompts = qm.get_all_prompts()

    if not prompts:
        source = _get_queue_source(qm.storage)
        console.print(f"No prompts in queue [dim](execute will check {source} instead)[/dim]")
        return

    table = Table(title=f"Queued Prompts ({len(prompts)})")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Title")
    table.add_column("Source", style="magenta")
    table.add_column("Model", style="green")
    table.add_column("Effort", style="green")
    table.add_column("Runs on", style="yellow")
    table.add_column("Added At", style="dim")

    for i, p in enumerate(prompts, start=1):
        table.add_row(
            str(i), p.title, p.source,
            p.model or "(default)", p.effort or "(default)",
            p.provider or "anthropic", p.added_at,
        )

    console.print(table)


@queue_app.command("remove")
def queue_remove(index: int = typer.Argument(..., help="1-based index of the prompt to remove.")) -> None:
    """Remove a prompt from the queue by index."""
    qm = QueueManager()
    try:
        qm.remove_prompt(index)
    except IndexError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Removed prompt {index}[/green]")


@queue_app.command("clear")
def queue_clear(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Clear the entire queue."""
    qm = QueueManager()
    prompts = qm.get_all_prompts()

    if not prompts:
        console.print("No prompts in queue")
        return

    if not yes and not typer.confirm(f"Clear all {len(prompts)} prompts?"):
        console.print("Cancelled")
        raise typer.Exit(code=0)

    qm.clear_queue()
    # Clearing the queue ends the drain those prompts belonged to, so the
    # chained session goes with them — the next `execute` is a new piece of
    # work and shouldn't append to an abandoned conversation.
    try:
        if os.path.exists(qm.storage.run_state_path):
            os.remove(qm.storage.run_state_path)
    except OSError:
        pass
    console.print("[green]✓ Queue cleared[/green]")


@queue_app.command("stats")
def queue_stats() -> None:
    """Show queue statistics."""
    qm = QueueManager()
    prompts = qm.get_all_prompts()
    client = ClaudeClient()

    total_tokens = sum(client.estimate_tokens(p.text) for p in prompts)

    console.print(f"Queue: {len(prompts)} prompts, ~{total_tokens:,} estimated tokens")
    console.print(f"Model: {client.model} (default)")
    console.print(f"Responses: {qm.storage.responses_dir}")


def _get_queue_source(storage: StorageService) -> str:
    """The markdown file `execute` loads from when nothing's been queued
    directly — `prompt_tools/future_prompts.md` unless overridden via
    `queue import`."""
    settings = storage.load_json(storage.settings_path)
    return settings.get("queue_source", DEFAULT_QUEUE_SOURCE) if isinstance(settings, dict) else DEFAULT_QUEUE_SOURCE


@queue_app.command("import")
def queue_import(
    file: str = typer.Argument(
        ..., help="Markdown file to use as the default queue source (==== delimited blocks)."
    ),
) -> None:
    """Set which file `execute` loads from by default (overrides prompt_tools/future_prompts.md).

    This only changes *which file is consulted* — it doesn't load anything
    immediately. `sandglass execute` reads whatever this points to the next
    time the queue is empty.
    """
    storage = StorageService()
    settings = storage.load_json(storage.settings_path)
    if not isinstance(settings, dict):
        settings = {}
    settings["queue_source"] = file
    storage.save_json(storage.settings_path, settings)
    console.print(f"[green]✓ Queue source set to {file}[/green]")


@queue_app.command("source")
def queue_source_cmd() -> None:
    """Show the file `execute` currently loads from by default."""
    storage = StorageService()
    console.print(f"Queue source: {_get_queue_source(storage)}")


# --- execute ---------------------------------------------------------------


@app.command()
def execute(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would run without calling Claude."
    ),
    once: bool = typer.Option(
        False,
        "--once",
        help=(
            "Run through the queue a single time and stop at the first failure "
            "(including a quota hit) instead of waiting out the subscription's "
            "usage limit and auto-retrying. This is the old behavior."
        ),
    ),
    poll_interval: int = typer.Option(
        DEFAULT_POLL_INTERVAL_SECONDS,
        "--poll-interval",
        help=(
            "Fallback wait (seconds) between retries after a quota hit, used only "
            "when Claude Code doesn't report an exact reset time. Ignored with --once."
        ),
    ),
    permission_mode: str = typer.Option(
        DEFAULT_PERMISSION_MODE,
        "--permission-mode",
        help=(
            "Passed through to `claude -p`. Defaults to bypassPermissions, since "
            "queued prompts run unattended and can't answer an interactive "
            "permission prompt. Pass a stricter mode (e.g. `default`) to require "
            "the same permission rules an interactive session would use."
        ),
    ),
    require_artifact: bool = typer.Option(
        True,
        "--require-artifact/--no-require-artifact",
        help=(
            "Refuse to cut a markdown block out of its source file when the prompt "
            "changed no file in the git working tree, and stop the run. Protects "
            "against archiving a block whose response was a refusal — the cut "
            "destroys the only copy of the block text. Skipped automatically "
            "outside a git repo. Pass --no-require-artifact for queues of "
            "read-only prompts (reviews, questions) that legitimately write nothing."
        ),
    ),
    on_refusal: str = typer.Option(
        "ask",
        "--on-refusal",
        help=(
            "What to do when a block responds but changes no file: 'ask' (default) "
            "spends one cheap turn asking the run whether the work was already "
            "DONE, is BLOCKED on something missing, or genuinely needed no file "
            "change (NOOP) — DONE/NOOP keep the queue moving, BLOCKED stops with "
            "the cause named. 'stop' always stops. Neither ever cuts a block on "
            "the model's word."
        ),
    ),
    accounts_mode: str = typer.Option(
        "auto",
        "--accounts",
        help=(
            "How to use a pool of Claude subscriptions defined in "
            "~/.sandglass/accounts.json (or $SANDGLASS_ACCOUNTS): 'auto' "
            "(default) rotates to the next account when one runs out of quota, "
            "if that file exists, and otherwise behaves exactly as before; "
            "'rotate' requires the file and errors if it is missing; 'off' "
            "ignores it and waits out the quota on the current login. Accounts "
            "must be your own — sharing credentials breaks Anthropic's terms."
        ),
    ),
    external: bool = typer.Option(
        True,
        "--external/--no-external",
        help=(
            "Honour a block's `**CLINE: pro**` / `provider:` marker and route it "
            "to a non-Anthropic endpoint (DeepSeek), using the key from "
            "~/.sandglass/providers.json. --no-external forces every block onto "
            "Anthropic. Only marked blocks are ever routed out; an unmarked "
            "block never leaves Anthropic either way."
        ),
    ),
    skip_executed: bool = typer.Option(
        True,
        "--skip-executed/--no-skip-executed",
        help=(
            "Before sending a block, drop it if some other runner already ran and "
            "cut it — the block is gone from the source file and present in the "
            "sibling history file. Costs nothing and prevents paying a full block "
            "to be told the work was already done. Requires both facts, so a "
            "re-authored block is never mistaken for a finished one."
        ),
    ),
    brief: bool = typer.Option(
        True,
        "--brief/--no-brief",
        help=(
            "Prepend a bounded project-state brief (the last work_log entries "
            "plus Progress.md) to each prompt, and tell it not to re-read those "
            "files in full. Every block otherwise re-reads an append-only log "
            "from cold, which on a mature project costs more than the task "
            "does. --no-brief restores the unbounded read."
        ),
    ),
    session_mode: str = typer.Option(
        SESSION_MODE_CHAIN,
        "--session-mode",
        help=(
            "How prompts map onto Claude Code sessions. "
            "'chain' (default) runs the whole queue in one warm session, so "
            "only the first block pays for the system prompt, CLAUDE.md and "
            "the files the queue has already read. "
            "'prompt' gives each block its own session — retries resume, but "
            "blocks never see each other. "
            "'isolate' persists no session at all: every block a cold start."
        ),
    ),
    tiers: bool = typer.Option(
        True,
        "--tiers/--no-tiers",
        help=(
            "Honour `TIER: SONNET`-style markers in queue blocks as model/effort "
            "defaults. --no-tiers runs every block on the default model "
            "regardless of what its text says."
        ),
    ),
    effort: Optional[str] = typer.Option(
        None,
        "--effort",
        help=(
            "Default reasoning depth for prompts that don't set their own "
            "(low|medium|high|xhigh|max). Lower is cheaper and terser."
        ),
    ),
    budget_usd: Optional[float] = typer.Option(
        None,
        "--budget-usd",
        help=(
            "Hard per-prompt spend cap passed to `claude --max-budget-usd`. "
            "Stops a runaway prompt at a known number instead of after the fact."
        ),
    ),
    stable_prefix: bool = typer.Option(
        True,
        "--stable-prefix/--no-stable-prefix",
        help=(
            "Move per-machine system-prompt sections (cwd, env, git status) into "
            "the first user message so the cached prompt prefix stays identical "
            "between blocks. Git status changes every time a prompt writes a "
            "file, which otherwise invalidates the cache for every block after it."
        ),
    ),
) -> None:
    """Execute all queued prompts sequentially via the Claude Code CLI.

    If nothing has been queued directly (via `queue add`), this loads
    `====`-delimited blocks from the default queue source
    (prompt_tools/future_prompts.md, or whatever `queue import` last set) and
    runs those instead — cutting each one into its sibling prompt_history.md
    as it completes, same as the manual "future prompts" workflow.

    By default, a quota hit doesn't stop the batch for good — it waits until
    the subscription's usage window is expected to refresh (or every
    --poll-interval seconds if no exact time is known) and automatically
    retries, until the queue is empty or something other than a quota limit
    goes wrong. Pass --once for the old single-pass behavior.
    """
    qm = QueueManager()
    prompts = qm.get_all_prompts()

    source_file = None
    if not prompts:
        source_file = _get_queue_source(qm.storage)
        imported = qm.import_from_markdown(source_file, use_tiers=tiers)
        if imported:
            console.print(f"[dim]Loaded {imported} prompt(s) from {source_file}[/dim]")
            prompts = qm.get_all_prompts()

    if not prompts:
        note = f" [dim](checked {source_file})[/dim]" if source_file else ""
        console.print(f"No prompts to execute{note}")
        return

    if dry_run:
        client = ClaudeClient()
        console.print(f"[bold]DRY RUN:[/bold] would execute {len(prompts)} prompt(s):")
        brief_text = project_docs.build_brief() if brief else None
        for i, p in enumerate(prompts, start=1):
            est = client.estimate_tokens(p.text)
            notes = [f"~{est:,} tokens"]
            if p.model:
                notes.append(f"model: {p.model}")
            if p.effort or effort:
                notes.append(f"effort: {p.effort or effort}")
            console.print(f"  [{i}] {p.title}  [dim]({', '.join(notes)})[/dim]")
        if brief_text:
            scope = (
                "the first" if session_mode == SESSION_MODE_CHAIN else "each"
            )
            console.print(
                f"[dim]+ a ~{client.estimate_tokens(brief_text):,}-token project-state "
                f"brief prepended to {scope}, replacing a full work_log read.[/dim]"
            )
        if session_mode == SESSION_MODE_CHAIN and len(prompts) > 1:
            console.print(
                f"[dim]Blocks 2-{len(prompts)} reuse one warm session — only block 1 "
                "pays for the project's context.[/dim]"
            )
        console.print("[dim]No prompts were sent. Run without --dry-run to execute.[/dim]")
        return

    client = ClaudeClient(
        permission_mode=permission_mode,
        effort=effort,
        budget_usd=budget_usd,
        stable_prefix=stable_prefix,
    )
    if not client.has_cli:
        console.print("[red]Error: the 'claude' CLI was not found on PATH.[/red]")
        console.print(
            "Install Claude Code (https://claude.com/claude-code), then run "
            "[cyan]claude auth login[/cyan] to sign in with your Pro/Max subscription."
        )
        raise typer.Exit(code=1)

    # Loaded before the auth notice so that notice describes the credential
    # the run will actually bill, not whichever account `claude` happens to be
    # logged into on this machine.
    pool = _load_account_pool(accounts_mode)
    if pool is not None and pool.current is not None:
        _verify_account_pool(pool)
        client.auth_token = pool.current.token
        console.print(
            f"[dim]Starting on '{pool.current_name}'; the run moves to the next "
            "account when a quota runs out.[/dim]"
        )

    _print_auth_notice(client)

    if permission_mode == "bypassPermissions":
        console.print(
            "[yellow]⚠ permission-mode is bypassPermissions (the default): queued prompts "
            "get full tool access — file writes, shell commands — with no confirmation "
            "step. Only queue prompts you trust.[/yellow]"
        )
    elif permission_mode != "default":
        console.print(
            f"[yellow]⚠ Running with --permission-mode {permission_mode}: queued prompts "
            "get tool access without asking. Only run prompts you trust.[/yellow]"
        )

    if not require_artifact:
        console.print(
            "[yellow]⚠ --no-require-artifact: a block will be cut from its source file "
            "even if the prompt changes nothing on disk.[/yellow]"
        )

    if session_mode not in SESSION_MODES:
        console.print(
            f"[red]Error: --session-mode must be one of {', '.join(SESSION_MODES)}.[/red]"
        )
        raise typer.Exit(code=1)

    if on_refusal not in ON_REFUSAL_MODES:
        console.print(
            f"[red]Error: --on-refusal must be one of {', '.join(ON_REFUSAL_MODES)}.[/red]"
        )
        raise typer.Exit(code=1)

    if session_mode == SESSION_MODE_CHAIN:
        console.print(
            "[dim]Chained session: the queue runs as one warm conversation, so only "
            "the first block pays for the project's context (--session-mode isolate "
            "to make every block a cold start).[/dim]"
        )
    if brief and project_docs.uses_convention():
        console.print(
            "[dim]Prepending a project-state brief to the first prompt "
            "(--no-brief to let each block read the full work_log itself).[/dim]"
        )

    registry = _load_provider_registry()
    routed = [p for p in qm.load_queue() if p.provider]
    if routed and external:
        vendors = ", ".join(sorted({p.provider for p in routed if p.provider}))
        # Said before the run rather than in the log afterwards: this is the
        # point where code leaves Anthropic for another vendor, and it is the
        # last moment Ctrl-C is still free.
        console.print(
            f"[yellow]↗ {len(routed)} block(s) are marked for {vendors}. Their "
            "prompts, the project brief and any files they read will be sent "
            "there, under that vendor's retention policy — not Anthropic's. "
            "--no-external keeps everything on Claude.[/yellow]"
        )
    elif routed:
        console.print(
            f"[dim]--no-external: {len(routed)} externally-marked block(s) will "
            "run on Anthropic.[/dim]"
        )

    engine = ExecutionEngine(
        qm, client,
        require_artifact=require_artifact,
        skip_executed=skip_executed,
        on_refusal=on_refusal,
        session_mode=session_mode,
        brief=brief,
        account_pool=pool,
        provider_registry=registry,
        allow_external=external,
        tiers=tiers,
    )
    _accept_break_as_interrupt()
    try:
        if once:
            asyncio.run(engine.execute_queue())
        else:
            asyncio.run(engine.run_with_auto_resume(poll_interval=poll_interval))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. Unexecuted prompts remain in the queue.[/yellow]")
        engine.record_stop(run_report.REASON_INTERRUPTED)
        raise typer.Exit(code=130)
    except Exception as exc:  # noqa: BLE001 — record it, then let it surface as usual
        # A crash is the case where "why did it stop" is hardest to answer
        # later and most worth having written down.
        engine.record_stop(run_report.REASON_CRASHED, f"{type(exc).__name__}: {exc}")
        raise


def _accept_break_as_interrupt() -> None:
    """Make a Windows CTRL_BREAK behave exactly like Ctrl-C.

    The control panel's Stop button interrupts a run by sending CTRL_BREAK_EVENT
    to its process group -- on Windows that is the only console signal one
    process can send another without also hitting itself, which is why the child
    is started in a group of its own.

    Python's *default* action for SIGBREAK, though, is to terminate outright.
    That would skip the `KeyboardInterrupt` handler in `execute` and with it the
    stop-reason bookkeeping and the "unexecuted prompts remain in the queue"
    guarantee -- so a stopped run would look, to the next one, like a crash.
    Mapping it to `KeyboardInterrupt` is what makes the button honest.

    No-op everywhere else: POSIX has no SIGBREAK and gets a real SIGINT.
    """
    if os.name != "nt" or not hasattr(signal, "SIGBREAK"):
        return

    def _raise(_signum, _frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGBREAK, _raise)
    except (ValueError, OSError) as exc:
        # Not the main thread, or no console attached. Worth saying, never
        # worth refusing to run over.
        logger.debug("Could not install a SIGBREAK handler: %s", exc)


def _load_account_pool(mode: str) -> "AccountPool | None":
    """Resolve `--accounts` into a pool, or None for single-account running.

    'auto' is the default because it is the only setting that is correct for
    both kinds of user: someone with no accounts file gets exactly the old
    behaviour with nothing to configure, and someone who has written one gets
    rotation without having to remember a flag on every run.
    """
    mode = (mode or "auto").lower()
    if mode not in ("auto", "rotate", "off"):
        console.print("[red]Error: --accounts must be one of auto, rotate, off.[/red]")
        raise typer.Exit(code=1)
    if mode == "off":
        return None

    try:
        pool = AccountPool.load()
    except AccountsError as exc:
        # Never silently degrade to single-account: a typo in the accounts
        # file would otherwise surface hours later as an unexplained quota
        # wait, which is the hardest possible way to notice it.
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    if pool is None:
        if mode == "rotate":
            console.print(
                f"[red]Error: --accounts rotate, but no accounts file at "
                f"{AccountPool.default_path()}.[/red]"
            )
            console.print(
                '[dim]Create it as {"accounts": [{"name": "personal", "token": "..."}]} '
                "— mint each token with [cyan]claude setup-token[/cyan].[/dim]"
            )
            raise typer.Exit(code=1)
        return None

    # A run quietly using two of three accounts looks identical to one that had
    # two all along -- right up until it waits for quota hours earlier than
    # expected and nothing on screen explains why.
    off = [a.name for a in pool.accounts if not a.enabled]
    if off:
        console.print(
            f"[dim]{len(off)} account(s) disabled and will be skipped: "
            f"{', '.join(off)} — re-enable with "
            f"[cyan]sandglass accounts --enable <name>[/cyan].[/dim]"
        )
    return pool


def _verify_account_pool(pool: AccountPool) -> bool:
    """Catch tokens that are obviously not tokens. Returns False if any are.

    This is all that can be checked for free: `claude auth status` reports
    `loggedIn: true` for the literal string "x", so it cannot tell a good
    token from a garbage one and must not be used to imply otherwise. Real
    validation costs a live request — `sandglass accounts --probe`.
    """
    ok = True
    seen: dict[str, str] = {}
    for account in pool.accounts:
        problem = accounts_mod.looks_malformed(account.token)
        if problem:
            ok = False
            console.print(
                f"[red]✖ {account.name}: {problem}.[/red] "
                "[dim]Re-mint with [cyan]sandglass setup-token[/cyan].[/dim]"
            )
            continue
        # Same token under two names makes the pool look like it has capacity
        # it doesn't; rotating onto a duplicate buys nothing.
        if account.token in seen:
            ok = False
            console.print(
                f"[yellow]⚠ '{seen[account.token]}' and '{account.name}' are the "
                "same token — rotating between them gains you no quota.[/yellow]"
            )
        else:
            seen[account.token] = account.name
    return ok


def _probe_account(client: ClaudeClient, account) -> tuple[bool, str]:
    """Send one minimal live request under `account`'s token.

    Run from an empty directory so the project's CLAUDE.md is not
    auto-discovered — that context alone has measured ~23,000 billed tokens
    in this repo, which would make checking three tokens cost more than the
    work it protects.
    """
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as empty_dir:
        try:
            result = subprocess.run(
                accounts_mod.probe_command(client._cli_path),
                capture_output=True, text=True, timeout=180, check=False,
                cwd=empty_dir,
                env=accounts_mod.subprocess_env(account.token),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f"probe failed to run: {exc}"

    if result.returncode == 0:
        return True, (result.stdout or "").strip().splitlines()[-1][:60] if result.stdout else "ok"
    message = (result.stderr or result.stdout or "no output").strip()
    return False, message.splitlines()[0][:160] if message else "unknown error"


def _print_auth_notice(client: ClaudeClient) -> None:
    """Warn the user if execution won't draw on a Pro/Max subscription quota."""
    status = client.get_auth_status()
    if status is None:
        console.print("[dim]Could not verify claude auth status — proceeding anyway.[/dim]")
        return
    if not status.get("loggedIn"):
        console.print(
            "[yellow]⚠ claude is not logged in. Run [cyan]claude auth login[/cyan] to use "
            "your Pro/Max subscription quota instead of API credits.[/yellow]"
        )
        return
    if status.get("authMethod") == "claude.ai":
        console.print(
            f"[dim]Using {status.get('email', 'your account')} — "
            f"{status.get('subscriptionType', 'unknown')} plan (subscription quota).[/dim]"
        )
    else:
        console.print(
            f"[yellow]⚠ claude is authenticated via {status.get('authMethod', 'unknown')}, "
            "not a claude.ai subscription — this run may bill API credits.[/yellow]"
        )


@app.command("setup-token")
def setup_token_cmd() -> None:
    """Mint a long-lived subscription token for the account you're signed in as.

    A passthrough to `claude setup-token` — the token is Claude Code's to
    issue, not Sandglass's. It exists because every other step of setting up
    an account pool is a `sandglass` command, and having exactly one of them
    be a `claude` command is a reliable way to lose a minute.
    """
    client = ClaudeClient()
    if not client.has_cli:
        console.print("[red]Error: the 'claude' CLI was not found on PATH.[/red]")
        raise typer.Exit(code=1)

    console.print(
        "[dim]Running `claude setup-token` — this mints a token for whichever "
        "account you are currently signed in as. Copy it into "
        f"{AccountPool.default_path()}, then run [cyan]sandglass accounts[/cyan] "
        "to check it.[/dim]"
    )
    # Inherit stdio: the browser handshake is interactive and the token is
    # printed for the user to copy, so neither may be captured.
    import subprocess

    raise typer.Exit(code=subprocess.run([client._cli_path, "setup-token"]).returncode)


@app.command()
def accounts(
    enable: Optional[str] = typer.Option(
        None,
        "--enable",
        metavar="NAME",
        help="Put an account back into the rotation, then show the pool.",
    ),
    disable: Optional[str] = typer.Option(
        None,
        "--disable",
        metavar="NAME",
        help=(
            "Take an account out of the rotation until you say otherwise. For "
            "an account whose weekly quota is gone: the pool then skips it "
            "outright instead of spending a block rediscovering it is spent. "
            "Refuses to disable the last enabled account."
        ),
    ),
    probe: bool = typer.Option(
        False,
        "--probe",
        help=(
            "Actually use each token, with one tiny request per account. This "
            "is the only way to tell a working token from an expired one — "
            "`claude auth status` reports success for any non-empty string. "
            "Costs a few tokens per account; run it once after setup."
        ),
    ),
) -> None:
    """List the pooled Claude subscriptions and show which quota is spent.

    Free by default, which also means it cannot confirm a token actually
    works — pass --probe for that.

    --enable/--disable edit the accounts file and then print the pool, so the
    command that made the change is also the one that shows the result.
    """
    if enable and disable:
        console.print("[red]Error: pass --enable or --disable, not both.[/red]")
        raise typer.Exit(code=1)

    if enable or disable:
        wanted = bool(enable)
        target = enable or disable
        try:
            changed = accounts_mod.set_enabled(target, wanted)
        except AccountsError as exc:
            console.print(f"[red]Error: {exc}[/red]")
            raise typer.Exit(code=1) from exc
        state = "enabled" if wanted else "disabled"
        if changed:
            console.print(f"[green]✔[/green] {target} is now [bold]{state}[/bold].")
        else:
            console.print(f"[dim]{target} was already {state}; nothing to do.[/dim]")
        console.print()

    try:
        pool = AccountPool.load()
    except AccountsError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    path = AccountPool.default_path()
    if pool is None:
        console.print(f"[yellow]No accounts file at {path}.[/yellow]")
        console.print(
            "[dim]Mint a token per account with [cyan]claude setup-token[/cyan] and "
            'write {"accounts": [{"name": "...", "token": "..."}]} there. Until then '
            "runs use whichever account `claude` is logged into.[/dim]"
        )
        return

    client = ClaudeClient()
    if not client.has_cli:
        console.print("[red]Error: the 'claude' CLI was not found on PATH.[/red]")
        raise typer.Exit(code=1)

    # Show recorded exhaustion alongside the identity check, so one command
    # answers both "are these tokens good" and "which of them can run now".
    pool.state_path = StorageService().accounts_state_path
    pool.load_state()

    console.print(f"[bold]Accounts[/bold] [dim]({path})[/dim]")
    _verify_account_pool(pool)

    for account in pool.accounts:
        if not account.enabled:
            # Not rendered as a problem: it is a state somebody chose.
            state = "[dim]○ disabled — skipped by every run[/dim]"
        elif account.is_available():
            state = "[green]●[/green] quota available"
        else:
            # Local wall-clock, not an ISO string: this line answers "how long
            # until it can run again", and 14:35 answers that at a glance.
            back = datetime.fromtimestamp(account.exhausted_until).strftime("%H:%M")
            state = f"[yellow]○[/yellow] spent, expected back around {back}"
        console.print(f"  {state} — {account.name}")

    if not probe:
        console.print(
            "\n[dim]Tokens are not verified: `claude auth status` accepts any "
            "non-empty string, so only a live request can tell a working token "
            "from an expired one. Run [cyan]sandglass accounts --probe[/cyan] to "
            "check them for real (costs a few tokens per account).[/dim]"
        )
        console.print(
            "[dim]Park an account you don't want used for a while with "
            "[cyan]sandglass accounts --disable <name>[/cyan]; bring it back "
            "with [cyan]--enable <name>[/cyan].[/dim]"
        )
        return

    console.print("\n[bold]Probing each token with one small request…[/bold]")
    failures = 0
    for account in pool.accounts:
        if not account.enabled:
            # A probe costs real tokens on the account being probed. Spending
            # them on one the pool was told to skip is pure waste.
            console.print(f"  [dim]–[/dim] {account.name}: disabled, not probed")
            continue
        ok, detail = _probe_account(client, account)
        if ok:
            console.print(f"  [green]✔[/green] {account.name}: works")
        else:
            failures += 1
            console.print(f"  [red]✖[/red] {account.name}: {detail}")
    if failures:
        console.print(
            f"\n[yellow]{failures} account(s) failed. Re-mint with "
            "[cyan]sandglass setup-token[/cyan] while signed in as that "
            "account, then paste the new token into the file above.[/yellow]"
        )
    else:
        console.print("\n[green]All accounts working.[/green]")


def _load_provider_registry() -> "providers_mod.ProviderRegistry | None":
    """Read the external-provider keys, or stop the run if the file is broken.

    A missing file is normal and means "no block can route externally". A file
    that exists but is malformed is an error for the same reason it is in the
    account pool: a typo would otherwise surface hours later as a block
    quietly running on Claude when it was meant to run somewhere cheap.
    """
    try:
        return providers_mod.ProviderRegistry.load()
    except providers_mod.ProvidersError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1) from exc


providers_app = typer.Typer(help="External (non-Anthropic) model providers.")
app.add_typer(providers_app, name="providers")


@providers_app.command("list")
def providers_list() -> None:
    """Show which external providers have a usable API key on this machine."""
    registry = _load_provider_registry()
    path = providers_mod.ProviderRegistry.default_path()
    console.print(f"[bold]Providers[/bold] [dim]({path})[/dim]")

    for name, provider in sorted(providers_mod.PROVIDERS.items()):
        key = registry.key_for(name) if registry else None
        count = registry.key_count(name) if registry else 0
        if key:
            problem = providers_mod.looks_malformed(key)
            # The count matters operationally: with one key, a vendor running
            # out of credit mid-run puts every block marked for it back on
            # Claude quota. With two, the run just moves to the second.
            configured = f"{count} keys configured" if count > 1 else "key configured"
            mark = (
                f"[green]●[/green] {configured}" if not problem
                else f"[yellow]○[/yellow] key looks wrong: {problem}"
            )
        else:
            mark = "[dim]○ no key[/dim]"
        console.print(f"  {mark} — {name}  [dim]{provider.base_url}[/dim]")
        console.print(
            f"      [dim]tiers: {', '.join(sorted(set(provider.tiers.values())))} "
            f"· env fallback: {provider.key_env} · {provider.docs_url}[/dim]"
        )

    console.print(
        "\n[dim]A block reaches one of these only by asking: put "
        "[cyan]**CLINE: pro**[/cyan] (or [cyan]provider: deepseek[/cyan]) near the "
        "top of the block. Unmarked blocks always run on Anthropic.[/dim]"
    )
    console.print(
        "[dim]Out of credit mid-run? The run moves to that vendor's next key "
        "([cyan]sandglass providers set <name> --add[/cyan]), then falls back to "
        "Claude — and only waits if Claude has no quota either.[/dim]"
    )


def _stored_keys(entry: object) -> list[str]:
    """The keys a providers-file entry already holds, in the order written."""
    if isinstance(entry, dict):
        raw = entry.get("api_keys", entry.get("api_key"))
    else:
        raw = entry
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [k for k in raw if isinstance(k, str) and k.strip()]
    return []


@providers_app.command("set")
def providers_set(
    name: str = typer.Argument(..., help="Provider name, e.g. deepseek."),
    api_key: str = typer.Option(
        ..., "--key", prompt=True, hide_input=True,
        help="The provider's API key. Prompted for (hidden) if not passed.",
    ),
    add: bool = typer.Option(
        False, "--add",
        help=(
            "Keep the keys already stored and add this one after them. A run "
            "that exhausts one key's balance moves to the next instead of "
            "falling back to Claude quota."
        ),
    ),
) -> None:
    """Store an API key for an external provider, outside the project tree.

    Written to ~/.sandglass/providers.json for the same reason account tokens
    are: `sandglass execute` runs blocks with `bypassPermissions` inside the
    project directory and persists their output verbatim, so a key kept in the
    repo is one incurious `cat` away from a response file that outlives the run.
    """
    provider = providers_mod.get(name)
    if provider is None:
        console.print(
            f"[red]Error: unknown provider {name!r}. Known: "
            f"{', '.join(sorted(providers_mod.PROVIDERS))}.[/red]"
        )
        raise typer.Exit(code=1)

    problem = providers_mod.looks_malformed(api_key)
    if problem:
        # Format only — a live check would cost a request, and the key will
        # announce itself the first time a block actually uses it.
        console.print(f"[yellow]⚠ That key {problem}. Storing it anyway.[/yellow]")

    path = providers_mod.ProviderRegistry.default_path()
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            console.print(f"[red]Error: could not read {path}: {exc}[/red]")
            raise typer.Exit(code=1) from exc
    if not isinstance(existing, dict):
        existing = {}
    # Preserve whichever shape the file is already in, so this never silently
    # rewrites a hand-authored file into the other one.
    target = existing.setdefault("providers", {}) if "providers" in existing else existing
    if add:
        kept = _stored_keys(target.get(provider.name))
        if api_key in kept:
            console.print(f"[yellow]That key is already stored for '{provider.name}'.[/yellow]")
            raise typer.Exit(code=0)
        target[provider.name] = {"api_keys": kept + [api_key]}
    else:
        target[provider.name] = {"api_key": api_key}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
    stored = len(_stored_keys(target.get(provider.name)))
    console.print(
        f"[green]✓ Key stored for '{provider.name}' in {path}"
        + (f" ({stored} keys total)" if stored > 1 else "")
        + "[/green]"
    )
    console.print(
        f"[dim]Blocks marked [cyan]**CLINE: pro**[/cyan] now run on "
        f"{provider.tiers['pro']} instead of consuming Claude quota.[/dim]"
    )


@app.command()
def why() -> None:
    """Explain why the last `sandglass execute` stopped (or what it's doing now).

    A queue run is unattended by design, so the moment it stops is almost never
    a moment anyone is watching. This reads the record the run leaves behind, so
    the answer survives the terminal scrolling, closing, or the machine sleeping.
    """
    storage = StorageService()
    report = run_report.load(storage)
    if report is None:
        console.print(
            "[dim]No run recorded yet in this project. Run `sandglass execute` "
            "first — every run writes its own state to "
            f"{storage.last_run_path}.[/dim]"
        )
        return
    for line in run_report.render(report):
        console.print(line)
    if report.prompt_id:
        console.print(
            f"  [dim]Last prompt: {report.prompt_id} · {report.completed} done "
            f"this run · {report.total_tokens:,} tokens · "
            f"${report.total_cost_usd:.2f}[/dim]"
        )


@app.command()
def dashboard(
    source: str = typer.Option(
        DEFAULT_QUEUE_SOURCE, "--source", help="Markdown queue source to report on."
    ),
    no_open: bool = typer.Option(
        False, "--no-open", help="Write the file but don't open it in a browser."
    ),
) -> None:
    """Write a visual status page (progress, phases, run state) and open it.

    Static HTML, regenerated after every completed block during `sandglass
    execute` -- leave the tab open and it keeps catching up on its own.
    """
    title = os.path.basename(os.path.abspath(os.getcwd())) or "Sandglass"
    path = dashboard_mod.write(source, title)
    console.print(f"[green]Dashboard written to {path}[/green]")
    console.print(
        "[dim]That page is a display — a file:// page can't start anything. For "
        "Run/Stop buttons, use [cyan]sandglass ui[/cyan].[/dim]"
    )
    if not no_open:
        dashboard_mod.open_in_browser(path)


@app.command()
def ui(
    source: str = typer.Option(
        DEFAULT_QUEUE_SOURCE, "--source", help="Markdown queue source to report on."
    ),
    port: int = typer.Option(
        0, "--port", help="Port to listen on. 0 (default) lets the OS pick a free one."
    ),
    no_open: bool = typer.Option(
        False, "--no-open", help="Print the URL but don't launch a browser."
    ),
) -> None:
    """Serve the dashboard with working buttons: run, stop, park an account.

    The same page `sandglass dashboard` writes, plus controls -- because a page
    opened from a file cannot start a process, so buttons there would be
    decoration. Run is not a re-implementation of anything: it spawns
    `sandglass execute` exactly as you would have typed it, and streams the
    output into the page. Stop sends the same interrupt Ctrl-C does, so the
    queue is left intact.

    Runs in the foreground on 127.0.0.1 with a key in the URL, and takes any run
    it started with it when you Ctrl-C. Nothing is left listening afterwards.
    """
    from . import webui

    title = os.path.basename(os.path.abspath(os.getcwd())) or "Sandglass"
    webui.serve(source, title, port=port, open_browser=not no_open)


@app.command()
def update(
    check_only: bool = typer.Option(
        False, "--check", help="Report whether an update is available; change nothing."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Attempt the pull even with uncommitted changes in the source tree. "
            "Never discards anything — git still refuses on conflict."
        ),
    ),
) -> None:
    """Update Sandglass itself from its git repository.

    Works out how this machine got Sandglass and does the right thing for that
    shape: an editable clone is pulled (the code is live, nothing to
    reinstall), a copy install is reinstalled from the remote with pipx, uv or
    pip as appropriate.

    Never resolves `sandglass` from PyPI — that name belongs to an unrelated
    project there, and installing it would replace this tool with something
    else entirely.
    """
    install = updater.detect_install()
    console.print(f"[dim]Installed as: {install.describe()}[/dim]")

    try:
        status = updater.check(install)
    except updater.UpdateError as exc:
        console.print(f"[red]✖ {exc}[/red]")
        raise typer.Exit(code=1)

    if status.detail:
        console.print(f"[yellow]{status.detail}[/yellow]")
    elif status.up_to_date:
        console.print(
            f"[green]✓ Already up to date[/green] "
            f"[dim]({status.branch} @ {status.current})[/dim]"
        )
        if status.ahead:
            console.print(
                f"[dim]{status.ahead} local commit(s) not pushed to the remote.[/dim]"
            )
        return
    else:
        console.print(
            f"[bold]{status.behind} new commit(s)[/bold] on {status.branch} "
            f"[dim]({status.current} → {status.latest})[/dim]"
        )
        for line in updater.pending_commits(install):
            console.print(f"    [dim]{line}[/dim]")

    if status.dirty_files:
        console.print(
            f"[yellow]⚠ {status.dirty_files} uncommitted change(s) in the source "
            "tree — commit or stash before updating.[/yellow]"
        )

    if check_only:
        return

    if not yes and not typer.confirm("Update now?"):
        console.print("Cancelled")
        raise typer.Exit(code=0)

    try:
        console.print(updater.update(install, force=force))
    except updater.UpdateError as exc:
        console.print(f"[red]✖ {exc}[/red]")
        raise typer.Exit(code=1)
    console.print(
        "[dim]Run `sandglass commands` to confirm the new version's commands "
        "are present.[/dim]"
    )


@app.command("rotate-logs")
def rotate_logs_cmd(
    keep: int = typer.Option(
        project_docs.DEFAULT_KEEP_ENTRIES,
        "--keep",
        "-k",
        help="How many of the most recent entries stay in the live file.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Archive old work_log.md / prompt_history.md entries into master_plan/archive/.

    These files are append-only, and a project's CLAUDE.md usually tells every
    agent to read the work log before starting. That is affordable once and
    ruinous per block: `sandglass execute` runs each block cold, so the whole
    file is re-read at full price every time, and it only ever grows.

    Rotating moves the older entries one directory over and leaves a pointer
    line behind. Nothing is deleted — history stays readable, it just stops
    being paid for on every single run.
    """
    if not project_docs.uses_convention():
        console.print(
            f"[yellow]No {project_docs.MASTER_PLAN_DIR}/ directory here — nothing to "
            "rotate. Run this from a project that keeps one.[/yellow]"
        )
        raise typer.Exit(code=1)

    targets = [
        project_docs.master_plan_path(project_docs.WORK_LOG_NAME),
        os.path.join("prompt_tools", "prompt_history.md"),
        os.path.join("prompt_tools", "history_prompts.md"),
    ]
    targets = [t for t in targets if os.path.exists(t)]
    if not targets:
        console.print("Nothing to rotate — no work_log.md or prompt history found.")
        return

    if not yes:
        console.print(f"Will keep the last {keep} entries in: {', '.join(targets)}")
        if not typer.confirm("Archive everything older?"):
            console.print("Cancelled")
            raise typer.Exit(code=0)

    rotated_any = False
    for path in targets:
        try:
            result = project_docs.rotate_log(path, keep=keep)
        except OSError as exc:
            console.print(f"[red]Could not rotate {path}: {exc}[/red]")
            continue
        if result is None:
            console.print(f"[dim]{path}: already at or below {keep} entries.[/dim]")
            continue
        moved, archive_path = result
        rotated_any = True
        console.print(f"[green]✓ {path}: archived {moved} entr(ies) → {archive_path}[/green]")

    if rotated_any:
        console.print(
            "[dim]Commit the archive alongside the trimmed file — the history is "
            "moved, not deleted.[/dim]"
        )


@queue_app.command("lint")
def queue_lint() -> None:
    """Check queued blocks for problems that would waste a run.

    Static checks only — no model call, no cost. It catches the cheap-to-detect
    failures: a block that references a file which doesn't exist, an empty
    block, an unknown model or effort value. A block that refuses because its
    dependency is missing still burns a full prompt's worth of quota and
    produces nothing, so it is worth a second of grep first.
    """
    _lint_agent_mirror()

    qm = QueueManager()
    prompts = qm.get_all_prompts()
    source_file = None
    if not prompts:
        source_file = _get_queue_source(qm.storage)
        if qm.import_from_markdown(source_file):
            prompts = qm.get_all_prompts()
            # Linting must not consume the queue: it is a read-only check, and
            # a run started right after should still see every block.
            qm.clear_queue()

    if not prompts:
        console.print("Nothing queued to lint.")
        return

    valid_efforts = {"low", "medium", "high", "xhigh", "max"}
    findings = 0
    for i, p in enumerate(prompts, start=1):
        issues: list[str] = []
        if not p.text.strip():
            issues.append("block is empty")
        elif not _COMMENT_RE.sub("", p.text).strip():
            # Seen in a real queue: a trailing `<!-- END OF QUEUE -->` marker
            # was imported as prompt #34 and would have been sent to Claude as
            # a task. Not empty, so the check above misses it.
            issues.append(
                "block is only an HTML comment — it will still be sent as a prompt"
            )
        if p.effort and p.effort.lower() not in valid_efforts:
            issues.append(
                f"effort '{p.effort}' is not one of {'|'.join(sorted(valid_efforts))}"
            )
        for path in _referenced_paths(p.text):
            if not os.path.exists(path):
                issues.append(f"references '{path}', which does not exist")
        if issues:
            findings += len(issues)
            console.print(f"[yellow]![/yellow] [{i}] {p.title}")
            for issue in issues:
                console.print(f"      [dim]{issue}[/dim]")

    if findings:
        console.print(
            f"\n[yellow]{findings} issue(s) across {len(prompts)} block(s).[/yellow] "
            "[dim]A missing path may be intentional (the block might create it) — "
            "this is a warning, not a verdict.[/dim]"
        )
    else:
        console.print(f"[green]✓ {len(prompts)} block(s), no issues found.[/green]")
    if source_file:
        console.print(f"[dim]Linted from {source_file}; queue left untouched.[/dim]")


def _lint_agent_mirror() -> None:
    """Warn when `CLAUDE.md` and `AGENTS.md` have drifted apart.

    They are meant to be one document under two names, because Claude Code reads
    only the first and everything on the AGENTS.md convention reads only the
    second. A rule that reached one of them is a rule half the agents working in
    this repo have never seen -- and a drifted pair looks completely normal until
    a block ignores a convention nobody ever told it about.

    Checked here rather than in a git hook because this is the command people
    already run before an unattended batch, which is exactly when a non-Claude
    block is about to read whichever file is behind.
    """
    claude_md, agents_md = "CLAUDE.md", "AGENTS.md"
    if not os.path.exists(claude_md):
        return

    if not os.path.exists(agents_md):
        console.print(
            f"[yellow]![/yellow] {agents_md} is missing — agents that read it "
            "(OpenCode/Grok, Codex) start with no project rules at all."
        )
        console.print(f"      [dim]Fix: copy {claude_md} to {agents_md}.[/dim]")
        return

    def _body(path: str) -> str:
        # Compared with line endings normalised: git may check one out CRLF and
        # the other LF depending on when each was written, and that difference
        # is not drift anybody needs to be told about.
        with open(path, encoding="utf-8") as fh:
            return fh.read().replace('\r\n', '\n')

    try:
        drifted = _body(claude_md) != _body(agents_md)
    except OSError as exc:
        logger.warning("Could not compare %s and %s: %s", claude_md, agents_md, exc)
        return

    if drifted:
        console.print(
            f"[yellow]![/yellow] {claude_md} and {agents_md} have drifted apart; "
            "they are meant to be identical."
        )
        console.print(
            "      [dim]Diff them, then copy whichever is current over the "
            "other.[/dim]"
        )


# Backtick-quoted tokens that look like repo-relative paths: a slash or a dot
# extension, no spaces, no URL scheme. Deliberately conservative — a false
# "missing file" warning trains people to ignore the linter.
_PATH_RE = re.compile(r"`([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,5}|[A-Za-z0-9_./\\-]+/)`")
_COMMENT_RE = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)


def _referenced_paths(text: str) -> list[str]:
    """Repo-relative paths a prompt block names in backticks."""
    seen: list[str] = []
    for match in _PATH_RE.finditer(text):
        candidate = match.group(1)
        if "://" in candidate or candidate.startswith(("http", "www.")):
            continue
        if candidate not in seen:
            seen.append(candidate)
    return seen


# --- history & responses -----------------------------------------------------


@app.command()
def history() -> None:
    """Show completed prompts (archive), with what each one actually cost."""
    storage = StorageService()
    data = storage.load_json(storage.history_path)
    completed = data.get("completed", []) if isinstance(data, dict) else []

    if not completed:
        console.print("No completed prompts yet")
        return

    table = Table(title=f"History ({len(completed)})")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Title")
    table.add_column("Tokens", justify="right", style="magenta")
    table.add_column("Cached", justify="right", style="green")
    table.add_column("Cost", justify="right", style="yellow")
    table.add_column("Completed At", style="dim")

    stale = 0
    total_tokens = total_cost = 0
    for i, entry in enumerate(completed, start=1):
        title = entry.get("prompt", {}).get("title", "")
        tokens = entry.get("tokens_used", 0)
        usage = entry.get("usage") or {}
        cost = entry.get("cost_usd")
        legacy = entry.get("accounting", 1) < ACCOUNTING_SCHEMA
        if legacy:
            stale += 1
        else:
            total_tokens += tokens
            total_cost += cost or 0.0
        table.add_row(
            str(i),
            title,
            f"{tokens:,}" + ("*" if legacy else ""),
            f"{usage.get('cache_read_tokens', 0):,}" if usage else "—",
            f"${cost:.2f}" if cost is not None else "—",
            entry.get("completed_at", ""),
        )

    console.print(table)
    if total_tokens or total_cost:
        console.print(
            f"[dim]Fully accounted: {total_tokens:,} tokens, ${total_cost:.2f}[/dim]"
        )
    if stale:
        # Not a cosmetic caveat: pre-schema-2 rows summed input+output only and
        # omitted cache tokens, so on a cached run they can be off by orders of
        # magnitude. Averaging them together with new rows would be misleading.
        console.print(
            f"[dim]* {stale} row(s) predate full-usage accounting — they counted "
            "only uncached input + output, so they are undercounts and are "
            "excluded from the totals above.[/dim]"
        )


@responses_app.command("list")
def responses_list() -> None:
    """List saved responses."""
    storage = StorageService()
    storage.ensure_dir(storage.responses_dir)
    files = sorted(
        f for f in os.listdir(storage.responses_dir) if f.startswith("response_") and f.endswith(".json")
    )

    if not files:
        console.print("No responses saved yet")
        return

    table = Table(title=f"Saved Responses ({len(files)})")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("File")
    table.add_column("Tokens", justify="right", style="magenta")
    table.add_column("Completed At", style="dim")

    for i, filename in enumerate(files, start=1):
        data = storage.load_json(os.path.join(storage.responses_dir, filename))
        tokens = data.get("tokens_used", 0)
        completed_at = data.get("completion_time", "")
        table.add_row(str(i), filename, f"{tokens:,}", completed_at)

    console.print(table)


@responses_app.command("show")
def responses_show(index: int = typer.Argument(..., help="1-based index from `responses list`.")) -> None:
    """View a specific saved response."""
    storage = StorageService()
    storage.ensure_dir(storage.responses_dir)
    files = sorted(
        f for f in os.listdir(storage.responses_dir) if f.startswith("response_") and f.endswith(".json")
    )

    if index < 1 or index > len(files):
        console.print(f"[red]Error: Index {index} out of range (1-{len(files)})[/red]")
        raise typer.Exit(code=1)

    data = storage.load_json(os.path.join(storage.responses_dir, files[index - 1]))
    console.print(f"[bold]Prompt ({data.get('prompt_id', '?')}):[/bold] {data.get('prompt_text', '')}")
    console.print(f"[bold]Model:[/bold] {data.get('model', '')}  [dim]({data.get('tokens_used', 0):,} tokens)[/dim]")
    console.print()
    console.print(data.get("response_text", ""))

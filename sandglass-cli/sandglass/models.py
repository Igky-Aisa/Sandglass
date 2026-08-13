"""Data models for Sandglass CLI."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


def _utc_now_iso() -> str:
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PromptObject:
    """A single prompt queued for execution."""

    id: str
    title: str
    text: str
    source: str = "text"  # "text" or "file"
    file_path: str | None = None
    added_at: str = field(default_factory=_utc_now_iso)
    model: str | None = None  # per-prompt model override; None = use the CLI default
    # Per-prompt reasoning depth (`claude --effort`). Lower effort means fewer,
    # more-consolidated tool calls and less preamble, so it is the cheapest
    # dial a prompt author has that doesn't change which model runs.
    effort: str | None = None
    # If set, this prompt was auto-loaded from a markdown queue source (e.g.
    # prompt_tools/future_prompts.md) rather than `queue add` — on successful
    # completion, the matching block is cut from this file into its history.
    origin_file: str | None = None
    # Claude Code session this prompt runs in, assigned on first execution and
    # persisted so a quota-interrupted prompt can `--resume` instead of
    # restarting from zero. See ExecutionEngine._resolve_session.
    session_id: str | None = None
    # Run this one block in its own throwaway session even when the rest of the
    # queue is chained. For work that must not inherit earlier context — a
    # second opinion, a review that shouldn't see the author's reasoning.
    isolate: bool = False
    # Send this block to a non-Anthropic provider (see providers.py). None —
    # the default and the only value a block gets without saying so explicitly
    # — means Anthropic. Routing externally sends the prompt and whatever files
    # the agent reads to a third party, so it is never inferred: a block leaves
    # Anthropic only when its author wrote a marker saying it may.
    provider: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PromptObject":
        return cls(
            id=data["id"],
            title=data.get("title", ""),
            text=data.get("text", ""),
            source=data.get("source", "text"),
            file_path=data.get("file_path"),
            added_at=data.get("added_at", _utc_now_iso()),
            model=data.get("model"),
            effort=data.get("effort"),
            origin_file=data.get("origin_file"),
            session_id=data.get("session_id"),
            isolate=bool(data.get("isolate", False)),
            provider=data.get("provider"),
        )


@dataclass
class Response:
    """Claude's response to a single prompt.

    ``tokens_used`` is every token the request was billed for, cache included.
    That distinction matters more than it looks: on a cached prompt the API's
    ``input_tokens`` is only the *uncached remainder*, so summing
    ``input_tokens + output_tokens`` (what Sandglass did before) can undercount
    a real run by two orders of magnitude — a measured do-nothing prompt in a
    project with a large CLAUDE.md reported 53 tokens that way against 23,788
    actually billed. The four component fields are kept separately because they
    price differently (cache reads ~0.1x base, 5-minute cache writes ~1.25x,
    1-hour cache writes ~2x), which makes their ratio — not the total — the
    number that tells you whether caching is working.
    """

    prompt_id: str
    text: str
    tokens_used: int = 0
    completion_time: str = field(default_factory=_utc_now_iso)
    model: str = ""
    # Usage breakdown, straight from the CLI's `result` event.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # What the run actually cost, as reported by `claude` (`total_cost_usd`).
    # Authoritative in a way a token count isn't: it already accounts for each
    # model's rates and the cache-tier multipliers.
    cost_usd: float = 0.0
    # The Claude Code session this ran in, as reported by the CLI. The caller
    # never names a session -- it reads the id back from here and resumes it
    # for the next prompt, so what gets recorded is always what actually
    # happened rather than what was intended.
    session_id: str | None = None
    # Which non-Anthropic provider served this response, if any. Matters for
    # reading `cost_usd`: the CLI prices every run off Anthropic's table, so on
    # an external block that figure is a rough stand-in, not a bill. Recorded
    # so the summary can say which numbers are which instead of silently
    # totalling two different currencies of estimate.
    provider: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Response":
        return cls(
            prompt_id=data.get("prompt_id", ""),
            text=data.get("text", ""),
            tokens_used=data.get("tokens_used", 0),
            completion_time=data.get("completion_time", _utc_now_iso()),
            model=data.get("model", ""),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cache_creation_tokens=data.get("cache_creation_tokens", 0),
            cache_read_tokens=data.get("cache_read_tokens", 0),
            cost_usd=data.get("cost_usd", 0.0),
            session_id=data.get("session_id"),
            provider=data.get("provider"),
        )


@dataclass
class ExecutionResult:
    """Aggregate result of executing the whole queue."""

    completed: int = 0
    failed: int = 0
    # Blocks dropped without being sent: some other runner already executed and
    # cut them. Counted apart from `completed` because nothing was spent.
    skipped: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    # Cache split across the whole run. Reported separately because the ratio
    # between them is the efficiency signal: reads are ~0.1x base price, writes
    # 1.25x-2x, so a run that keeps re-creating the same cache is paying a
    # premium for a prefix it never gets to reuse.
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_time: float = 0.0  # seconds
    responses: list[Response] = field(default_factory=list)
    # Why execute_queue() stopped before the whole queue was done, if it did:
    # None = fully completed, "quota" = usage limit hit, "error" = anything else.
    stopped_reason: str | None = None
    resume_at: str | None = None  # ISO timestamp the quota is expected to refresh, if known

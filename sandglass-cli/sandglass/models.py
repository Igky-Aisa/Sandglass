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
    # If set, this prompt was auto-loaded from a markdown queue source (e.g.
    # prompt_tools/future_prompts.md) rather than `queue add` — on successful
    # completion, the matching block is cut from this file into its history.
    origin_file: str | None = None

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
            origin_file=data.get("origin_file"),
        )


@dataclass
class Response:
    """Claude's response to a single prompt."""

    prompt_id: str
    text: str
    tokens_used: int = 0
    completion_time: str = field(default_factory=_utc_now_iso)
    model: str = ""

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
        )


@dataclass
class ExecutionResult:
    """Aggregate result of executing the whole queue."""

    completed: int = 0
    failed: int = 0
    total_tokens: int = 0
    total_time: float = 0.0  # seconds
    responses: list[Response] = field(default_factory=list)
    # Why execute_queue() stopped before the whole queue was done, if it did:
    # None = fully completed, "quota" = usage limit hit, "error" = anything else.
    stopped_reason: str | None = None
    resume_at: str | None = None  # ISO timestamp the quota is expected to refresh, if known

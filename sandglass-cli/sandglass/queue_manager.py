"""Queue operations: load, save, add, remove, clear, get."""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from . import prompt_source, providers
from .models import PromptObject
from .storage import StorageService

logger = logging.getLogger(__name__)

_TITLE_MAX = 60
# Front-matter keys a prompt block may set for itself. Anything else on a
# leading `key: value` line is treated as prose, not configuration -- a prompt
# is free to start with "note: ..." without Sandglass reinterpreting it.
_HEADER_KEYS = ("model", "effort", "isolate", "provider")
# Values that make `isolate: <x>` mean "yes". Anything else is read as "no",
# so a typo degrades to the cheaper default rather than silently opting a
# block out of the chain.
_TRUTHY = {"true", "yes", "1", "on"}
_HEADER_RE = re.compile(
    rf"^({'|'.join(_HEADER_KEYS)})\s*:\s*(\S.*)$", re.IGNORECASE
)

# Queue files in the wild already annotate blocks with a tier marker on the
# first line -- `**TIER: SONNET**`, `**TIER: CHEAP - EXTERNAL-OK**` and so on --
# written by the prompt author to say how much model this block deserves.
# Sandglass used to ignore those entirely and run every block on the default
# (Opus), which is both expensive and not what the author asked for. Reading
# them is therefore a fix, not a new convention: the intent was already stated,
# it just wasn't being honoured.
#
# This is a real behaviour change, so it is deliberately visible rather than
# silent: the resolved model/effort is printed by `queue add`, shown in
# `queue list`, and logged per prompt. An explicit `model:`/`effort:` header
# always wins over a tier marker, and `--no-tiers` on `execute` turns the
# mapping off entirely.
_TIER_RE = re.compile(r"\*{0,2}TIER\s*:\s*([A-Za-z]+)", re.IGNORECASE)
TIER_MAP: dict[str, tuple[str | None, str | None]] = {
    # tier -> (model, effort)
    "opus": ("opus", "high"),
    "sonnet": ("sonnet", "medium"),
    "haiku": ("haiku", "low"),
    "cheap": ("haiku", "low"),
}
# Only look for a tier marker near the top of a block; the word could plausibly
# appear in prose further down, and a marker buried on line 40 isn't front
# matter anyway.
_TIER_SCAN_CHARS = 300

# The marker that sends a block to a non-Anthropic provider -- `**CLINE: pro**`,
# `**CLINE: flash**`, or a literal model id. Read in the same place and the same
# way as `TIER:`, because it is the same kind of thing: a note the prompt author
# already writes for a human reader, which Sandglass can act on.
#
# It is a SEPARATE marker from `TIER:` on purpose. Queue files in this project
# already carry `**TIER: CHEAP - EXTERNAL-OK**` on blocks written months ago,
# and those must keep meaning what they meant when they were written (haiku, on
# Anthropic). "EXTERNAL-OK" is the author granting *permission* for a block to
# leave Anthropic; `CLINE:` is the author *exercising* it. Conflating the two
# would retroactively reroute a pile of existing blocks to a third party on the
# strength of a comment nobody wrote with that consequence in mind.
_CLINE_RE = re.compile(
    r"\*{0,2}(?:CLINE|EXTERNAL)\s*:\s*([A-Za-z0-9._\-]+)", re.IGNORECASE
)
# Which provider a bare tier name means. Only one external provider is wired up
# (see providers.py), so `CLINE: pro` needs no vendor name -- but a block that
# does name one (`CLINE: deepseek-v4-pro`) still routes correctly, because the
# tier value is passed to the provider to resolve.
DEFAULT_EXTERNAL_PROVIDER = "deepseek"


class QueueManager:
    """Manages the prompt queue backed by .sandglass/queue.json."""

    def __init__(self, storage: StorageService | None = None):
        self.storage = storage or StorageService()
        self.storage.ensure_sandglass_dir()

    # --- Persistence ------------------------------------------------------

    def load_queue(self) -> list[PromptObject]:
        """Load the queue, returning an empty list if none exists."""
        data = self.storage.load_json(self.storage.queue_path)
        raw = data.get("prompts", []) if isinstance(data, dict) else []
        prompts: list[PromptObject] = []
        for item in raw:
            try:
                prompts.append(PromptObject.from_dict(item))
            except (KeyError, TypeError) as exc:
                logger.warning("Skipping malformed queue entry %r: %s", item, exc)
        return prompts

    def save_queue(self, prompts: list[PromptObject]) -> None:
        """Persist the queue atomically."""
        data = {"prompts": [p.to_dict() for p in prompts]}
        self.storage.save_json(self.storage.queue_path, data)

    # --- Mutations --------------------------------------------------------

    def add_prompt(
        self,
        text: str | None = None,
        file_path: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        origin_file: str | None = None,
        use_tiers: bool = True,
    ) -> str:
        """Add a prompt (from raw text or a file) and return its generated id.

        If ``text``/the file's content starts with ``model:``/``effort:``
        header lines followed by a blank line, those are stripped from the
        stored prompt text and applied to the prompt — unless the matching
        argument is passed explicitly, which always takes precedence. Failing
        both, a ``TIER:`` marker near the top of the block supplies defaults
        (see :data:`TIER_MAP`); pass ``use_tiers=False`` to ignore markers.
        ``origin_file`` marks a prompt as loaded from a markdown queue source
        (see :func:`import_from_markdown`) rather than added directly.
        """
        if bool(text) == bool(file_path):
            raise ValueError("Provide exactly one of `text` or `file_path`.")

        source = "text"
        if file_path:
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"File '{file_path}' not found")
            with open(file_path, "r", encoding="utf-8") as fh:
                text = fh.read()
            source = "file"

        text = text or ""
        headers, text = self._extract_headers(text)
        tier_model, tier_effort = self._tier_defaults(text) if use_tiers else (None, None)
        ext_provider, ext_model = (
            self._external_defaults(text) if use_tiers else (None, None)
        )
        # Precedence, most explicit first: caller argument, front matter, tier
        # marker. A tier marker is the weakest signal because it's a hint
        # written for a human reader that Sandglass happens to be able to use.
        provider = headers.get("provider") or ext_provider
        # An external block's model has to come from the provider's own
        # namespace -- `opus` means nothing to DeepSeek -- so a routing marker
        # outranks a TIER marker on model choice while leaving effort alone.
        model = model or headers.get("model") or ext_model or tier_model
        effort = effort or headers.get("effort") or tier_effort
        isolate = headers.get("isolate", "").strip().lower() in _TRUTHY

        queue = self.load_queue()
        prompt_id = f"{len(queue) + 1:03d}"
        prompt = PromptObject(
            id=prompt_id,
            title=self._derive_title(text, file_path),
            text=text,
            source=source,
            file_path=file_path,
            model=model,
            effort=effort,
            origin_file=origin_file,
            isolate=isolate,
            provider=provider,
        )
        queue.append(prompt)
        self.save_queue(queue)
        logger.info(
            "Added prompt %s to queue (source=%s, model=%s, effort=%s, provider=%s)",
            prompt_id, source, model or "default", effort or "default",
            provider or "anthropic",
        )
        return prompt_id

    def import_from_markdown(self, source_file: str, use_tiers: bool = True) -> int:
        """Load every `====`-delimited block from ``source_file`` into the queue.

        Each block goes through the same model-header parsing as
        :meth:`add_prompt` and is tagged with ``origin_file=source_file`` so a
        successful run can cut it out of the source file afterward. Blocks
        already sitting in the queue are unaffected — call this only when the
        queue is empty, which is what `sandglass execute` does by default.
        Returns the number of prompts added (0 if the file is missing/empty).
        """
        blocks = prompt_source.read_blocks(source_file)
        for block in blocks:
            self.add_prompt(
                text=block["text"], origin_file=source_file, use_tiers=use_tiers
            )
        if blocks:
            logger.info("Imported %d prompt(s) from %s", len(blocks), source_file)
        return len(blocks)

    def remove_prompt(self, index: int) -> PromptObject:
        """Remove the prompt at a 1-based index and return it."""
        queue = self.load_queue()
        if index < 1 or index > len(queue):
            raise IndexError(f"Index {index} out of range (1-{len(queue)})")
        removed = queue.pop(index - 1)
        self.save_queue(queue)
        logger.info("Removed prompt %s from queue", removed.id)
        return removed

    def remove_prompt_entry(self, prompt: PromptObject) -> Optional[PromptObject]:
        """Drop this exact prompt; return None if it is already gone.

        Neither position nor id can be trusted here. `.sandglass/queue.json`
        lives inside the project directory, which is exactly where queued
        blocks run with full tool access, so a long block can rewrite or clear
        the queue mid-run. Observed live: a 20-minute block emptied the queue
        at 11:41, and the engine crashed at 11:44 popping position 1 from an
        empty list, discarding a completed $10 block.

        Ids are sequence numbers assigned on insert, not identities — clear
        the queue and add something else and it is `001` again. Removing by id
        alone would therefore delete an *unrelated* block that inherited the
        number. The prompt's text is what actually identifies it, so both must
        match.

        Returns None rather than raising: by the time this is called the work
        is done, and the entry is bookkeeping. Something else having removed
        it first is not a reason to fail the run.
        """
        queue = self.load_queue()
        for i, candidate in enumerate(queue):
            if candidate.id == prompt.id and candidate.text == prompt.text:
                removed = queue.pop(i)
                self.save_queue(queue)
                logger.info("Removed prompt %s from queue", removed.id)
                return removed
        logger.warning(
            "Prompt %s (%s) was no longer in the queue when the run tried to "
            "remove it — something else modified %s during the run.",
            prompt.id, prompt.title[:60], self.storage.queue_path,
        )
        return None

    def clear_queue(self) -> None:
        """Empty the queue."""
        self.save_queue([])
        logger.info("Queue cleared")

    # --- Accessors --------------------------------------------------------

    def get_prompt(self, index: int) -> PromptObject:
        """Return the prompt at a 1-based index."""
        queue = self.load_queue()
        if index < 1 or index > len(queue):
            raise IndexError(f"Index {index} out of range (1-{len(queue)})")
        return queue[index - 1]

    def get_all_prompts(self) -> list[PromptObject]:
        """Return the entire queue."""
        return self.load_queue()

    # --- Helpers ----------------------------------------------------------

    @staticmethod
    def _extract_headers(text: str) -> tuple[dict[str, str], str]:
        """Strip leading ``key: value`` front matter (blank line, then body).

        Deliberately strict — a bare match on line 1 isn't enough, since a
        genuine prompt could start with the word "model:". Requiring the header
        block to be followed by an empty line keeps false positives effectively
        impossible while still matching the plain front-matter style shown in
        the docs:

            model: Opus
            effort: low

            Do this and that...
        """
        lines = text.splitlines()
        headers: dict[str, str] = {}
        idx = 0
        while idx < len(lines):
            match = _HEADER_RE.match(lines[idx].strip())
            if not match:
                break
            headers[match.group(1).lower()] = match.group(2).strip()
            idx += 1
        # No headers, or nothing after them, or no blank line separating them
        # from the body -> treat the whole thing as literal prompt text.
        if not headers or idx >= len(lines) or lines[idx].strip() != "":
            return {}, text
        return headers, "\n".join(lines[idx + 1:]).lstrip("\n")

    @staticmethod
    def _tier_defaults(text: str) -> tuple[str | None, str | None]:
        """Model/effort implied by a `TIER:` marker near the top of a block.

        Returns ``(None, None)`` when there is no recognised marker. See
        :data:`TIER_MAP` for why this is read at all.
        """
        match = _TIER_RE.search(text[:_TIER_SCAN_CHARS])
        if not match:
            return None, None
        return TIER_MAP.get(match.group(1).lower(), (None, None))

    @staticmethod
    def _external_defaults(text: str) -> tuple[str | None, str | None]:
        """``(provider, model)`` implied by a `CLINE:`/`EXTERNAL:` marker.

        Returns ``(None, None)`` when the block carries no routing marker,
        which is the overwhelming majority of blocks and means "Anthropic".

        The value may be a tier the provider understands (`pro`, `flash`), a
        literal model id (`deepseek-v4-pro`), or the provider's own name
        (`deepseek`) to take its default model. An unrecognised value is
        forwarded to the provider rather than rejected here: model names are
        the vendor's to change, and failing a block over a name Sandglass
        hasn't heard of would age worse than passing it through.
        """
        match = _CLINE_RE.search(text[:_TIER_SCAN_CHARS])
        if not match:
            return None, None
        value = match.group(1).strip()

        named = providers.get(value)
        if named is not None:  # `CLINE: deepseek`
            return named.name, named.default_model

        provider = providers.get(DEFAULT_EXTERNAL_PROVIDER)
        if provider is None:  # pragma: no cover - only if the registry is gutted
            return None, None
        return provider.name, provider.resolve_model(value)

    @staticmethod
    def _derive_title(text: str, file_path: str | None) -> str:
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not first_line and file_path:
            return f"[From file] {os.path.basename(file_path)}"
        if not first_line:
            return "(empty prompt)"
        return first_line if len(first_line) <= _TITLE_MAX else first_line[: _TITLE_MAX - 1] + "…"

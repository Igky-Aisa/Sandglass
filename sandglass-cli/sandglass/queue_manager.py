"""Queue operations: load, save, add, remove, clear, get."""

from __future__ import annotations

import logging
import os
import re

from . import prompt_source
from .models import PromptObject
from .storage import StorageService

logger = logging.getLogger(__name__)

_TITLE_MAX = 60
_MODEL_HEADER_RE = re.compile(r"^model\s*:\s*(\S.*)$", re.IGNORECASE)


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
        origin_file: str | None = None,
    ) -> str:
        """Add a prompt (from raw text or a file) and return its generated id.

        If ``text``/the file's content starts with a ``model: <name>`` header
        line followed by a blank line, that header is stripped from the
        stored prompt text and used as the prompt's model — unless ``model``
        is passed explicitly, which always takes precedence. ``origin_file``
        marks a prompt as loaded from a markdown queue source (see
        :func:`import_from_markdown`) rather than added directly.
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
        header_model, text = self._extract_model_header(text)
        model = model or header_model

        queue = self.load_queue()
        prompt_id = f"{len(queue) + 1:03d}"
        prompt = PromptObject(
            id=prompt_id,
            title=self._derive_title(text, file_path),
            text=text,
            source=source,
            file_path=file_path,
            model=model,
            origin_file=origin_file,
        )
        queue.append(prompt)
        self.save_queue(queue)
        logger.info(
            "Added prompt %s to queue (source=%s, model=%s)", prompt_id, source, model or "default"
        )
        return prompt_id

    def import_from_markdown(self, source_file: str) -> int:
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
            self.add_prompt(text=block["text"], origin_file=source_file)
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
    def _extract_model_header(text: str) -> tuple[str | None, str]:
        """Strip a leading ``model: <name>`` header (blank line, then body).

        Deliberately strict — a bare match on line 1 isn't enough, since a
        genuine prompt could start with the word "model:". Requiring an
        empty line 2 keeps false positives effectively impossible while still
        matching the plain front-matter style shown in the docs:

            model: Opus

            Do this and that...
        """
        lines = text.splitlines()
        if len(lines) >= 2:
            match = _MODEL_HEADER_RE.match(lines[0].strip())
            if match and lines[1].strip() == "":
                model = match.group(1).strip()
                body = "\n".join(lines[2:]).lstrip("\n")
                return model, body
        return None, text

    @staticmethod
    def _derive_title(text: str, file_path: str | None) -> str:
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not first_line and file_path:
            return f"[From file] {os.path.basename(file_path)}"
        if not first_line:
            return "(empty prompt)"
        return first_line if len(first_line) <= _TITLE_MAX else first_line[: _TITLE_MAX - 1] + "…"

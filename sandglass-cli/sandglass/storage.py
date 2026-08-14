"""Centralized JSON file I/O with atomic writes and graceful error handling."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class StorageService:
    """Handles all JSON reads/writes under the .sandglass/ base directory."""

    def __init__(self, base_path: str = ".sandglass"):
        self.base_path = base_path

    # --- Well-known paths -------------------------------------------------

    @property
    def queue_path(self) -> str:
        return os.path.join(self.base_path, "queue.json")

    @property
    def responses_dir(self) -> str:
        return os.path.join(self.base_path, "responses")

    @property
    def history_path(self) -> str:
        return os.path.join(self.base_path, "history.json")

    @property
    def settings_path(self) -> str:
        return os.path.join(self.base_path, "settings.json")

    @property
    def accounts_state_path(self) -> str:
        """When each pooled account's quota is expected back.

        Names and epoch timestamps only — never a token. Kept on disk for the
        same reason `run_state_path` is: the thing it must survive is the
        process ending. A run that restarts after a crash would otherwise see
        a full pool, start at the first account, and spend a real request
        rediscovering a quota it had already found.
        """
        return os.path.join(self.base_path, "accounts_state.json")

    @property
    def run_state_path(self) -> str:
        """Which Claude Code session the current queue drain is running in.

        Kept on disk rather than in memory because the thing it has to survive
        is exactly a process ending: a quota hit can stop `sandglass execute`
        entirely, and the run that picks the queue back up hours later still
        needs to rejoin the same conversation instead of starting over.
        """
        return os.path.join(self.base_path, "run_state.json")

    @property
    def progress_notify_path(self) -> str:
        """The highest prompt-throughput percentage milestone already pushed
        to the phone, so a restarted run doesn't re-announce 5%, 10%, 15%...
        from scratch, and a run that completes several blocks inside one 5%
        band only notifies once. Per-project, like every other file under
        `.sandglass/` -- a fresh project starts back at "nothing notified".
        """
        return os.path.join(self.base_path, "progress_notify.json")

    @property
    def last_run_path(self) -> str:
        """What the most recent `sandglass execute` is doing, and why it ended.

        Separate from `run_state_path` on purpose: that one is machinery the
        next run consumes, this one is an explanation a person reads (via
        `sandglass why`). See sandglass/run_report.py.
        """
        return os.path.join(self.base_path, "last_run.json")

    # --- Directory management --------------------------------------------

    def ensure_dir(self, path: str) -> None:
        """Create a directory (and parents) if it does not already exist."""
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            logger.error("Failed to create directory %s: %s", path, exc)
            raise

    def ensure_sandglass_dir(self) -> None:
        """Ensure the base .sandglass/ directory and responses/ subdir exist."""
        self.ensure_dir(self.base_path)
        self.ensure_dir(self.responses_dir)

    # --- JSON I/O ---------------------------------------------------------

    def load_json(self, path: str) -> dict:
        """Load JSON from ``path``.

        Returns an empty dict if the file is missing. If the file exists but is
        not valid JSON, it is backed up alongside itself and an empty dict is
        returned (never raises on corruption).
        """
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Invalid JSON in %s: %s. Backing up and resetting.", path, exc)
            self._backup_corrupt(path)
            return {}
        except OSError as exc:
            logger.error("Failed to read %s: %s", path, exc)
            return {}

    def save_json(self, path: str, data: dict) -> None:
        """Atomically write ``data`` as JSON to ``path``.

        Writes to a temp file in the same directory, then renames it over the
        target so a crash mid-write can never corrupt the existing file.
        """
        directory = os.path.dirname(path) or "."
        self.ensure_dir(directory)

        # Serialize first so a serialization error never touches the target file.
        payload = json.dumps(data, indent=2, ensure_ascii=False)

        tmp_fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)  # atomic on Windows and POSIX
        except OSError as exc:
            logger.error("Failed to write %s: %s", path, exc)
            self._cleanup_tmp(tmp_path)
            raise

    # --- Internal helpers -------------------------------------------------

    def _backup_corrupt(self, path: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup = f"{path}.{stamp}.bak"
        try:
            os.replace(path, backup)
            logger.warning("Corrupt file backed up to %s", backup)
        except OSError as exc:
            logger.error("Failed to back up corrupt file %s: %s", path, exc)

    @staticmethod
    def _cleanup_tmp(tmp_path: str) -> None:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass

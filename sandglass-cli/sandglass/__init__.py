"""Sandglass CLI — batch prompt queue executor for Claude."""

import logging as _logging
import os as _os
import sys as _sys

if _sys.platform == "win32":
    # Default Windows console codepages (cp1252/cp437) can't render the
    # checkmarks/emoji this tool prints. Force UTF-8 on import so any entry
    # point — the CLI, the engine, or a direct import — is safe on Windows.
    for _stream in (_sys.stdout, _sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a .env file in the current directory into
    os.environ, without overriding anything already set there (an explicit
    shell env var always wins). A format this simple doesn't need the
    python-dotenv dependency -- keeps installation as lightweight as the
    README already claims. Silently does nothing if the file is missing;
    used for things like `SANDGLASS_NTFY_TOPIC` (see notify.py) that are
    natural to keep in a local, gitignored .env rather than passed by hand
    on every invocation.
    """
    if not _os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    _os.environ.setdefault(key, value)
    except OSError:
        pass


_load_dotenv()

# INFO-level audit trail (API calls, queue mutations) goes to stderr so it
# never interleaves with the Rich-rendered stdout output; DEBUG stays hidden
# unless a caller raises the root level explicitly.
_logging.basicConfig(
    level=_logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=_sys.stderr,
)

__version__ = "0.8.0"

from .models import PromptObject, Response, ExecutionResult
from .storage import StorageService
from .queue_manager import QueueManager

__all__ = [
    "__version__",
    "PromptObject",
    "Response",
    "ExecutionResult",
    "StorageService",
    "QueueManager",
]

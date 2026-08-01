"""Claude Code CLI wrapper — runs prompts against the Claude Code CLI in
headless mode (`claude -p`) instead of the pay-per-token Messages API.

This tool's whole point is to burn through a Claude Pro/Max subscription's
*rolling, refreshing* usage quota, not to pay for separate API credits.
Those are two different Anthropic products with different billing:
  - Messages API (api key)  -> pay-per-token credits, no "refresh"
  - Claude Code / claude.ai -> subscription with a quota that resets on a
                                rolling window (what `claude auth status`
                                reports as authMethod "claude.ai")
Shelling out to the already-installed `claude` CLI means execution draws on
whatever account `claude` is logged into -- typically the subscription.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from typing import Callable, Optional

from .models import Response

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"
# Sandglass's whole point is unattended batch execution -- there's no one
# around to answer an interactive permission prompt -- so it defaults to full
# tool access rather than stalling/silently-denying on the first tool a
# queued prompt needs. `sandglass execute` prints a warning every run so
# this is never silent.
DEFAULT_PERMISSION_MODE = "bypassPermissions"
# `--output-format stream-json` emits one JSON object per line. asyncio's
# default per-line buffer is 64KB, which a tool result embedding a large
# file's contents (very plausible under bypassPermissions) can exceed --
# raised well above any realistic single-event size.
STREAM_BUFFER_LIMIT = 10 * 1024 * 1024


class ClaudeCLINotFoundError(RuntimeError):
    """Raised when the `claude` binary isn't on PATH."""

    def __init__(self) -> None:
        super().__init__(
            "The 'claude' CLI was not found on PATH. Install Claude Code "
            "(https://claude.com/claude-code), then run `claude auth login` "
            "to authenticate with your Pro/Max subscription."
        )


class QuotaExceededError(RuntimeError):
    """Raised when Claude Code reports the account's usage limit is hit.

    Carries ``rate_limit_info`` (the raw ``rate_limit_event`` payload, if one
    arrived — e.g. ``{"status": "rejected", "resetsAt": <epoch>, ...}``) so a
    caller can auto-resume at the exact time the quota refreshes instead of
    polling blindly.
    """

    def __init__(self, message: str, rate_limit_info: Optional[dict] = None):
        super().__init__(message)
        self.rate_limit_info = rate_limit_info

    @property
    def resets_at(self) -> Optional[int]:
        """Epoch seconds the quota is expected to refresh, if known."""
        if self.rate_limit_info:
            return self.rate_limit_info.get("resetsAt")
        return None


class ClaudeClient:
    """Thin async wrapper around `claude -p` (Claude Code headless mode)."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
    ):
        self.model = model
        self.permission_mode = permission_mode
        self._cli_path = shutil.which("claude")

    @property
    def has_cli(self) -> bool:
        return self._cli_path is not None

    def get_auth_status(self) -> Optional[dict]:
        """Best-effort look at how `claude` is authenticated.

        Used to warn the user if usage would be billed as pay-per-token API
        credits instead of drawing on a Pro/Max subscription's quota. Returns
        ``None`` if the CLI is missing or the check fails for any reason —
        callers should treat that as "unknown", not "not logged in".
        """
        if not self._cli_path:
            return None
        try:
            result = subprocess.run(
                [self._cli_path, "auth", "status"],
                capture_output=True, text=True, timeout=20, check=False,
            )
            return json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read claude auth status: %s", exc)
            return None

    async def send_prompt(
        self,
        text: str,
        on_chunk: Optional[Callable[[str], None]] = None,
        model: Optional[str] = None,
    ) -> Response:
        """Run ``text`` through `claude -p`, streaming the reply.

        ``model`` overrides ``self.model`` for just this call (used for
        per-prompt model overrides from the queue). Calls
        ``on_chunk(text_piece)`` for each streamed text delta (for live
        progress) and returns a fully accumulated :class:`Response`. Raises
        :class:`QuotaExceededError` if Claude Code reports the subscription's
        usage limit is hit, or :class:`RuntimeError` for any other failure.
        """
        if not self._cli_path:
            raise ClaudeCLINotFoundError()

        effective_model = self._normalize_model(model) if model else self.model

        logger.info(
            "Sending prompt to Claude Code CLI (model=%s, chars=%d, permission_mode=%s)",
            effective_model, len(text), self.permission_mode,
        )

        cmd = [
            self._cli_path,
            "-p", text,
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--no-session-persistence",
            "--permission-mode", self.permission_mode,
            "--model", effective_model,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_BUFFER_LIMIT,
        )

        pieces: list[str] = []
        final_result: Optional[dict] = None
        rate_limit_info: Optional[dict] = None

        assert proc.stdout is not None
        try:
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")
                if etype == "rate_limit_event":
                    rate_limit_info = event.get("rate_limit_info")
                    if rate_limit_info and rate_limit_info.get("status") not in (None, "allowed"):
                        logger.warning("Rate limit status: %s", rate_limit_info)
                elif etype == "stream_event":
                    inner = event.get("event", {})
                    if inner.get("type") == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            piece = delta.get("text", "")
                            pieces.append(piece)
                            if on_chunk is not None:
                                on_chunk(piece)
                elif etype == "result":
                    final_result = event
        except (asyncio.LimitOverrunError, ValueError) as exc:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"claude CLI produced a single output line longer than "
                f"{STREAM_BUFFER_LIMIT:,} bytes ({exc}) — this is a "
                "Sandglass buffer limit, not a quota or auth issue."
            ) from exc

        stderr_data = await proc.stderr.read()
        exit_code = await proc.wait()

        if final_result is None:
            stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                stderr_text or f"claude exited with code {exit_code} and produced no result"
            )

        if final_result.get("is_error") or exit_code != 0:
            error_text = final_result.get("result") or "Unknown error from claude CLI"
            logger.error("claude CLI reported an error: %s", error_text)
            if self._looks_like_quota_error(error_text, rate_limit_info):
                raise QuotaExceededError(error_text, rate_limit_info=rate_limit_info)
            raise RuntimeError(error_text)

        usage = final_result.get("usage", {}) or {}
        tokens = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
        logger.info("Received response from Claude Code CLI (tokens=%d)", tokens)

        return Response(
            prompt_id="",
            text=final_result.get("result") or "".join(pieces),
            tokens_used=tokens,
            model=effective_model,
        )

    @staticmethod
    def _looks_like_quota_error(text: str, rate_limit_info: Optional[dict]) -> bool:
        if rate_limit_info and rate_limit_info.get("status") not in (None, "allowed"):
            return True
        lowered = (text or "").lower()
        return any(kw in lowered for kw in ("usage limit", "rate limit", "quota"))

    @staticmethod
    def _normalize_model(model: str) -> str:
        """Lowercase short aliases ("Opus" -> "opus"); leave full model IDs
        (which always contain a hyphen, e.g. "claude-opus-4-8") untouched —
        `claude --model` resolves either form itself.
        """
        model = model.strip()
        return model.lower() if "-" not in model else model

    def estimate_tokens(self, text: str) -> int:
        """Rough token estimate (~4 characters per token)."""
        return len(text) // 4

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

from .accounts import subprocess_env
from .models import Response

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"
# `claude` normally bakes per-machine facts (cwd, env info, memory paths, git
# status) into the system prompt. Git status in particular changes every time a
# prompt writes a file, which shifts the prompt prefix and forces the whole
# cached span after it to be re-created instead of read -- and cache creation
# is billed at a multiple of base rate while a cache read is a fraction of it.
# Moving those sections into the first user message keeps the prefix stable
# across blocks. Harmless for unattended runs: the same facts still reach the
# model, just later in the prompt.
STABLE_PREFIX_FLAG = "--exclude-dynamic-system-prompt-sections"
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

    def __init__(
        self,
        message: str,
        rate_limit_info: Optional[dict] = None,
        session_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.rate_limit_info = rate_limit_info
        # The session the interrupted attempt was running in, if it got far
        # enough to have one. Carried on the exception because this is exactly
        # the case resuming exists for -- a quota hit part-way through real
        # work -- and without it the retry would throw that work away and
        # start cold, which is the failure chaining is meant to prevent.
        self.session_id = session_id

    @property
    def resets_at(self) -> Optional[int]:
        """Epoch seconds the quota is expected to refresh, if known."""
        if self.rate_limit_info:
            return self.rate_limit_info.get("resetsAt")
        return None


class SessionNotResumableError(RuntimeError):
    """Raised when `--resume <id>` names a session the CLI can't find.

    Not a real failure — the prompt simply has to start over. Split out from
    :class:`RuntimeError` so :meth:`ClaudeClient.send_prompt` can retry itself
    from a clean session instead of surfacing a confusing error for what is
    really just a cold start.
    """


class ClaudeClient:
    """Thin async wrapper around `claude -p` (Claude Code headless mode)."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        effort: Optional[str] = None,
        budget_usd: Optional[float] = None,
        stable_prefix: bool = True,
        auth_token: Optional[str] = None,
    ):
        self.model = model
        self.permission_mode = permission_mode
        # Run-wide defaults; a queued prompt can override `effort` per block.
        self.effort = effort
        self.budget_usd = budget_usd
        self.stable_prefix = stable_prefix
        # Which subscription to run under, when a pool of them is in play.
        # None means "whatever `claude` itself is logged into", which is the
        # single-account default and leaves the machine's own login untouched.
        # Mutated in place by the engine when it rotates -- see accounts.py.
        self.auth_token = auth_token
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
                # Under rotation this reports the *pooled* account rather than
                # the machine's own login, so the pre-flight notice describes
                # the credential the run will actually bill.
                env=subprocess_env(self.auth_token),
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
        effort: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        persist_session: bool = True,
        _retried: bool = False,
    ) -> Response:
        """Run ``text`` through `claude -p`, streaming the reply.

        ``model``/``effort`` override the client defaults for just this call
        (used for per-prompt overrides from the queue). Calls
        ``on_chunk(text_piece)`` for each streamed text delta (for live
        progress) and returns a fully accumulated :class:`Response`. Raises
        :class:`QuotaExceededError` if Claude Code reports the subscription's
        usage limit is hit, or :class:`RuntimeError` for any other failure.

        Session handling has three shapes:

        - ``resume_session_id`` set — continue that conversation. Everything it
          already read and wrote is a cached prefix, so the work comes back at
          a fraction of re-deriving it, and the model knows which files it has
          already written (which is what stops a resumed run concluding
          "already done" and tripping the artifact gate).
        - ``persist_session=True`` with no id — a normal run whose session the
          CLI creates and names; the id it chose comes back on the response so
          the caller can resume it later.
        - ``persist_session=False`` — nothing written to disk, nothing
          resumable.

        Deliberately never passes ``--session-id``. Naming a session up front
        means guessing whether the CLI has already created it, and guessing
        wrong is unrecoverable in the worst way: an id that was created but not
        recorded fails every subsequent attempt with "already in use", forever,
        with no way for the queue to move. Letting the CLI assign the id and
        reading it back off the result removes that class of failure entirely.
        """
        if not self._cli_path:
            raise ClaudeCLINotFoundError()

        effective_model = self._normalize_model(model) if model else self.model
        effective_effort = effort or self.effort

        logger.info(
            "Sending prompt to Claude Code CLI (model=%s, effort=%s, chars=%d, "
            "permission_mode=%s, session=%s)",
            effective_model, effective_effort or "default", len(text),
            self.permission_mode,
            f"resuming {resume_session_id}" if resume_session_id
            else ("new" if persist_session else "ephemeral"),
        )

        cmd = [
            self._cli_path,
            "-p",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--permission-mode", self.permission_mode,
            "--model", effective_model,
        ]
        if resume_session_id:
            cmd += ["--resume", resume_session_id]
        elif not persist_session:
            cmd.append("--no-session-persistence")
        if self.stable_prefix:
            cmd.append(STABLE_PREFIX_FLAG)
        if effective_effort:
            cmd += ["--effort", effective_effort]
        if self.budget_usd is not None:
            cmd += ["--max-budget-usd", str(self.budget_usd)]

        # The prompt is a POSITIONAL argument, not the value of `-p` (`-p` is a
        # boolean flag: `claude [options] [prompt]`). It therefore has to come
        # last, behind `--`, or the CLI's option parser reads any prompt that
        # opens with a dash as a flag and dies with "unknown option". That is
        # not a rare shape: a markdown bullet, a `---` rule, or an em-dash list
        # all start a perfectly ordinary block this way.
        cmd += ["--", text]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_BUFFER_LIMIT,
            # None when not rotating -- the subprocess then inherits this
            # process's environment exactly as it always has.
            env=subprocess_env(self.auth_token),
        )

        pieces: list[str] = []
        final_result: Optional[dict] = None
        rate_limit_info: Optional[dict] = None
        observed_session_id: Optional[str] = None

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

                # Every event carries the session it belongs to; the init event
                # arrives before any work, so this is known even if the run
                # then fails. It's how the caller learns which conversation to
                # resume without ever having to name one.
                if not observed_session_id and event.get("session_id"):
                    observed_session_id = event["session_id"]

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
        stderr_text = stderr_data.decode("utf-8", errors="replace").strip()

        # A rejected session never produces a `result` event at all -- the CLI
        # refuses on stderr and exits. Both failure shapes therefore have to
        # reach the same recovery below; an earlier version only handled the
        # `result`-event shape, which made a stale session id an unrecoverable
        # deadlock: every attempt failed identically and the queue never moved.
        error_text: Optional[str] = None
        if final_result is None:
            error_text = (
                stderr_text
                or f"claude exited with code {exit_code} and produced no result"
            )
        elif final_result.get("is_error") or exit_code != 0:
            error_text = (
                final_result.get("result")
                or stderr_text
                or "Unknown error from claude CLI"
            )

        if error_text is not None:
            logger.error("claude CLI reported an error: %s", error_text)
            if self._looks_like_quota_error(error_text, rate_limit_info):
                raise QuotaExceededError(
                    error_text,
                    rate_limit_info=rate_limit_info,
                    session_id=observed_session_id or resume_session_id,
                )
            if (
                resume_session_id
                and not _retried
                and self._looks_like_unusable_session(error_text)
            ):
                # The conversation we meant to continue can't be joined --
                # missing, locked by another process, or otherwise unusable.
                # Nothing is actually wrong with the prompt, so run it in a
                # fresh session rather than reporting a failure a human has to
                # decode. Costs a cold start; beats stopping the batch.
                logger.warning(
                    "Session %s is unusable (%s); running this prompt in a new session.",
                    resume_session_id, error_text,
                )
                return await self.send_prompt(
                    text, on_chunk, model=model, effort=effort,
                    resume_session_id=None, persist_session=True, _retried=True,
                )
            raise RuntimeError(error_text)

        usage = final_result.get("usage", {}) or {}
        response = self._response_from_usage(
            usage,
            text=final_result.get("result") or "".join(pieces),
            model=effective_model,
            cost_usd=final_result.get("total_cost_usd") or 0.0,
        )
        response.session_id = final_result.get("session_id") or observed_session_id
        logger.info(
            "Received response from Claude Code CLI (billed=%d tokens "
            "[in=%d out=%d cache_write=%d cache_read=%d], cost=$%.4f)",
            response.tokens_used, response.input_tokens, response.output_tokens,
            response.cache_creation_tokens, response.cache_read_tokens,
            response.cost_usd,
        )
        return response

    @staticmethod
    def _response_from_usage(
        usage: dict, *, text: str, model: str, cost_usd: float
    ) -> Response:
        """Build a :class:`Response` from the CLI's ``result.usage`` object.

        Counts all four token buckets, not just ``input_tokens`` +
        ``output_tokens``. On a cached prompt ``input_tokens`` reports only the
        *uncached remainder*, so the naive sum silently omits the dominant
        term: a measured run billing 23,788 tokens reported 53 that way.
        """
        input_tokens = usage.get("input_tokens") or 0
        output_tokens = usage.get("output_tokens") or 0
        cache_creation = usage.get("cache_creation_input_tokens") or 0
        cache_read = usage.get("cache_read_input_tokens") or 0
        return Response(
            prompt_id="",
            text=text,
            tokens_used=input_tokens + output_tokens + cache_creation + cache_read,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
            cost_usd=float(cost_usd or 0.0),
        )

    @staticmethod
    def _looks_like_unusable_session(text: str) -> bool:
        """Whether an error means "that conversation can't be joined".

        Covers both directions — the session is missing, or it exists but is
        locked/in use by something else — because the recovery is the same
        either way: run this prompt in a fresh session. Matched on text
        because the CLI reports these as plain error output with no
        machine-readable code; the alternative is treating an entirely
        recoverable condition as a hard stop.
        """
        lowered = (text or "").lower()
        if "session" not in lowered and "conversation" not in lowered:
            return False
        return any(
            kw in lowered
            for kw in (
                "not found", "no conversation", "does not exist", "no such",
                "already exists", "already in use", "in use", "duplicate",
                "locked", "cannot be resumed", "could not be resumed",
            )
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

"""Non-Anthropic model providers, reached through the same `claude` CLI.

Claude Code will talk to any endpoint that speaks the Anthropic wire format,
which several vendors now publish specifically so their models can be driven by
it. DeepSeek is the one wired up here: point `ANTHROPIC_BASE_URL` at
`https://api.deepseek.com/anthropic`, supply a DeepSeek key, and every tool the
agent has — file edits, bash, the lot — works exactly as it does against
Anthropic, because none of that lives on the server side.

Why bother, given the account pool already exists: the pool spreads a queue
across subscriptions you are *already paying a flat rate for*, which is free at
the margin but finite. An external provider is the opposite trade — metered, so
never free, but it doesn't consume a quota you were saving for work that
actually needs Opus. Blocks a prompt author has marked cheap-and-external-OK
are precisely the ones worth spending pennies on instead of a slice of the
night's Claude budget.

**Two things this module must never get wrong**, both about where credentials
and code end up:

- **`CLAUDE_CODE_OAUTH_TOKEN` is stripped from an external subprocess.** It is
  a live subscription credential, and leaving it set while `ANTHROPIC_BASE_URL`
  points somewhere else would transmit it to that third party as a bearer
  token. Nothing about the run would look wrong.
- **Routing is opt-in per block, never global-by-default.** Sending a block
  external sends the prompt, the injected project brief, and whatever files the
  agent reads to a third-party vendor under their retention policy — not
  Anthropic's. That is a decision only the prompt author can make, so it is
  carried by an explicit marker in the block itself (see
  ``queue_manager._external_defaults``), and a block with no marker never
  leaves Anthropic.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .accounts import TOKEN_ENV_VAR

logger = logging.getLogger(__name__)

# Where API keys are read from, unless SANDGLASS_PROVIDERS names another path.
# Same reasoning as the account pool's file: outside the project tree, because
# blocks run with `bypassPermissions` in the project directory and their
# responses are persisted verbatim to `.sandglass/responses/`.
DEFAULT_PROVIDERS_FILENAME = "providers.json"

# The environment variables Claude Code reads to talk to a non-Anthropic
# endpoint. `ANTHROPIC_AUTH_TOKEN` is sent as a bearer token and
# `ANTHROPIC_API_KEY` as an `x-api-key` header; DeepSeek's own documentation
# shows the latter while the Claude Code docs describe the former, so both are
# set to the same key. That is safe *only* because the base URL is pointed away
# from Anthropic in the same breath -- see `Provider.subprocess_env`.
BASE_URL_ENV_VAR = "ANTHROPIC_BASE_URL"
AUTH_ENV_VARS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
# Claude Code runs a cheap side model for its own housekeeping (summaries and
# the like). Left unset it would try to reach a Claude model on an endpoint
# that has never heard of one, so it is pinned to the provider's small model.
SMALL_MODEL_ENV_VAR = "ANTHROPIC_SMALL_FAST_MODEL"


class ProvidersError(RuntimeError):
    """The providers file exists but could not be used as written."""


@dataclass(frozen=True)
class Provider:
    """An Anthropic-compatible endpoint that is not Anthropic."""

    name: str
    base_url: str
    # Environment variable consulted when the providers file has no key. Handy
    # for CI and for anyone who already keeps the key in their shell profile.
    key_env: str
    # Short tier names a block may ask for, e.g. `**CLINE: pro**`. Deliberately
    # not the raw model ids: a queue file written today should still route
    # sensibly when the vendor renames its models, and "pro"/"flash" is the
    # distinction a prompt author actually means.
    tiers: dict[str, str]
    default_model: str
    docs_url: str

    def resolve_model(self, tier_or_model: Optional[str]) -> str:
        """Turn `pro`, `flash`, or a literal model id into a model id."""
        if not tier_or_model:
            return self.default_model
        key = tier_or_model.strip().lower()
        return self.tiers.get(key, tier_or_model.strip())

    def subprocess_env(self, api_key: str) -> dict:
        """The environment an external `claude` subprocess should run under.

        Builds on a copy of the current environment so the agent keeps PATH,
        HOME and everything else it needs to actually do work -- then makes the
        three changes that matter:

        1. Points the CLI at the provider's endpoint.
        2. Supplies the provider's key under both header conventions.
        3. **Removes the Claude subscription token.** Without this step the
           run would hand a live Anthropic credential to a third-party server,
           and nothing in the output would say so.
        """
        env = dict(os.environ)
        env[BASE_URL_ENV_VAR] = self.base_url
        for name in AUTH_ENV_VARS:
            env[name] = api_key
        env[SMALL_MODEL_ENV_VAR] = self.default_model
        # The one line in this module that is load-bearing for security.
        env.pop(TOKEN_ENV_VAR, None)
        return env


# DeepSeek publishes an Anthropic-compatible endpoint precisely for this.
# Model ids per their docs: `claude-opus*` maps to deepseek-v4-pro and
# `claude-sonnet*`/`claude-haiku*` to deepseek-v4-flash, with anything
# unrecognised falling back to flash -- so naming them explicitly is the
# difference between getting the model you asked for and getting the cheap one.
DEEPSEEK = Provider(
    name="deepseek",
    base_url="https://api.deepseek.com/anthropic",
    key_env="DEEPSEEK_API_KEY",
    tiers={
        "pro": "deepseek-v4-pro",
        "flash": "deepseek-v4-flash",
        # Aliases, so a block that thinks in Claude tiers still lands somewhere
        # sensible rather than silently defaulting to flash.
        "opus": "deepseek-v4-pro",
        "high": "deepseek-v4-pro",
        "sonnet": "deepseek-v4-flash",
        "haiku": "deepseek-v4-flash",
        "cheap": "deepseek-v4-flash",
    },
    default_model="deepseek-v4-flash",
    docs_url="https://api-docs.deepseek.com/guides/anthropic_api",
)

PROVIDERS: dict[str, Provider] = {DEEPSEEK.name: DEEPSEEK}


@dataclass
class ProviderRegistry:
    """Which external providers this machine has a usable key for."""

    keys: dict[str, str]

    @classmethod
    def default_path(cls) -> Path:
        override = os.environ.get("SANDGLASS_PROVIDERS")
        if override:
            return Path(override).expanduser()
        return Path.home() / ".sandglass" / DEFAULT_PROVIDERS_FILENAME

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "ProviderRegistry":
        """Read configured keys from disk, falling back to the environment.

        Always returns a registry, never None: an empty one simply means no
        block can route externally, which is the correct default state for a
        machine that has never opted in. A file that exists but is malformed
        *is* an error, so a typo surfaces at load rather than as an
        unexplained fallback halfway through a night's run.

        Accepts either shape, because both are the obvious thing to write::

            {"deepseek": {"api_key": "sk-..."}}
            {"providers": {"deepseek": {"api_key": "sk-..."}}}
        """
        keys: dict[str, str] = {}

        # The environment is the weakest source, so it is read first and any
        # file entry overwrites it.
        for name, provider in PROVIDERS.items():
            from_env = os.environ.get(provider.key_env)
            if from_env and from_env.strip():
                keys[name] = from_env.strip()

        path = path or cls.default_path()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProvidersError(f"Could not read {path}: {exc}") from exc
            if not isinstance(raw, dict):
                raise ProvidersError(f"{path}: expected a JSON object at the top level.")
            entries = raw.get("providers") if isinstance(raw.get("providers"), dict) else raw
            for name, entry in entries.items():
                if name not in PROVIDERS:
                    logger.warning(
                        "%s: ignoring unknown provider %r (known: %s)",
                        path, name, ", ".join(sorted(PROVIDERS)),
                    )
                    continue
                key = entry.get("api_key") if isinstance(entry, dict) else entry
                if not key or not isinstance(key, str):
                    raise ProvidersError(
                        f"{path}: provider {name!r} has no 'api_key'."
                    )
                keys[name] = key.strip()
            _warn_if_world_readable(path)

        if keys:
            logger.info("Provider keys available for: %s", ", ".join(sorted(keys)))
        return cls(keys=keys)

    def key_for(self, name: str) -> Optional[str]:
        return self.keys.get(name)

    def has(self, name: str) -> bool:
        return bool(self.keys.get(name))


def get(name: Optional[str]) -> Optional[Provider]:
    """The provider called ``name``, or None for Anthropic/unknown names."""
    if not name:
        return None
    return PROVIDERS.get(name.strip().lower())


def looks_malformed(key: str) -> Optional[str]:
    """Why an API key can't possibly be valid, or None if it might be.

    Same deliberately-weak contract as `accounts.looks_malformed`: format only,
    no live call, and no assertion about the prefix -- the issuing format is the
    vendor's to change, and rejecting a good key is worse than accepting a bad
    one that will announce itself on first use.
    """
    if not key or not key.strip():
        return "empty"
    if any(ch.isspace() for ch in key):
        return "contains whitespace — probably a partial or wrapped paste"
    if "…" in key or "..." in key:
        return "contains an ellipsis — this looks like a placeholder, not a key"
    if len(key) < 16:
        return f"only {len(key)} characters — probably truncated"
    return None


def _warn_if_world_readable(path: Path) -> None:
    """Say so if the key file is readable by other users on this machine.

    POSIX only, for the reason given in accounts.py: Windows ACLs aren't
    expressible in st_mode bits, and a warning that is wrong more often than
    right teaches people to ignore warnings.
    """
    if os.name == "nt":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        logger.warning(
            "%s is readable by other users on this machine. "
            "Restrict it with `chmod 600 %s`.", path, path,
        )

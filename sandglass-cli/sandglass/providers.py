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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# `_atomic_write_json` is shared rather than copied: both files hold live
# credentials, and the one rule that matters -- never leave a truncated
# credential file behind -- must be implemented once or it will drift.
from .accounts import TOKEN_ENV_VAR, AccountsError, _atomic_write_json

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
        """Turn `pro`, `flash`, or a literal model id into a model id.

        Permissive by design, and only safe to call once the decision to route
        here has already been made: it passes an unknown string straight
        through, so the vendor can ship a model this Sandglass has never heard
        of. Use `known_model` to *make* that decision.
        """
        if not tier_or_model:
            return self.default_model
        key = tier_or_model.strip().lower()
        return self.tiers.get(key, tier_or_model.strip())

    def known_model(self, tier_or_model: Optional[str]) -> Optional[str]:
        """The model this value names here, or None if it names nothing.

        The strict half of `resolve_model`, for the one caller that is deciding
        whether a block leaves Anthropic at all. Accepts a tier (`pro`), a model
        id this provider already lists, or any **vendor-prefixed** name
        (`deepseek-v9-turbo`) -- that prefix is what keeps the "the vendor may
        rename its models" case working without accepting arbitrary strings.

        Everything else returns None, and the difference is not academic: a
        marker sliced in half by a scan window (`STO`) reached `resolve_model`,
        came back unchanged, and routed a money-path block to a third party
        because a passed-through string looked exactly like a model id.
        """
        if not tier_or_model:
            return None
        key = tier_or_model.strip().lower()
        if key in self.tiers:
            return self.tiers[key]
        if key in set(self.tiers.values()):
            return key
        if key == self.name or key.startswith(f"{self.name}-"):
            return tier_or_model.strip()
        return None

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
        # The vendor-qualified spellings. These are the ones that can be
        # written as a plain `model:` and still say which vendor they mean --
        # see `provider_for_model`, which is why they carry the prefix.
        "deepseek-pro": "deepseek-v4-pro",
        "deepseek-flash": "deepseek-v4-flash",
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
    """Which external providers this machine has a usable key for.

    A provider may hold **more than one key**, in file order, and the registry
    tracks which of them is in use. That exists for one reason: an external key
    is metered, so it doesn't hit a quota that refreshes on a clock — it hits a
    balance of zero, which no amount of waiting fixes. When that happens the run
    moves to the next key for that vendor, and only when every one of them is
    spent does it fall back to Anthropic (see
    ``ExecutionEngine._execute_with_rotation``).

    **Credit state is per-run and never persisted.** An empty balance is undone
    by a human topping the account up, not by time passing, so writing it to
    disk would bench a freshly-funded key on the next run for no reason. A new
    `sandglass execute` always gives every key another chance.

    **Parking is the opposite: an instruction, so it *is* persisted.** A parked
    vendor keeps its keys and is simply never used, exactly as a disabled
    account is skipped by the pool — "stop sending work there until I say
    otherwise" has to survive the run that heard it, or the command is
    decoration. It is distinct from `--no-external`, which turns off every
    vendor for one run, and from having no key, which is a thing to fix rather
    than a decision someone made.
    """

    # Written as either one key or several; normalised to a list below so the
    # rest of the module only ever deals with one shape.
    keys: dict[str, "str | list[str]"]
    # Vendors switched off in the providers file. Held separately from `keys`
    # rather than by dropping them, so the page and the CLI can say "parked"
    # instead of the actively misleading "no key" -- and so re-enabling one
    # doesn't mean typing the key in again.
    parked: set[str] = field(default_factory=set)
    # Which key each provider is currently on, and the providers whose every
    # key has come back "no credit" during this run.
    _index: dict[str, int] = field(default_factory=dict)
    _spent: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        normalised: dict[str, list[str]] = {}
        for name, value in (self.keys or {}).items():
            candidates = [value] if isinstance(value, str) else list(value or [])
            usable = [str(k).strip() for k in candidates if k and str(k).strip()]
            if usable:
                normalised[name] = usable
        self.keys = normalised
        self.parked = {str(n).strip().lower() for n in (self.parked or set())}

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

        and either singular or plural, because a vendor account that has run
        out of credit is only recoverable mid-run if a second one was named::

            {"deepseek": {"api_keys": ["sk-first", "sk-second"]}}

        An entry may also be switched off, keeping its keys for later::

            {"deepseek": {"api_key": "sk-...", "enabled": false}}
        """
        keys: dict[str, list[str]] = {}
        parked: set[str] = set()

        # The environment is the weakest source, so it is read first and any
        # file entry overwrites it.
        for name, provider in PROVIDERS.items():
            from_env = os.environ.get(provider.key_env)
            if from_env and from_env.strip():
                keys[name] = [from_env.strip()]

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
                enabled = _entry_enabled(entry)
                if not enabled:
                    parked.add(name)
                # A parked entry is allowed to carry no key at all: parking is
                # a decision that can be taken before a key ever exists, and
                # demanding one would make "off" harder to write than "on".
                entry_keys = _entry_keys(path, name, entry, required=enabled)
                if entry_keys:
                    keys[name] = entry_keys
            _warn_if_world_readable(path)

        if keys:
            logger.info(
                "Provider keys available for: %s",
                ", ".join(
                    f"{name} ({len(k)} keys)" if len(k) > 1 else name
                    for name, k in sorted(keys.items())
                ),
            )
        if parked:
            logger.info("Providers parked (skipped until re-enabled): %s",
                        ", ".join(sorted(parked)))
        return cls(keys=keys, parked=parked)

    # --- Keys in use ------------------------------------------------------

    def key_for(self, name: str) -> Optional[str]:
        """The key a call to ``name`` should use right now, if any is left.

        Parked vendors return None here, which is what makes one switch enough:
        every caller already handles "no key available" by falling back to
        Anthropic, so nothing else has to learn about parking to respect it.
        """
        if self.is_parked(name):
            return None
        pool = self.keys.get(name) or []
        index = self._index.get(name, 0)
        if name in self._spent or index >= len(pool):
            return None
        return pool[index]

    def has(self, name: str) -> bool:
        return self.key_for(name) is not None

    def is_parked(self, name: str) -> bool:
        """True when this vendor is switched off in the providers file."""
        return (name or "").strip().lower() in self.parked

    def key_count(self, name: str) -> int:
        """How many keys are configured for ``name``, spent ones included."""
        return len(self.keys.get(name) or [])

    def is_out_of_credit(self, name: str) -> bool:
        """True once every configured key for ``name`` has refused for money."""
        return name in self._spent

    def mark_out_of_credit(self, name: str) -> Optional[str]:
        """Retire the key in use for ``name`` and hand back the next one.

        ``None`` means there is no next one, i.e. the vendor is unusable for the
        rest of this run and blocks marked for it belong on Anthropic.
        """
        pool = self.keys.get(name) or []
        index = self._index.get(name, 0) + 1
        self._index[name] = index
        if index >= len(pool):
            self._spent.add(name)
            logger.warning(
                "Every %s key (%d) is out of credit; blocks marked for it will "
                "run on Anthropic instead.", name, len(pool),
            )
            return None
        logger.info(
            "Rotated %s to key %d of %d after an out-of-credit refusal",
            name, index + 1, len(pool),
        )
        return pool[index]

    def restore_credit(self) -> list[str]:
        """Let every retired key back in, and say which vendors those were.

        Called after the run has sat out a quota wait, which is measured in
        hours and is exactly when a human who saw the "out of credit"
        notification may have topped the account up. The cost of being wrong is
        one refused request per vendor, which returns instantly and bills
        nothing; the cost of not asking is a whole night of cheap blocks
        spending Claude quota.
        """
        revived = sorted(self._spent)
        self._index.clear()
        self._spent.clear()
        return revived


def _entry_keys(
    path: Path, name: str, entry: object, required: bool = True
) -> list[str]:
    """The keys one providers-file entry declares, in the order written."""
    if isinstance(entry, dict):
        raw = entry.get("api_keys", entry.get("api_key"))
    else:
        raw = entry
    candidates = [raw] if isinstance(raw, str) else list(raw or []) if isinstance(raw, list) else []
    keys = [k.strip() for k in candidates if isinstance(k, str) and k.strip()]
    if not keys and required:
        raise ProvidersError(
            f"{path}: provider {name!r} has no 'api_key' (or 'api_keys')."
        )
    return keys


def _entry_enabled(entry: object) -> bool:
    """Read a provider entry's on/off state, accepting either spelling.

    Mirrors `accounts._entry_enabled` deliberately, down to which key wins:
    the two files are edited by the same person on the same evening, and a
    provider that honoured only one of `enabled`/`disabled` would be a trap.
    Anything unparseable counts as enabled, which is also the harmless
    direction here -- a block still only reaches a vendor by asking for it.
    """
    if not isinstance(entry, dict):
        return True
    if "enabled" in entry:
        return bool(entry.get("enabled"))
    if "disabled" in entry:
        return not bool(entry.get("disabled"))
    return True


def set_enabled(
    name: str,
    enabled: bool,
    path: Optional[Path] = None,
) -> bool:
    """Park an external provider, or put it back. Returns True if that changed.

    The counterpart to `accounts.set_enabled`, and written back to the same
    place the keys live for the same reason: parking is an instruction, so it
    has to outlive the run that heard it. A state kept only in run-state would
    quietly evaporate the next time `.sandglass/` was cleaned, and the vendor
    would start taking work again with nobody having said so.

    Unlike the account pool there is **no last-one-standing guard**: every
    provider being off is a perfectly coherent state -- it just means every
    block runs on Anthropic, which is what an unconfigured machine does anyway.

    Parking keeps the keys. Re-enabling is a switch, never a re-paste, so
    turning a vendor off for a week costs nothing to undo.
    """
    name = (name or "").strip().lower()
    if name not in PROVIDERS:
        raise ProvidersError(
            f"Unknown provider {name!r}. Known: {', '.join(sorted(PROVIDERS))}."
        )

    path = path or ProviderRegistry.default_path()
    raw: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProvidersError(f"Could not read {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ProvidersError(f"{path}: expected a JSON object at the top level.")
        raw = loaded

    # Preserve whichever shape the file is already in, exactly as
    # `providers set` does, so this never silently rewrites a hand-authored
    # file into the other one.
    entries = raw["providers"] if isinstance(raw.get("providers"), dict) else raw
    entry = entries.get(name)
    if isinstance(entry, str):
        # A bare `"deepseek": "sk-..."` has nowhere to hang a flag, so it is
        # widened to the object form -- keeping the key, which is the whole
        # point of parking rather than deleting.
        entry = {"api_key": entry}
    elif not isinstance(entry, dict):
        # Parking a vendor before its key exists is legitimate: "never send
        # anything there" is a decision, not a configuration step.
        entry = {}

    if _entry_enabled(entry) == enabled and name in entries:
        return False

    entry["enabled"] = enabled
    # Never leave both spellings behind -- a stale `disabled` that disagrees
    # with the `enabled` just written is a bug waiting for the next reader.
    entry.pop("disabled", None)
    entries[name] = entry

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _atomic_write_json(path, raw)
    except AccountsError as exc:  # shared writer, module-local error type
        raise ProvidersError(str(exc)) from exc
    logger.info("%s provider %r in %s", "Enabled" if enabled else "Parked", name, path)
    return True


def get(name: Optional[str]) -> Optional[Provider]:
    """The provider called ``name``, or None for Anthropic/unknown names."""
    if not name:
        return None
    return PROVIDERS.get(name.strip().lower())


def provider_for_model(model: Optional[str]) -> Optional[Provider]:
    """Which provider a model name unambiguously belongs to, if any.

    This is what lets `model: deepseek-pro` route on its own, with no separate
    marker: naming a vendor's model *is* choosing that vendor, and making
    someone say it twice is a rule they will eventually forget on the one block
    where it matters.

    Matched on the **vendor prefix only** — `deepseek-anything` — never on the
    tier map. The tier map deliberately contains bare words like `pro`, `opus`
    and `haiku` so they can be resolved once a provider is already chosen; if
    they were matched here instead, an ordinary `model: opus` block would route
    itself to DeepSeek. Prefix matching also survives the vendor shipping a
    model Sandglass has never heard of.
    """
    if not model:
        return None
    key = model.strip().lower()
    for provider in PROVIDERS.values():
        if key == provider.name or key.startswith(f"{provider.name}-"):
            return provider
    return None


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

"""Several Claude subscriptions, one queue, one at a time.

A single subscription's quota is a hard ceiling on how much an unattended run
can get through: when it lands, `sandglass execute` sleeps until the window
refreshes, which on a long queue means most of a night spent idle. Someone
holding more than one subscription can carry on under the next one instead of
waiting.

**These must be accounts you hold yourself.** Anthropic's Consumer Terms
forbid sharing credentials or making an account available to anyone else, so a
pool assembled from other people's logins is exactly the thing that is not
allowed — the number of accounts is not what matters, whose they are is.

Two deliberate design choices, both about blast radius:

- **The token file lives outside the repository.** `sandglass execute` runs
  blocks with `bypassPermissions` in the project directory, so any block can
  read any file there without being asked, and block output is persisted
  verbatim to `.sandglass/responses/`. A credential inside the project tree is
  therefore one incurious `cat` away from being written into a response file
  that outlives the run. Outside the tree it is not reachable that way.
- **Tokens are never logged, printed, or put in the run report.** Only the
  account's `name` appears anywhere a human or a file can see, which is why
  accounts have a name at all.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Where the pool is read from, unless SANDGLASS_ACCOUNTS names another path.
# Deliberately under the user's home directory rather than the project: see the
# module docstring for why "somewhere a block can't read" is the requirement.
DEFAULT_ACCOUNTS_FILENAME = "accounts.json"

# The environment variable `claude` reads a long-lived subscription token from
# (minted by `claude setup-token`). Setting it per-subprocess is what makes a
# switch take effect without touching the machine's own logged-in account --
# the interactive `claude` you use by hand is left completely alone.
TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

# Stripped from the environment of every rotated subprocess. If it survives, it
# outranks the subscription token and the run silently bills pay-per-token API
# credits instead -- which defeats the entire purpose of the pool and is the
# kind of mistake whose first symptom is an invoice.
CONFLICTING_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# How long an account is considered spent when the CLI reports a quota hit but
# gives no reset time. Long enough not to thrash back onto a dead account,
# short enough that a full pool isn't written off for the night.
DEFAULT_COOLDOWN_SECONDS = 60 * 60


class AccountsError(RuntimeError):
    """The accounts file exists but could not be used as written."""


@dataclass
class Account:
    """One subscription in the pool."""

    name: str
    token: str
    # Epoch seconds until which this account is considered spent. None means
    # available. Set when the CLI reports a quota hit, from the reset time it
    # supplies -- so the pool knows not just that an account is done, but when
    # it comes back.
    exhausted_until: Optional[float] = None
    # Whether the pool may draw on this account at all. Distinct from
    # `exhausted_until` on purpose: exhaustion is transient and clears itself
    # when the window refreshes, whereas disabled is a standing decision by a
    # human and is undone only by another one. Persisted in the accounts file
    # (see `set_enabled`), because "skip this one until I say otherwise" has to
    # survive the run that was told it.
    enabled: bool = True
    # Per-run accounting, reported at the end so a rotated run can be read as
    # "what did each account actually do" rather than one merged total.
    blocks: int = 0
    tokens: int = 0
    cost_usd: float = 0.0

    def is_available(self, now: Optional[float] = None) -> bool:
        if not self.enabled:
            return False
        if self.exhausted_until is None:
            return True
        return (now if now is not None else time.time()) >= self.exhausted_until

    def __repr__(self) -> str:  # pragma: no cover - defensive, not logic
        # A token must not reach a traceback, a log line, or a debugger's
        # locals dump. The default dataclass repr would print it in all three.
        if not self.enabled:
            state = "disabled"
        else:
            state = "available" if self.is_available() else "exhausted"
        return f"<Account {self.name!r} {state}>"


@dataclass
class AccountPool:
    """The accounts a run may draw on, and which one it is on now."""

    accounts: list[Account]
    index: int = 0
    # Names in the order they were switched to, including the first. The run
    # report renders this so a finished run says which accounts it touched.
    history: list[str] = field(default_factory=list)
    # Where exhaustion timestamps persist between runs. Set by the engine from
    # StorageService; None keeps the pool purely in-memory (used by tests).
    state_path: Optional[str] = None

    def __post_init__(self) -> None:
        if self.accounts and not self.history:
            self.history.append(self.accounts[self.index].name)

    # --- Persisted exhaustion state --------------------------------------

    def load_state(self) -> None:
        """Re-apply exhaustion times recorded by an earlier run.

        Without this a restarted run sees a full pool, starts at the first
        account, and spends a real request rediscovering a quota the previous
        process had already found — once per dead account, every restart.
        Times already in the past are ignored, so a stale file self-clears.
        """
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            # Never fatal: the worst case without it is the wasted probe this
            # is here to avoid, which is not worth failing a run over.
            logger.warning("Could not read %s: %s", self.state_path, exc)
            return

        now = time.time()
        restored = 0
        for account in self.accounts:
            until = saved.get(account.name)
            if isinstance(until, (int, float)) and until > now:
                account.exhausted_until = float(until)
                restored += 1
        if restored:
            logger.info(
                "Restored %d exhausted account(s) from %s", restored, self.state_path
            )
        # Start on an account that actually has quota, rather than burning the
        # first block of the run proving that account one is still spent.
        if self.current is not None and not self.current.is_available(now):
            self.advance()

    def save_state(self) -> None:
        """Write the exhaustion times. Names and epochs only — never tokens."""
        if not self.state_path:
            return
        payload = {
            a.name: a.exhausted_until
            for a in self.accounts
            if a.exhausted_until is not None
        }
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        except OSError as exc:
            logger.warning("Could not write %s: %s", self.state_path, exc)

    # --- Loading ---------------------------------------------------------

    @classmethod
    def default_path(cls) -> Path:
        override = os.environ.get("SANDGLASS_ACCOUNTS")
        if override:
            return Path(override).expanduser()
        return Path.home() / ".sandglass" / DEFAULT_ACCOUNTS_FILENAME

    @classmethod
    def load(cls, path: Optional[Path] = None) -> Optional["AccountPool"]:
        """Read the pool, or return None when no accounts file exists.

        A missing file is the normal case, not an error: rotation is opt-in
        and most runs use the machine's own logged-in account. A file that
        exists but is malformed *is* an error -- silently falling back to
        single-account mode there would hide a typo behind a night of
        unexplained quota waits.
        """
        path = path or cls.default_path()
        if not path.exists():
            return None

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AccountsError(f"Could not read {path}: {exc}") from exc

        entries = raw.get("accounts") if isinstance(raw, dict) else raw
        if not isinstance(entries, list) or not entries:
            raise AccountsError(
                f"{path} has no 'accounts' list. Expected "
                '{"accounts": [{"name": "...", "token": "..."}]}'
            )

        accounts: list[Account] = []
        seen: set[str] = set()
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise AccountsError(f"{path}: account #{i + 1} is not an object.")
            name = str(entry.get("name") or f"account-{i + 1}")
            token = entry.get("token")
            if not token or not isinstance(token, str):
                raise AccountsError(
                    f"{path}: account {name!r} has no 'token'. Mint one with "
                    "`claude setup-token`."
                )
            if name in seen:
                raise AccountsError(f"{path}: duplicate account name {name!r}.")
            seen.add(name)
            accounts.append(
                Account(name=name, token=token, enabled=_entry_enabled(entry))
            )

        # A pool where every account is switched off is not a degraded pool, it
        # is an unusable one: `advance()` would find nothing and `earliest_reset`
        # has no time to offer, so the engine would wait for a refresh that is
        # never coming. Say so at load, where the fix is obvious.
        if not any(a.enabled for a in accounts):
            raise AccountsError(
                f"{path}: every account is disabled, so no block could run. "
                "Re-enable one with `sandglass accounts --enable <name>`."
            )

        # Start on an account that may actually be used. Exhaustion is applied
        # later by `load_state`, but disabled is known right now, and beginning
        # on a disabled account would put its name in `history` as though the
        # run had touched it.
        index = next(i for i, a in enumerate(accounts) if a.enabled)

        _warn_if_world_readable(path)
        disabled = [a.name for a in accounts if not a.enabled]
        logger.info(
            "Loaded %d account(s) from %s%s",
            len(accounts), path,
            f" ({len(disabled)} disabled: {', '.join(disabled)})" if disabled else "",
        )
        return cls(accounts=accounts, index=index)

    # --- Current account -------------------------------------------------

    @property
    def current(self) -> Optional[Account]:
        if not self.accounts:
            return None
        return self.accounts[self.index]

    @property
    def current_name(self) -> str:
        account = self.current
        return account.name if account else "default"

    # --- Rotation --------------------------------------------------------

    def mark_exhausted(self, resets_at: Optional[float] = None) -> None:
        """Record that the account in use has hit its quota."""
        account = self.current
        if account is None:
            return
        account.exhausted_until = (
            float(resets_at) if resets_at else time.time() + DEFAULT_COOLDOWN_SECONDS
        )
        logger.info(
            "Account %s marked exhausted until %s",
            account.name, account.exhausted_until,
        )
        self.save_state()

    def advance(self) -> Optional[Account]:
        """Move to the next account with quota left, if there is one.

        Walks forward from the current position so accounts are used in file
        order and each is drained before the next is touched -- one account
        deeply cached beats several shallowly cached, since a switch costs a
        cold prompt cache on the receiving account.
        """
        now = time.time()
        for offset in range(1, len(self.accounts) + 1):
            candidate = (self.index + offset) % len(self.accounts)
            if self.accounts[candidate].is_available(now):
                self.index = candidate
                self.history.append(self.accounts[candidate].name)
                return self.accounts[candidate]
        return None

    def earliest_reset(self) -> Optional[float]:
        """When the first exhausted account comes back, in epoch seconds.

        Disabled accounts are excluded: their quota may well refresh at a known
        time, but the pool still won't use them, so counting one here would make
        the engine wake up to an account it is not allowed to touch.
        """
        times = [
            a.exhausted_until
            for a in self.accounts
            if a.exhausted_until and a.enabled
        ]
        return min(times) if times else None

    def clear_expired(self) -> None:
        """Let accounts whose reset time has passed back into the rotation."""
        now = time.time()
        changed = False
        for account in self.accounts:
            if account.exhausted_until and now >= account.exhausted_until:
                account.exhausted_until = None
                changed = True
        if changed:
            self.save_state()

    # --- Accounting ------------------------------------------------------

    def record_usage(self, tokens: int, cost_usd: float) -> None:
        account = self.current
        if account is None:
            return
        account.blocks += 1
        account.tokens += tokens
        account.cost_usd += cost_usd

    def usage_summary(self) -> list[dict]:
        """Per-account totals for the run report. Never includes tokens."""
        return [
            {
                "name": a.name,
                "blocks": a.blocks,
                "tokens": a.tokens,
                "cost_usd": round(a.cost_usd, 4),
            }
            for a in self.accounts
            if a.blocks
        ]


def looks_malformed(token: str) -> Optional[str]:
    """Why a token can't possibly be valid, or None if it might be.

    Deliberately weak. `claude auth status` cannot help here -- it reports
    `loggedIn: true` for the literal string "x", because it only notices that
    the environment variable is set and never checks it against anything. So
    the only real test is a live request (see `probe`), and the only thing
    worth doing for free is catching the two paste errors that produce a
    token which is obviously not one. No prefix is asserted: the issuing
    format is Claude Code's to change, and a wrong guess here would reject
    good tokens, which is worse than accepting bad ones.
    """
    if not token or not token.strip():
        return "empty"
    if any(ch.isspace() for ch in token):
        return "contains whitespace — probably a partial or wrapped paste"
    if "…" in token or "..." in token:
        return "contains an ellipsis — this looks like the placeholder from the docs"
    if len(token) < 20:
        return f"only {len(token)} characters — probably truncated"
    return None


def probe_command(cli_path: str) -> list[str]:
    """The cheapest real request that proves a token works.

    Validity cannot be checked offline, so this spends a live request -- kept
    as small as possible. It is run from an empty directory by the caller so
    the project's CLAUDE.md is not auto-discovered: in this repo that context
    alone has been measured at ~23,000 billed tokens, which would make
    "check my tokens" cost more than the blocks it protects.
    """
    return [
        cli_path, "-p",
        "--no-session-persistence",
        "--model", "claude-haiku-4-5",
        "--", "Reply with exactly: OK",
    ]


def subprocess_env(token: Optional[str]) -> Optional[dict]:
    """The environment a rotated `claude` subprocess should run under.

    Returns None when there is no token to inject, so the caller inherits the
    parent environment untouched and single-account runs behave exactly as
    they did before rotation existed.
    """
    if not token:
        return None
    env = dict(os.environ)
    env[TOKEN_ENV_VAR] = token
    for name in CONFLICTING_ENV_VARS:
        env.pop(name, None)
    return env


def _warn_if_world_readable(path: Path) -> None:
    """Say so if the token file is readable by other users on this machine.

    POSIX only -- Windows ACLs aren't expressible in the st_mode bits and
    guessing at them would produce a warning that is wrong more often than
    right, which teaches people to ignore warnings.
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


def _entry_enabled(entry: dict) -> bool:
    """Read an account entry's on/off state, accepting either spelling.

    `{"enabled": false}` and `{"disabled": true}` are both obvious things to
    write by hand, so both are honoured. `enabled` wins when a file somehow
    carries both, and anything unparseable counts as enabled -- the safe
    direction, since a typo that silently benched an account would look exactly
    like the quota problems this pool exists to solve.
    """
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
    """Flip one account's `enabled` flag in the accounts file, in place.

    Returns True if the file changed, False if it already said that.

    Written back to the accounts file rather than to the run-state file next to
    the exhaustion times, because the two answer different questions. Exhaustion
    is a fact about the last few hours that expires on its own; disabled is an
    instruction, and an instruction that evaporated when the state file was
    cleaned would be worse than not having the command at all.

    The write is careful in two ways that matter for a file holding live
    credentials: it is read-modify-write on the parsed JSON, so every key it
    doesn't understand (tokens included) is carried through untouched, and the
    replacement is staged in a 0600 temp file in the same directory and moved
    into place with `os.replace`, so an interrupted write can never leave a
    truncated token file behind.
    """
    path = path or AccountPool.default_path()
    if not path.exists():
        raise AccountsError(
            f"No accounts file at {path}. There is nothing to enable or disable."
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AccountsError(f"Could not read {path}: {exc}") from exc

    entries = raw.get("accounts") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        raise AccountsError(f"{path} has no 'accounts' list.")

    names = [
        str(e.get("name") or f"account-{i + 1}")
        for i, e in enumerate(entries)
        if isinstance(e, dict)
    ]
    if name not in names:
        raise AccountsError(
            f"No account named {name!r} in {path}. Known: {', '.join(names)}."
        )

    target = names.index(name)
    if not enabled:
        # Refuse to switch off the last one standing. `AccountPool.load` treats
        # an all-disabled file as an error, so allowing it here would produce a
        # file that the next run cannot start from -- and the person who ran
        # this command is not the one who would have to work out why.
        still_on = [
            n
            for i, n in enumerate(names)
            if _entry_enabled(entries[i]) and i != target
        ]
        if not still_on:
            raise AccountsError(
                f"{name!r} is the only enabled account left; disabling it would "
                "leave the pool with nothing to run on. Enable another first."
            )

    entry = entries[target]
    if _entry_enabled(entry) == enabled:
        return False

    entry["enabled"] = enabled
    # Never leave both spellings behind: a stale `disabled` key that disagrees
    # with the one just written is a bug waiting for whoever reads the file next.
    entry.pop("disabled", None)

    _atomic_write_json(path, raw)
    logger.info("%s account %r in %s", "Enabled" if enabled else "Disabled", name, path)
    return True


def _atomic_write_json(path: Path, payload: object) -> None:
    """Replace ``path`` with ``payload`` as JSON, without ever truncating it."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        # 0600 from creation, not chmod-after: on POSIX the file must never
        # exist, even briefly, in a mode another user could read a token from.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(str(tmp), str(path))
    except OSError as exc:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise AccountsError(f"Could not write {path}: {exc}") from exc

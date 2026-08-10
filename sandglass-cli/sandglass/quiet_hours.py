"""Quiet hours — suppress push notifications while you're asleep.

Sandglass exists to run unattended for hours, which means a quota wait
routinely spans the middle of the night. Every ntfy notification is a phone
buzzing on a nightstand at 3am for something you cannot act on until morning,
so notifications are silenced between the configured hours — 22:00–06:00 by
default, on out of the box.

**Stored globally, unlike every other setting.** `queue_source` lives in the
per-directory `.sandglass/settings.json` because it describes a project. Sleep
hours describe the *person*: you sleep the same hours whichever directory you
launched a run from, and having to set them again per project would guarantee
the one run that wakes you is the one in the directory you forgot. So these
live in `~/.sandglass/settings.json` instead.

`SANDGLASS_QUIET_HOURS` overrides the file when set (`"22:00-06:00"`, or
`"off"` to disable) — the same env-var-first philosophy as the ntfy topic
itself, so a headless/CI run can opt out without touching a home directory it
may not even have.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime

from .storage import StorageService

logger = logging.getLogger(__name__)

QUIET_HOURS_ENV = "SANDGLASS_QUIET_HOURS"
SETTINGS_KEY = "quiet_hours"

DEFAULT_START = "22:00"
DEFAULT_END = "06:00"

MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class QuietHours:
    """A resolved quiet-hours window, as minutes-since-midnight local time."""

    start: int
    end: int
    enabled: bool = True

    @property
    def start_text(self) -> str:
        return format_minutes(self.start)

    @property
    def end_text(self) -> str:
        return format_minutes(self.end)

    def contains(self, minutes: int) -> bool:
        """Whether ``minutes`` (since local midnight) falls inside the window.

        A window that wraps past midnight (the normal case — 22:00 to 06:00)
        is two ranges, not one, which is why this can't be a plain ``<=``.
        """
        if not self.enabled or self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= minutes < self.end
        return minutes >= self.start or minutes < self.end


def parse_time(value: str | int) -> int:
    """Parse ``22``, ``"22"``, ``"22:00"`` or ``"6:30"`` into minutes past midnight.

    Bare hours are accepted because that is how the window is spoken about
    ("22 to 6"); typing ``:00`` twice for the common case would be noise.

    Raises ValueError on anything unparseable, so the CLI can report it.
    """
    text = str(value).strip()
    if not text:
        raise ValueError("empty time")

    if ":" in text:
        hour_text, _, minute_text = text.partition(":")
    else:
        hour_text, minute_text = text, "0"

    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        raise ValueError(f"'{value}' is not a time — use 22, 22:00 or 6:30") from None

    if not 0 <= hour <= 23:
        raise ValueError(f"hour {hour} is out of range (0-23)")
    if not 0 <= minute <= 59:
        raise ValueError(f"minute {minute} is out of range (0-59)")
    return hour * 60 + minute


def format_minutes(minutes: int) -> str:
    """Render minutes-past-midnight as ``HH:MM``."""
    minutes %= MINUTES_PER_DAY
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def global_settings_path() -> str:
    """`~/.sandglass/settings.json` — deliberately NOT the per-directory one."""
    return os.path.join(os.path.expanduser("~"), ".sandglass", "settings.json")


def _from_env() -> QuietHours | None:
    """Parse `SANDGLASS_QUIET_HOURS`, or None if unset/unusable.

    A malformed value is ignored (with a warning) rather than raising: this is
    read on the notification path of a long unattended run, where a typo in an
    env var must never be able to take the run down.
    """
    raw = os.environ.get(QUIET_HOURS_ENV)
    if raw is None:
        return None
    raw = raw.strip()
    if raw.lower() in {"off", "none", "disabled", ""}:
        return QuietHours(parse_time(DEFAULT_START), parse_time(DEFAULT_END), enabled=False)
    start_text, sep, end_text = raw.partition("-")
    if not sep:
        logger.warning("%s=%r is not a range like '22:00-06:00'; ignoring.", QUIET_HOURS_ENV, raw)
        return None
    try:
        return QuietHours(parse_time(start_text), parse_time(end_text), enabled=True)
    except ValueError as exc:
        logger.warning("%s=%r is invalid (%s); ignoring.", QUIET_HOURS_ENV, raw, exc)
        return None


def load() -> QuietHours:
    """The window in effect: env var if set, else the global settings file,
    else the 22:00–06:00 default."""
    from_env = _from_env()
    if from_env is not None:
        return from_env

    settings = StorageService().load_json(global_settings_path())
    stored = settings.get(SETTINGS_KEY) if isinstance(settings, dict) else None
    if not isinstance(stored, dict):
        return QuietHours(parse_time(DEFAULT_START), parse_time(DEFAULT_END))

    try:
        start = parse_time(stored.get("start", DEFAULT_START))
        end = parse_time(stored.get("end", DEFAULT_END))
    except ValueError as exc:
        logger.warning("Invalid quiet_hours in %s (%s); using defaults.", global_settings_path(), exc)
        start, end = parse_time(DEFAULT_START), parse_time(DEFAULT_END)
    return QuietHours(start, end, enabled=bool(stored.get("enabled", True)))


def save(start: int, end: int, enabled: bool = True) -> QuietHours:
    """Persist the window to the global settings file and return it."""
    path = global_settings_path()
    storage = StorageService()
    settings = storage.load_json(path)
    if not isinstance(settings, dict):
        settings = {}
    settings[SETTINGS_KEY] = {
        "start": format_minutes(start),
        "end": format_minutes(end),
        "enabled": enabled,
    }
    storage.save_json(path, settings)
    return QuietHours(start, end, enabled)


def is_quiet(now: datetime | None = None) -> bool:
    """Whether notifications should be held right now (local time).

    Never raises — a broken config silences nothing rather than breaking the
    run that was trying to notify.
    """
    try:
        moment = now or datetime.now()
        return load().contains(moment.hour * 60 + moment.minute)
    except Exception as exc:  # noqa: BLE001 — a notification must never break a run
        logger.warning("Quiet-hours check failed (%s); notifying anyway.", exc)
        return False

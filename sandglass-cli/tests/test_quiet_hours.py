from datetime import datetime

import pytest

from sandglass import quiet_hours


@pytest.fixture(autouse=True)
def _isolated_global_settings(tmp_path, monkeypatch):
    """Point the global settings file at a temp dir and clear the env override,
    so a test never reads or writes the developer's real ~/.sandglass/."""
    monkeypatch.delenv(quiet_hours.QUIET_HOURS_ENV, raising=False)
    monkeypatch.setattr(
        quiet_hours, "global_settings_path", lambda: str(tmp_path / "settings.json")
    )


# --- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [("22", 22 * 60), (22, 22 * 60), ("22:00", 22 * 60), ("6", 6 * 60), ("06:30", 6 * 60 + 30), ("0:00", 0)],
)
def test_parse_time_accepts_bare_hours_and_hh_mm(value, expected):
    assert quiet_hours.parse_time(value) == expected


@pytest.mark.parametrize("value", ["", "abc", "25", "22:60", "-1", "22:xx"])
def test_parse_time_rejects_nonsense(value):
    with pytest.raises(ValueError):
        quiet_hours.parse_time(value)


def test_format_minutes_pads_to_hh_mm():
    assert quiet_hours.format_minutes(6 * 60) == "06:00"
    assert quiet_hours.format_minutes(22 * 60 + 5) == "22:05"


# --- the window ------------------------------------------------------------


def test_window_wrapping_midnight_covers_both_sides():
    window = quiet_hours.QuietHours(22 * 60, 6 * 60)

    assert window.contains(23 * 60) is True  # before midnight
    assert window.contains(3 * 60) is True  # after midnight
    assert window.contains(22 * 60) is True  # inclusive start
    assert window.contains(6 * 60) is False  # exclusive end
    assert window.contains(12 * 60) is False  # midday


def test_window_not_wrapping_midnight_is_a_plain_range():
    window = quiet_hours.QuietHours(9 * 60, 17 * 60)

    assert window.contains(12 * 60) is True
    assert window.contains(3 * 60) is False


def test_disabled_window_contains_nothing():
    window = quiet_hours.QuietHours(22 * 60, 6 * 60, enabled=False)

    assert window.contains(23 * 60) is False


def test_zero_length_window_contains_nothing():
    window = quiet_hours.QuietHours(22 * 60, 22 * 60)

    assert window.contains(22 * 60) is False


# --- persistence -----------------------------------------------------------


def test_default_window_is_22_to_6_and_enabled():
    window = quiet_hours.load()

    assert (window.start_text, window.end_text, window.enabled) == ("22:00", "06:00", True)


def test_save_then_load_round_trips():
    quiet_hours.save(23 * 60 + 30, 7 * 60)

    window = quiet_hours.load()
    assert (window.start_text, window.end_text, window.enabled) == ("23:30", "07:00", True)


def test_save_preserves_other_keys_in_the_global_settings_file(tmp_path):
    from sandglass.storage import StorageService

    path = quiet_hours.global_settings_path()
    StorageService().save_json(path, {"something_else": "keep me"})

    quiet_hours.save(22 * 60, 6 * 60)

    assert StorageService().load_json(path)["something_else"] == "keep me"


def test_corrupt_stored_window_falls_back_to_defaults_instead_of_raising():
    from sandglass.storage import StorageService

    StorageService().save_json(
        quiet_hours.global_settings_path(), {"quiet_hours": {"start": "nope", "end": "06:00"}}
    )

    window = quiet_hours.load()
    assert (window.start_text, window.end_text) == ("22:00", "06:00")


# --- env override ----------------------------------------------------------


def test_env_var_overrides_the_saved_window(monkeypatch):
    quiet_hours.save(22 * 60, 6 * 60)
    monkeypatch.setenv(quiet_hours.QUIET_HOURS_ENV, "01:00-02:00")

    window = quiet_hours.load()
    assert (window.start_text, window.end_text) == ("01:00", "02:00")


def test_env_var_off_disables_quiet_hours(monkeypatch):
    monkeypatch.setenv(quiet_hours.QUIET_HOURS_ENV, "off")

    assert quiet_hours.load().enabled is False


def test_malformed_env_var_is_ignored_rather_than_fatal(monkeypatch):
    quiet_hours.save(23 * 60, 7 * 60)
    monkeypatch.setenv(quiet_hours.QUIET_HOURS_ENV, "garbage")

    window = quiet_hours.load()
    assert (window.start_text, window.end_text) == ("23:00", "07:00")


# --- is_quiet --------------------------------------------------------------


def test_is_quiet_uses_local_wall_clock_time():
    quiet_hours.save(22 * 60, 6 * 60)

    assert quiet_hours.is_quiet(datetime(2026, 8, 9, 23, 30)) is True
    assert quiet_hours.is_quiet(datetime(2026, 8, 9, 3, 0)) is True
    assert quiet_hours.is_quiet(datetime(2026, 8, 9, 14, 0)) is False


def test_is_quiet_never_raises_on_a_broken_config(monkeypatch):
    monkeypatch.setattr(quiet_hours, "load", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert quiet_hours.is_quiet() is False  # fails open -- notifying beats crashing

"""Tests for the static HTML status page (`sandglass dashboard`).

Assertions stay at the level of "does the generated page say the right
thing" rather than pinning exact markup, since the visual design is free to
change -- what must not silently break is that the numbers and labels shown
are the ones the underlying data actually has.
"""

from __future__ import annotations

import os

import pytest

from sandglass import dashboard, run_report
from sandglass.storage import StorageService


@pytest.fixture
def storage(tmp_path):
    return StorageService(base_path=str(tmp_path / ".sandglass"))


def _source(tmp_path):
    return str(tmp_path / "prompt_tools" / "future_prompts.md")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_generate_reports_no_data_yet(tmp_path, storage):
    html = dashboard.generate(_source(tmp_path), "My Project", storage=storage)
    assert "My Project" in html
    assert "No blocks queued or completed yet" in html


def test_generate_shows_overall_progress(tmp_path, storage):
    source = _source(tmp_path)
    _write(source, "First block\n\n====\n\nSecond block\n")
    _write(
        os.path.join(os.path.dirname(source), "prompt_history.md"),
        "# History\n\n---\n\n## Done one\n\nstuff\n",
    )

    html = dashboard.generate(source, "My Project", storage=storage)

    assert "33%" in html
    assert ">1<" in html  # done
    assert ">2<" in html  # remaining
    assert ">3<" in html  # total


def test_generate_omits_phases_section_when_no_block_declares_one(tmp_path, storage):
    source = _source(tmp_path)
    _write(source, "Just a plain block\n")

    html = dashboard.generate(source, "My Project", storage=storage)

    assert "Phases" not in html


def test_generate_includes_phases_when_declared(tmp_path, storage):
    source = _source(tmp_path)
    _write(source, "phase: Phase 1\n\nStill queued\n")
    _write(
        os.path.join(os.path.dirname(source), "prompt_history.md"),
        "# History\n\n---\n\n## Done one\n\n**Executed:** 2026-08-14\n\n```\n"
        "============================\n\nphase: Phase 1\n\nDo the thing.\n\n"
        "============================\n```\n",
    )

    html = dashboard.generate(source, "My Project", storage=storage)

    assert "Phases" in html
    assert "Phase 1" in html
    assert ">1/2<" in html


def test_generate_shows_idle_when_no_run_recorded(tmp_path, storage):
    html = dashboard.generate(_source(tmp_path), "My Project", storage=storage)
    assert "Idle" in html


def test_generate_shows_running_status(tmp_path, storage):
    run_report.save(
        storage,
        run_report.RunReport(status=run_report.STATUS_RUNNING, prompt_id="003", prompt_title="Do a thing"),
    )
    html = dashboard.generate(_source(tmp_path), "My Project", storage=storage)
    assert "Running" in html
    assert "Do a thing" in html


def test_generate_shows_quota_stop_as_a_stopped_reason_not_generic(tmp_path, storage):
    run_report.save(
        storage,
        run_report.RunReport(
            status=run_report.STATUS_STOPPED,
            reason=run_report.REASON_QUOTA,
            detail="You've hit your session limit",
            remaining=5,
        ),
    )
    html = dashboard.generate(_source(tmp_path), "My Project", storage=storage)
    assert "Quota hit" in html


def test_generate_escapes_title_and_phase_names(tmp_path, storage):
    source = _source(tmp_path)
    _write(source, "phase: <script>alert(1)</script>\n\nBlock\n")

    html = dashboard.generate(source, "<script>evil</script>", storage=storage)

    assert "<script>evil</script>" not in html
    assert "<script>alert(1)</script>" not in html


# --- hero icon --------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_icon_cache():
    """`_icon_data_uri` is `lru_cache`d across the whole test session, so a
    test that points `_ICON_PATH` at a missing file must not leak that
    negative result into every other test (or vice versa)."""
    dashboard._icon_data_uri.cache_clear()
    yield
    dashboard._icon_data_uri.cache_clear()


def test_generate_includes_the_hero_medallion_when_the_icon_is_present(tmp_path, storage):
    html_out = dashboard.generate(_source(tmp_path), "My Project", storage=storage)
    assert 'class="medallion"' in html_out
    assert "data:image/jpeg;base64," in html_out


def test_generate_omits_the_hero_gracefully_when_the_icon_is_missing(tmp_path, storage, monkeypatch):
    monkeypatch.setattr(dashboard, "_ICON_PATH", str(tmp_path / "does-not-exist.jpg"))
    dashboard._icon_data_uri.cache_clear()

    html_out = dashboard.generate(_source(tmp_path), "My Project", storage=storage)

    assert 'class="medallion"' not in html_out
    assert "My Project" in html_out  # the rest of the page still renders


def test_write_creates_file_under_sandglass_dir(tmp_path, storage):
    source = _source(tmp_path)
    _write(source, "A block\n")

    path = dashboard.write(source, "My Project", storage=storage)

    assert os.path.exists(path)
    assert path.endswith(dashboard.DASHBOARD_FILENAME)
    with open(path, encoding="utf-8") as fh:
        assert "My Project" in fh.read()


# --- External providers ------------------------------------------------------
#
# The panel exists because "what can run my blocks right now" was only half
# answered while it listed Claude accounts alone. The risk it introduces is the
# opposite reading -- that a configured provider means work is going there --
# so the card has to be unambiguous about metered and opt-in, and it must never
# render a key.


def _registry(keys):
    from sandglass.providers import ProviderRegistry

    return ProviderRegistry(keys=keys)


def test_providers_card_shows_a_configured_vendor_without_its_key():
    secret = "sk-doNotRenderThisAnywhere0123456789"
    card = dashboard._providers_card(_registry({"deepseek": [secret]}))
    assert "deepseek" in card
    assert "1 key" in card and "metered" in card
    # The whole reason the card takes counts rather than keys.
    assert secret not in card


def test_providers_card_counts_several_keys():
    card = dashboard._providers_card(
        _registry({"deepseek": ["sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                "sk-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]})
    )
    # Operationally different from one key: a mid-run balance refusal is a
    # switch rather than a fallback onto the Claude quota routing existed to save.
    assert "2 keys" in card


def test_providers_card_names_the_fix_when_no_key_is_configured():
    card = dashboard._providers_card(_registry({}))
    assert "no key" in card
    assert "sandglass providers set deepseek" in card


def test_providers_card_shows_out_of_credit_only_from_a_live_registry():
    registry = _registry({"deepseek": ["sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]})
    registry._spent.add("deepseek")
    card = dashboard._providers_card(registry)
    assert "out of credit" in card
    # Says what happens next, because waiting is not what fixes this.
    assert "fall back to Claude" in card


def test_providers_card_says_nothing_routes_there_on_its_own():
    card = dashboard._providers_card(_registry({"deepseek": ["sk-" + "c" * 32]}))
    assert "opt-in per block" in card


def test_a_broken_providers_file_costs_the_card_not_the_page(tmp_path, storage, monkeypatch):
    from sandglass import providers as providers_mod

    def _explode(path=None):
        raise providers_mod.ProvidersError("providers.json is not valid JSON")

    monkeypatch.setattr(providers_mod.ProviderRegistry, "load", staticmethod(_explode))
    assert dashboard._providers_card(None) == ""
    # And the page around it still renders.
    page = dashboard.generate(_source(tmp_path), "Test", storage=storage)
    assert "<html" in page

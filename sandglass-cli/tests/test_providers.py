"""External provider routing: marker parsing, key loading, and — above all —
what does and does not end up in the subprocess environment."""

from __future__ import annotations

import json

import pytest

from sandglass import providers
from sandglass.accounts import TOKEN_ENV_VAR
from sandglass.models import PromptObject
from sandglass.queue_manager import QueueManager
from sandglass.storage import StorageService


@pytest.fixture
def qm(tmp_path):
    return QueueManager(storage=StorageService(base_path=str(tmp_path / ".sandglass")))


# --- The environment handed to an external subprocess ---------------------
#
# This is the part with real consequences, so it is tested first and hardest.


def test_subscription_token_is_stripped_from_an_external_call(monkeypatch):
    """The single most important line in providers.py.

    Left in place, a live Claude subscription credential would be sent to a
    third-party server as a bearer token, and nothing in the run's output
    would look wrong.
    """
    monkeypatch.setenv(TOKEN_ENV_VAR, "sk-ant-oat01-a-real-subscription-token")
    env = providers.DEEPSEEK.subprocess_env("sk-deepseek-key")
    assert TOKEN_ENV_VAR not in env


def test_external_env_points_away_from_anthropic():
    env = providers.DEEPSEEK.subprocess_env("sk-deepseek-key")
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-deepseek-key"
    assert env["ANTHROPIC_API_KEY"] == "sk-deepseek-key"
    # The housekeeping model has to be one the endpoint has heard of, or the
    # CLI's own side calls fail against a vendor with no Claude models.
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "deepseek-v4-flash"


def test_external_env_keeps_the_rest_of_the_environment(monkeypatch):
    """The agent still has to be able to do work — PATH, HOME and the rest."""
    monkeypatch.setenv("SOME_UNRELATED_VAR", "keep-me")
    env = providers.DEEPSEEK.subprocess_env("sk-deepseek-key")
    assert env["SOME_UNRELATED_VAR"] == "keep-me"
    assert "PATH" in env


# --- Model resolution -----------------------------------------------------


@pytest.mark.parametrize(
    "asked,expected",
    [
        ("pro", "deepseek-v4-pro"),
        ("PRO", "deepseek-v4-pro"),
        ("flash", "deepseek-v4-flash"),
        ("opus", "deepseek-v4-pro"),       # a Claude tier name, mapped across
        ("haiku", "deepseek-v4-flash"),
        ("deepseek-v4-pro", "deepseek-v4-pro"),  # literal id, passed through
        (None, "deepseek-v4-flash"),       # unstated -> the cheap one
    ],
)
def test_resolve_model(asked, expected):
    assert providers.DEEPSEEK.resolve_model(asked) == expected


@pytest.mark.parametrize(
    "model", ["deepseek-pro", "deepseek-flash", "deepseek-v4-pro", "DeepSeek-Pro",
              "deepseek", "deepseek-v9-something-new"],
)
def test_a_vendor_prefixed_model_names_its_own_provider(model):
    """Naming a vendor's model IS choosing that vendor — the whole point of
    letting `model: deepseek-pro` route without a second marker."""
    assert providers.provider_for_model(model) is providers.DEEPSEEK


@pytest.mark.parametrize(
    "model",
    # Every one of these is in DEEPSEEK.tiers so it can be resolved *after* a
    # provider is chosen. Matching on the tier map instead of the vendor prefix
    # would send an ordinary `model: opus` block to DeepSeek.
    ["opus", "sonnet", "haiku", "pro", "flash", "cheap", "high",
     "claude-opus-4-8", "", None],
)
def test_an_anthropic_model_never_names_a_provider(model):
    assert providers.provider_for_model(model) is None


def test_an_unknown_model_name_is_forwarded_not_rejected():
    """Model names are the vendor's to change; failing a block over one Sandglass
    hasn't heard of would age badly."""
    assert providers.DEEPSEEK.resolve_model("deepseek-v5-turbo") == "deepseek-v5-turbo"


# --- Marker parsing -------------------------------------------------------


def test_cline_marker_routes_the_block(qm):
    prompt_id = qm.add_prompt(text="**CLINE: pro** — external-OK\n\nDo the thing.")
    added = qm.get_prompt(int(prompt_id))
    assert added.provider == "deepseek"
    assert added.model == "deepseek-v4-pro"


def test_external_marker_is_accepted_as_a_synonym(qm):
    added = qm.get_prompt(int(qm.add_prompt(text="**EXTERNAL: flash**\n\nDo it.")))
    assert added.provider == "deepseek"
    assert added.model == "deepseek-v4-flash"


def test_naming_the_provider_alone_takes_its_default_model(qm):
    added = qm.get_prompt(int(qm.add_prompt(text="**CLINE: deepseek**\n\nDo it.")))
    assert added.provider == "deepseek"
    assert added.model == "deepseek-v4-flash"


def test_the_model_alone_routes_the_block(qm):
    """`model: deepseek-pro` with no marker anywhere — the spelling most people
    reach for first, and the one that would otherwise fail confusingly by
    asking Claude for a model it has never heard of."""
    added = qm.get_prompt(int(qm.add_prompt(text="model: deepseek-pro\n\nDo it.")))
    assert added.provider == "deepseek"
    assert added.model == "deepseek-pro"


def test_the_model_option_alone_routes_the_block(qm):
    added = qm.get_prompt(int(qm.add_prompt(text="Do it.", model="deepseek-pro")))
    assert added.provider == "deepseek"


def test_an_ordinary_model_header_still_does_not_route(qm):
    added = qm.get_prompt(int(qm.add_prompt(text="model: opus\n\nDo it.")))
    assert added.provider is None
    assert added.model == "opus"


def test_provider_front_matter_beats_a_marker(qm):
    text = "provider: deepseek\nmodel: deepseek-v4-pro\n\n**CLINE: flash**\n\nDo it."
    added = qm.get_prompt(int(qm.add_prompt(text=text)))
    assert added.provider == "deepseek"
    assert added.model == "deepseek-v4-pro"


def test_cline_stop_is_a_refusal_not_a_routing_command(qm):
    """A human warning off external routing (`**CLINE: STOP** — Claude only`)
    must never be parsed as the routing command it names — found live on an
    Azymetrix queue where this exact phrasing sent unattended, money-path
    blocks to DeepSeek instead of keeping them on Claude."""
    text = "**CLINE: STOP** — money path, concurrency, crash recovery. Claude only.\n\nDo it."
    added = qm.get_prompt(int(qm.add_prompt(text=text)))
    assert added.provider is None


@pytest.mark.parametrize(
    "value", ["stop", "STOP", "no", "off", "none", "never", "claude-only", "disabled"]
)
def test_cline_negation_values_never_route(qm, value):
    added = qm.get_prompt(int(qm.add_prompt(text=f"**CLINE: {value}**\n\nDo it.")))
    assert added.provider is None


def test_an_ordinary_block_never_leaves_anthropic(qm):
    added = qm.get_prompt(int(qm.add_prompt(text="Add a badge to the editor.")))
    assert added.provider is None


def test_existing_external_ok_tier_markers_are_not_rerouted(qm):
    """`**TIER: CHEAP - EXTERNAL-OK**` predates external routing and appears on
    blocks written long before it existed. "EXTERNAL-OK" is the author granting
    permission, not exercising it — treating the two as the same thing would
    retroactively send a pile of old blocks to a third party on the strength of
    a comment nobody wrote with that consequence in mind."""
    added = qm.get_prompt(
        int(qm.add_prompt(text="**TIER: CHEAP - EXTERNAL-OK** - a dirty-flag badge"))
    )
    assert added.provider is None
    assert added.model == "haiku"


def test_a_marker_buried_in_prose_is_not_front_matter(qm):
    text = "Do the thing.\n\n" + ("filler. " * 60) + "\n**CLINE: pro**\n"
    added = qm.get_prompt(int(qm.add_prompt(text=text)))
    assert added.provider is None


def test_no_tiers_disables_marker_routing(qm):
    prompt_id = qm.add_prompt(text="**CLINE: pro**\n\nDo it.", use_tiers=False)
    assert qm.get_prompt(int(prompt_id)).provider is None


def test_provider_survives_a_queue_round_trip(qm):
    qm.add_prompt(text="**CLINE: pro**\n\nDo it.")
    reloaded = qm.load_queue()[0]
    assert reloaded.provider == "deepseek"
    assert PromptObject.from_dict(reloaded.to_dict()).provider == "deepseek"


# --- Key loading ----------------------------------------------------------


def test_missing_file_is_not_an_error(tmp_path, monkeypatch):
    """Rotation off, providers off: a machine that never opted in is normal."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    registry = providers.ProviderRegistry.load(tmp_path / "nope.json")
    assert registry.keys == {}
    assert not registry.has("deepseek")


def test_key_loads_from_either_file_shape(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps({"deepseek": {"api_key": "sk-flat-key-1234567890"}}))
    nested = tmp_path / "nested.json"
    nested.write_text(
        json.dumps({"providers": {"deepseek": {"api_key": "sk-nested-key-123456"}}})
    )
    assert providers.ProviderRegistry.load(flat).key_for("deepseek") == "sk-flat-key-1234567890"
    assert providers.ProviderRegistry.load(nested).key_for("deepseek") == "sk-nested-key-123456"


def test_the_file_beats_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-the-environment")
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"deepseek": {"api_key": "sk-from-the-file-123456"}}))
    assert providers.ProviderRegistry.load(path).key_for("deepseek") == "sk-from-the-file-123456"


def test_environment_is_used_when_there_is_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-the-environment")
    assert (
        providers.ProviderRegistry.load(tmp_path / "nope.json").key_for("deepseek")
        == "sk-from-the-environment"
    )


def test_a_malformed_file_is_an_error_not_a_silent_fallback(tmp_path, monkeypatch):
    """Degrading quietly would surface hours later as a block that ran on Claude
    when it was meant to run somewhere cheap — the hardest way to notice a typo."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "providers.json"
    path.write_text("{not json")
    with pytest.raises(providers.ProvidersError):
        providers.ProviderRegistry.load(path)


def test_an_entry_with_no_key_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"deepseek": {}}))
    with pytest.raises(providers.ProvidersError):
        providers.ProviderRegistry.load(path)


def test_an_unknown_provider_in_the_file_is_ignored_not_fatal(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps({
            "some-future-vendor": {"api_key": "sk-whatever-1234567890"},
            "deepseek": {"api_key": "sk-real-key-1234567890"},
        })
    )
    registry = providers.ProviderRegistry.load(path)
    assert registry.key_for("deepseek") == "sk-real-key-1234567890"
    assert registry.key_for("some-future-vendor") is None


# --- Key hygiene ----------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ("sk-" + "a" * 30, None),
        ("", "empty"),
        ("   ", "empty"),
        ("sk-abc…", "ellipsis"),
        ("sk-abc...", "ellipsis"),
        ("sk-abc def ghi jkl mno", "whitespace"),
        ("sk-short", "characters"),
    ],
)
def test_looks_malformed(key, expected):
    problem = providers.looks_malformed(key)
    if expected is None:
        assert problem is None
    else:
        assert problem is not None and expected in problem


def test_get_is_case_insensitive_and_safe_on_nonsense():
    assert providers.get("DeepSeek") is providers.DEEPSEEK
    assert providers.get("not-a-vendor") is None
    assert providers.get(None) is None
    assert providers.get("") is None


# --- Running out of credit ------------------------------------------------
#
# A metered key doesn't hit a quota, it hits a balance of zero — and no amount
# of waiting refills it. So the registry's job here is to know which key is in
# use, retire the one that just refused, and be honest about the moment there
# is nothing left to move to.


def test_several_keys_load_and_are_used_in_the_order_written(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps({"deepseek": {"api_keys": ["sk-first-1234567890", "sk-second-123456789"]}})
    )
    registry = providers.ProviderRegistry.load(path)

    assert registry.key_count("deepseek") == 2
    assert registry.key_for("deepseek") == "sk-first-1234567890"
    assert registry.mark_out_of_credit("deepseek") == "sk-second-123456789"
    assert registry.key_for("deepseek") == "sk-second-123456789"
    assert not registry.is_out_of_credit("deepseek")


def test_the_last_key_running_dry_writes_the_vendor_off_for_the_run():
    registry = providers.ProviderRegistry(keys={"deepseek": "sk-only-key-1234567890"})

    assert registry.mark_out_of_credit("deepseek") is None
    assert registry.is_out_of_credit("deepseek")
    # None, not the dead key: the engine reads this as "send it to Claude",
    # and handing back a key that just refused would loop instead.
    assert registry.key_for("deepseek") is None
    assert not registry.has("deepseek")


def test_credit_state_never_reaches_disk(tmp_path, monkeypatch):
    """An empty balance is undone by a human paying, not by time — so persisting
    it would bench a freshly-funded key on the next run for no reason."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"deepseek": {"api_key": "sk-only-key-1234567890"}}))

    spent = providers.ProviderRegistry.load(path)
    spent.mark_out_of_credit("deepseek")
    assert spent.is_out_of_credit("deepseek")

    assert providers.ProviderRegistry.load(path).key_for("deepseek") == "sk-only-key-1234567890"


def test_restore_credit_names_what_it_revived():
    registry = providers.ProviderRegistry(keys={"deepseek": ["sk-a-key-1234567890", "sk-b-key-1234567890"]})
    registry.mark_out_of_credit("deepseek")
    registry.mark_out_of_credit("deepseek")

    assert registry.restore_credit() == ["deepseek"]
    # Back to the first key, not to wherever the rotation had got to.
    assert registry.key_for("deepseek") == "sk-a-key-1234567890"
    assert registry.restore_credit() == []


def test_an_empty_key_list_is_an_error_not_a_silent_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"deepseek": {"api_keys": []}}))
    with pytest.raises(providers.ProvidersError):
        providers.ProviderRegistry.load(path)


@pytest.mark.parametrize(
    "text,credit",
    [
        # DeepSeek's own wording, verbatim from a run that stopped a queue.
        ("API Error: 402 Insufficient Balance", True),
        ("Your credit balance is too low to run this request", True),
        ("Error code: 429 - insufficient_quota", True),
        ("You've hit your weekly limit · resets Aug 20, 6am", False),
        ("rate limit exceeded", False),
        # A bare 402 with no money word is not evidence: it turns up in ids.
        ("model claude-402-preview not found", False),
    ],
)
def test_a_refusal_about_money_is_told_apart_from_one_about_speed(text, credit):
    from sandglass.claude_client import ClaudeClient

    assert ClaudeClient._looks_like_credit_error(text) is credit

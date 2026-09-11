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


# The live P19.03 block, verbatim down to the character positions: `**CLINE:
# STOP**` begins at index 290 of the body, so the old `text[:300]` slice fed the
# regex `**CLINE: STO`, and "STO" missed the negation list by one letter. It ran
# on DeepSeek -- a money-path block whose own second paragraph says "Never
# external".
_STRADDLING_BLOCK = (
    "model: sonnet\neffort: high\n\n"
    "**TIER: SONNET** — it decides whether a witnessed exit price exists at all. "
    "Too weak a model\nsubstitutes a mark for a fill, which is the one thing "
    "`_write_close`'s docstring forbids by name:\na number the operator reads as "
    '"what this trade made" beside a price the trade never exited at.\n\n'
    "**CLINE: STOP** — money path: it writes the realised result of a trade. "
    "Never external.\n\nGive the OBSERVED close path an exit price too."
)


def test_a_negation_straddling_the_scan_window_still_refuses(qm):
    """The scan window bounds where a marker may START, not how much of it is read.

    Slicing the text first turns a half-read marker into a *different value*,
    which is strictly worse than not matching at all: `STO` is not `STOP`, so
    the block routed. Live incident, Azymetrix P19.03.
    """
    body = _STRADDLING_BLOCK.split("\n\n", 1)[1]
    assert body.index("CLINE") == 290, "the fixture must still straddle the boundary"
    added = qm.get_prompt(int(qm.add_prompt(text=_STRADDLING_BLOCK)))
    assert added.provider is None
    # And the rest of the block's intent survives intact.
    assert added.model == "sonnet" and added.effort == "high"


def test_a_positive_marker_straddling_the_window_is_read_whole(qm):
    """The same fix in the other direction: `**CLINE: pro**` starting at 295 is
    a real routing request, not `p`."""
    text = ("x" * 286) + " **CLINE: pro** and the rest of the block."
    added = qm.get_prompt(int(qm.add_prompt(text=text)))
    assert added.provider == "deepseek" and added.model == "deepseek-v4-pro"


@pytest.mark.parametrize("value", ["STO", "banana", "sto", "cl", "prox"])
def test_an_unrecognised_marker_value_does_not_route(qm, value):
    """Fail closed. Forwarding an unknown value to the vendor is how a mangled
    marker became a routing decision: `resolve_model` passes anything through,
    so `STO` looked exactly like a model id. A value that is neither a known
    tier nor vendor-prefixed is far likelier to be a broken marker than a model.
    """
    added = qm.get_prompt(int(qm.add_prompt(text=f"**CLINE: {value}**\n\nDo it.")))
    assert added.provider is None


def test_a_future_vendor_model_id_still_routes(qm):
    """Fail-closed must not mean "only models Sandglass has heard of": the
    vendor prefix is enough to be unambiguous about where the block goes."""
    added = qm.get_prompt(int(qm.add_prompt(text="**CLINE: deepseek-v9-turbo**\n\nDo it.")))
    assert added.provider == "deepseek" and added.model == "deepseek-v9-turbo"


def test_a_refusal_anywhere_in_the_block_beats_every_other_signal(qm):
    """A block that contradicts itself is resolved toward not spending money at
    a third party -- including against explicit `provider:` front matter and a
    vendor-prefixed model name, both of which normally win outright."""
    text = (
        "provider: deepseek\nmodel: deepseek-pro\n\n"
        "Rewrite the ledger writer.\n\n" + ("filler. " * 80) +
        "\n**CLINE: STOP** — money path. Never external."
    )
    added = qm.get_prompt(int(qm.add_prompt(text=text)))
    assert added.provider is None


def test_a_stale_queue_entry_is_refused_at_run_time(qm):
    """The retroactive half. A queue imported by the buggy parser still holds
    `provider: deepseek` on a block that forbids it, and re-importing is a
    manual step nobody knows they owe. The engine therefore re-reads the block's
    own text rather than trusting the field."""
    from sandglass.queue_manager import refuses_external

    stale = PromptObject(
        id="001", title="P19.03", text=_STRADDLING_BLOCK, source="text",
        provider="deepseek", model="sonnet",
    )
    assert refuses_external(stale.text)


def test_known_model_accepts_tiers_and_vendor_prefixes_only():
    deepseek = providers.DEEPSEEK
    assert deepseek.known_model("pro") == "deepseek-v4-pro"
    assert deepseek.known_model("deepseek-v4-flash") == "deepseek-v4-flash"
    assert deepseek.known_model("deepseek-v9-turbo") == "deepseek-v9-turbo"
    assert deepseek.known_model("STO") is None
    assert deepseek.known_model("") is None
    # `resolve_model` stays permissive on purpose -- it runs after the decision
    # to route has already been made.
    assert deepseek.resolve_model("STO") == "STO"


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


# --- Parking a vendor -----------------------------------------------------
#
# The persistent counterpart to `--no-external`: a standing "don't send work
# there", written to the same file the keys are, and undone by a switch rather
# than by re-pasting a key.


def test_a_parked_provider_hands_out_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps({"deepseek": {"api_key": "sk-still-here-1234567890", "enabled": False}})
    )
    registry = providers.ProviderRegistry.load(path)
    assert registry.is_parked("deepseek")
    assert registry.key_for("deepseek") is None
    assert not registry.has("deepseek")
    # The key is kept, which is the whole difference between parking and
    # deleting: re-enabling must never mean typing it in again.
    assert registry.key_count("deepseek") == 1


def test_disabled_is_honoured_as_well_as_enabled(tmp_path, monkeypatch):
    """Both spellings are obvious things to write by hand, as in accounts.json."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps({"deepseek": {"api_key": "sk-a-key-1234567890", "disabled": True}})
    )
    assert providers.ProviderRegistry.load(path).is_parked("deepseek")


def test_parking_beats_a_key_in_the_environment(tmp_path, monkeypatch):
    """Parking is an instruction, so a key lying around in the shell can't undo it."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-the-environment")
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"deepseek": {"enabled": False}}))
    assert providers.ProviderRegistry.load(path).key_for("deepseek") is None


def test_a_parked_entry_may_carry_no_key_at_all(tmp_path, monkeypatch):
    """"Never send anything there" is a decision, not a configuration step."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"deepseek": {"enabled": False}}))
    registry = providers.ProviderRegistry.load(path)
    assert registry.is_parked("deepseek")
    assert registry.key_count("deepseek") == 0


def test_set_enabled_round_trips_and_keeps_the_key(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"deepseek": {"api_key": "sk-keep-me-1234567890"}}))

    assert providers.set_enabled("deepseek", False, path) is True
    assert providers.set_enabled("deepseek", False, path) is False  # already parked
    assert json.loads(path.read_text())["deepseek"]["api_key"] == "sk-keep-me-1234567890"

    assert providers.set_enabled("deepseek", True, path) is True
    assert providers.ProviderRegistry.load(path).key_for("deepseek") == "sk-keep-me-1234567890"


def test_set_enabled_preserves_the_nested_file_shape(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps({"providers": {"deepseek": {"api_key": "sk-nested-key-123456"}}})
    )
    providers.set_enabled("deepseek", False, path)
    written = json.loads(path.read_text())
    assert "deepseek" not in written  # not rewritten into the flat shape
    assert written["providers"]["deepseek"]["enabled"] is False


def test_set_enabled_widens_a_bare_string_entry_without_losing_it(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"deepseek": "sk-bare-string-1234567890"}))
    providers.set_enabled("deepseek", False, path)
    entry = json.loads(path.read_text())["deepseek"]
    assert entry == {"api_key": "sk-bare-string-1234567890", "enabled": False}


def test_set_enabled_never_leaves_both_spellings_behind(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps({"deepseek": {"api_key": "sk-a-key-1234567890", "disabled": True}})
    )
    providers.set_enabled("deepseek", True, path)
    entry = json.loads(path.read_text())["deepseek"]
    assert entry["enabled"] is True and "disabled" not in entry


def test_a_provider_can_be_parked_before_its_file_exists(tmp_path):
    """No file is the normal state of a machine that never opted in; deciding
    'not this vendor, ever' should not require configuring it first."""
    path = tmp_path / "nested" / "providers.json"
    assert providers.set_enabled("deepseek", False, path) is True
    assert providers.ProviderRegistry.load(path).is_parked("deepseek")


def test_parking_an_unknown_provider_is_an_error(tmp_path):
    with pytest.raises(providers.ProvidersError):
        providers.set_enabled("some-future-vendor", False, tmp_path / "providers.json")


def test_every_provider_may_be_parked_at_once(tmp_path, monkeypatch):
    """Unlike the account pool there is no last-one-standing guard: all-off just
    means every block runs on Anthropic, which is what an unconfigured machine does."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "providers.json"
    for name in providers.PROVIDERS:
        providers.set_enabled(name, False, path)
    registry = providers.ProviderRegistry.load(path)
    assert all(registry.is_parked(name) for name in providers.PROVIDERS)

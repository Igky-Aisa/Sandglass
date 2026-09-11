"""Tests for running a queue across several of your own subscriptions.

Two invariants carry the weight here. The first is that a quota hit must
*retry* the interrupted block under the next account rather than skip it — the
block never ran, so dropping it would lose work the queue was explicitly told
to do. The second is that a token must not escape into anything durable: not
the run report, not a log line, not a repr in a traceback. The accounts file
sits outside the project precisely because blocks run with bypassPermissions
inside it, and everything here exists to keep that boundary honest.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from sandglass.accounts import (
    Account,
    AccountPool,
    AccountsError,
    subprocess_env,
)
from sandglass.claude_client import AccountUnusableError, QuotaExceededError
from sandglass.execution_engine import ExecutionEngine
from sandglass.models import PromptObject, Response


def _write_pool(tmp_path, names=("acct1", "acct2", "acct3")) -> AccountPool:
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps(
            {"accounts": [{"name": n, "token": f"tok-{n}"} for n in names]}
        ),
        encoding="utf-8",
    )
    pool = AccountPool.load(path)
    assert pool is not None
    return pool


class _FakeClient:
    """Fails with a quota error `fail_times` times, then succeeds."""

    def __init__(self, fail_times: int, resets_at=None):
        self.fail_times = fail_times
        self.resets_at = resets_at
        self.auth_token = None
        self.model = "m"
        self.effort = None
        # Which credential each attempt actually ran under -- the only direct
        # evidence that a switch took effect rather than merely being logged.
        self.tokens_seen: list = []

    async def send(self) -> Response:
        self.tokens_seen.append(self.auth_token)
        if self.fail_times > 0:
            self.fail_times -= 1
            info = {"resetsAt": self.resets_at} if self.resets_at else None
            raise QuotaExceededError("usage limit reached", rate_limit_info=info)
        return Response(
            prompt_id="1", text="done", tokens_used=100, model="m", cost_usd=0.5
        )


def _engine(pool, client) -> ExecutionEngine:
    engine = ExecutionEngine.__new__(ExecutionEngine)
    engine.account_pool = pool
    engine.claude_client = client
    # Hand-built rather than constructed, so the few attributes the rotation
    # path touches have to be set explicitly. No external providers here: these
    # tests are about Claude accounts.
    engine.provider_registry = None
    if pool is not None and pool.current is not None:
        client.auth_token = pool.current.token
    engine.execute_prompt = lambda prompt: client.send()  # type: ignore[assignment]
    return engine


PROMPT = PromptObject(id="001", title="t", text="x")


# --- Rotation --------------------------------------------------------------


def test_quota_hit_retries_the_same_block_under_the_next_account(tmp_path):
    pool = _write_pool(tmp_path)
    client = _FakeClient(fail_times=2)

    response = asyncio.run(_engine(pool, client)._execute_with_rotation(PROMPT))

    assert response.text == "done"
    # Same block, three attempts, three different credentials -- not three
    # different blocks, and not the same credential retried.
    assert client.tokens_seen == ["tok-acct1", "tok-acct2", "tok-acct3"]
    assert pool.history == ["acct1", "acct2", "acct3"]


def test_error_surfaces_only_once_every_account_is_spent(tmp_path):
    pool = _write_pool(tmp_path, names=("a", "b"))
    client = _FakeClient(fail_times=99)

    with pytest.raises(QuotaExceededError):
        asyncio.run(_engine(pool, client)._execute_with_rotation(PROMPT))

    # Each account tried exactly once; no spinning on an exhausted pool.
    assert client.tokens_seen == ["tok-a", "tok-b"]


def test_without_a_pool_behaviour_is_unchanged(tmp_path):
    client = _FakeClient(fail_times=1)

    with pytest.raises(QuotaExceededError):
        asyncio.run(_engine(None, client)._execute_with_rotation(PROMPT))

    # One attempt, no token injected: exactly the pre-rotation path.
    assert client.tokens_seen == [None]


def test_resume_waits_for_the_earliest_account_not_the_last_to_fail(tmp_path):
    pool = _write_pool(tmp_path)
    now = time.time()
    pool.accounts[0].exhausted_until = now + 9000
    pool.accounts[1].exhausted_until = now + 300
    pool.accounts[2].exhausted_until = now + 6000

    assert pool.earliest_reset() == pytest.approx(now + 300, abs=1)


def test_refreshed_accounts_re_enter_the_rotation(tmp_path):
    pool = _write_pool(tmp_path, names=("a", "b"))
    pool.accounts[0].exhausted_until = time.time() - 10
    pool.accounts[1].exhausted_until = time.time() + 9000

    pool.clear_expired()

    assert pool.accounts[0].is_available()
    assert not pool.accounts[1].is_available()


def test_rotation_wraps_back_to_the_first_account_once_it_refreshes(tmp_path):
    """The full cycle: 1→2→3→1, waiting only if 1 hasn't come back yet."""
    pool = _write_pool(tmp_path)

    # 1 → 2 → 3, each recording when it comes back.
    pool.mark_exhausted(time.time() + 3600)
    assert pool.advance().name == "acct2"
    pool.mark_exhausted(time.time() + 3600)
    assert pool.advance().name == "acct3"
    pool.mark_exhausted(time.time() + 3600)

    # acct1 is still inside its window: nothing to move to, so the caller
    # waits rather than probing a credential known to be spent.
    assert pool.advance() is None
    assert pool.earliest_reset() is not None

    # Once acct1's window has rolled over, the cycle continues into it.
    pool.accounts[0].exhausted_until = time.time() - 1
    assert pool.advance().name == "acct1"
    assert pool.history == ["acct1", "acct2", "acct3", "acct1"]


def test_exhaustion_survives_a_restart(tmp_path):
    state = tmp_path / "accounts_state.json"

    pool = _write_pool(tmp_path)
    pool.state_path = str(state)
    pool.mark_exhausted(time.time() + 3600)   # acct1 spent
    pool.advance()

    # A new process reads the same pool file and the same state file.
    restarted = _write_pool(tmp_path)
    restarted.state_path = str(state)
    restarted.load_state()

    assert not restarted.accounts[0].is_available()
    # Crucially it does not start on the spent account: no block is burned
    # rediscovering a quota the previous run already found.
    assert restarted.current_name == "acct2"


def test_persisted_state_never_contains_a_token(tmp_path):
    state = tmp_path / "accounts_state.json"
    pool = _write_pool(tmp_path)
    pool.state_path = str(state)
    pool.mark_exhausted(time.time() + 3600)

    # This file lives in .sandglass/, inside the repo, readable by any block.
    assert "tok-" not in state.read_text(encoding="utf-8")


def test_stale_state_self_clears(tmp_path):
    state = tmp_path / "accounts_state.json"
    state.write_text(json.dumps({"acct1": time.time() - 9000}), encoding="utf-8")

    pool = _write_pool(tmp_path)
    pool.state_path = str(state)
    pool.load_state()

    # A window that closed while the process was down is simply over.
    assert pool.accounts[0].is_available()
    assert pool.current_name == "acct1"


# --- Credential containment ------------------------------------------------


def test_repr_does_not_leak_the_token():
    # Guards tracebacks and debugger locals, where a dataclass's default repr
    # would print the token verbatim.
    assert "SECRET" not in repr(Account(name="personal", token="sk-ant-SECRET"))


def test_usage_summary_carries_no_credentials(tmp_path):
    pool = _write_pool(tmp_path, names=("a", "b"))
    pool.record_usage(500, 1.25)
    pool.advance()
    pool.record_usage(300, 0.75)

    summary = pool.usage_summary()
    blob = json.dumps(summary)

    # This is what lands in .sandglass/last_run.json, inside the repo, where a
    # block running with bypassPermissions could read it back.
    assert "tok-a" not in blob and "tok-b" not in blob
    assert summary[0] == {"name": "a", "blocks": 1, "tokens": 500, "cost_usd": 1.25}


def test_api_key_is_stripped_from_the_rotated_environment(monkeypatch):
    # Either variable outranks the subscription token and would silently bill
    # pay-per-token credits -- a failure whose first symptom is an invoice.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-survive")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "nor-this")

    env = subprocess_env("tok123")

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok123"
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_no_token_means_the_environment_is_inherited_untouched():
    # Single-account runs must not gain an env copy they never had.
    assert subprocess_env(None) is None


# --- Token sanity ----------------------------------------------------------


@pytest.mark.parametrize(
    "token, expect",
    [
        ("sk-ant-oat01-" + "a" * 40, None),          # plausible
        ("", "empty"),
        ("   ", "empty"),
        ("sk-ant-oat01-…", "ellipsis"),              # pasted from the docs
        ("sk-ant-oat01-...", "ellipsis"),
        ("sk-ant-oat01-aaa bbb", "whitespace"),      # wrapped paste
        ("short", "truncated"),
    ],
)
def test_only_obviously_broken_tokens_are_rejected(token, expect):
    """The check must not guess at the issuing format.

    `claude auth status` accepts any non-empty string, so nothing offline can
    tell a valid token from an expired one. Asserting a prefix here would
    reject good tokens the day Claude Code changes the format — strictly
    worse than accepting bad ones, which `--probe` catches anyway.
    """
    from sandglass.accounts import looks_malformed

    problem = looks_malformed(token)
    if expect is None:
        assert problem is None
    else:
        assert problem is not None and expect in problem


def test_probe_runs_a_real_request_not_an_auth_status_check():
    # Regression guard for the bug this replaced: `auth status` reports
    # loggedIn:true for the literal string "x", so validity was never
    # something it could answer.
    from sandglass.accounts import probe_command

    cmd = probe_command("/usr/bin/claude")
    assert "-p" in cmd and "auth" not in cmd
    assert "--no-session-persistence" in cmd


# --- Loading ---------------------------------------------------------------


def test_a_missing_accounts_file_is_not_an_error(tmp_path):
    # Rotation is opt-in; no file is the normal case, not a misconfiguration.
    assert AccountPool.load(tmp_path / "nope.json") is None


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"accounts": [{"name": "a"}]}, "token"),
        ({"accounts": []}, "accounts"),
        ({"accounts": [{"name": "a", "token": "x"}, {"name": "a", "token": "y"}]},
         "duplicate"),
    ],
)
def test_a_malformed_accounts_file_fails_loudly(tmp_path, payload, expected):
    # Degrading silently to single-account would surface hours later as an
    # unexplained quota wait -- the hardest possible way to notice a typo.
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AccountsError) as exc:
        AccountPool.load(path)

    assert expected in str(exc.value).lower()


# --- Disabling an account -----------------------------------------------------
#
# Disabled and exhausted look the same to `advance()` on purpose, but they are
# not the same fact: exhaustion expires on a clock and disabled does not. These
# check that the difference survives everywhere it matters — the file, the
# loader, the wait calculation, and the guard that stops the pool being emptied.


def _write_raw(tmp_path, entries) -> "object":
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"accounts": entries}), encoding="utf-8")
    return path


def test_disabled_account_is_never_available():
    account = Account(name="parked", token="tok", enabled=False)
    assert account.is_available() is False
    # Even with quota demonstrably free: `exhausted_until` is None here.
    assert account.exhausted_until is None
    assert "disabled" in repr(account)
    assert "tok" not in repr(account)


def test_loader_accepts_either_spelling(tmp_path):
    path = _write_raw(
        tmp_path,
        [
            {"name": "a", "token": "tok-a"},
            {"name": "b", "token": "tok-b", "enabled": False},
            {"name": "c", "token": "tok-c", "disabled": True},
        ],
    )
    pool = AccountPool.load(path)
    assert [a.enabled for a in pool.accounts] == [True, False, False]


def test_pool_starts_on_the_first_enabled_account(tmp_path):
    path = _write_raw(
        tmp_path,
        [
            {"name": "a", "token": "tok-a", "enabled": False},
            {"name": "b", "token": "tok-b"},
        ],
    )
    pool = AccountPool.load(path)
    # Not just the index: `history` is what the run report prints, and starting
    # on a disabled account would claim the run had used it.
    assert pool.current_name == "b"
    assert pool.history == ["b"]


def test_all_disabled_is_a_load_error_not_a_silent_stall(tmp_path):
    path = _write_raw(
        tmp_path,
        [
            {"name": "a", "token": "tok-a", "enabled": False},
            {"name": "b", "token": "tok-b", "enabled": False},
        ],
    )
    with pytest.raises(AccountsError) as exc:
        AccountPool.load(path)
    assert "every account is disabled" in str(exc.value)


def test_advance_skips_disabled_accounts(tmp_path):
    path = _write_raw(
        tmp_path,
        [
            {"name": "a", "token": "tok-a"},
            {"name": "b", "token": "tok-b", "enabled": False},
            {"name": "c", "token": "tok-c"},
        ],
    )
    pool = AccountPool.load(path)
    assert pool.current_name == "a"
    assert pool.advance().name == "c"


def test_earliest_reset_ignores_disabled_accounts(tmp_path):
    path = _write_raw(
        tmp_path,
        [
            {"name": "a", "token": "tok-a"},
            {"name": "b", "token": "tok-b", "enabled": False},
        ],
    )
    pool = AccountPool.load(path)
    soon, later = time.time() + 60, time.time() + 6000
    # The disabled one comes back first, but the pool still won't use it, so
    # waking up at `soon` would wake the engine to nothing it can run.
    pool.accounts[1].exhausted_until = soon
    pool.accounts[0].exhausted_until = later
    assert pool.earliest_reset() == later


def test_set_enabled_round_trips_and_keeps_every_other_key(tmp_path):
    from sandglass.accounts import set_enabled

    path = _write_raw(
        tmp_path,
        [
            {"name": "a", "token": "tok-a", "note": "keep me"},
            {"name": "b", "token": "tok-b"},
        ],
    )
    assert set_enabled("a", False, path) is True
    saved = json.loads(path.read_text(encoding="utf-8"))["accounts"]
    assert saved[0]["enabled"] is False
    # A rewrite of a credential file must carry through what it doesn't know.
    assert saved[0]["token"] == "tok-a"
    assert saved[0]["note"] == "keep me"
    assert saved[1] == {"name": "b", "token": "tok-b"}

    # Idempotent: saying it twice is not an error, it is just already true.
    assert set_enabled("a", False, path) is False
    assert set_enabled("a", True, path) is True
    assert json.loads(path.read_text(encoding="utf-8"))["accounts"][0]["enabled"] is True


def test_set_enabled_clears_the_legacy_disabled_key(tmp_path):
    from sandglass.accounts import set_enabled

    path = _write_raw(tmp_path, [
        {"name": "a", "token": "tok-a", "disabled": True},
        {"name": "b", "token": "tok-b"},
    ])
    set_enabled("a", True, path)
    entry = json.loads(path.read_text(encoding="utf-8"))["accounts"][0]
    assert entry["enabled"] is True
    # Two keys disagreeing about the same fact is the bug this prevents.
    assert "disabled" not in entry


def test_cannot_disable_the_last_enabled_account(tmp_path):
    from sandglass.accounts import set_enabled

    path = _write_raw(tmp_path, [
        {"name": "a", "token": "tok-a"},
        {"name": "b", "token": "tok-b", "enabled": False},
    ])
    with pytest.raises(AccountsError) as exc:
        set_enabled("a", False, path)
    assert "only enabled account" in str(exc.value)
    # And the file is untouched, not half-written.
    assert json.loads(path.read_text(encoding="utf-8"))["accounts"][0] == {
        "name": "a", "token": "tok-a",
    }


def test_set_enabled_rejects_an_unknown_name(tmp_path):
    from sandglass.accounts import set_enabled

    path = _write_raw(tmp_path, [{"name": "a", "token": "tok-a"}])
    with pytest.raises(AccountsError) as exc:
        set_enabled("typo", False, path)
    assert "typo" in str(exc.value) and "Known: a" in str(exc.value)


# --- Parking an account while the run is going ----------------------------
#
# The pool is read once, at startup, and then lived in for hours. Parking is an
# instruction given during exactly those hours, so a pool that never looks at
# the file again makes the button decorative -- which is what happened live on
# 2026-09-11: a run that began at 02:24 rotated onto an account parked at 02:25,
# two hours later.


class _ParkingClient:
    """Quota-fails a fixed number of times, recording the credential each time."""

    def __init__(self, fail_times: int, park=None):
        self.fail_times = fail_times
        self.auth_token = None
        self.model = "m"
        self.effort = None
        self.tokens_seen: list = []
        # Called before each attempt, so a test can change the accounts file
        # mid-run exactly as a human pressing Park would.
        self.park = park

    async def send(self) -> Response:
        if self.park is not None:
            self.park()
        self.tokens_seen.append(self.auth_token)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise QuotaExceededError("usage limit reached")
        return Response(
            prompt_id="1", text="done", tokens_used=100, model="m", cost_usd=0.5
        )


def _set_enabled_in_file(path, name: str, enabled: bool) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    for entry in raw["accounts"]:
        if entry["name"] == name:
            entry["enabled"] = enabled
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_refresh_enabled_picks_up_a_park_written_after_load(tmp_path):
    pool = _write_pool(tmp_path, names=("a", "b"))
    path = tmp_path / "accounts.json"

    _set_enabled_in_file(path, "b", False)
    assert pool.accounts[1].enabled is True, "still the startup answer"

    assert pool.refresh_enabled() == {"b": False}
    assert pool.accounts[1].enabled is False
    # Idempotent: nothing "changed" the second time.
    assert pool.refresh_enabled() == {}


def test_refresh_enabled_touches_nothing_but_the_flag(tmp_path):
    """Tokens and exhaustion are deliberately not re-read: swapping a
    credential under a half-finished block is not what Park means, and this
    process -- not the file -- is the authority on what is spent."""
    pool = _write_pool(tmp_path, names=("a", "b"))
    path = tmp_path / "accounts.json"
    pool.accounts[0].exhausted_until = time.time() + 3600

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["accounts"][0]["token"] = "tok-rotated-elsewhere"
    path.write_text(json.dumps(raw), encoding="utf-8")

    pool.refresh_enabled()
    assert pool.accounts[0].token == "tok-a"
    assert pool.accounts[0].exhausted_until is not None


def test_refresh_enabled_survives_an_unreadable_file(tmp_path):
    """The file is written by another process; a read landing mid-write must
    cost the run nothing."""
    pool = _write_pool(tmp_path, names=("a", "b"))
    (tmp_path / "accounts.json").write_text("{ half a fi", encoding="utf-8")

    assert pool.refresh_enabled() == {}
    assert [a.enabled for a in pool.accounts] == [True, True]


def test_rotation_never_lands_on_an_account_parked_mid_run(tmp_path):
    pool = _write_pool(tmp_path, names=("a", "b", "c"))
    path = tmp_path / "accounts.json"
    # Parked after the pool was loaded -- the whole point.
    _set_enabled_in_file(path, "b", False)

    client = _ParkingClient(fail_times=1)
    response = asyncio.run(_engine(pool, client)._execute_with_rotation(PROMPT))

    assert response.text == "done"
    assert client.tokens_seen == ["tok-a", "tok-c"], "b was parked and must be skipped"


def test_a_block_leaves_an_account_parked_while_it_was_in_use(tmp_path):
    pool = _write_pool(tmp_path, names=("a", "b"))
    path = tmp_path / "accounts.json"
    _set_enabled_in_file(path, "a", False)

    client = _ParkingClient(fail_times=0)
    asyncio.run(_engine(pool, client)._execute_with_rotation(PROMPT))

    # The block ran on 'b': the current account was parked before it started,
    # and the per-block check moved off it rather than using it one last time.
    assert client.tokens_seen == ["tok-b"]
    assert pool.current_name == "b"


def test_parking_everything_stops_the_run_instead_of_waiting_forever(tmp_path):
    """A pool whose every account a human switched off will still be switched
    off in six hours. `earliest_reset` has nothing to offer, so the quota wait
    would poll all night for a change that is never coming."""
    pool = _write_pool(tmp_path, names=("a", "b"))
    path = tmp_path / "accounts.json"
    _set_enabled_in_file(path, "a", False)
    _set_enabled_in_file(path, "b", False)

    client = _ParkingClient(fail_times=0)
    with pytest.raises(AccountsError) as exc:
        asyncio.run(_engine(pool, client)._execute_with_rotation(PROMPT))
    assert "parked" in str(exc.value)


# --- An account that is shut, not merely spent ----------------------------


class _ClosedAccountClient:
    """Refuses under `dead_token` the way a closed subscription does."""

    def __init__(self, dead_tokens):
        self.dead_tokens = set(dead_tokens)
        self.auth_token = None
        self.model = "m"
        self.effort = None
        self.tokens_seen: list = []

    async def send(self) -> Response:
        self.tokens_seen.append(self.auth_token)
        if self.auth_token in self.dead_tokens:
            raise AccountUnusableError(
                "Your organization has disabled Claude subscription access for "
                "Claude Code · Use an Anthropic API key instead"
            )
        return Response(
            prompt_id="1", text="done", tokens_used=100, model="m", cost_usd=0.5
        )


def test_a_closed_account_rotates_instead_of_ending_the_queue(tmp_path):
    """Live incident: one closed account ended a 20-block queue at 4:30am with
    two healthy accounts sitting unused beside it."""
    pool = _write_pool(tmp_path, names=("a", "b"))
    client = _ClosedAccountClient(dead_tokens=["tok-a"])

    response = asyncio.run(_engine(pool, client)._execute_with_rotation(PROMPT))

    assert response.text == "done"
    assert client.tokens_seen == ["tok-a", "tok-b"]
    names = [name for name, _ in pool.unusable()]
    assert names == ["a"]
    assert "organization has disabled" in pool.accounts[0].unusable_reason


def test_a_closed_account_is_not_tried_again_by_a_later_block(tmp_path):
    pool = _write_pool(tmp_path, names=("a", "b"))
    client = _ClosedAccountClient(dead_tokens=["tok-a"])
    engine = _engine(pool, client)

    asyncio.run(engine._execute_with_rotation(PROMPT))
    asyncio.run(engine._execute_with_rotation(PROMPT))

    # 'a' refused once and was dropped for the run; the second block never
    # touches it. Re-probing a shut account costs a real request every block.
    assert client.tokens_seen == ["tok-a", "tok-b", "tok-b"]


def test_a_closed_account_is_never_written_into_the_accounts_file(tmp_path):
    """The fix for a closed account is a human re-opening it or parking it on
    purpose. A run editing somebody's configuration on the strength of one
    error string is a bigger decision than it looks."""
    pool = _write_pool(tmp_path, names=("a", "b"))
    client = _ClosedAccountClient(dead_tokens=["tok-a"])

    asyncio.run(_engine(pool, client)._execute_with_rotation(PROMPT))

    raw = json.loads((tmp_path / "accounts.json").read_text(encoding="utf-8"))
    assert all(e.get("enabled") is not False for e in raw["accounts"])


def test_every_account_closed_stops_rather_than_waiting(tmp_path):
    pool = _write_pool(tmp_path, names=("a", "b"))
    client = _ClosedAccountClient(dead_tokens=["tok-a", "tok-b"])

    with pytest.raises(AccountsError):
        asyncio.run(_engine(pool, client)._execute_with_rotation(PROMPT))
    assert len(pool.unusable()) == 2


def test_a_single_account_run_still_surfaces_the_refusal(tmp_path):
    """No pool means no second credential to try, so this really is the end of
    the run -- and the CLI's own wording already says what to do about it."""
    client = _ClosedAccountClient(dead_tokens=[None])

    with pytest.raises(AccountUnusableError):
        asyncio.run(_engine(None, client)._execute_with_rotation(PROMPT))

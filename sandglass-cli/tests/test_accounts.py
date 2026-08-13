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
from sandglass.claude_client import QuotaExceededError
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

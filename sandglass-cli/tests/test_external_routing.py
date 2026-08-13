"""How the engine treats a block marked for a non-Anthropic provider.

The unit-level facts (marker parsing, key loading, what lands in the
subprocess environment) live in test_providers.py. These are the decisions the
*engine* makes around a routed block — which are the ones that cost money or
disclose code if they are wrong.
"""

from __future__ import annotations

import asyncio

import pytest

from sandglass.accounts import Account, AccountPool
from sandglass.execution_engine import ExecutionEngine
from sandglass.models import Response
from sandglass.providers import ProviderRegistry
from sandglass.queue_manager import QueueManager
from sandglass.storage import StorageService


@pytest.fixture
def qm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # A stray key in the developer's own environment would otherwise decide
    # which branch these tests take.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return QueueManager(StorageService(base_path=str(tmp_path / ".sandglass")))


class _RecordingClient:
    model = "claude-opus-4-8"
    effort = None

    def __init__(self):
        self.calls: list[dict] = []
        self._minted = 0

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, model=None, effort=None,
                          resume_session_id=None, persist_session=True,
                          provider=None):
        self.calls.append({
            "text": text,
            "model": model,
            "resume_session_id": resume_session_id,
            "persist_session": persist_session,
            "provider": provider,
        })
        session_id = resume_session_id
        if not session_id and persist_session:
            self._minted += 1
            session_id = f"sess-{self._minted}"
        return Response(
            prompt_id="", text="done", tokens_used=10, cost_usd=0.5,
            model=model or self.model, session_id=session_id,
            provider=provider[0].name if provider else None,
        )


def _registry(**keys) -> ProviderRegistry:
    return ProviderRegistry(keys=dict(keys))


def _engine(qm, client, **kwargs):
    kwargs.setdefault("provider_registry", _registry(deepseek="sk-a-test-key-1234567890"))
    return ExecutionEngine(qm, client, **kwargs)


def test_a_marked_block_is_routed_and_an_unmarked_one_is_not(qm):
    qm.add_prompt(text="**CLINE: pro**\n\nCheap task.")
    qm.add_prompt(text="Expensive task.")
    client = _RecordingClient()

    asyncio.run(_engine(qm, client).execute_queue())

    routed, normal = client.calls
    assert routed["provider"] is not None
    assert routed["provider"][0].name == "deepseek"
    assert routed["provider"][1] == "sk-a-test-key-1234567890"
    assert normal["provider"] is None


def test_a_routed_block_neither_joins_nor_starts_the_chain(qm):
    """Resuming would replay the whole accumulated Claude conversation — every
    file the queue has read so far — to a third-party endpoint, which is a far
    larger disclosure than the block's author agreed to. Feeding the external
    turn back into the chain is no better: later Claude blocks would inherit
    another vendor's output as their own history."""
    qm.add_prompt(text="First Claude task.")
    qm.add_prompt(text="**CLINE: flash**\n\nExternal task.")
    qm.add_prompt(text="Third Claude task.")
    client = _RecordingClient()

    asyncio.run(_engine(qm, client).execute_queue())

    first, external, third = client.calls
    assert first["resume_session_id"] is None  # cold start, opens "sess-1"
    assert external["resume_session_id"] is None
    assert external["persist_session"] is False
    # The Claude block after it rejoins the session the *first* one opened,
    # not anything the external detour created.
    assert third["resume_session_id"] == "sess-1"


def test_no_key_falls_back_to_anthropic_rather_than_failing(qm):
    """A routing marker states a cost preference, not a correctness requirement.
    Stopping a queue over a missing config line would be a worse failure than
    running the block on Claude — and falling back *toward* Anthropic is the
    safe direction, never silently outward to a third party."""
    qm.add_prompt(text="**CLINE: pro**\n\nCheap task.")
    client = _RecordingClient()

    result = asyncio.run(_engine(qm, client, provider_registry=_registry()).execute_queue())

    assert result.completed == 1
    assert client.calls[0]["provider"] is None


def test_no_external_flag_forces_everything_onto_anthropic(qm):
    qm.add_prompt(text="**CLINE: pro**\n\nCheap task.")
    client = _RecordingClient()

    asyncio.run(_engine(qm, client, allow_external=False).execute_queue())

    assert client.calls[0]["provider"] is None
    # And with routing off it is an ordinary chained block again.
    assert client.calls[0]["persist_session"] is True


def test_a_routed_block_is_not_charged_to_a_claude_account(qm):
    """Folding third-party spend into an account's totals would misreport both:
    it inflates that account's apparent burn and hides the external cost."""
    qm.add_prompt(text="Claude task.")
    qm.add_prompt(text="**CLINE: pro**\n\nExternal task.")
    pool = AccountPool(accounts=[Account(name="personal", token="t" * 40)])

    asyncio.run(_engine(qm, _RecordingClient(), account_pool=pool).execute_queue())

    summary = pool.usage_summary()
    assert len(summary) == 1
    assert summary[0]["blocks"] == 1  # the Claude block only
    assert summary[0]["tokens"] == 10


def test_an_external_rate_limit_does_not_park_a_claude_account(qm):
    """A 429 from someone else's endpoint says nothing about a Claude
    subscription. Rotating on it would mark a healthy account spent and park
    it for an hour on the strength of another vendor's quota."""
    from sandglass.claude_client import QuotaExceededError

    class _ExternalQuotaClient(_RecordingClient):
        async def send_prompt(self, text, on_chunk=None, provider=None, **kwargs):
            if provider is not None:
                raise QuotaExceededError("rate limit exceeded")
            return await super().send_prompt(text, on_chunk, provider=provider, **kwargs)

    qm.add_prompt(text="**CLINE: pro**\n\nExternal task.")
    pool = AccountPool(
        accounts=[Account(name="a", token="t" * 40), Account(name="b", token="u" * 40)]
    )

    asyncio.run(_engine(qm, _ExternalQuotaClient(), account_pool=pool).execute_queue())

    assert all(a.exhausted_until is None for a in pool.accounts)
    assert pool.current_name == "a"


def test_the_model_sent_externally_is_the_providers_own(qm):
    """A Claude model id reaching a third-party endpoint is silently remapped to
    whatever that vendor considers equivalent, so the run would quietly get a
    model nobody chose."""
    qm.add_prompt(text="**CLINE: pro**\n\nTask.")
    client = _RecordingClient()

    asyncio.run(_engine(qm, client).execute_queue())

    assert client.calls[0]["model"] == "deepseek-v4-pro"

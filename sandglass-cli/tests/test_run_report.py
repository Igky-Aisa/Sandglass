"""Tests for the 'why did it stop' record.

The behaviour under test is diagnostic, so the assertions are about what a
person is told, not about internal shape: every terminal reason has to produce
an explanation that names the cause and what to do next.
"""

from __future__ import annotations

import pytest

from sandglass import run_report
from sandglass.storage import StorageService


@pytest.fixture
def storage(tmp_path):
    return StorageService(base_path=str(tmp_path / ".sandglass"))


def test_no_record_yet_reads_as_absent(storage):
    assert run_report.load(storage) is None


def test_a_report_round_trips(storage):
    run_report.save(
        storage,
        run_report.RunReport(
            status=run_report.STATUS_STOPPED,
            reason=run_report.REASON_QUOTA,
            detail="You've hit your session limit · resets 6:20pm",
            prompt_id="047",
            remaining=31,
        ),
    )

    loaded = run_report.load(storage)

    assert loaded is not None
    assert loaded.reason == run_report.REASON_QUOTA
    assert loaded.prompt_id == "047"
    assert loaded.remaining == 31


def test_unknown_fields_from_a_newer_version_do_not_break_loading(storage):
    storage.ensure_sandglass_dir()
    storage.save_json(
        storage.last_run_path,
        {"status": "stopped", "reason": "quota", "invented_later": True},
    )

    loaded = run_report.load(storage)

    assert loaded is not None and loaded.reason == "quota"


def test_a_killed_run_is_reported_as_killed_not_as_running(storage):
    """The case this module exists for: a run stopped from outside never gets
    to write why, so a `running` record whose process is gone IS the answer."""
    report = run_report.RunReport(
        status=run_report.STATUS_RUNNING,
        prompt_id="048",
        prompt_title="P6.04 — order placement",
        pid=999_999_998,  # never a live PID in practice
        remaining=31,
    )

    assert run_report.effective_reason(report) == run_report.REASON_VANISHED
    headline, what, todo = run_report.explain(report)
    assert "Killed" in headline
    # Must rule out the two things a user would otherwise assume.
    assert "NOT a quota" in what and "048" in what
    assert "sandglass execute" in todo


def test_a_live_run_is_reported_as_running(storage):
    import os

    report = run_report.RunReport(status=run_report.STATUS_RUNNING, pid=os.getpid())

    assert run_report.effective_reason(report) == run_report.STATUS_RUNNING
    assert "Still running" in run_report.explain(report)[0]


def test_a_quota_wait_is_a_live_state_not_a_stop(storage):
    import os

    report = run_report.RunReport(
        status=run_report.STATUS_WAITING,
        reason=run_report.REASON_QUOTA,
        resume_at="2026-08-11T22:20:00+00:00",
        pid=os.getpid(),
        remaining=31,
    )

    headline, what, todo = run_report.explain(report)

    assert "Waiting" in headline
    assert "22:20" in what
    assert "resumes on its own" in todo


@pytest.mark.parametrize(
    "reason,expected",
    [
        (run_report.REASON_QUOTA, "Token limits"),
        (run_report.REASON_ERROR, "error"),
        (run_report.REASON_NO_ARTIFACT, "no work product"),
        (run_report.REASON_STALLED, "quota"),
        (run_report.REASON_INTERRUPTED, "Ctrl-C"),
        (run_report.REASON_CRASHED, "crashed"),
        (run_report.REASON_COMPLETE, "Finished"),
    ],
)
def test_every_reason_produces_a_specific_explanation(reason, expected):
    report = run_report.RunReport(status=run_report.STATUS_STOPPED, reason=reason)

    headline, what, todo = run_report.explain(report)

    assert expected.lower() in (headline + " " + what).lower()
    # A diagnostic that doesn't say what to do next is half an answer.
    assert todo.strip()


def test_the_verbatim_error_is_shown_never_paraphrased():
    """The CLI's own text carries facts a summary would lose -- the reset time
    in a quota message, for one."""
    detail = "You've hit your session limit · resets 6:20pm (America/Santiago)"
    report = run_report.RunReport(
        status=run_report.STATUS_STOPPED, reason=run_report.REASON_QUOTA, detail=detail
    )

    rendered = "\n".join(run_report.render(report))

    assert detail in rendered


# --- what a real run actually records ---------------------------------------


class _Client:
    model = "m"
    effort = None

    def __init__(self, quota_on: str | None = None):
        self.quota_on = quota_on

    def estimate_tokens(self, text):
        return len(text) // 4

    async def send_prompt(self, text, on_chunk=None, **kwargs):
        from sandglass.claude_client import QuotaExceededError
        from sandglass.models import Response

        if self.quota_on and self.quota_on in text:
            raise QuotaExceededError(
                "You've hit your session limit · resets 6:20pm",
                rate_limit_info={"status": "rejected", "resetsAt": 1786486800},
            )
        return Response(prompt_id="", text="done", tokens_used=10, model="m")


def _run(qm, client):
    import asyncio

    from sandglass.execution_engine import ExecutionEngine

    asyncio.run(ExecutionEngine(qm, client).execute_queue())


def test_a_quota_stop_is_recorded_with_its_message_and_reset_time(tmp_path, monkeypatch):
    from sandglass.queue_manager import QueueManager

    monkeypatch.chdir(tmp_path)
    store = StorageService(base_path=str(tmp_path / ".sandglass"))
    qm = QueueManager(store)
    qm.add_prompt("build the thing")
    qm.add_prompt("build the other thing")

    _run(qm, _Client(quota_on="build the thing"))

    saved = run_report.load(store)
    assert saved.reason == run_report.REASON_QUOTA
    assert "session limit" in saved.detail
    assert saved.resume_at  # the reset time survives for `sandglass why`
    assert saved.remaining == 2  # nothing was cut


def test_a_clean_drain_records_completion(tmp_path, monkeypatch):
    from sandglass.queue_manager import QueueManager

    monkeypatch.chdir(tmp_path)
    store = StorageService(base_path=str(tmp_path / ".sandglass"))
    qm = QueueManager(store)
    qm.add_prompt("a task")

    _run(qm, _Client())

    saved = run_report.load(store)
    assert saved.status == run_report.STATUS_COMPLETE
    assert saved.remaining == 0
    assert "Finished" in run_report.explain(saved)[0]


def test_saving_never_raises_even_if_the_directory_is_unusable(tmp_path):
    """A diagnostic must not be the thing that kills a run."""

    class _Broken(StorageService):
        def ensure_sandglass_dir(self):
            raise OSError("disk full")

    run_report.save(_Broken(base_path=str(tmp_path)), run_report.RunReport())

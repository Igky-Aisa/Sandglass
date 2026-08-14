"""Shared fixtures for the test suite.

`_sandboxed_cwd` chdirs every test into its own scratch directory before it
runs. Without it, a test that forgets to sandbox its own working directory can
touch this repository's real files as a side effect -- specifically
`master_plan/work_log.md`: `ExecutionEngine._append_work_log_entry` resolves
its target against `os.path.isdir("master_plan")` relative to the process
CWD, not against any test's `tmp_path`, so running the suite with CWD at the
repo root (an easy mistake -- `python -m pytest sandglass-cli/tests` from one
directory up does it) makes every test with a bare `qm.add_prompt("first")`
and no send-a-real-response client append a real, fake entry to this file.
Documented as a known landmine in work_log.md (2026-08-09) after it first
happened; it happened again, twice, before this fixture existed to stop it.

Autouse rather than opt-in per test, because opt-in only protects the tests
someone remembered to annotate -- which is exactly how this kept recurring.
"""

import pytest


@pytest.fixture(autouse=True)
def _sandboxed_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

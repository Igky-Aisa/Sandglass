"""Tests for the localhost control panel.

The weight here is on the two things that are new risks rather than new
features. First, this is the only part of Sandglass that listens on a socket,
and `localhost` is not a private address: any page in any other tab can POST to
it, so the key check is load-bearing rather than decorative. Second, the Run
button must remain a remote control -- it spawns the same command you would have
typed and streams it back, so there is no second implementation of a run that
could drift from the real one.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from sandglass import webui


@pytest.fixture()
def server(tmp_path):
    """A real bound server on a random port, torn down after the test."""
    source = tmp_path / "future_prompts.md"
    source.write_text("first block\n", encoding="utf-8")

    httpd = webui.build_server(str(source), "Test Project", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.controller.shutdown()
        httpd.server_close()


def _url(httpd, path: str, key: str | None = "") -> str:
    base = f"http://127.0.0.1:{httpd.server_address[1]}{path}"
    if key == "":
        key = httpd.key
    return f"{base}?k={key}" if key else base


def _get(httpd, path: str, key: str | None = "", headers: dict | None = None):
    req = urllib.request.Request(_url(httpd, path, key), headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post(httpd, path: str, body: dict | None = None, key: str | None = "", headers=None):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        _url(httpd, path, key),
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


# --- The key is the only thing between a stray tab and your machine ----------


def test_no_key_is_refused(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/", key=None)
    assert exc.value.code == 403


def test_wrong_key_is_refused(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/", key="not-the-key")
    assert exc.value.code == 403


def test_run_cannot_be_started_without_the_key(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, "/api/run", key="not-the-key")
    assert exc.value.code == 403
    assert server.controller.running is False


def test_a_foreign_origin_is_refused_even_with_the_key(server):
    # Defence in depth: a page that somehow learned the key still cannot drive
    # this from somewhere else.
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, "/api/run", headers={"Origin": "https://evil.example"})
    assert exc.value.code == 403
    assert server.controller.running is False


def test_our_own_origin_is_accepted(server):
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    status, payload = _post(server, "/api/stop", headers={"Origin": origin})
    # Nothing is running, so this is a refusal on the merits -- not a 403.
    assert status == 200 and payload["ok"] is False


# --- The page ----------------------------------------------------------------


def test_served_page_has_the_controls_a_static_file_cannot(server):
    status, body = _get(server, "/")
    assert status == 200
    assert 'id="btn-run"' in body and 'id="btn-stop"' in body
    # A served page drives itself from JS; a meta refresh would throw away the
    # log scroll position and button focus every few seconds.
    assert "http-equiv" not in body


def test_unknown_paths_are_not_served(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/../../etc/passwd")
    assert exc.value.code in (403, 404)


# --- Run/stop ----------------------------------------------------------------


def test_run_streams_the_command_output_and_reports_completion(server):
    server.controller._command = [
        sys.executable,
        "-c",
        "print('block 1 done'); print('block 2 done')",
    ]

    status, payload = _post(server, "/api/run")
    assert status == 200 and payload["ok"] is True

    deadline = time.time() + 10
    while time.time() < deadline and server.controller.running:
        time.sleep(0.05)
    # Give the pump thread its last write after the process exits.
    time.sleep(0.3)

    _, state = _get(server, "/api/state")
    body = json.loads(state)
    assert body["running"] is False
    text = "\n".join(body["lines"])
    assert "block 1 done" in text and "block 2 done" in text
    assert "exit 0" in text


def test_a_second_run_is_refused_while_one_is_going(server):
    server.controller._command = [sys.executable, "-c", "import time; time.sleep(5)"]
    _post(server, "/api/run")
    try:
        status, payload = _post(server, "/api/run")
        assert status == 200
        assert payload["ok"] is False and "already" in payload["message"]
    finally:
        server.controller.stop()


def test_stop_ends_a_running_process(server):
    server.controller._command = [sys.executable, "-c", "import time; time.sleep(30)"]
    _post(server, "/api/run")
    assert server.controller.running is True

    status, payload = _post(server, "/api/stop")
    assert status == 200 and payload["ok"] is True
    assert server.controller.running is False


def test_stop_with_nothing_running_is_a_message_not_an_error(server):
    status, payload = _post(server, "/api/stop")
    assert status == 200
    assert payload["ok"] is False and "Nothing is running" in payload["message"]


def test_log_is_asked_for_by_cursor_so_it_is_never_resent(server):
    controller = server.controller
    controller._append("one")
    controller._append("two")

    lines, cursor = controller.log_since(0)
    assert lines == ["one", "two"]
    # Asking again from the returned cursor yields nothing, which is what stops
    # a long run re-sending its whole log on every poll.
    assert controller.log_since(cursor) == ([], cursor)


# --- Account toggles ---------------------------------------------------------


def test_account_toggle_rejects_a_malformed_request(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, "/api/account", {"name": "personal"})  # no `enabled`
    assert exc.value.code == 400


def test_account_toggle_reports_a_refusal_rather_than_swallowing_it(server, monkeypatch):
    from sandglass import accounts as accounts_mod

    def _refuse(name, enabled, path=None):
        raise accounts_mod.AccountsError("'solo' is the only enabled account left")

    monkeypatch.setattr(accounts_mod, "set_enabled", _refuse)

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, "/api/account", {"name": "solo", "enabled": False})
    assert exc.value.code == 409
    # The reason has to reach the page: "that was your last enabled account" is
    # the entire point of the guard, and a silent failure would hide it.
    assert "only enabled account" in exc.value.read().decode("utf-8")


# --- The command buttons -----------------------------------------------------
#
# This endpoint is the one that could turn a page-with-buttons into a way to run
# arbitrary commands on the machine, so the tests are mostly about what it
# refuses. The client picks a name from a fixed table and never contributes to
# the argv; everything below is a way of checking that stays true.


def test_every_allowlisted_command_has_a_button(server):
    # Dead surface is worse than no surface: a command reachable over HTTP that
    # no button uses is an endpoint nobody is thinking about.
    _, page = _get(server, "/")
    for name in webui.SAFE_COMMANDS:
        assert f'data-cmd="{name}"' in page, name


def test_an_unknown_command_is_refused(server):
    status, payload = _post(server, "/api/command", {"name": "rm-rf"})
    assert status == 200 and payload["ok"] is False
    # The name came from the client and must not be echoed back into the page.
    assert "rm-rf" not in payload["message"]


def test_the_client_cannot_contribute_to_the_argv(server):
    # The table is keyed on exact names, so a name carrying arguments simply
    # isn't in it -- there is no path from request text to a command line.
    status, payload = _post(server, "/api/command", {"name": "queue-list; whoami"})
    assert status == 200 and payload["ok"] is False


def test_a_command_name_must_be_a_string(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, "/api/command", {"name": ["queue", "clear"]})
    assert exc.value.code == 400


def test_clearing_the_queue_needs_confirming(server):
    ok, message, _ = server.controller.run_command("queue-clear", confirmed=False)
    assert ok is False and "confirm" in message.lower()


def test_clearing_the_queue_is_refused_while_a_run_is_going(server):
    server.controller._command = [sys.executable, "-c", "import time; time.sleep(5)"]
    _post(server, "/api/run")
    try:
        ok, message, _ = server.controller.run_command("queue-clear", confirmed=True)
        # Emptying the queue under a running executor is never what anyone
        # means by pressing that button.
        assert ok is False and "Stop the run first" in message
    finally:
        server.controller.stop()


def test_a_read_only_command_runs_and_returns_its_output(server, tmp_path):
    server.controller._cwd = str(tmp_path)
    status, payload = _post(server, "/api/command", {"name": "queue-list"})
    assert status == 200 and payload["ok"] is True
    assert isinstance(payload["output"], str)


def test_commands_need_the_key_like_everything_else(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, "/api/command", {"name": "queue-clear", "confirm": True}, key="no")
    assert exc.value.code == 403

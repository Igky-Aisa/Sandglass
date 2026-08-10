import urllib.error

import pytest

from sandglass import notify


@pytest.fixture(autouse=True)
def _clean_ntfy_env(monkeypatch):
    """Every test starts unconfigured -- avoids leaking a real topic from the
    developer's own shell environment into a test run that shouldn't send
    anything over the network.

    Quiet hours are forced off too: they are on by default, so without this
    every send() test would fail or pass purely according to what time of day
    the suite happens to run at."""
    monkeypatch.delenv(notify.NTFY_TOPIC_ENV, raising=False)
    monkeypatch.delenv(notify.NTFY_SERVER_ENV, raising=False)
    monkeypatch.setattr("sandglass.notify.quiet_hours.is_quiet", lambda: False)


def test_is_configured_false_without_a_topic():
    assert notify.is_configured() is False


def test_is_configured_true_once_a_topic_is_set(monkeypatch):
    monkeypatch.setenv(notify.NTFY_TOPIC_ENV, "my-topic")

    assert notify.is_configured() is True


def test_send_is_a_noop_without_a_topic(monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(1))

    sent = notify.send("hello")

    assert sent is False
    assert calls == []  # never even attempted a network call


def test_send_posts_to_the_configured_topic(monkeypatch):
    monkeypatch.setenv(notify.NTFY_TOPIC_ENV, "my-topic")
    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"ok"

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    sent = notify.send("hello world", title="Sandglass: test", priority="high")

    assert sent is True
    assert captured["url"] == "https://ntfy.sh/my-topic"
    assert captured["data"] == b"hello world"
    assert captured["headers"]["title"] == "Sandglass: test"
    assert captured["headers"]["priority"] == "high"


def test_send_respects_a_custom_server(monkeypatch):
    monkeypatch.setenv(notify.NTFY_TOPIC_ENV, "my-topic")
    monkeypatch.setenv(notify.NTFY_SERVER_ENV, "https://ntfy.example.com/")
    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"ok"

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    notify.send("hello")

    assert captured["url"] == "https://ntfy.example.com/my-topic"


def test_send_is_suppressed_during_quiet_hours(monkeypatch):
    monkeypatch.setenv(notify.NTFY_TOPIC_ENV, "my-topic")
    monkeypatch.setattr("sandglass.notify.quiet_hours.is_quiet", lambda: True)
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(1))

    sent = notify.send("wake up", title="Sandglass: token limits hit", priority="high")

    assert sent is False
    assert calls == []  # dropped before any network call, at any priority


def test_send_returns_false_and_does_not_raise_on_network_error(monkeypatch):
    monkeypatch.setenv(notify.NTFY_TOPIC_ENV, "my-topic")

    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("network is down")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    sent = notify.send("hello")

    assert sent is False

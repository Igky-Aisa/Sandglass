"""A localhost control panel: the dashboard, plus buttons that actually do things.

`sandglass dashboard` writes a static file, and a static file is a display -- a
page opened from `file://` cannot start a process, so a play button there would
be decoration. Buttons need something listening. This module is that something,
kept as small as it can be while still being safe to point a browser at.

**It is a remote control, not a second implementation.** The Run button spawns
exactly the command you would have typed -- `sandglass execute`, as a child
process -- and streams its output into the page. Nothing about how a run works
is re-implemented here, so nothing here can drift from what the CLI does.

**It runs in the foreground and dies with Ctrl-C.** The static dashboard was
chosen over a server precisely to avoid "one more background thing to manage";
that objection is about a daemon running *alongside* your work, and this is not
one -- it is the thing you are watching. When it exits it takes any run it
started with it.

Three things this module must not get wrong, all of them about the fact that
`localhost` is not a private address:

- **Every request must carry the key**, minted per process and handed out only
  in the URL printed to your terminal. Any page in any other tab can POST to
  `http://127.0.0.1:<port>` -- the same-origin policy stops it *reading* the
  reply, not sending the request. Without a shared secret, a page you happened
  to visit could start runs on your machine.
- **It binds 127.0.0.1 only**, never `0.0.0.0`. That is the difference between
  "this machine" and "everyone on this network".
- **The page carries no token**, only account names and states, for the same
  reason `dashboard.html` doesn't: it is served out of a project directory that
  blocks running under `bypassPermissions` can read.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import dashboard as dashboard_mod

logger = logging.getLogger(__name__)

# How far back the page can scroll through a run's output. A long unattended run
# prints far more than anyone reads; this is a live view, and the durable record
# is `.sandglass/responses/` plus the run report.
LOG_LINES = 2000

# Rich writes colour as ANSI escapes, which a browser renders as mojibake. The
# CLI already drops its hourglass animation when stdout is not a terminal, so
# what reaches here is ordinary lines plus colour codes.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# How long a stopped run gets to shut down cleanly before it is killed. The CLI
# treats an interrupt as "stop after tidying up, queue intact", and that tidying
# is the whole reason to interrupt rather than terminate.
_STOP_GRACE_SECONDS = 10


# The short, non-streaming commands a button may run, as a fixed table of
# name -> (argv tail, needs an explicit confirmation, refuse while a run is on).
#
# **The client picks a name from this table and never supplies an argument.**
# That is the whole security model of this endpoint: anything that let a request
# contribute to the argv would turn a page-with-buttons into a way to run
# arbitrary commands on the machine, which is a very different thing to have
# listening on a port.
#
# Deliberately excluded, and why: `setup-token` and `providers set` print or
# accept a credential (a browser is the wrong place for either); `accounts
# --probe` bills real tokens on every account; `update` rewrites the tool that
# is running; `queue add/remove/import` need arguments, which is exactly what
# this table refuses to accept.
SAFE_COMMANDS: dict = {
    "queue-list": (["queue", "list"], False, False),
    "queue-lint": (["queue", "lint"], False, False),
    "dry-run": (["execute", "--dry-run"], False, False),
    "why": (["why"], False, False),
    # Destructive, so it needs `confirm`. Refused mid-run as well: emptying the
    # queue under a running executor is never what anyone means by it.
    "queue-clear": (["queue", "clear", "--yes"], True, True),
}

# Generous, because `--dry-run` reads every block and `lint` stats every path a
# block mentions. Nothing in the table talks to a model, so none of it should
# come close.
_COMMAND_TIMEOUT_SECONDS = 120


def _child_env() -> dict:
    """The environment every child gets, and why it isn't just `os.environ`.

    Children run with cwd set to the *project*, which can drop the very path
    entry that made this package importable -- a source checkout being run from
    its own directory is importable because of cwd and nothing else, and moving
    cwd takes that away. So the package's parent directory is carried through
    explicitly on PYTHONPATH.

    UTF-8 because Windows consoles default to a codepage that cannot encode the
    box drawing and arrows the CLI prints.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    package_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        package_parent + os.pathsep + existing if existing else package_parent
    )
    return env


class RunController:
    """Starts, watches and stops one `sandglass execute` child process."""

    def __init__(self, cwd: Optional[str] = None, command: Optional[list] = None) -> None:
        self._cwd = cwd or os.getcwd()
        # Overridable so a test can drive the whole start/stream/exit path
        # against a command that finishes in milliseconds. Production never
        # passes it -- the default below is the one true command.
        self._command = command
        self._proc: Optional[subprocess.Popen] = None
        self._lines: deque = deque(maxlen=LOG_LINES)
        self._cursor = 0
        self._lock = threading.Lock()

    # --- state ----------------------------------------------------------

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def _append(self, text: str) -> None:
        with self._lock:
            self._lines.append((self._cursor, text))
            self._cursor += 1

    def log_since(self, since: int):
        """Lines the client has not seen yet, and the cursor to ask from next.

        A client that has fallen further behind than the buffer is deliberately
        not told so -- it simply receives the oldest lines still held. The
        alternative, an error, would turn a page left open overnight into a
        broken one in order to protect output nobody was reading.
        """
        with self._lock:
            return [text for i, text in self._lines if i >= since], self._cursor

    # --- lifecycle ------------------------------------------------------

    def start(self, extra_args: Optional[list] = None):
        if self.running:
            return False, "A run is already going."

        # `-m sandglass` rather than the `sandglass` console script: the script
        # may not be on PATH in every environment this gets launched from, but
        # if this module is importable then so is the package it lives in.
        cmd = self._command or (
            [sys.executable, "-m", "sandglass", "execute"] + list(extra_args or [])
        )

        env = _child_env()
        # Unbuffered so a line reaches the page when it happens, rather than
        # when an 8KB buffer fills.
        env["PYTHONUNBUFFERED"] = "1"

        kwargs = {}
        if os.name == "nt":
            # Its own process group, so Stop can deliver the same interrupt
            # Ctrl-C would without also killing this server.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self._cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **kwargs,
            )
        except OSError as exc:
            logger.error("Could not start a run: %s", exc)
            return False, f"Could not start a run: {exc}"

        self._proc = proc
        self._append("$ sandglass execute")
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()
        return True, "Run started."

    def _pump(self, proc: subprocess.Popen) -> None:
        """Copy the child's output into the ring buffer until it exits."""
        try:
            for raw in proc.stdout:
                self._append(_ANSI_RE.sub("", raw.rstrip()))
        except (OSError, ValueError) as exc:  # pipe closed under us on stop
            logger.debug("Log pump ended: %s", exc)
        finally:
            code = proc.wait()
            self._append("")
            self._append(
                "- run finished (exit 0)"
                if code == 0
                else f"- run stopped (exit {code})"
            )

    def stop(self):
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False, "Nothing is running."

        # Interrupt rather than terminate: the CLI's own Ctrl-C handling leaves
        # the queue exactly as it was, and a killed process never gets to do it.
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except (OSError, ValueError) as exc:
            logger.warning("Could not interrupt the run cleanly: %s", exc)

        try:
            proc.wait(timeout=_STOP_GRACE_SECONDS)
            return True, "Run stopped; the queue is untouched."
        except subprocess.TimeoutExpired:
            proc.kill()
            # Reap it before reporting: `kill` only asks, and returning while
            # `poll()` is still None would have the page show a run that the
            # caller was just told had stopped.
            proc.wait()
            return True, "Run did not stop in time and was killed."

    def run_command(self, name: str, confirmed: bool = False):
        """Run one command from `SAFE_COMMANDS` and hand back what it printed.

        Synchronous, unlike `start`: these all finish in well under a second and
        have no interesting middle, so streaming them would be machinery for
        nothing. Returns (ok, message, output).
        """
        entry = SAFE_COMMANDS.get(name)
        if entry is None:
            # Never echo the name back into the page: it came from the client.
            return False, "Unknown command.", ""
        args, needs_confirm, blocked_while_running = entry

        if blocked_while_running and self.running:
            return False, "Stop the run first — that would change it underneath.", ""
        if needs_confirm and not confirmed:
            return False, "That one needs confirming.", ""

        try:
            done = subprocess.run(
                [sys.executable, "-m", "sandglass"] + args,
                cwd=self._cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_child_env(),
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return False, "Timed out.", ""
        except OSError as exc:
            logger.error("Could not run %s: %s", name, exc)
            return False, f"Could not run it: {exc}", ""

        output = _ANSI_RE.sub("", (done.stdout or "") + (done.stderr or "")).strip()
        ok = done.returncode == 0
        message = "Done." if ok else f"Exited with {done.returncode}."
        return ok, message, output

    def shutdown(self) -> None:
        """Take any child with us.

        A server that exits leaving an orphaned run behind is the exact
        background-process problem this design exists to avoid.
        """
        if self.running:
            self.stop()


class _Handler(BaseHTTPRequestHandler):
    server_version = "Sandglass"

    # --- plumbing -------------------------------------------------------

    def log_message(self, fmt, *args) -> None:
        # One line per poll would bury the URL the user still has to read.
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _authorised(self, query) -> bool:
        supplied = self.headers.get("X-Sandglass-Key") or (query.get("k") or [""])[0]
        if not secrets.compare_digest(supplied or "", self.server.key):
            return False
        # Defence in depth: a page that somehow learned the key still cannot
        # drive this from another origin.
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.allowed_origins:
            return False
        return True

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Nothing here is meant to be embedded anywhere else.
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    # --- routes ---------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorised(query):
            self._send(403, b"Forbidden", "text/plain; charset=utf-8")
            return

        if parsed.path in ("/", "/index.html"):
            html = dashboard_mod.generate(
                self.server.source_file,
                self.server.title,
                live_key=self.server.key,
            )
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if parsed.path == "/api/state":
            try:
                since = int((query.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            controller = self.server.controller
            lines, cursor = controller.log_since(since)
            self._json(
                {"running": controller.running, "lines": lines, "cursor": cursor}
            )
            return

        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorised(query):
            self._send(403, b"Forbidden", "text/plain; charset=utf-8")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except (ValueError, json.JSONDecodeError):
            self._json({"ok": False, "message": "Malformed request body."}, 400)
            return

        controller = self.server.controller

        if parsed.path == "/api/run":
            ok, message = controller.start()
            self._json({"ok": ok, "message": message})
            return

        if parsed.path == "/api/stop":
            ok, message = controller.stop()
            self._json({"ok": ok, "message": message})
            return

        if parsed.path == "/api/command":
            name = payload.get("name")
            if not isinstance(name, str):
                self._json({"ok": False, "message": "Expected a command name."}, 400)
                return
            ok, message, output = controller.run_command(
                name, confirmed=payload.get("confirm") is True
            )
            self._json({"ok": ok, "message": message, "output": output})
            return

        if parsed.path == "/api/account":
            name, enabled = payload.get("name"), payload.get("enabled")
            if not isinstance(name, str) or not isinstance(enabled, bool):
                self._json(
                    {"ok": False, "message": "Expected a name and a state."}, 400
                )
                return
            from .accounts import AccountsError, set_enabled

            try:
                set_enabled(name, enabled)
            except AccountsError as exc:
                # These refusals are worth reading: "that was your last enabled
                # account" is the whole reason the guard exists.
                self._json({"ok": False, "message": str(exc)}, 409)
                return
            state = "enabled" if enabled else "disabled"
            self._json({"ok": True, "message": f"{name} is now {state}."})
            return

        self._send(404, b"Not found", "text/plain; charset=utf-8")


def build_server(
    source_file: str,
    title: str,
    host: str = "127.0.0.1",
    port: int = 0,
    controller: Optional[RunController] = None,
) -> ThreadingHTTPServer:
    """Bind the control panel and hang its state off the server object.

    Split out from `serve` so a test can drive real HTTP against it without
    also taking over the process with `serve_forever`.
    """
    httpd = ThreadingHTTPServer((host, port), _Handler)
    actual_port = httpd.server_address[1]
    httpd.key = secrets.token_urlsafe(24)
    httpd.controller = controller or RunController()
    httpd.source_file = source_file
    httpd.title = title
    httpd.allowed_origins = {
        f"http://{host}:{actual_port}",
        f"http://localhost:{actual_port}",
    }
    return httpd


def serve(
    source_file: str,
    title: str,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """Run the control panel until interrupted. Blocks.

    `port=0` lets the OS pick a free one, which is right for something you reach
    by clicking the link just printed: a fixed port is one more thing that can
    already be taken by something else.
    """
    httpd = build_server(source_file, title, host=host, port=port)
    url = f"http://{host}:{httpd.server_address[1]}/?k={httpd.key}"

    # Flushed explicitly: piped to a file, `print` block-buffers, and the URL
    # would not appear until the buffer filled -- which for two short lines
    # means "when the server exits", i.e. exactly too late to be useful.
    print(f"Sandglass control panel: {url}", flush=True)
    print("Ctrl-C here closes it (and stops any run it started).", flush=True)

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.warning("Could not open a browser: %s", exc)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("")
        print("Closing the control panel...")
    finally:
        httpd.controller.shutdown()
        httpd.server_close()

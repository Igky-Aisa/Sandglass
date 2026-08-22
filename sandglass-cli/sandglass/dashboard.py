"""A self-contained HTML status page for one project's queue, generated from
the same data `Progress.md` and the 5%-milestone ntfy push already read --
`prompt_source.throughput()` and `prompt_source.phase_breakdown()` -- plus the
current run state from `run_report`.

Deliberately static, not served. A local HTTP server is one more background
process to manage, kill, and forget about; a plain file has none of that.
Freshness instead comes from two things: the file is rewritten after every
completed block (`execution_engine._maybe_notify_progress`, the same hook
that updates `Progress.md`), and the page reloads itself on a short timer, so
a tab left open during a run keeps catching up without a manual refresh.
"""

from __future__ import annotations

import base64
import functools
import html
import json
import logging
import os
import webbrowser
from datetime import datetime, timezone

from . import prompt_source, run_report
from .storage import StorageService

logger = logging.getLogger(__name__)

DASHBOARD_FILENAME = "dashboard.html"
_REFRESH_SECONDS = 8

_ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "hourglass_icon.jpg")


@functools.lru_cache(maxsize=1)
def _icon_data_uri() -> str | None:
    """The brand hourglass icon as a base64 data: URI, or None if it can't be
    read -- a missing/corrupt asset must not break `sandglass dashboard`
    itself, so the hero section is simply skipped rather than the page
    failing to generate. Cached: it's the same ~11KB file on every call
    within a process, and a long `sandglass execute` run regenerates the
    dashboard after every block.
    """
    try:
        with open(_ICON_PATH, "rb") as fh:
            encoded = base64.b64encode(fh.read()).decode("ascii")
    except OSError as exc:
        logger.warning("Could not read dashboard icon %s: %s", _ICON_PATH, exc)
        return None
    return f"data:image/jpeg;base64,{encoded}"


_GREEN = "#22c55e"
_YELLOW = "#eab308"
_RED = "#ef4444"
_PURPLE = "#8b5cf6"
_IDLE_COLOR = "#64748b"

# `run_report.effective_reason()` returns STATUS_RUNNING/STATUS_WAITING/
# REASON_VANISHED as live states, but for anything stopped it returns the
# *specific* reason (REASON_QUOTA, REASON_ERROR, ...) rather than the generic
# STATUS_STOPPED -- so every one of those has to be mapped to red here, not
# just the status constant.
_STATUS_COLORS: dict[str, tuple[str, str]] = {
    run_report.STATUS_RUNNING: (_GREEN, "Running"),
    run_report.STATUS_WAITING: (_YELLOW, "Waiting"),
    run_report.REASON_VANISHED: (_RED, "Vanished"),
    run_report.REASON_COMPLETE: (_PURPLE, "Complete"),
    run_report.REASON_QUOTA: (_RED, "Quota hit"),
    run_report.REASON_ERROR: (_RED, "Error"),
    run_report.REASON_NO_ARTIFACT: (_RED, "No work product"),
    run_report.REASON_STALLED: (_RED, "Stalled"),
    run_report.REASON_INTERRUPTED: (_RED, "Interrupted"),
    run_report.REASON_CRASHED: (_RED, "Crashed"),
    run_report.REASON_LEFT_IN_PLACE: (_RED, "Left in place"),
}


def _status_badge(storage: StorageService) -> tuple[str, str, str]:
    """``(color, label, detail)`` for the run-state pill.

    No report on disk at all means no `sandglass execute` has run here yet --
    that is "Idle", not an error.
    """
    report = run_report.load(storage)
    if report is None:
        return _IDLE_COLOR, "Idle", "No run recorded yet."
    reason = run_report.effective_reason(report)
    color, label = _STATUS_COLORS.get(reason, (_IDLE_COLOR, reason.replace("_", " ").title()))
    _headline, what, _next = run_report.explain(report)
    return color, label, what


def _phase_rows(phases: dict[str, tuple[int, int]]) -> str:
    if not phases:
        return ""
    max_total = max(done + remaining for done, remaining in phases.values()) or 1
    rows = []
    for name, (done, remaining) in phases.items():
        total = done + remaining
        pct = round((done / total) * 100) if total else 0
        width_pct = round((total / max_total) * 100)
        rows.append(
            '<div class="phase-row">'
            f'<div class="phase-name">{html.escape(name)}</div>'
            f'<div class="phase-track" style="--w:{width_pct}%">'
            f'<div class="phase-fill" style="--p:{pct}%"></div>'
            "</div>"
            f'<div class="phase-count">{done}/{total}</div>'
            "</div>"
        )
    return (
        '<section class="card">'
        '<h2>Phases</h2>'
        '<div class="phases">' + "".join(rows) + "</div>"
        "</section>"
    )


def _control_bar(live: bool) -> str:
    """The Run/Stop buttons and the live log, or nothing on a static page.

    Omitted entirely from a file written to disk. A `file://` page has nothing
    to send a request to, so rendering buttons there would produce controls that
    look enabled and silently do nothing -- worse than not offering them. They
    appear only when `sandglass ui` is serving the page and there is a process
    on the other end able to act on a click.
    """
    if not live:
        return ""
    return """
    <section class="card">
      <h2>Run</h2>
      <div class="controls">
        <button id="btn-run" class="btn btn-go">▶ Run queue</button>
        <button id="btn-stop" class="btn btn-stop" disabled>■ Stop</button>
        <span id="run-state" class="run-state">idle</span>
      </div>
      <div id="toast" class="toast"></div>
      <pre id="log" class="log" hidden></pre>
    </section>

    <section class="card">
      <h2>Queue</h2>
      <div class="controls">
        <button class="btn" data-cmd="queue-list">Show queue</button>
        <button class="btn" data-cmd="queue-lint">Check for problems</button>
        <button class="btn" data-cmd="dry-run">Dry run</button>
        <button class="btn" data-cmd="why">Why did it stop?</button>
        <button class="btn btn-stop" data-cmd="queue-clear" data-confirm="Clear it — blocks stay in your markdown">Clear queue</button>
      </div>
      <div class="hint">
        Everything here is free — nothing above sends a prompt to a model.
        Clearing empties Sandglass&rsquo;s copy of the queue; blocks that came
        from a markdown file are still in that file and re-import on the next run.
      </div>
      <pre id="cmdout" class="log" hidden></pre>
    </section>
    """


def _live_script(key: str) -> str:
    """The client half of the control panel.

    Two loops, deliberately at different rates and doing different things:

    - **State**, every 1.2s -- cheap JSON. Whether a run is going, and any
      output produced since the last poll, asked for by cursor so a long run
      never re-sends its whole log.
    - **Cards**, every 6s -- re-fetches this same page and swaps only `#cards`.
      Everything on the page is already rendered server-side, so this keeps one
      source of truth for how a card looks instead of a second one in JS. The
      control bar and the log live outside `#cards` precisely so a refresh
      cannot scroll the log or steal focus from a button mid-click.
    """
    return (
        "<script>\n"
        "(function () {\n"
        f"  var KEY = {json.dumps(key)};\n"
        "  var cursor = 0, running = false;\n"
        "  var $ = function (id) { return document.getElementById(id); };\n"
        "  function toast(msg, bad) {\n"
        "    var el = $('toast');\n"
        "    el.textContent = msg || '';\n"
        "    el.style.color = bad ? '#ef4444' : '';\n"
        "  }\n"
        "  function post(path, body) {\n"
        "    return fetch(path + '?k=' + encodeURIComponent(KEY), {\n"
        "      method: 'POST',\n"
        "      headers: { 'Content-Type': 'application/json' },\n"
        "      body: JSON.stringify(body || {})\n"
        "    }).then(function (r) { return r.json(); })\n"
        "      .then(function (d) { toast(d.message, !d.ok); return d; })\n"
        "      .catch(function (e) { toast('Lost contact with Sandglass: ' + e, true); });\n"
        "  }\n"
        "  function setRunning(on) {\n"
        "    running = on;\n"
        "    $('btn-run').disabled = on;\n"
        "    $('btn-stop').disabled = !on;\n"
        "    $('run-state').textContent = on ? 'running' : 'idle';\n"
        "  }\n"
        "  function pollState() {\n"
        "    fetch('/api/state?k=' + encodeURIComponent(KEY) + '&since=' + cursor)\n"
        "      .then(function (r) { return r.json(); })\n"
        "      .then(function (d) {\n"
        "        if (d.running !== running) { setRunning(d.running); }\n"
        "        cursor = d.cursor;\n"
        "        if (d.lines && d.lines.length) {\n"
        "          var log = $('log');\n"
        "          log.hidden = false;\n"
        "          // Only auto-scroll when already at the bottom, so reading\n"
        "          // back through a run isn't yanked away by new output.\n"
        "          var atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;\n"
        "          log.textContent += d.lines.join('\\n') + '\\n';\n"
        "          if (atEnd) { log.scrollTop = log.scrollHeight; }\n"
        "        }\n"
        "      })\n"
        "      .catch(function () { $('run-state').textContent = 'disconnected'; });\n"
        "  }\n"
        "  function refreshCards() {\n"
        "    fetch('/?k=' + encodeURIComponent(KEY))\n"
        "      .then(function (r) { return r.text(); })\n"
        "      .then(function (html) {\n"
        "        var fresh = new DOMParser().parseFromString(html, 'text/html')\n"
        "          .getElementById('cards');\n"
        "        if (fresh) { $('cards').innerHTML = fresh.innerHTML; }\n"
        "      })\n"
        "      .catch(function () { /* a dropped refresh is harmless; the next one retries */ });\n"
        "  }\n"
        "  $('btn-run').addEventListener('click', function () {\n"
        "    setRunning(true);\n"
        "    post('/api/run').then(function (d) { if (d && !d.ok) { setRunning(false); } });\n"
        "  });\n"
        "  $('btn-stop').addEventListener('click', function () {\n"
        "    $('btn-stop').disabled = true;\n"
        "    toast('Stopping after the current block...');\n"
        "    post('/api/stop');\n"
        "  });\n"
        "  // Destructive buttons arm on the first click and fire on the second,\n"
        "  // rather than opening a confirm() dialog: the question stays where\n"
        "  // the button is, and a stray click cannot clear a queue.\n"
        "  var armed = null;\n"
        "  function disarm() {\n"
        "    if (!armed) { return; }\n"
        "    armed.textContent = armed.dataset.label;\n"
        "    armed.classList.remove('btn-armed');\n"
        "    armed = null;\n"
        "  }\n"
        "  function runCommand(btn) {\n"
        "    var name = btn.dataset.cmd;\n"
        "    var out = $('cmdout');\n"
        "    btn.disabled = true;\n"
        "    toast('Running ' + name + '...');\n"
        "    post('/api/command', { name: name, confirm: true }).then(function (d) {\n"
        "      btn.disabled = false;\n"
        "      if (!d) { return; }\n"
        "      out.hidden = false;\n"
        "      out.textContent = d.output || d.message || '(no output)';\n"
        "      out.scrollTop = 0;\n"
        "      refreshCards();\n"
        "    });\n"
        "  }\n"
        "  document.addEventListener('click', function (ev) {\n"
        "    var btn = ev.target.closest ? ev.target.closest('[data-cmd]') : null;\n"
        "    if (!btn) { disarm(); return; }\n"
        "    if (!btn.dataset.confirm) { runCommand(btn); return; }\n"
        "    if (armed === btn) { disarm(); runCommand(btn); return; }\n"
        "    disarm();\n"
        "    armed = btn;\n"
        "    btn.dataset.label = btn.dataset.label || btn.textContent;\n"
        "    btn.textContent = btn.dataset.confirm;\n"
        "    btn.classList.add('btn-armed');\n"
        "  });\n"
        "  // Delegated, because the account rows are replaced wholesale by\n"
        "  // every card refresh and per-row listeners would not survive it.\n"
        "  document.addEventListener('click', function (ev) {\n"
        "    var btn = ev.target.closest ? ev.target.closest('.acct-toggle') : null;\n"
        "    if (!btn) { return; }\n"
        "    btn.disabled = true;\n"
        "    post('/api/account', {\n"
        "      name: btn.dataset.name,\n"
        "      enabled: btn.dataset.enable === 'true'\n"
        "    }).then(refreshCards);\n"
        "  });\n"
        "  pollState();\n"
        "  setInterval(pollState, 1200);\n"
        "  setInterval(refreshCards, 6000);\n"
        "})();\n"
        "</script>"
    )


def _accounts_card(storage: StorageService, live: bool = False) -> str:
    """Render the pooled subscriptions and which of them a run may use now.

    Omitted entirely when there is no accounts file, which is the normal case
    for a single-subscription machine -- an empty card would just be a question
    mark on every dashboard that will never have an answer.

    **Only names and states are read.** `Account.__repr__` keeps tokens out of
    logs for the same reason this keeps them out of the page: `dashboard.html`
    lives in `.sandglass/`, inside the project tree, where any block running
    under `bypassPermissions` can read it.
    """
    try:
        from .accounts import AccountPool

        pool = AccountPool.load()
        if pool is None:
            return ""
        pool.state_path = storage.accounts_state_path
        pool.load_state()
        accounts = list(pool.accounts)
    except Exception as exc:  # noqa: BLE001 - see below
        # Deliberately broad. This runs after every completed block in an
        # unattended run; a malformed accounts file must cost the dashboard one
        # card, never the run. The CLI reports the same fault properly, with a
        # human present to read it.
        logger.warning("Could not read the account pool for the dashboard: %s", exc)
        return ""

    if not accounts:
        return ""

    rows = []
    usable = 0
    for account in accounts:
        if not account.enabled:
            color, label = _IDLE_COLOR, "disabled"
        elif account.is_available():
            color, label = _GREEN, "quota available"
            usable += 1
        else:
            # Local wall-clock, matching `sandglass accounts`: this line answers
            # "how long until it can run again", and 14:35 answers that at a
            # glance in a way an ISO timestamp does not.
            back = datetime.fromtimestamp(account.exhausted_until).strftime("%H:%M")
            color, label = _YELLOW, f"spent — back around {back}"
        # On a served page each row also carries the one control that makes
        # sense for it: park it, or bring it back. The button is rendered from
        # the same state the label is, so the two can never disagree.
        toggle = ""
        if live:
            action = "enable" if not account.enabled else "disable"
            toggle = (
                f'<button class="btn btn-mini acct-toggle" data-name="'
                f'{html.escape(account.name, quote=True)}" data-enable='
                f'"{"true" if not account.enabled else "false"}">'
                f'{"Enable" if action == "enable" else "Park"}</button>'
            )
        rows.append(
            '<div class="acct">'
            f'<span class="acct-dot" style="--c:{color}"></span>'
            f'<span class="acct-name">{html.escape(account.name)}</span>'
            f'<span class="acct-state">{html.escape(label)}</span>'
            f"{toggle}"
            "</div>"
        )

    summary = f"{usable} of {len(accounts)} ready to run"
    return (
        '<section class="card">'
        "<h2>Accounts</h2>"
        '<div class="accts">' + "".join(rows) + "</div>"
        f'<div class="status-detail">{html.escape(summary)}</div>'
        "</section>"
    )


def _providers_card(registry=None) -> str:
    """Render the non-Anthropic endpoints a block can be routed to.

    Kept as a separate card from Accounts rather than four rows in one list,
    because the two are not the same kind of thing and reading them as one would
    be actively misleading. A Claude account is flat-rate with a quota that
    refreshes on a clock, and the pool reaches for the next one by itself. A
    provider is metered, has a balance rather than a window, and is **never**
    used unless a block asks for it by name -- so "deepseek: ready" must not be
    read as "some of my work is going there".

    ``registry`` is the run's live one when this is regenerated mid-run, which
    is the only way the out-of-credit state can be known: it is per-run and
    deliberately never persisted, so a dashboard generated from a different
    process cannot see it and does not pretend to.

    **Key counts, never keys.** Same reason as the accounts card: this page is
    written inside the project tree.
    """
    try:
        from . import providers as providers_mod

        if registry is None:
            registry = providers_mod.ProviderRegistry.load()
        known = sorted(providers_mod.PROVIDERS.items())
    except Exception as exc:  # noqa: BLE001 - a broken file costs a card, not a run
        logger.warning("Could not read providers for the dashboard: %s", exc)
        return ""

    if not known:
        return ""

    rows = []
    configured = 0
    for name, provider in known:
        key = registry.key_for(name) if registry else None
        count = registry.key_count(name) if registry else 0
        spent = bool(registry and registry.is_out_of_credit(name))

        if spent:
            # Only ever true mid-run. A human topping the account up is what
            # undoes this, not waiting, so it is phrased as an instruction.
            color, label = _RED, "out of credit — blocks fall back to Claude"
        elif key:
            problem = providers_mod.looks_malformed(key)
            if problem:
                color, label = _YELLOW, f"key looks wrong: {problem}"
            else:
                configured += 1
                # The count is operational, not trivia: with one key, running
                # out mid-run puts every block marked for it back on Claude
                # quota; with two, the run just moves to the second.
                color = _GREEN
                label = f"{count} keys · metered" if count > 1 else "1 key · metered"
        else:
            color, label = _IDLE_COLOR, f"no key — sandglass providers set {name}"

        rows.append(
            '<div class="acct">'
            f'<span class="acct-dot" style="--c:{color}"></span>'
            f'<span class="acct-name">{html.escape(name)}</span>'
            f'<span class="acct-state">{html.escape(label)}</span>'
            "<span></span>"
            "</div>"
        )

    note = (
        "Pay-per-token, and opt-in per block — a block reaches one of these only "
        "by asking (provider: / CLINE:). Nothing routes here on its own."
    )
    return (
        '<section class="card">'
        "<h2>External providers</h2>"
        '<div class="accts">' + "".join(rows) + "</div>"
        f'<div class="status-detail">{html.escape(note)}</div>'
        "</section>"
    )


def generate(
    source_file: str,
    title: str,
    storage: StorageService | None = None,
    live_key: str | None = None,
    registry=None,
) -> str:
    """Render the dashboard as a self-contained HTML string.

    ``source_file`` is the markdown queue source (``prompt_tools/future_prompts.md``
    by convention) -- the same argument `prompt_source.throughput` and
    `phase_breakdown` already take, so this reads exactly what those do.

    ``live_key`` turns the page from a display into a control panel: it is the
    per-process key `webui.serve` mints, and passing it adds the Run/Stop
    buttons, the account toggles and the live log, all of which need something
    listening to be anything other than decoration. A page written to a file
    never gets one, so the static dashboard stays exactly what it was.
    """
    storage = storage or StorageService()
    counted = prompt_source.throughput(source_file)
    phases = prompt_source.phase_breakdown(source_file)
    color, status_label, status_detail = _status_badge(storage)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if counted is None:
        progress_block = (
            '<div class="ring-empty">No blocks queued or completed yet.</div>'
        )
    else:
        done, remaining, total, pct = counted
        progress_block = f"""
        <div class="ring" style="--pct:{pct}">
          <div class="ring-pct">{pct}%</div>
        </div>
        <div class="stat-row">
          <div class="stat"><span class="stat-n">{done}</span><span class="stat-l">done</span></div>
          <div class="stat"><span class="stat-n">{remaining}</span><span class="stat-l">remaining</span></div>
          <div class="stat"><span class="stat-n">{total}</span><span class="stat-l">total</span></div>
        </div>
        """

    live = bool(live_key)
    phases_section = _phase_rows(phases)
    accounts_section = _accounts_card(storage, live=live)
    providers_section = _providers_card(registry)
    control_section = _control_bar(live)
    # A served page refreshes itself from JavaScript, which can leave the log
    # scrolled where you left it and a button focused. A meta refresh would
    # throw both away every few seconds, mid-click.
    refresh_meta = (
        "" if live else f'<meta http-equiv="refresh" content="{_REFRESH_SECONDS}">'
    )
    footer_text = (
        "Live — buttons act on this machine. Ctrl-C in the terminal closes it."
        if live
        else f"Auto-refreshes every {_REFRESH_SECONDS}s — regenerated by "
        "Sandglass after every completed block."
    )
    live_script = _live_script(live_key) if live else ""
    safe_title = html.escape(title)
    icon_uri = _icon_data_uri()
    hero_section = (
        f"""
    <section class="hero">
      <div class="medallion">
        <div class="halo-spin"></div>
        <div class="halo-glow"></div>
        <img src="{icon_uri}" alt="" width="190" height="190">
      </div>
    </section>
    """
        if icon_uri
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{safe_title} — Sandglass</title>
{refresh_meta}
<style>
  :root {{
    --bg: #0b0f17;
    --card: #131a26;
    --border: #232d3f;
    --text: #e7ecf3;
    --muted: #8b97ab;
    --accent: #7c9eff;
    --accent-2: #a78bfa;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 2.5rem 1.5rem;
    background: radial-gradient(circle at top, #111827 0%, var(--bg) 55%);
    color: var(--text);
    font-family: -apple-system, "Segoe UI", Inter, Roboto, sans-serif;
    display: flex;
    justify-content: center;
  }}
  .page {{ width: 100%; max-width: 640px; }}
  header {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: 1.75rem;
    flex-wrap: wrap;
    gap: 0.5rem;
  }}
  h1 {{
    font-size: 1.6rem;
    margin: 0;
    background: linear-gradient(90deg, var(--accent), var(--accent-2));
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }}
  .timestamp {{ color: var(--muted); font-size: 0.8rem; }}
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 1.5rem;
    margin-bottom: 1.25rem;
    box-shadow: 0 12px 30px -18px rgba(0,0,0,0.6);
  }}
  .card h2 {{
    margin: 0 0 1rem;
    font-size: 0.8rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
  }}
  .badge {{
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.8rem;
    border-radius: 999px;
    font-size: 0.85rem;
    font-weight: 600;
    background: color-mix(in srgb, {color} 18%, transparent);
    color: {color};
    border: 1px solid color-mix(in srgb, {color} 40%, transparent);
  }}
  .badge .dot {{
    width: 0.5rem; height: 0.5rem; border-radius: 50%;
    background: {color};
    box-shadow: 0 0 8px {color};
  }}
  .status-detail {{ margin-top: 0.75rem; color: var(--muted); font-size: 0.9rem; line-height: 1.4; }}
  .ring {{
    --size: 168px;
    width: var(--size); height: var(--size);
    border-radius: 50%;
    margin: 0.25rem auto 1.5rem;
    display: flex; align-items: center; justify-content: center;
    background:
      radial-gradient(closest-side, var(--card) 76%, transparent 77% 100%),
      conic-gradient(var(--accent) calc(var(--pct) * 1%), var(--border) 0);
  }}
  .ring-pct {{ font-size: 2.1rem; font-weight: 700; }}
  .ring-empty {{ text-align: center; color: var(--muted); padding: 2rem 0; }}
  .stat-row {{ display: flex; justify-content: space-around; text-align: center; }}
  .stat {{ display: flex; flex-direction: column; }}
  .stat-n {{ font-size: 1.3rem; font-weight: 700; }}
  .stat-l {{ font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
  .phases {{ display: flex; flex-direction: column; gap: 0.9rem; }}
  .phase-row {{ display: grid; grid-template-columns: 1fr 2.2fr auto; align-items: center; gap: 0.75rem; }}
  .phase-name {{ font-size: 0.85rem; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .phase-track {{
    height: 10px; border-radius: 999px; background: var(--border);
    width: 100%; position: relative; overflow: hidden;
  }}
  .phase-track::before {{
    content: ""; position: absolute; inset: 0; width: var(--w);
    background: var(--border);
  }}
  .phase-fill {{
    position: absolute; inset: 0; width: calc(var(--w) * var(--p) / 100);
    background: linear-gradient(90deg, var(--accent), var(--accent-2));
    border-radius: 999px;
  }}
  .phase-count {{ font-size: 0.8rem; color: var(--muted); min-width: 3.5rem; text-align: right; }}
  .accts {{ display: flex; flex-direction: column; gap: 0.55rem; }}
  .acct {{ display: grid; grid-template-columns: auto 1fr auto auto; align-items: center; gap: 0.6rem; }}
  .acct-dot {{
    width: 9px; height: 9px; border-radius: 50%; background: var(--c);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--c) 22%, transparent);
  }}
  .acct-name {{ font-size: 0.85rem; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .acct-state {{ font-size: 0.78rem; color: var(--muted); text-align: right; }}
  .controls {{ display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; }}
  .btn {{
    font: inherit; font-size: 0.85rem; font-weight: 600; letter-spacing: 0.01em;
    color: var(--text); background: var(--card); border: 1px solid var(--border);
    border-radius: 9px; padding: 0.5rem 1rem; cursor: pointer;
    transition: transform 0.08s ease, filter 0.15s ease, opacity 0.15s ease;
  }}
  .btn:hover:not(:disabled) {{ filter: brightness(1.25); }}
  .btn:active:not(:disabled) {{ transform: translateY(1px); }}
  .btn:disabled {{ opacity: 0.4; cursor: default; }}
  .btn-go {{ background: linear-gradient(135deg, var(--accent), var(--accent-2)); border-color: transparent; color: #0b0f17; }}
  .btn-stop {{ border-color: {_RED}; color: {_RED}; }}
  .btn-mini {{ padding: 0.25rem 0.6rem; font-size: 0.72rem; font-weight: 500; }}
  .run-state {{ font-size: 0.8rem; color: var(--muted); }}
  .hint {{ margin-top: 0.7rem; font-size: 0.75rem; line-height: 1.55; color: var(--muted); }}
  .btn-armed {{ border-color: {_RED}; background: {_RED}; color: #0b0f17; }}
  .toast {{ margin-top: 0.7rem; font-size: 0.8rem; color: var(--muted); min-height: 1.1em; }}
  .log {{
    margin-top: 0.9rem; max-height: 22rem; overflow: auto; white-space: pre-wrap;
    word-break: break-word; font-size: 0.75rem; line-height: 1.5;
    color: var(--muted); background: var(--bg); border: 1px solid var(--border);
    border-radius: 9px; padding: 0.75rem 0.9rem;
  }}
  footer {{ text-align: center; color: var(--muted); font-size: 0.75rem; margin-top: 1rem; }}

  .hero {{ display: flex; justify-content: center; padding: 0.5rem 0 2rem; }}
  .medallion {{
    position: relative;
    width: 190px; height: 190px;
    animation: sway 6s ease-in-out infinite;
  }}
  .medallion img {{
    position: relative; z-index: 2;
    width: 100%; height: 100%;
    object-fit: cover;
    border-radius: 50%;
    display: block;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.08), 0 22px 45px -15px rgba(0,0,0,0.65);
  }}
  .halo-spin {{
    position: absolute; inset: -28px;
    border-radius: 50%;
    background: conic-gradient(from 0deg, #2f7d7d, #e0a952, #7c9eff, #2f7d7d);
    filter: blur(20px);
    opacity: 0.55;
    animation: spin 14s linear infinite;
  }}
  .halo-glow {{
    position: absolute; inset: -8px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(224,169,82,0.4), rgba(45,125,125,0.28) 55%, transparent 75%);
    animation: breathe 4s ease-in-out infinite;
  }}
  @keyframes sway {{
    0%, 100% {{ transform: rotate(-2.5deg); }}
    50% {{ transform: rotate(2.5deg); }}
  }}
  @keyframes spin {{
    to {{ transform: rotate(360deg); }}
  }}
  @keyframes breathe {{
    0%, 100% {{ transform: scale(0.94); opacity: 0.55; }}
    50% {{ transform: scale(1.07); opacity: 1; }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .medallion, .halo-spin, .halo-glow {{ animation: none; }}
  }}
</style>
</head>
<body>
  <div class="page">
    <header>
      <h1>{safe_title}</h1>
      <div class="timestamp">Updated {generated_at}</div>
    </header>
    {hero_section}
    {control_section}
    <div id="cards">
    <section class="card">
      <h2>Status</h2>
      <span class="badge"><span class="dot"></span>{html.escape(status_label)}</span>
      <div class="status-detail">{html.escape(status_detail)}</div>
    </section>

    <section class="card">
      <h2>Overall progress</h2>
      {progress_block}
    </section>

    {phases_section}

    {accounts_section}

    {providers_section}
    </div>

    <footer>{footer_text}</footer>
  </div>
{live_script}
</body>
</html>
"""


def write(
    source_file: str,
    title: str,
    storage: StorageService | None = None,
    registry=None,
) -> str:
    """Generate and save the dashboard, returning the path written to.

    ``registry`` is the run's live `ProviderRegistry` when a run is what is
    calling. Passing it is the only way the page can show a vendor that has run
    out of credit, since that state is per-run and never written to disk.
    """
    storage = storage or StorageService()
    storage.ensure_sandglass_dir()
    path = os.path.join(storage.base_path, DASHBOARD_FILENAME)
    html_text = generate(source_file, title, storage=storage, registry=registry)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    return path


def open_in_browser(path: str) -> None:
    webbrowser.open(f"file://{os.path.abspath(path)}")

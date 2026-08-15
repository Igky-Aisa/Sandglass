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

import html
import os
import webbrowser
from datetime import datetime, timezone

from . import prompt_source, run_report
from .storage import StorageService

DASHBOARD_FILENAME = "dashboard.html"
_REFRESH_SECONDS = 8

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


def generate(source_file: str, title: str, storage: StorageService | None = None) -> str:
    """Render the dashboard as a self-contained HTML string.

    ``source_file`` is the markdown queue source (``prompt_tools/future_prompts.md``
    by convention) -- the same argument `prompt_source.throughput` and
    `phase_breakdown` already take, so this reads exactly what those do.
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

    phases_section = _phase_rows(phases)
    safe_title = html.escape(title)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{safe_title} — Sandglass</title>
<meta http-equiv="refresh" content="{_REFRESH_SECONDS}">
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
  footer {{ text-align: center; color: var(--muted); font-size: 0.75rem; margin-top: 1rem; }}
</style>
</head>
<body>
  <div class="page">
    <header>
      <h1>{safe_title}</h1>
      <div class="timestamp">Updated {generated_at}</div>
    </header>

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

    <footer>Auto-refreshes every {_REFRESH_SECONDS}s — regenerated by Sandglass after every completed block.</footer>
  </div>
</body>
</html>
"""


def write(source_file: str, title: str, storage: StorageService | None = None) -> str:
    """Generate and save the dashboard, returning the path written to."""
    storage = storage or StorageService()
    storage.ensure_sandglass_dir()
    path = os.path.join(storage.base_path, DASHBOARD_FILENAME)
    html_text = generate(source_file, title, storage=storage)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    return path


def open_in_browser(path: str) -> None:
    webbrowser.open(f"file://{os.path.abspath(path)}")

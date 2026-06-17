"""Render the RPC execution results to Markdown + JSON."""

from __future__ import annotations

import json
from pathlib import Path

from sentinel.execute.models import ExecCaseSet, ExecReport, UIReport

_ICON = {"pass": "✅", "fail": "❌", "error": "⚠️"}
_UI_ICON = {"ok": "✅", "issues": "❌", "load_error": "⚠️"}


def render_markdown(report: ExecReport, cases: ExecCaseSet) -> str:
    roll = report.rollup()
    by_id = {c.id: c for c in cases.cases}
    lines = [
        f"# Sentinel — RPC Test Results: `{report.module}`",
        "",
        f"- **Executed against:** `{report.db}`"
        + (f"  _(clone of `{report.source_db}`)_" if report.cloned else "  _(existing DB — explicit opt-in)_"),
        f"- **Results:** {roll['pass']} passed · {roll['fail']} failed · {roll['error']} errored "
        f"({len(report.results)} cases)",
        "",
        "> **fail** = the module behaved differently than asserted (often a confirmed bug). "
        "**error** = an unexpected RPC fault during setup (usually an invalid case, not a defect).",
        "",
        "| # | Case | Result | Detail |",
        "|---|------|--------|--------|",
    ]
    for c in report.results:
        detail = c.message or "all steps passed"
        lines.append(f"| {c.id} | {c.title} | {_ICON.get(c.status, '?')} {c.status} | {detail} |")

    # Per-case step detail for anything that didn't pass.
    notable = [c for c in report.results if c.status != "pass"]
    if notable:
        lines += ["", "## Details (failed / errored cases)", ""]
        for c in notable:
            src = by_id.get(c.id)
            lines.append(f"### {_ICON.get(c.status)} {c.id} — {c.title}")
            if src and src.note:
                lines.append(f"_Probing: {src.note}_")
            for s in c.steps:
                mark = "✓" if s.ok else "✗"
                lines.append(f"- {mark} `{s.op}` — {s.detail}")
            lines.append("")
    return "\n".join(lines)


def write_report(report: ExecReport, cases: ExecCaseSet, out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.md").write_text(render_markdown(report, cases), encoding="utf-8")
    (out_dir / "results.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / "cases.json").write_text(cases.model_dump_json(indent=2), encoding="utf-8")
    return {
        "results_md": str(out_dir / "results.md"),
        "results_json": str(out_dir / "results.json"),
        "cases_json": str(out_dir / "cases.json"),
        "dir": str(out_dir),
    }


def render_ui_markdown(report: UIReport) -> str:
    roll = report.rollup()
    lines = [
        f"# Sentinel — UI Smoke Crawl: `{report.module}`",
        "",
        f"- **Web client:** {report.url}  (db `{report.db}`)",
        f"- **Pages:** {roll['ok']} clean · {roll['issues']} with issues · {roll['load_error']} failed to load "
        f"({len(report.pages)} actions)",
        "",
        "> Each page is the addon's window action opened in the Odoo web client. **issues** = console "
        "errors, uncaught JS, a 5xx request, or an Odoo error dialog. **load_error** = the page didn't load.",
        "",
        "| Action | Result | Signal |",
        "|--------|--------|--------|",
    ]
    for p in report.pages:
        sig = "clean"
        if p.status != "ok":
            bits = []
            if p.error_dialog:
                bits.append("error dialog")
            if p.page_errors:
                bits.append(f"{len(p.page_errors)} JS error(s)")
            if p.console_errors:
                bits.append(f"{len(p.console_errors)} console error(s)")
            if p.failed_requests:
                bits.append(f"{len(p.failed_requests)} failed request(s)")
            sig = "; ".join(bits) or p.status
        lines.append(f"| {p.name} (#{p.action_id}) | {_UI_ICON.get(p.status, '?')} {p.status} | {sig} |")

    notable = [p for p in report.pages if p.status != "ok"]
    if notable:
        lines += ["", "## Details (pages with issues)", ""]
        for p in notable:
            lines.append(f"### {_UI_ICON.get(p.status)} {p.name} (#{p.action_id}) — `{p.model or ''}`")
            if p.error_dialog:
                lines.append(f"- **Error dialog:** {p.error_dialog}")
            for e in p.page_errors:
                lines.append(f"- **JS:** {e}")
            for e in p.console_errors:
                lines.append(f"- **console:** {e}")
            for f in p.failed_requests:
                lines.append(f"- **request:** {f}")
            if p.screenshot:
                lines.append(f"- _screenshot:_ `{p.screenshot}`")
            lines.append("")
    return "\n".join(lines)


def write_ui_report(report: UIReport, out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ui_results.md").write_text(render_ui_markdown(report), encoding="utf-8")
    (out_dir / "ui_results.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return {
        "ui_results_md": str(out_dir / "ui_results.md"),
        "ui_results_json": str(out_dir / "ui_results.json"),
        "dir": str(out_dir),
    }

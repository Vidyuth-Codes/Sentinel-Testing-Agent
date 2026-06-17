"""Drive the Claude Code engine to produce a structured Odoo audit.

Two passes:
  1. `generate_report` — read the addon + System Map, write the human Markdown report.
  2. `structure_report` — convert that report into JSON (Finding[] + TestPlan) and save.

Pass 2 is a cheap pure-transformation call (no code reading), so the expensive
code-reading work happens once. If pass 2's JSON can't be parsed, the Markdown report
is still saved and `AuditOutcome.structured` is False — we never lose the human report.
"""

from __future__ import annotations

import json
import re
from uuid import UUID, uuid4

from sentinel.audit.models import (
    AuditOutcome,
    AuditTestCase,
    RequirementCoverage,
    TestPlan,
)
from sentinel.core.models import CodeLocation, Evidence, Finding
from sentinel.engine import ClaudeCodeEngine, build_system_prompt
from sentinel.engine.claude_code import EngineResult
from sentinel.paths import run_dir

# --- prompts -----------------------------------------------------------------

REPORT_PROMPT = (
    "Produce a complete TEST PLAN and BUG/GAP REPORT for this Odoo module. "
    "Read the addon source in the granted directory and use the System Map. "
    "Follow the output format in your instructions: a requirement-coverage table, concrete "
    "test cases (channel rpc or ui), and a Findings section of real bugs/logic gaps — each with "
    "`file:line` (or `model.method`) evidence, the offending snippet, the impact, and a suggested "
    "fix. Be thorough but only report defects you can point to in the code. "
    "End with a short Coverage note: what you read and what you did NOT get to."
)

REPORT_PROMPT_NO_SOURCE = (
    "Produce a TEST PLAN and FUNCTIONAL FINDINGS report for this Odoo module from the System Map "
    "ONLY — no source code is available, so do NOT reference code or `file:line`. "
    "Cover: (1) a requirement-coverage table of the module's intended behaviours, inferred from its "
    "models, states, actions, menus and security (covered / partial / gap); (2) concrete test cases "
    "(channel rpc or ui) for the real `action_*` methods and state transitions; (3) UI and logic-FLOW "
    "findings — missing guards/validations the configuration implies, broken or risky flows, "
    "security/access gaps, and view/menu problems — each described functionally with a `Flow:` "
    "walkthrough (no code). End with a Coverage note: what the System Map showed, and what would need "
    "the source code or a live record to confirm."
)


def report_prompt(has_source: bool) -> str:
    return REPORT_PROMPT if has_source else REPORT_PROMPT_NO_SOURCE

_EXTRACT_SYSTEM = (
    "You convert an Odoo QA report into STRICT JSON for machine consumption. "
    "Output ONLY a single JSON object — no prose, no Markdown, no code fences. "
    "Preserve the report's findings and test cases faithfully; do not invent new ones. "
    "Use these enumerations exactly:\n"
    "  category: bug | logic-gap | security | performance | ui | integration | code-quality\n"
    "  layer:    backend | frontend | integration\n"
    "  severity: critical | high | medium | low | info\n"
    "  status:   covered | partial | gap\n"
    "  channel:  rpc | ui\n"
    "Schema:\n"
    "{\n"
    '  "findings": [ {"title": str, "category": str, "layer": str, "severity": str,\n'
    '                 "confidence": number(0..1), "file": str|null, "line": int|null,\n'
    '                 "evidence": str, "impact": str, "suggested_fix": str|null} ],\n'
    '  "requirement_coverage": [ {"behaviour": str, "status": str, "note": str|null} ],\n'
    '  "test_cases": [ {"id": str, "title": str, "type": str, "channel": str, "priority": str,\n'
    '                   "preconditions": str|null, "steps": [str], "expected": str} ],\n'
    '  "coverage_note": str|null\n'
    "}"
)


def _extract_user(report_md: str) -> str:
    return "Convert this Odoo QA report into the JSON schema. Report follows:\n\n" + report_md


# --- normalisation maps (LLM vocabulary -> canonical Finding vocabulary) ------

_CATEGORY_MAP = {
    "bug": "functional_bug", "functional": "functional_bug", "functional_bug": "functional_bug",
    "logic-gap": "logic_error", "logic_gap": "logic_error", "logic": "logic_error",
    "logic_error": "logic_error", "logic-error": "logic_error",
    "security": "security",
    "performance": "performance", "perf": "performance",
    "ui": "ui_visual", "ui_visual": "ui_visual", "ui-visual": "ui_visual",
    "integration": "integration_contract", "contract": "integration_contract",
    "integration_contract": "integration_contract", "integration-contract": "integration_contract",
    "code-quality": "code_quality", "code_quality": "code_quality", "quality": "code_quality",
    "runtime": "runtime_error", "runtime_error": "runtime_error",
    "accessibility": "accessibility", "a11y": "accessibility",
}
_LAYER_MAP = {
    "backend": "backend", "be": "backend", "python": "backend", "orm": "backend",
    "frontend": "frontend", "fe": "frontend", "ui": "frontend", "owl": "frontend", "js": "frontend",
    "integration": "integration", "contract": "integration",
}
_SEVERITIES = {"critical", "high", "medium", "low", "info"}
_SEVERITY_ALIASES = {"crit": "critical", "blocker": "critical", "major": "high",
                     "minor": "low", "med": "medium", "moderate": "medium", "informational": "info"}


def _norm_category(v: str | None) -> str:
    return _CATEGORY_MAP.get((v or "").strip().lower(), "functional_bug")


def _norm_layer(v: str | None) -> str:
    return _LAYER_MAP.get((v or "").strip().lower(), "backend")


def _norm_severity(v: str | None) -> str:
    s = (v or "").strip().lower()
    if s in _SEVERITIES:
        return s
    return _SEVERITY_ALIASES.get(s, "medium")


def _clamp_conf(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.6  # llm-only default
    return max(0.0, min(1.0, f))


# --- JSON parsing (robust to fences / stray prose) ---------------------------


def parse_json_object(text: str) -> dict:
    """Best-effort extraction of a single JSON object from an LLM response."""
    t = (text or "").strip()
    if t.startswith("```"):
        # drop the opening fence line (``` or ```json) and a trailing fence
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
        t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # fall back to the outermost {...} span
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        return json.loads(t[i : j + 1])
    raise ValueError("no JSON object found in extraction output")


# --- mapping raw dicts -> typed models ---------------------------------------


def _to_finding(run_id: UUID, raw: dict) -> Finding:
    title = str(raw.get("title") or "Untitled finding").strip()
    impact = str(raw.get("impact") or "").strip()
    file = raw.get("file") or None
    line = raw.get("line")
    line_start = int(line) if isinstance(line, int) or (isinstance(line, str) and str(line).isdigit()) else None
    evidence = raw.get("evidence") or None
    return Finding(
        run_id=run_id,
        title=title,
        description=impact or title,
        category=_norm_category(raw.get("category")),
        layer=_norm_layer(raw.get("layer")),
        severity=_norm_severity(raw.get("severity")),
        confidence=_clamp_conf(raw.get("confidence")),
        source="llm",
        location=CodeLocation(file=str(file) if file else None, line_start=line_start),
        evidence=Evidence(code_snippet=str(evidence) if evidence else None),
        suggested_fix=(str(raw["suggested_fix"]).strip() if raw.get("suggested_fix") else None),
    )


def _to_test_plan(data: dict) -> TestPlan:
    cov = []
    for r in data.get("requirement_coverage") or []:
        if not isinstance(r, dict):
            continue
        status = str(r.get("status") or "partial").strip().lower()
        cov.append(RequirementCoverage(
            behaviour=str(r.get("behaviour") or "").strip(),
            status=status if status in ("covered", "partial", "gap") else "partial",
            note=(str(r["note"]).strip() if r.get("note") else None),
        ))
    cases = []
    for c in data.get("test_cases") or []:
        if not isinstance(c, dict):
            continue
        steps = c.get("steps") or []
        cases.append(AuditTestCase(
            id=str(c.get("id") or f"TC-{len(cases) + 1:02d}"),
            title=str(c.get("title") or "").strip(),
            type=(str(c["type"]).strip() if c.get("type") else None),
            channel=(str(c["channel"]).strip().lower() if c.get("channel") else None),
            priority=(str(c["priority"]).strip().lower() if c.get("priority") else None),
            preconditions=(str(c["preconditions"]).strip() if c.get("preconditions") else None),
            steps=[str(s).strip() for s in steps if str(s).strip()],
            expected=(str(c["expected"]).strip() if c.get("expected") else None),
        ))
    return TestPlan(requirement_coverage=cov, test_cases=cases)


def map_extraction(run_id: UUID, data: dict) -> tuple[list[Finding], TestPlan, str | None]:
    findings = [_to_finding(run_id, f) for f in (data.get("findings") or []) if isinstance(f, dict)]
    test_plan = _to_test_plan(data)
    note = data.get("coverage_note")
    return findings, test_plan, (str(note).strip() if note else None)


# --- the two passes ----------------------------------------------------------


def _source_dir(addons: str | None) -> str | None:
    """The addon path only counts as 'source' if it's a real Odoo addon folder."""
    from pathlib import Path
    return addons if (addons and (Path(addons) / "__manifest__.py").exists()) else None


def generate_report(
    engine: ClaudeCodeEngine, *, module: str, addons: str | None,
    summary: str | None = "", timeout: int = 1200,
) -> EngineResult:
    """Pass 1 — write the Markdown report. With source: read the addon + ground in code.
    Without source: a System-Map-only functional/flow report (no code)."""
    src = _source_dir(addons)
    return engine.run_sync(
        report_prompt(src is not None),
        code_dir=src,
        system_prompt=build_system_prompt(summary or "", has_source=src is not None),
        max_turns=80,
        timeout=timeout,
    )


def structure_report(
    engine: ClaudeCodeEngine, *, module: str, report_md: str,
    pass1_cost: float | None = None, save: bool = True, timeout: int = 300,
) -> AuditOutcome:
    """Pass 2 — convert the Markdown report into structured JSON, then persist artifacts."""
    run_id = uuid4()
    findings: list[Finding] = []
    test_plan = TestPlan()
    note: str | None = None
    structured = True
    cost = pass1_cost or 0.0

    try:
        res = engine.run_sync(_extract_user(report_md), system_prompt=_EXTRACT_SYSTEM,
                              max_turns=2, timeout=timeout)
        cost += res.cost_usd or 0.0
        data = parse_json_object(res.text)
        findings, test_plan, note = map_extraction(run_id, data)
    except Exception:  # noqa: BLE001 — extraction is best-effort; the Markdown report is the source of truth
        structured = False

    outcome = AuditOutcome(
        module=module, markdown=report_md, findings=findings, test_plan=test_plan,
        coverage_note=note, cost_usd=round(cost, 6) if cost else None, structured=structured,
    )
    if save:
        outcome.saved = _save(outcome)
    return outcome


def run_full_audit(
    engine: ClaudeCodeEngine, *, module: str, addons: str | None,
    summary: str | None = "", save: bool = True, report_timeout: int = 1200,
) -> AuditOutcome:
    """Convenience: pass 1 then pass 2 (used by the CLI and the non-streaming endpoint)."""
    report = generate_report(engine, module=module, addons=addons, summary=summary,
                             timeout=report_timeout)
    return structure_report(engine, module=module, report_md=report.text,
                            pass1_cost=report.cost_usd, save=save)


# --- persistence -------------------------------------------------------------


def _save(outcome: AuditOutcome) -> dict[str, str]:
    out = run_dir(f"audit-{outcome.module}")
    (out / "report.md").write_text(outcome.markdown, encoding="utf-8")
    (out / "findings.json").write_text(
        json.dumps([f.model_dump(mode="json") for f in outcome.findings], indent=2),
        encoding="utf-8",
    )
    (out / "test_plan.json").write_text(
        outcome.test_plan.model_dump_json(indent=2), encoding="utf-8"
    )
    return {
        "report_md": str(out / "report.md"),
        "findings_json": str(out / "findings.json"),
        "test_plan_json": str(out / "test_plan.json"),
        "dir": str(out),
    }

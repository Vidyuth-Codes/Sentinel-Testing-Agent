"""Tests for the Phase 2 audit's structured-extraction layer (no live engine)."""

from uuid import uuid4

import pytest

from sentinel.audit.runner import map_extraction, parse_json_object

_RAW = {
    "findings": [
        {
            "title": "action_dispose does not guard current state",
            "category": "logic-gap",          # -> logic_error
            "layer": "be",                      # -> backend
            "severity": "crit",                 # -> critical
            "confidence": 1.4,                  # -> clamp to 1.0
            "file": "models/asset.py",
            "line": "212",                      # numeric string -> int
            "evidence": "def action_dispose(self): self.state = 'disposed'",
            "impact": "An asset can be disposed from any state, skipping approval.",
            "suggested_fix": "Guard on self.state in ('active',).",
        },
        {
            "title": "Mystery category falls back",
            "category": "wat",                  # unknown -> functional_bug
            "layer": "owl",                     # -> frontend
            "severity": "informational",        # -> info
            # confidence missing -> default 0.6
            "evidence": "x",
            "impact": "",
        },
    ],
    "requirement_coverage": [
        {"behaviour": "Dispose an asset", "status": "gap", "note": "no state guard"},
        {"behaviour": "Create an asset", "status": "covered"},
        {"behaviour": "Edge", "status": "weird"},  # unknown status -> partial
    ],
    "test_cases": [
        {"id": "TC-01", "title": "Dispose from draft is blocked", "type": "workflow",
         "channel": "RPC", "priority": "High", "steps": ["create draft asset", "call action_dispose"],
         "expected": "UserError raised"},
    ],
    "coverage_note": "Read models/asset.py; did not reach the wizard/ dir.",
}


def test_parse_json_object_strips_fences_and_prose():
    fenced = '```json\n{"a": 1, "b": [2, 3]}\n```'
    assert parse_json_object(fenced) == {"a": 1, "b": [2, 3]}
    prose = 'Here is the JSON you asked for:\n{"a": 1}\nHope that helps!'
    assert parse_json_object(prose) == {"a": 1}
    assert parse_json_object('{"x": true}') == {"x": True}


def test_parse_json_object_raises_when_no_object():
    with pytest.raises((ValueError, Exception)):
        parse_json_object("no json here at all")


def test_map_extraction_normalises_findings():
    run_id = uuid4()
    findings, plan, note = map_extraction(run_id, _RAW)

    assert len(findings) == 2
    f0 = findings[0]
    assert f0.run_id == run_id
    assert f0.category == "logic_error"
    assert f0.layer == "backend"
    assert f0.severity == "critical"
    assert f0.confidence == 1.0                      # clamped
    assert f0.source == "llm"
    assert f0.location.file == "models/asset.py"
    assert f0.location.line_start == 212             # string coerced to int
    assert f0.location.short() == "models/asset.py:212"
    assert "approval" in f0.description
    assert f0.evidence.code_snippet.startswith("def action_dispose")

    f1 = findings[1]
    assert f1.category == "functional_bug"           # unknown -> default
    assert f1.layer == "frontend"                    # owl -> frontend
    assert f1.severity == "info"
    assert f1.confidence == 0.6                       # default
    assert f1.description == f1.title                 # empty impact falls back to title


def test_map_extraction_builds_test_plan_and_coverage():
    findings, plan, note = map_extraction(uuid4(), _RAW)

    assert len(plan.test_cases) == 1
    tc = plan.test_cases[0]
    assert tc.id == "TC-01"
    assert tc.channel == "rpc"                        # lowercased
    assert tc.priority == "high"
    assert tc.steps == ["create draft asset", "call action_dispose"]

    roll = plan.coverage_rollup()
    assert roll == {"covered": 1, "partial": 1, "gap": 1}   # 'weird' -> partial
    assert note and "wizard" in note


def test_severity_rollup_counts():
    from sentinel.audit.models import AuditOutcome

    findings, plan, _ = map_extraction(uuid4(), _RAW)
    outcome = AuditOutcome(module="assetz", markdown="# report", findings=findings, test_plan=plan)
    roll = outcome.severity_rollup()
    assert roll["critical"] == 1
    assert roll["info"] == 1
    assert roll["high"] == 0

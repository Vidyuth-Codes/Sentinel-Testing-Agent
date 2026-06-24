"""Tests for false-positive control: dedup + adversarial verification."""

from __future__ import annotations

from uuid import uuid4

from sentinel.audit.verify import dedup_findings, verify_findings
from sentinel.core.models import CodeLocation, Evidence, Finding
from sentinel.engine.claude_code import EngineResult


def _finding(title, file, line, sev="high", conf=0.6):
    return Finding(
        run_id=uuid4(), title=title, description=title, category="logic_error",
        layer="backend", severity=sev, confidence=conf, source="llm",
        location=CodeLocation(file=file, line_start=line),
        evidence=Evidence(code_snippet="snippet"),
    )


class _FakeEngine:
    """Stands in for ClaudeCodeEngine: returns a fixed verdict for index 0."""

    def __init__(self, verdict_json: str):
        self.cli_path = "fake"
        self._json = verdict_json

    def available(self) -> bool:
        return True

    def run_sync(self, prompt, *, code_dir=None, system_prompt="", resume=None,
                 max_turns=40, timeout=600) -> EngineResult:
        return EngineResult(text=self._json, cost_usd=0.01)


def _make_addon(tmp_path):
    (tmp_path / "__manifest__.py").write_text("{'name': 't'}", encoding="utf-8")
    models = tmp_path / "models"
    models.mkdir()
    (models / "foo.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    return str(tmp_path)


def test_dedup_merges_same_defect():
    a = _finding("Manager approval bypass", "models/foo.py", 10, sev="high", conf=0.6)
    b = _finding("manager  approval   bypass!", "models/foo.py", 10, sev="critical", conf=0.9)
    out = dedup_findings([a, b])
    assert len(out) == 1
    assert out[0].severity == "critical"  # kept the stronger one
    assert out[0].dedup_key  # stamped


def test_verify_refutes_missing_file_deterministically(tmp_path):
    addon = _make_addon(tmp_path)
    f = _finding("Ghost bug", "models/does_not_exist.py", 5)
    engine = _FakeEngine('{"verdicts": []}')
    out, stats = verify_findings(engine, [f], addons=addon)
    assert out[0].status == "false_positive"
    assert out[0].verified is False
    assert "not found" in (out[0].evidence.tool_output or "")
    assert stats["refuted"] == 1


def test_verify_confirms_real_finding_via_llm(tmp_path):
    addon = _make_addon(tmp_path)
    f = _finding("Real bug in foo", "models/foo.py", 1)
    engine = _FakeEngine('{"verdicts":[{"i":0,"real":true,"reason":"confirmed in code","confidence":0.9}]}')
    out, stats = verify_findings(engine, [f], addons=addon)
    assert out[0].verified is True
    assert out[0].status == "verified"
    assert out[0].confidence >= 0.8
    assert stats["verified"] == 1


def test_verify_drops_llm_false_positive(tmp_path):
    addon = _make_addon(tmp_path)
    f = _finding("Overclaimed bug", "models/foo.py", 1, conf=0.8)
    engine = _FakeEngine('{"verdicts":[{"i":0,"real":false,"reason":"code already guards this","confidence":0.1}]}')
    out, stats = verify_findings(engine, [f], addons=addon)
    assert out[0].status == "false_positive"
    assert out[0].confidence <= 0.2
    assert stats["refuted"] == 1


def test_verify_skips_without_source():
    f = _finding("No source finding", "models/foo.py", 1)
    engine = _FakeEngine('{"verdicts": []}')
    out, stats = verify_findings(engine, [f], addons=None)
    assert stats["unverified"] == 1
    assert out[0].status == "new"  # untouched

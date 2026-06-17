"""Structured shapes for a Phase 2 audit: the test plan + the run outcome.

Findings reuse the canonical `core.models.Finding` schema (the detection↔reporting
contract). The test-plan shapes live here because they are specific to the Odoo QA
output format defined in `skills/odoo-qa/SKILL.md`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sentinel.core.models import SEVERITY_ORDER, Finding


class RequirementCoverage(BaseModel):
    behaviour: str
    status: str = "partial"  # covered | partial | gap
    note: str | None = None


class AuditTestCase(BaseModel):
    id: str
    title: str
    type: str | None = None  # functional | workflow | ui | security | validation
    channel: str | None = None  # rpc | ui
    priority: str | None = None  # high | medium | low
    preconditions: str | None = None
    steps: list[str] = Field(default_factory=list)
    expected: str | None = None


class TestPlan(BaseModel):
    requirement_coverage: list[RequirementCoverage] = Field(default_factory=list)
    test_cases: list[AuditTestCase] = Field(default_factory=list)

    def coverage_rollup(self) -> dict[str, int]:
        roll = {"covered": 0, "partial": 0, "gap": 0}
        for r in self.requirement_coverage:
            roll[r.status if r.status in roll else "partial"] += 1
        return roll


class AuditOutcome(BaseModel):
    """Everything a single audit produced — Markdown for humans, structure for tools."""

    module: str
    markdown: str  # the pass-1 human report (saved as report.md)
    findings: list[Finding] = Field(default_factory=list)
    test_plan: TestPlan = Field(default_factory=TestPlan)
    coverage_note: str | None = None
    cost_usd: float | None = None  # pass 1 + pass 2
    structured: bool = True  # False if pass-2 JSON extraction failed (Markdown still valid)
    saved: dict[str, str] = Field(default_factory=dict)  # artifact name -> path

    def severity_rollup(self) -> dict[str, int]:
        roll = {s: 0 for s in SEVERITY_ORDER}
        for f in self.findings:
            if f.severity in roll:
                roll[f.severity] += 1
        return roll

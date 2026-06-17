"""Core domain models for Sentinel — the structured-findings schema.

These mirror section 3 of the Low-Level Design. They are storage-agnostic:
findings/runs serialise to JSON files under `output/<run>/`. `Finding` is the
contract Phase 2 (Claude Code reasoning) will populate; a database is deferred.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

# --- Controlled vocabularies (the detection<->reporting contract) -------------

Category = Literal[
    "functional_bug",
    "logic_error",
    "ui_visual",
    "runtime_error",
    "integration_contract",
    "security",
    "accessibility",
    "performance",
    "code_quality",
]
Layer = Literal["frontend", "backend", "integration"]
Severity = Literal["critical", "high", "medium", "low", "info"]
Source = Literal["static", "llm", "dynamic_ui", "dynamic_api"]
Status = Literal["new", "verified", "false_positive", "wont_fix", "acknowledged"]

# Ordering helper so reports can sort high-impact first.
SEVERITY_ORDER: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "info": 4,
}


# --- Finding -----------------------------------------------------------------


class CodeLocation(BaseModel):
    file: str | None = None  # repo-relative path
    line_start: int | None = None
    line_end: int | None = None
    route: str | None = None  # UI findings, e.g. "/dashboard"
    endpoint: str | None = None  # API findings, e.g. "POST /api/login"

    def short(self) -> str:
        if self.file and self.line_start:
            return f"{self.file}:{self.line_start}"
        if self.file:
            return self.file
        if self.endpoint:
            return self.endpoint
        if self.route:
            return self.route
        return "(unknown location)"


class Evidence(BaseModel):
    screenshot_key: str | None = None
    console_log: str | None = None
    network_trace: str | None = None
    tool_output: str | None = None  # raw linter/scanner line
    code_snippet: str | None = None


class Finding(BaseModel):
    finding_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    title: str
    description: str
    category: Category
    layer: Layer
    severity: Severity
    confidence: float = 0.0  # 0.0–1.0
    source: Source
    location: CodeLocation = Field(default_factory=CodeLocation)
    evidence: Evidence = Field(default_factory=Evidence)
    repro_steps: list[str] = Field(default_factory=list)
    suggested_fix: str | None = None
    status: Status = "new"
    dedup_key: str | None = None
    verified: bool = False
    rule_id: str | None = None  # optional stable check id (set by a future deterministic pass)


# --- Project map / test plan (produced by ingest) ----------------------------


class ProjectMap(BaseModel):
    frontend_framework: str | None = None  # react|vue|angular|none
    backend_framework: str | None = None  # fastapi|express|flask|django|none
    languages: list[str] = Field(default_factory=list)
    run_commands: dict[str, str] = Field(default_factory=dict)
    routes: list[str] = Field(default_factory=list)
    endpoints: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    file_count: int = 0
    loc: int = 0


class TestPlanItem(BaseModel):
    id: str
    kind: Literal["static", "code_reasoning", "ui", "api", "a11y"]
    target: str
    status: Literal["pending", "running", "done", "skipped"] = "pending"
    skip_reason: str | None = None


# --- Run result (Phase 1: the whole audit output) ----------------------------


class RunResult(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    project_ref: str
    project_map: ProjectMap = Field(default_factory=ProjectMap)
    test_plan: list[TestPlanItem] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    coverage: dict[str, str] = Field(default_factory=dict)  # tool/layer -> "ran"|"skipped: ..."
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    def severity_rollup(self) -> dict[str, int]:
        rollup = {s: 0 for s in SEVERITY_ORDER}
        for f in self.findings:
            rollup[f.severity] += 1
        return rollup

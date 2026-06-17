"""Phase 2 — the Claude Code audit: a grounded bug/gap report + test plan, structured.

`run_full_audit` drives the Claude Code engine twice: pass 1 produces the human
Markdown report (read the addon, reason with the System Map); pass 2 converts that
report into structured JSON (`Finding[]` + `TestPlan`) which is saved alongside the
Markdown. See `Sentinel_Low_Level_Design.md` §4, §11.
"""

from sentinel.audit.models import (
    AuditOutcome,
    AuditTestCase,
    RequirementCoverage,
    TestPlan,
)
from sentinel.audit.runner import generate_report, run_full_audit, structure_report

__all__ = [
    "AuditOutcome",
    "AuditTestCase",
    "RequirementCoverage",
    "TestPlan",
    "generate_report",
    "run_full_audit",
    "structure_report",
]

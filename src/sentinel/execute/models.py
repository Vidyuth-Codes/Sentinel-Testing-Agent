"""Schemas for the Phase 3 RPC flow executor.

An `ExecCase` is a deterministic sequence of `ExecStep`s run over XML-RPC against a
throwaway database. Steps reference records created earlier via a small symbol table
(`ref` names), so a case can `create` a record, `call` an `action_*` method on it, and
`assert` the resulting field/state.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

StepOp = Literal["create", "search", "call", "write", "assert"]


class ExecStep(BaseModel):
    op: StepOp
    model: str | None = None
    # create / write
    values: dict[str, Any] = Field(default_factory=dict)
    ref: str | None = None  # name to store a created/searched id under
    # search
    domain: list = Field(default_factory=list)
    limit: int = 1
    # call
    ref_ids: list[str] = Field(default_factory=list)  # ref names whose ids to pass
    method: str | None = None
    args: list = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    expect: Literal["ok", "error"] = "ok"  # for call: succeed, or raise a UserError/ValidationError
    # assert
    field: str | None = None
    equals: Any = None


class ExecCase(BaseModel):
    id: str
    title: str
    model: str | None = None
    note: str | None = None  # what this case is probing (often a hypothesis from the audit)
    steps: list[ExecStep] = Field(default_factory=list)


class ExecCaseSet(BaseModel):
    cases: list[ExecCase] = Field(default_factory=list)


class StepResult(BaseModel):
    index: int
    op: str
    ok: bool
    detail: str = ""


class CaseResult(BaseModel):
    id: str
    title: str
    status: Literal["pass", "fail", "error"] = "pass"
    message: str = ""
    steps: list[StepResult] = Field(default_factory=list)


class ExecReport(BaseModel):
    module: str
    url: str
    db: str  # the DB actually executed against (clone or existing)
    source_db: str
    cloned: bool
    results: list[CaseResult] = Field(default_factory=list)

    def rollup(self) -> dict[str, int]:
        r = {"pass": 0, "fail": 0, "error": 0}
        for c in self.results:
            r[c.status] = r.get(c.status, 0) + 1
        return r


# --- UI smoke crawl (Playwright) ---------------------------------------------


class UIPageResult(BaseModel):
    action_id: int
    name: str
    model: str | None = None
    url: str
    status: Literal["ok", "issues", "load_error"] = "ok"
    console_errors: list[str] = Field(default_factory=list)
    page_errors: list[str] = Field(default_factory=list)  # uncaught JS exceptions
    failed_requests: list[str] = Field(default_factory=list)  # "500 GET /web/..."
    error_dialog: str | None = None  # text of an Odoo error dialog, if shown
    screenshot: str | None = None


class UIReport(BaseModel):
    module: str
    url: str
    db: str
    pages: list[UIPageResult] = Field(default_factory=list)

    def rollup(self) -> dict[str, int]:
        r = {"ok": 0, "issues": 0, "load_error": 0}
        for p in self.pages:
            r[p.status] = r.get(p.status, 0) + 1
        return r

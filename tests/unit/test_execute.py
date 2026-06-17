"""Tests for the Phase 3 RPC flow executor (no live Odoo — a fake client)."""

from sentinel.execute.generate import _to_caseset
from sentinel.execute.models import ExecCase, ExecStep
from sentinel.execute.runner import run_case, run_cases
from sentinel.odoo.rpc import OdooRPCError


class FakeOdoo:
    """Simulates a tiny asset model with a draft -> confirmed -> disposed state machine,
    where action_dispose is guarded (only allowed from 'confirmed')."""

    def __init__(self):
        self.recs: dict = {}
        self._next = 1

    def authenticate(self):
        return 1

    def create(self, model, values):
        i = self._next; self._next += 1
        self.recs[(model, i)] = {**values, "state": "draft"}
        return i

    def search(self, model, domain=None, limit=1):
        ids = [i for (m, i) in self.recs if m == model]
        if ids:
            return ids[:limit]
        return [1] if model == "res.company" else []

    def read(self, model, ids, fields=None):
        rec = self.recs.get((model, ids[0]), {})
        return [{f: rec.get(f) for f in (fields or rec.keys())}]

    def write(self, model, ids, values):
        for i in ids:
            self.recs.setdefault((model, i), {}).update(values)
        return True

    def unlink(self, model, ids):
        for i in ids:
            self.recs.pop((model, i), None)
        return True

    def execute_kw(self, model, method, args=None, kwargs=None):
        ids = (args or [[]])[0]
        if method == "action_confirm":
            for i in ids:
                self.recs[(model, i)]["state"] = "confirmed"
            return True
        if method == "action_dispose":
            for i in ids:
                if self.recs[(model, i)].get("state") != "confirmed":
                    raise OdooRPCError("cannot dispose from draft (guarded)")
                self.recs[(model, i)]["state"] = "disposed"
            return True
        if method == "action_returns_none":
            # mimic Odoo: method ran, but XML-RPC can't serialize the None return
            raise OdooRPCError("asset.action_returns_none failed: TypeError: cannot marshal None "
                               "unless allow_none is enabled")
        raise OdooRPCError(f"unknown method {method}")


def _case(*steps):
    return ExecCase(id="X", title="t", steps=[ExecStep(**s) for s in steps])


def test_happy_path_passes():
    c = _case(
        {"op": "create", "model": "asset", "values": {"name": "A"}, "ref": "a"},
        {"op": "call", "model": "asset", "ref_ids": ["a"], "method": "action_confirm", "expect": "ok"},
        {"op": "assert", "model": "asset", "ref": "a", "field": "state", "equals": "confirmed"},
    )
    assert run_case(FakeOdoo(), c).status == "pass"


def test_guard_present_expected_error_passes():
    # dispose from draft SHOULD raise; asserting expect=error => the guard exists => pass
    c = _case(
        {"op": "create", "model": "asset", "values": {"name": "A"}, "ref": "a"},
        {"op": "call", "model": "asset", "ref_ids": ["a"], "method": "action_dispose", "expect": "error"},
    )
    assert run_case(FakeOdoo(), c).status == "pass"


def test_missing_guard_detected_as_fail():
    # If we expect dispose-from-draft to succeed but the module guards it, that's a 'fail'
    # (behaviour differs from the assertion) — i.e. how a real missing/extra guard surfaces.
    c = _case(
        {"op": "create", "model": "asset", "values": {"name": "A"}, "ref": "a"},
        {"op": "call", "model": "asset", "ref_ids": ["a"], "method": "action_dispose", "expect": "ok"},
    )
    r = run_case(FakeOdoo(), c)
    assert r.status == "fail"
    assert "expected success" in r.message


def test_none_marshal_return_is_treated_as_success():
    # an action that returns None (unmarshalable over XML-RPC) still ran -> 'ok' call passes
    c = _case(
        {"op": "create", "model": "asset", "values": {"name": "A"}, "ref": "a"},
        {"op": "call", "model": "asset", "ref_ids": ["a"], "method": "action_returns_none", "expect": "ok"},
    )
    assert run_case(FakeOdoo(), c).status == "pass"


def test_assertion_mismatch_is_fail():
    c = _case(
        {"op": "create", "model": "asset", "values": {"name": "A"}, "ref": "a"},
        {"op": "assert", "model": "asset", "ref": "a", "field": "state", "equals": "confirmed"},
    )
    r = run_case(FakeOdoo(), c)
    assert r.status == "fail" and "expected" in r.message


def test_ref_resolution_in_values_and_teardown():
    fake = FakeOdoo()
    c = _case(
        {"op": "search", "model": "res.company", "domain": [], "limit": 1, "ref": "co"},
        {"op": "create", "model": "asset", "values": {"name": "A", "company_id": "$co"}, "ref": "a"},
        {"op": "assert", "model": "asset", "ref": "a", "field": "company_id", "equals": 1},
    )
    assert run_case(fake, c).status == "pass"
    # created asset was unlinked in teardown
    assert not [k for k in fake.recs if k[0] == "asset"]


def test_run_cases_rollup():
    cases = [
        _case({"op": "create", "model": "asset", "values": {"name": "A"}, "ref": "a"},
              {"op": "assert", "model": "asset", "ref": "a", "field": "state", "equals": "draft"}),
        _case({"op": "create", "model": "asset", "values": {"name": "B"}, "ref": "b"},
              {"op": "assert", "model": "asset", "ref": "b", "field": "state", "equals": "confirmed"}),
    ]
    results = run_cases(FakeOdoo(), cases)
    statuses = sorted(r.status for r in results)
    assert statuses == ["fail", "pass"]


def test_augment_required_fills_missing_fields():
    from sentinel.execute.runner import _augment_required

    class F:
        def fields_get(self, model, attrs=None):
            return {
                "name": {"required": True, "type": "char"},
                "location_id": {"required": True, "type": "many2one", "relation": "loc"},
                "state": {"required": True, "type": "selection", "selection": [["draft", "D"], ["done", "X"]]},
                "qty": {"required": True, "type": "integer"},
                "note": {"required": False, "type": "char"},
            }

        def search(self, model, domain=None, limit=1):
            return [42] if model == "loc" else []

    vals, filled = _augment_required(F(), "m", {"name": "given"})
    assert vals["name"] == "given"          # caller value preserved
    assert vals["location_id"] == 42        # required relation filled from search
    assert vals["state"] == "draft"         # first selection key
    assert vals["qty"] == 0                  # scalar default
    assert "note" not in vals               # non-required untouched
    assert set(filled) == {"location_id", "state", "qty"}


def test_short_fault_extracts_meaningful_line():
    from sentinel.odoo.rpc import _short_fault
    tb = ("Traceback (most recent call last):\n"
          "  File \"rpc.py\", line 165, in xmlrpc_2\n    response =\n"
          "odoo.exceptions.ValidationError: Please complete all required checklist items\n")
    assert _short_fault(tb) == "odoo.exceptions.ValidationError: Please complete all required checklist items"
    assert _short_fault("plain message") == "plain message"


def test_ui_report_render_and_rollup():
    from sentinel.execute.models import UIPageResult, UIReport
    from sentinel.execute.report import render_ui_markdown

    rep = UIReport(module="assetz", url="http://x", db="d", pages=[
        UIPageResult(action_id=1, name="Assets", model="assetz.asset", url="u1", status="ok"),
        UIPageResult(action_id=2, name="Broken View", model="assetz.x", url="u2", status="issues",
                     console_errors=["TypeError: undefined is not a function"],
                     failed_requests=["500 POST /web/dataset/call_kw"]),
        UIPageResult(action_id=3, name="Dead", model=None, url="u3", status="load_error",
                     page_errors=["navigation: timeout"]),
    ])
    assert rep.rollup() == {"ok": 1, "issues": 1, "load_error": 1}
    md = render_ui_markdown(rep)
    assert "UI Smoke Crawl" in md
    assert "Broken View" in md and "500 POST" in md and "Dead" in md


def test_generate_caseset_parser_is_lenient():
    raw = {"cases": [
        {"id": "EX-1", "title": "ok", "steps": [
            {"op": "create", "model": "m", "values": {"x": 1}, "ref": "a"},
            {"op": "bogus", "model": "m"},                       # unknown op -> dropped
            {"op": "assert", "model": "m", "ref": "a", "field": "state", "equals": "draft"},
        ]},
        {"id": "EX-2", "title": "no steps", "steps": []},        # no valid steps -> dropped
        "junk",                                                  # not a dict -> skipped
    ]}
    cs = _to_caseset(raw)
    assert len(cs.cases) == 1
    assert len(cs.cases[0].steps) == 2  # the bogus op was filtered out

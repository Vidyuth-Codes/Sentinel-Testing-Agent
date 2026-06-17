"""Deterministic RPC flow executor.

Runs each `ExecCase`'s steps over XML-RPC against the (throwaway) database, tracking
created record ids in a per-case symbol table, evaluating assertions, and recording
pass / fail / error. Records created during a case are unlinked best-effort afterwards.

Outcome semantics:
  - pass  : every step behaved as expected.
  - fail  : an assertion was false, or a call's expect (ok/error) didn't match — i.e. the
            module behaved differently than the case asserted (often a confirmed bug).
  - error : an unexpected RPC fault in setup (create/search/write) — usually an invalid case
            (bad required fields), not necessarily a module defect.
"""

from __future__ import annotations

from sentinel.execute.models import CaseResult, ExecCase, ExecStep, StepResult
from sentinel.odoo.rpc import OdooRPCClient, OdooRPCError


def _resolve(val, refs: dict):
    if isinstance(val, str) and val.startswith("$"):
        return refs.get(val[1:])
    if isinstance(val, list):
        return [_resolve(v, refs) for v in val]
    if isinstance(val, dict):
        return {k: _resolve(v, refs) for k, v in val.items()}
    return val


def _augment_required(client: OdooRPCClient, model: str, values: dict) -> tuple[dict, list[str]]:
    """Fill required fields the generated case omitted, by introspecting the model.

    LLM-generated cases routinely forget a required field (e.g. a mandatory location_id),
    which would error out before the actual behaviour is ever exercised. We look up the model's
    required fields and supply a value for any that are missing: an existing record for required
    relations, a sensible default for scalars. Returns (values, list-of-auto-filled-field-names).
    """
    cache = getattr(client, "_sentinel_fcache", None)
    if cache is None:
        cache = {}
        setattr(client, "_sentinel_fcache", cache)
    meta = cache.get(model)
    if meta is None:
        try:
            meta = client.fields_get(model, ["required", "type", "relation", "selection"])
        except Exception:  # noqa: BLE001 — non-Odoo client (tests) or RPC issue: skip augmentation
            meta = {}
        cache[model] = meta

    out = dict(values)
    filled: list[str] = []
    for fname, info in meta.items():
        if not info.get("required"):
            continue
        if out.get(fname) not in (None, False, "", []):
            continue
        ftype = info.get("type")
        val = None
        if ftype == "many2one" and info.get("relation"):
            ids = client.search(info["relation"], [], limit=1)
            val = ids[0] if ids else None
        elif ftype in ("char", "text", "html"):
            val = "Sentinel test"
        elif ftype == "integer":
            val = 0
        elif ftype in ("float", "monetary"):
            val = 0.0
        elif ftype == "boolean":
            val = False
        elif ftype == "selection":
            sel = info.get("selection") or []
            val = sel[0][0] if sel else None
        if val is not None:
            out[fname] = val
            filled.append(fname)
    return out, filled


def _ids_for(step: ExecStep, refs: dict) -> list[int]:
    ids = []
    for name in step.ref_ids:
        v = refs.get(name)
        if isinstance(v, int):
            ids.append(v)
    return ids


def _compare(actual, expected) -> bool:
    # many2one comes back as [id, "Name"]
    if isinstance(actual, list) and len(actual) == 2 and isinstance(actual[0], int):
        return expected == actual[0] or str(expected) == str(actual[1])
    if isinstance(actual, bool) or isinstance(expected, bool):
        return bool(actual) == bool(expected)
    return actual == expected or str(actual) == str(expected)


def _run_step(client: OdooRPCClient, step: ExecStep, refs: dict, created: list) -> tuple[bool, str, bool]:
    """Return (ok, detail, is_error)."""
    op = step.op
    try:
        if op == "create":
            vals, filled = _augment_required(client, step.model, _resolve(step.values, refs))
            rid = client.create(step.model, vals)
            if step.ref:
                refs[step.ref] = rid
            created.append((step.model, rid))
            extra = f" (auto-filled {', '.join(filled)})" if filled else ""
            return True, f"created {step.model}#{rid}{extra}", False

        if op == "search":
            ids = client.search(step.model, step.domain, limit=step.limit or 1)
            if not ids:
                return False, f"no {step.model} matched {step.domain}", True
            if step.ref:
                refs[step.ref] = ids[0]
            return True, f"found {step.model}#{ids[0]}", False

        if op == "write":
            ids = _ids_for(step, refs)
            client.write(step.model, ids, _resolve(step.values, refs))
            return True, f"wrote {step.model}{ids}", False

        if op == "call":
            ids = _ids_for(step, refs)
            args = ([ids] if ids else []) + list(_resolve(step.args, refs))
            try:
                client.execute_kw(step.model, step.method, args, _resolve(step.kwargs, refs))
            except OdooRPCError as exc:
                msg = str(exc)
                # Odoo action methods often return None, which XML-RPC can't marshal — the method
                # still RAN, so treat it as a successful call (not a fault).
                if "marshal" in msg.lower() and "none" in msg.lower():
                    if step.expect == "error":
                        return False, f"{step.method} ran (returned None) but an error was expected", False
                    return True, f"{step.method} ok (returned None — not marshalable over XML-RPC)", False
                if step.expect == "error":
                    return True, f"{step.method} raised as expected: {msg[:160]}", False
                return False, f"{step.method} raised but expected success: {msg[:200]}", False
            if step.expect == "error":
                return False, f"{step.method} succeeded but an error was expected (missing guard?)", False
            return True, f"{step.method} ok", False

        if op == "assert":
            rid = refs.get(step.ref)
            if not isinstance(rid, int):
                return False, f"assert: ref '{step.ref}' is not a record id", True
            rec = client.read(step.model, [rid], [step.field])
            actual = rec[0].get(step.field) if rec else None
            if _compare(actual, step.equals):
                return True, f"{step.field}={actual!r}", False
            return False, f"{step.field}={actual!r} expected {step.equals!r}", False

        return False, f"unknown op '{op}'", True
    except OdooRPCError as exc:
        return False, str(exc)[:300], True
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:300], True


def run_case(client: OdooRPCClient, case: ExecCase) -> CaseResult:
    refs: dict = {}
    created: list = []
    steps: list[StepResult] = []
    status, message = "pass", ""

    for i, step in enumerate(case.steps):
        ok, detail, is_error = _run_step(client, step, refs, created)
        steps.append(StepResult(index=i, op=step.op, ok=ok, detail=detail))
        if not ok:
            status = "error" if is_error else "fail"
            message = f"step {i + 1} ({step.op}): {detail}"
            break

    # best-effort teardown (newest first); ignore failures
    for model, rid in reversed(created):
        try:
            client.unlink(model, [rid])
        except Exception:  # noqa: BLE001
            pass

    return CaseResult(id=case.id, title=case.title, status=status, message=message, steps=steps)


def run_cases(client: OdooRPCClient, cases: list[ExecCase]) -> list[CaseResult]:
    return [run_case(client, c) for c in cases]

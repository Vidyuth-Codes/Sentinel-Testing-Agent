"""Generate executable RPC test cases with Claude Code.

The engine reads the addon source (so it knows real models, required fields, and
`action_*`/`button_*` methods) and emits a strict JSON set of executable op-sequences.
This is grounded code reasoning — the same engine as the audit — but the output is
machine-runnable steps, not prose.
"""

from __future__ import annotations

from sentinel.audit.runner import parse_json_object
from sentinel.engine import ClaudeCodeEngine
from sentinel.execute.models import ExecCase, ExecCaseSet, ExecStep

_VALID_OPS = {"create", "search", "call", "write", "assert"}

_GEN_SYSTEM = (
    "You are Sentinel, generating EXECUTABLE RPC test cases for an Odoo 18 module. They will be "
    "run verbatim over XML-RPC against a THROWAWAY copy of the database. Output ONLY one JSON "
    "object — no prose, no Markdown, no code fences.\n\n"
    "Read the addon source to use REAL technical model names, REAL field names, and REAL "
    "`action_*`/`button_*` method names. Each case is an ordered list of steps sharing a symbol "
    "table: a step's `ref` stores the created/searched record id; later steps reference it as "
    "\"$ref\" (in `values` or `ref_ids`).\n\n"
    "Step ops:\n"
    '  {"op":"search","model":M,"domain":[...],"limit":1,"ref":"co"}   find an existing record id\n'
    '  {"op":"create","model":M,"values":{...},"ref":"a"}              create a record\n'
    '  {"op":"call","model":M,"ref_ids":["a"],"method":"action_x","args":[],"kwargs":{},"expect":"ok"|"error"}\n'
    '  {"op":"write","model":M,"ref_ids":["a"],"values":{...}}\n'
    '  {"op":"assert","model":M,"ref":"a","field":"state","equals":"confirmed"}\n\n'
    "Rules:\n"
    "- In `create.values` set ALL fields the scenario needs — every required field, AND any field "
    "the action/constraint under test reads (e.g. checklist lines, readings). For a required "
    "many2one, either `search` an existing record first and use \"$ref\", or create the parent. "
    "If the setup would be too complex to satisfy over RPC, pick a simpler scenario instead.\n"
    "- Call ONLY PUBLIC methods. Methods starting with '_' (e.g. cron methods like "
    "`_cron_*`, compute methods) CANNOT be called over RPC — never target them. To exercise a cron, "
    "call the public action it wraps, or skip it.\n"
    "- To set a system/config value, do NOT create `ir.config_parameter` (keys are unique). Use a "
    'call: {"op":"call","model":"ir.config_parameter","method":"set_param","args":["the.key","value"]}.\n'
    "- Use expect:\"error\" when an action SHOULD be blocked (illegal state transition, a "
    "@api.constrains that must fire) — this verifies the guard exists. Use expect:\"ok\" for a valid "
    "flow that should succeed.\n"
    "- Prefer cases that probe state machines and constraints, ideally ones tied to real risks.\n"
    "- Keep each case 2–6 steps. Make ids/titles short.\n\n"
    'Schema: {"cases":[{"id":str,"title":str,"model":str,"note":str,"steps":[step,...]}]}\n\n'
    "WORKED EXAMPLE (adapt every model/field name to THIS module's real schema — note how the "
    "created location id is threaded into the asset via \"$loc\", and how only required fields are set):\n"
    '{"cases":[{"id":"EX-01","title":"Dispose blocked from draft","model":"the.asset.model",'
    '"note":"action_dispose should require a confirmed state","steps":['
    '{"op":"create","model":"the.location.model","values":{"name":"T-Loc"},"ref":"loc"},'
    '{"op":"create","model":"the.asset.model","values":{"name":"T-Asset","location_id":"$loc"},"ref":"a"},'
    '{"op":"call","model":"the.asset.model","ref_ids":["a"],"method":"action_confirm","expect":"ok"},'
    '{"op":"assert","model":"the.asset.model","ref":"a","field":"state","equals":"confirmed"}]}]}\n'
    "Before emitting each `create`, re-check the model's required fields and ensure EVERY one is "
    "present in `values` (relations via \"$ref\" to a record you created/searched earlier)."
)


def _prompt(max_cases: int, seed: str | None) -> str:
    p = (
        f"Generate up to {max_cases} executable RPC test cases for this module. Read the addon "
        "source first to ground every model/field/method in what actually exists."
    )
    if seed:
        p += ("\n\nFocus especially on verifying these suspected issues (confirm whether each is a "
              "real defect by constructing a case that would fail if it is):\n" + seed)
    return p


def _to_caseset(data: dict) -> ExecCaseSet:
    cases: list[ExecCase] = []
    for c in data.get("cases", []) if isinstance(data, dict) else []:
        if not isinstance(c, dict):
            continue
        steps: list[ExecStep] = []
        for s in c.get("steps", []):
            if not isinstance(s, dict) or s.get("op") not in _VALID_OPS:
                continue
            try:
                steps.append(ExecStep(**{k: v for k, v in s.items() if k in ExecStep.model_fields}))
            except Exception:  # noqa: BLE001 — skip a malformed step, keep the rest
                continue
        if steps:
            cases.append(ExecCase(
                id=str(c.get("id") or f"EX-{len(cases) + 1:02d}"),
                title=str(c.get("title") or "").strip(),
                model=c.get("model"),
                note=(str(c["note"]).strip() if c.get("note") else None),
                steps=steps,
            ))
    return ExecCaseSet(cases=cases)


def generate_cases(
    engine: ClaudeCodeEngine, *, module: str, addons: str | None, summary: str = "",
    max_cases: int = 8, seed: str | None = None, timeout: int = 900,
) -> tuple[ExecCaseSet, float | None]:
    system = _GEN_SYSTEM
    if summary:
        system += "\n\n# SYSTEM MAP (live introspection)\n" + summary

    cost = 0.0
    last_err: Exception | None = None
    # The model occasionally answers with prose or nothing; retry once, stricter, before giving up.
    for attempt in range(2):
        prompt = _prompt(max_cases, seed)
        if attempt == 1:
            prompt += "\n\nIMPORTANT: respond with ONLY the JSON object — no prose, no code fences."
        res = engine.run_sync(prompt, code_dir=addons, system_prompt=system,
                              max_turns=60, timeout=timeout)
        cost += res.cost_usd or 0.0
        try:
            return _to_caseset(parse_json_object(res.text)), cost
        except Exception as exc:  # noqa: BLE001 — parse failure: retry or surface cleanly
            last_err = exc

    raise ValueError(
        "Claude Code did not return parseable test cases after 2 attempts "
        f"({last_err}). Try re-running, or pass --cases with a prepared JSON."
    )

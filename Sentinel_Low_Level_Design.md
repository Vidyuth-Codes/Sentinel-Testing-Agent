# Sentinel — Low-Level Design

# Agentic QA & Bug-Detection Agent for Odoo

**Version:** 2.0 — Odoo + Claude Code architecture  |  **Date:** June 2026  |  **Classification:** Internal — R&D
**Status:** Active — Phase 1 built & running
**Prepared by:** Vidyuth  |  **Author:** Vidyuth

**Source requirements:** [`Sentinel_Requirement_Document.md`](Sentinel_Requirement_Document.md)
**Traceability:** Each section references the FR/NFR/AC IDs it satisfies.

> **Supersedes v1.0.** Version 1.0 specified a generic, multi-stack agent built on a **LangGraph**
> state graph, a **pgvector** code-RAG index, **metered Claude API** calls, a Docker sandbox for
> *every* run, and Postgres/Redis/S3 infrastructure. That design is **retired**. The system that was
> actually built is an **Odoo-specific** agent whose reasoning is **Claude Code on a subscription**
> (no RAG, no LangGraph, no per-token API). pgvector, LangGraph, and the metered-API client are
> **gone**; the **Docker sandbox** and **Playwright** survive as **Phase 3** components, and a
> **React** frontend remains a planned upgrade of today's HTML/JS UI.

---

## 1. Architecture Overview

Sentinel splits cleanly into a **deterministic layer** (Odoo tools, no LLM) and a **reasoning
layer** (Claude Code on the team's subscription). A FastAPI service exposes both; a single-page web
UI renders chat + a System Map dashboard. Target-instance *execution* (Phase 3) runs against a
**duplicate database** inside a **Docker sandbox**.

```
┌───────────────────────────────────────────────────────────────┐
│ FRONTEND  (web UI — this repo)                                 │
│   chat · System Map dashboard · report viewer                  │
│   HTML/JS today · React/Redux planned                          │
└───────────────┬───────────────────────────────────────────────┘
                │  HTTP (FastAPI) + SSE
┌───────────────▼───────────────────────────────────────────────┐
│ BACKEND  (FastAPI — src/sentinel/web/app.py)                  │
│   /api/introspect → deterministic Odoo tools  (NO LLM)        │
│   /api/chat[/stream], /api/audit[/stream] → Claude Code       │
└───────┬───────────────────────────────────────┬───────────────┘
        │ deterministic tools                   │ reasoning engine
┌───────▼────────────────────────┐   ┌──────────▼────────────────┐
│ Odoo tools (src/sentinel/odoo) │   │ Claude Code engine         │
│  rpc · introspect (System Map) │   │ (engine/claude_code.py)    │
│  addon_scan (AST) · context    │   │  headless `claude -p`      │
│  · report                      │   │  Read/Grep/Glob (read-only)│
│                                │   │  guided by the Odoo-QA     │
│                                │   │  skill + System Map        │
└────────────────────────────────┘   └────────────────────────────┘
        │                                       │
        │  SUBSCRIPTION billing (flat) ── no ANTHROPIC_API_KEY needed
        ▼
┌───────────────────────────────────────────────────────────────┐
│ PHASE 3 (planned): RPC flow executor · Playwright UI executor  │
│   running against a DUPLICATE DB inside a Docker sandbox        │
└───────────────────────────────────────────────────────────────┘
```

**Why this shape (design rationale):**

- **Deterministic introspection runs before the LLM** (FR-01–04) so Claude Code reasons *with* a
  precise System Map of the module instead of guessing — fewer hallucinated findings (NFR-04).
- **Claude Code replaces RAG.** It reads the real addon with `Read`/`Grep`/`Glob` and follows
  references like a developer, so there is **no chunk/embed/pgvector** step to build or maintain.
- **Subscription, not metered API.** The engine strips `ANTHROPIC_API_KEY` from its environment so
  runs bill to the signed-in Claude Code subscription (NFR-03).
- **Read-only by construction.** The engine is restricted to read tools; introspection issues no
  writes. Real execution is deferred to Phase 3 and isolated to a duplicate DB (NFR-01, NFR-02).

---

## 2. Repository Structure (as built)

```
sentinel-testing-agent/
├── src/sentinel/
│   ├── cli.py                 ← `sentinel web | introspect | scan-addons`
│   ├── paths.py               ← output dir resolution (output/<run>/)
│   ├── core/
│   │   └── models.py          ← Finding, CodeLocation, Evidence, RunResult + taxonomy (the findings schema)
│   ├── odoo/                  ← DETERMINISTIC Odoo tools (no LLM) — the Understand layer
│   │   ├── rpc.py             ← read-only XML-RPC client (auth, search_read, fields_get, execute_kw)
│   │   ├── introspect.py      ← build_system_map(): live instance → SystemMap
│   │   ├── addon_scan.py      ← AST scan of addon source on disk; cross-check vs live
│   │   ├── schema.py          ← SystemMap + OdooModelInfo/Field/View/Action/Access/Rule/Cron/…
│   │   ├── context.py         ← summarize_system_map(): compact System Map brief for the engine
│   │   ├── investigate.py     ← per-record diagnosis: resolve ref → fetch data graph → render for the engine
│   │   ├── deployment.py      ← instance-wide scan: split installed modules into custom vs core Odoo
│   │   └── report.py          ← System Map → Markdown "understanding report" + JSON
│   ├── engine/               ← REASONING layer — Claude Code on subscription
│   │   ├── claude_code.py     ← ClaudeCodeEngine: headless `claude -p`, sync + streaming
│   │   └── skill.py           ← load Odoo-QA skill + assemble system prompt (+ System Map)
│   ├── audit/                ← Phase 2 — the structured audit (two-pass)
│   │   ├── runner.py          ← generate_report (pass 1) + structure_report (pass 2) + persist
│   │   └── models.py          ← TestPlan / RequirementCoverage / AuditTestCase / AuditOutcome
│   ├── execute/              ← Phase 3 — RPC flow executor + UI smoke crawl
│   │   ├── generate.py        ← Claude Code → executable op-sequences (create/call/assert)
│   │   ├── provision.py       ← clone source DB via the `db` service (or existing-DB opt-in)
│   │   ├── runner.py          ← deterministic XML-RPC executor (refs, asserts, auto-fill, teardown)
│   │   ├── ui_playwright.py    ← Playwright crawl of the web client (console/JS/network/screenshots)
│   │   ├── report.py          ← results.{md,json} + cases.json + ui_results.{md,json}
│   │   └── models.py          ← ExecStep / ExecCase / CaseResult / ExecReport / UIPageResult / UIReport
│   └── web/
│       ├── app.py             ← FastAPI: /api/config, /api/introspect, /api/chat[/stream], /api/audit[/stream]
│       └── static/index.html  ← single-page UI (chat + System Map dashboard)
├── skills/odoo-qa/SKILL.md    ← the testing playbook injected as Claude Code's system prompt
├── tests/unit/                ← pytest suite (Odoo layer: System Map counts + the LLM brief)
└── output/                    ← run artifacts (System Maps, test plans) — git-ignored
```

> **Cleanup note.** The retired generic/metered-API modules — `llm/` (raw-Anthropic client), `plan/`
> (metered-API planner), `ingest/` + `pipeline.py` + `report/` + `static/` (the generic
> stack-detect → lint → report audit), and the `sentinel audit`/`plan` CLI commands — **have been
> removed**. The one piece worth keeping, `summarize_system_map`, was relocated to `odoo/context.py`.
> `core/models.py` is retained as the `Finding` schema that Phase 2 will populate.

---

## 3. Core Data Model

### 3.1 `Finding` — `src/sentinel/core/models.py`

Storage-agnostic Pydantic models, serialised to JSON files under `output/<run>/` (no database in
Phase 1/2).

```python
Category = Literal["functional_bug","logic_error","ui_visual","runtime_error",
                   "integration_contract","security","accessibility","performance","code_quality"]
Layer    = Literal["frontend","backend","integration"]
Severity = Literal["critical","high","medium","low","info"]
Source   = Literal["static","llm","dynamic_ui","dynamic_api"]
Status   = Literal["new","verified","false_positive","wont_fix","acknowledged"]

class CodeLocation(BaseModel):
    file: str | None; line_start: int | None; line_end: int | None
    route: str | None; endpoint: str | None
    def short(self) -> str: ...          # "models/asset.py:123"

class Evidence(BaseModel):
    screenshot_key: str | None; console_log: str | None; network_trace: str | None
    tool_output: str | None; code_snippet: str | None

class Finding(BaseModel):
    finding_id: UUID; run_id: UUID
    title: str; description: str
    category: Category; layer: Layer; severity: Severity; confidence: float = 0.0
    source: Source; location: CodeLocation; evidence: Evidence
    repro_steps: list[str]; suggested_fix: str | None
    status: Status = "new"; dedup_key: str | None; verified: bool = False
    rule_id: str | None                  # optional stable check id (set by a future deterministic pass)
```

`RunResult` wraps a run: `project_ref`, `project_map`, `test_plan`, `findings`, `coverage`
(tool/layer → `"ran"` | `"skipped: …"`), timestamps, and `severity_rollup()`.

### 3.2 `SystemMap` — `src/sentinel/odoo/schema.py`

The agent's model of "what the addon built", produced by introspection (FR-02). Attribution to the
addon comes from `ir.model.data`.

```python
class OdooField(BaseModel):
    name; string; ttype; required; readonly; store; relation; related; compute; help
    owned_by_addon: bool                 # field added by the target addon

class OdooModelInfo(BaseModel):
    model; name; transient
    owned_by_addon: bool                 # True = new model; False = extended core model
    fields: list[OdooField]
    n_fields / n_fields_owned: int

class SystemMap(BaseModel):
    url; db; module; server_version; generated_at
    modules_installed: int; module_depends: list[str]
    models: list[OdooModelInfo]
    views:  list[OdooView]; actions: list[OdooAction]; menus: list[OdooMenu]
    access: list[OdooAccess]; rules: list[OdooRule]
    crons:  list[OdooCron]; automations: list[OdooAutomation]; sequences: list[OdooSequence]

    owned_models / extended_models: list[str]
    def counts(self) -> dict[str,int]    # new_models, extended_models, fields_owned, views,
                                         # actions, menus, access_rules, record_rules,
                                         # scheduled_actions, automations, sequences
```

> There is **no Postgres schema** in the current design. Findings/System Maps/test plans are JSON +
> Markdown files under `output/<run>/`. A database is deferred (§14).

---

## 4. The Reasoning Engine — Claude Code — `src/sentinel/engine/claude_code.py`

`ClaudeCodeEngine` drives the Claude Code CLI in **headless `print` mode** via `subprocess`
(FR-05). Subprocess (not the Agent SDK's bidirectional control protocol) is used deliberately: the
SDK's `initialize` handshake hangs on this Windows setup, and `subprocess.run(timeout=…)` cleanly
terminates the child so no orphaned `claude` processes are left behind.

**CLI resolution.** `_find_cli()` prefers the native `…\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe`
(clean subprocess, no `.cmd` arg-length limits), honouring a `SENTINEL_CLAUDE_PATH` override, then
falling back through `PATH` and `npm\claude.cmd`. `available()` is false when none resolve → the web
layer uses the mock fallback (NFR-07).

**Invocation (sync).** `run_sync(prompt, *, code_dir, system_prompt, resume, max_turns, timeout)`:

```python
cmd = [cli, "-p", prompt,
       "--output-format", "json",
       "--permission-mode", "bypassPermissions",
       "--allowedTools", "Read,Grep,Glob"]          # read-only ⇒ inspect, never edit (NFR-01)
if system_prompt: cmd += ["--append-system-prompt", system_prompt]
if code_dir:      cmd += ["--add-dir", code_dir]     # grant the addon dir
if resume:        cmd += ["--resume", resume]        # multi-turn continuity (FR-22)

env = dict(os.environ)
if os.environ.get("SENTINEL_FORCE_SUBSCRIPTION", "1") != "0":
    env.pop("ANTHROPIC_API_KEY", None)               # bill the subscription, not the API (NFR-03)

proc = subprocess.run(cmd, cwd=repo_root(), env=env, timeout=timeout, capture_output=True, …)
```

Key design points:
- **cwd is the (non-git) Sentinel repo**, *not* the addon. Pointing cwd at the addon (a git repo)
  makes the CLI's git-aware startup very slow; the addon is granted via `--add-dir` + absolute-path
  reads instead. The system prompt tells the model the addon's absolute path.
- **System-prompt cap** of 24 000 chars keeps the whole command line under the Windows ~32k limit
  (NFR-09).
- The JSON result yields `EngineResult(text, session_id, cost_usd, is_error)`; `session_id` powers
  conversation continuity, `cost_usd` is surfaced to the UI (FR-07).

**Invocation (streaming).** `run_stream(...)` uses `--output-format stream-json --verbose` and a
`subprocess.Popen`, parsing each JSON line into UI events (FR-23):

| Emitted event | From | Meaning |
|---|---|---|
| `{"type":"text","text":…}` | `assistant` text block | prose delta — stream into the chat bubble |
| `{"type":"tool","name":…,"input":…}` | `assistant` `tool_use` block | a `Read`/`Grep`/`Glob` call — progress signal |
| `{"type":"result","session_id":…,"cost_usd":…,"is_error":…,"result":…}` | `result` | final answer + session id + cost |
| `{"type":"error","message":…}` | startup/timeout | engine failure |

A daemon `threading.Timer(timeout, proc.kill)` enforces the wall-clock cap; on exit the child is
killed if still alive. If no `result` line was seen, an `error` event is emitted (NFR-07).

`EngineUnavailable` is raised whenever the CLI is missing, fails to launch, times out, or produces
no output — the web layer catches it and degrades to the mock engine.

---

## 5. The Odoo Tools (deterministic, no LLM) — `src/sentinel/odoo/`

### 5.1 `rpc.py` — XML-RPC client
A thin, **read-only** wrapper over Odoo's External API: `version()`, `authenticate()` (sets `uid`),
`search_read`, `fields_get`, and a generic `execute_kw`. Raises `OdooAuthError` / `OdooRPCError`,
which callers turn into clear UI/CLI messages (NFR-07). (FR-01)

### 5.2 `introspect.py` — `build_system_map(client, module) → SystemMap`
Queries `ir.model.data` to discover exactly which records the target addon **created** (XML-IDs in
the module's namespace), then fetches details for each facet — models & fields (`ir.model`,
`ir.model.fields`, attributing `owned_by_addon`), views, actions, menus, `ir.model.access`,
`ir.rule`, `ir.cron`, automations, sequences — and distinguishes **new** models from **extended**
core models. (FR-02)

### 5.3 `addon_scan.py` — static source cross-check
AST-parses the addon on disk: `__manifest__.py` (name, version, depends), model classes, field
declarations, decorators (`@api.depends`, `@api.constrains`, `@api.model_create_multi`), and method
names. Produces an `AddonScan` used to cross-check the live System Map against the source (e.g. a
field defined in code but absent from the instance, or vice-versa). (FR-03)

### 5.4 `report.py` — understanding report
`render_system_map_markdown(smap, scan)` / `write_system_map(...)` render the System Map (+ optional
scan) into a Markdown "understanding report" and JSON: new vs extended models, field counts, views,
actions, security, automations. (FR-04)

---

## 6. The Odoo-QA Skill — `src/sentinel/engine/skill.py` + `skills/odoo-qa/SKILL.md`

The **single source of truth for *how* Sentinel tests** an Odoo module is the Markdown skill body.
`load_skill()` reads `skills/odoo-qa/SKILL.md`, strips the YAML front-matter, and returns the
playbook. `build_system_prompt(system_map_summary)` concatenates:

1. the **skill** (role = senior Odoo 18 QA engineer; read-only; ground every finding; output
   formats for findings and test plans), and
2. the **System Map summary** (a compact, LLM-friendly brief of the module) under a
   `# SYSTEM MAP` header — or a "read the manifest to orient" note if none exists yet.

Injecting the skill as the **system prompt** (rather than dropping a `.claude/` folder into the
user's addon) keeps the addon untouched (NFR-01). The skill is editable Markdown — the testing
playbook evolves without code changes (NFR-08). (FR-06)

---

## 6A. The Two-Pass Structured Audit (Phase 2) — `src/sentinel/audit/`

A one-shot audit must yield both a **human Markdown report** and **machine-readable structure**
(`Finding[]` + a test plan) per FR-18/19/20. `audit/runner.py` does this in two engine passes so the
expensive code-reading happens once:

**Pass 1 — `generate_report`.** Claude Code reads the addon (`--add-dir`) with the Odoo-QA skill +
System Map as system prompt and writes the Markdown report (`REPORT_PROMPT`: requirement-coverage
table, rpc/ui test cases, grounded findings with `file:line` evidence, and a Coverage note).

**Pass 2 — `structure_report`.** A second, cheap call (no code reading, no skill) converts that report
into strict JSON against a fixed schema (`_EXTRACT_SYSTEM`). The result is parsed
(`parse_json_object` — tolerant of code fences / stray prose) and mapped (`map_extraction`):

- findings → canonical `core.models.Finding` (category/layer/severity **normalised** from the skill's
  vocabulary to the controlled `Literal`s via `_CATEGORY_MAP`/`_LAYER_MAP`/severity aliases; `source="llm"`;
  `file`/`line` → `CodeLocation`; evidence → `Evidence`; confidence clamped to 0–1);
- requirement coverage + test cases → `audit/models.TestPlan`.

The result is an **`AuditOutcome`** (Markdown + `Finding[]` + `TestPlan` + coverage note + total cost).
Pass 2 is **best-effort**: if the JSON can't be parsed, `structured=False` and the Markdown report is
still saved — the human report is never lost.

**Persistence** (`_save`): `output/audit-<module>-<timestamp>/` gets `report.md`, `findings.json`
(`Finding[]`), and `test_plan.json`.

The CLI (`sentinel audit`) and the non-streaming `/api/audit` call `run_full_audit` (both passes). The
streaming `/api/audit/stream` runs pass 1 live to the UI, then calls `structure_report` server-side and
emits a final `summary` event (counts, rollup, saved paths, cost).

---

## 6C. Deployment Scan — what's custom across the instance (`src/sentinel/odoo/deployment.py`)

For a single addon you give a module name; for a **heavily-customised deployment** (many tailored
modules) the first question is *"what has been custom-built here?"*. `scan_deployment` reads
`ir.module.module` and splits installed modules into **custom/non-standard** vs **core Odoo**.

The classifier combines two signals, because **author alone is unreliable** — partner developers
frequently leave the scaffold's `author = "Odoo S.A."`. A module is treated as **core Odoo only if**
its author is Odoo/OCA **AND** its version is the 4-part `series.x.y` form (e.g. `18.0.1.3`). Custom
partner modules keep the scaffold's **5-part** version (`18.0.1.0.5`), so they're caught even with a
faked author; non-Odoo authors (e.g. a client company) are caught regardless of version. Validated on
NLC staging: author-only found **1** custom module; the combined rule found the real **35**
(`*_customisation`, `*_approval`, `bom_*`, `pcd_*`, `studio_customization`, …).

Exposed at `POST /api/deployment` (the list) + `POST /api/deployment/overview` (an engine narrative
grouping the customisations by business area) and the **Scan Modules** button — each custom module is
clickable to run the normal single-module Understand. Read-only.

---

## 6B. Live-Data Investigation — per-record diagnosis (`src/sentinel/odoo/investigate.py`)

The **support/troubleshooting** capability: a functional user asks, in plain language, about a
*specific live record* — *"why does S00437 still show 0 delivered?"* — and Sentinel reads that
record's real data and explains what happened. This is the forensic, SO437-style analysis.

**Why not an MCP tool for the engine?** Claude Code accepts MCP servers, but the engine already
avoids the Agent SDK because its stdio handshake hangs on this Windows setup — MCP uses the same
handshake, so it's risky here. Instead Sentinel does the querying itself (deterministic, reliable)
and feeds the result to the engine to reason over:

```
question ─▶ extract_references()  ─ pull "S00437" / "INV/2026/00010" / "WH/OUT/00032" from the text
         ─▶ resolve_record()      ─ search business models (sale.order, account.move, stock.picking…) by name
         ─▶ fetch_record_graph()  ─ read the record's state + related rows (1 hop) + chatter + field-change history
         ─▶ render_graph()        ─ compact text bundle (capped ~16k)
         ─▶ Claude Code reasons over the data (INVESTIGATE_SYSTEM) ─▶ plain-language diagnosis
```

- **Read-only** — only `search_read`/`read`; never writes. Needs no source code (it's about *data*, not code).
- **Grounded** — the answer cites the record's actual related docs, statuses, and history; the prompt
  forbids inventing values and tells the agent to say what extra record/field it would need.
- Exposed at `POST /api/investigate/stream` and the **🔍 Diagnose** button. Validated live against
  `assetz`-adjacent staging (correctly diagnosed a real order's pending-delivery / not-invoiced state
  and flagged a 57-line order with only one delivery as anomalous).

---

## 7. Web / API Layer — `src/sentinel/web/app.py` (FastAPI)

`FastAPI(title="Sentinel — Odoo Testing Agent")`. A single `ClaudeCodeEngine` instance is shared;
two in-memory caches key off the module: `_SUMMARY` (System Map brief) and `_SESSION` (Claude Code
session id for multi-turn continuity).

| Endpoint | Layer | Behaviour | FRs |
|---|---|---|---|
| `GET /` | — | Serves `static/index.html`. | — |
| `GET /api/config` | — | Version, connection defaults, and whether the engine is `claude-code` or `mock`. | — |
| `POST /api/introspect` | deterministic | Connect → `build_system_map` → optional `scan_addon`; caches the System Map summary; returns counts, the understanding-report markdown, and the model list. **No LLM.** | FR-01–04 |
| `POST /api/chat` | reasoning | `run_sync` with the skill+System Map prompt, the addon as `code_dir`, and the cached session for continuity; returns reply + cost. Falls back to `_mock_reply` if the engine is unavailable. | FR-22, NFR-07 |
| `POST /api/chat/stream` | reasoning | SSE variant of chat — streams `text`/`tool`/`result` events. | FR-23 |
| `POST /api/audit` | reasoning | Runs the two-pass `run_full_audit` (§6A): saves `report.md` + `findings.json` + `test_plan.json`; returns markdown, finding count, severity rollup, coverage rollup, `structured` flag, cost, and saved paths. | FR-18–21 |
| `POST /api/audit/stream` | reasoning | SSE variant — streams pass 1 (the report) live, then runs pass 2 server-side and emits a final `summary` event (counts, rollup, saved paths, cost). | FR-18–21, FR-23 |
| `POST /api/overview` | reasoning | Short business overview of the module from the System Map (no source). | — |
| `POST /api/investigate/stream` | data + reasoning | Per-record diagnosis (§6B): resolve a reference → fetch the live data graph → engine explains what happened. Read-only. | support |
| `POST /api/flow/stream` | data + reasoning | Explain a flow (bills, sales orders, deliveries…) grounded in **real example records** (counts per state + samples + one detailed example); hypothetical example if none exist. Read-only. | support |
| `POST /api/deployment` | deterministic | Instance-wide scan (§6C): custom/non-standard modules vs core Odoo. | support |
| `POST /api/deployment/overview` | reasoning | Engine narrative grouping the custom modules by business area. | support |

Re-introspecting a module **resets** its cached session (`_SESSION.pop`) so a fresh understanding
starts a fresh conversation. SSE responses set `Cache-Control: no-cache` / `X-Accel-Buffering: no`.

**CLI surface** — `src/sentinel/cli.py`: `sentinel web` (launch the UI), `sentinel introspect`
(System Map → files), `sentinel scan-addons` (static scan), and `sentinel audit --module M --addons P
[--db DB …]` (the full Phase 2 audit via `run_full_audit`; with `--db` it introspects first for System
Map context), `sentinel run-tests --module M --addons P --db DB [--use-existing-db | --master-pw …]`
(the Phase 3 RPC flow executor, §10), and `sentinel run-ui --module M --db DB` (the Phase 3 Playwright
UI smoke crawl, §10). The old generic `plan`/`audit` pipeline commands were removed in the cleanup
(§9); the new `audit` drives the Claude Code engine.

---

## 8. Frontend — `src/sentinel/web/static/index.html`

A dark-theme **single-page app** (vanilla HTML/JS, `marked.js` for Markdown). Layout: a header with
engine/connection status, a connection form (prefilled for `assetz`), and a split pane — **chat** on
one side, the **System Map dashboard** (counts, models, understanding report) on the other.
**Understand** calls `/api/introspect`; **chat** streams from `/api/chat/stream`; a one-shot audit
streams from `/api/audit/stream`.

**Planned upgrade (roadmap):** migrate to **React** (with Redux for run/chat/findings state and a
stream helper for SSE) once Phase 2 stabilises. The HTML/JS UI is sufficient until then. (Req §13 Q4)

---

## 9. Removed Legacy (record of the cleanup)

The v1.0 generic/metered-API design left a body of code that the Odoo + Claude Code product never
used. It has been **removed**; this section records what went and why, so the history is legible.

| Removed | Was | Why it went |
|---|---|---|
| `llm/client.py` | Raw `anthropic` SDK wrapper (metered API, `.env` key, mock mode) | Reasoning moved to the Claude Code CLI (subscription); the SDK path and the API key are obsolete. |
| `plan/` | Metered-API test-plan / coverage generator (`generate_test_plan`, `analyze`, `ingest`, `report`, `models`) | Gap analysis + test-plan generation is now Claude Code's job via `/api/audit`. |
| `ingest/` + `pipeline.py` + `report/` | Generic stack-detect → run-command infer → lint → Markdown/JSON audit pipeline | Generic multi-stack auditing is out of scope (Odoo only). |
| `static/` (`base`, `engine`, `runners/*`) | Deterministic linters: zero-dep Python AST checker + ruff/eslint adapters | Only ever invoked by the removed generic pipeline; Odoo-specific code analysis is the engine's job, guided by the skill. |
| `sentinel audit` / `sentinel plan` CLI | Entry points for the two pipelines above | Their backing code was removed; the CLI is now `web` / `introspect` / `scan-addons`. |
| `tests/sample_app/` + `test_ingest`/`test_pipeline`/`test_builtin_python` | Generic React+FastAPI fixture and its tests | Tested only the removed generic path; replaced by an Odoo-layer smoke test. |

**Kept / relocated.** `summarize_system_map` was moved from `plan/context.py` to **`odoo/context.py`**
(its only consumers are `odoo.schema`/`odoo.addon_scan`). `core/models.py` (the `Finding`/`RunResult`
schema and taxonomy) is **retained** as the structured-findings contract Phase 2 will populate, even
though no live caller produces `Finding`s yet.

> **Neuro-symbolic note.** Removing the linters does **not** abandon the deterministic-first stance:
> the System Map (live RPC introspection) and `addon_scan` (AST of the source) remain the deterministic
> signals that ground the engine's reasoning. Re-introducing a deterministic Python lint pass for Odoo
> backend code is a possible Phase 2 enhancement (§14).

---

## 10. Phase 3 — Execute + Report

Phase 3 turns reasoning into **executed results** against a database that is never production
(FR-13–17, NFR-02). The **RPC flow executor is built** (`src/sentinel/execute/`); the Playwright UI
executor and the Docker sandbox are still planned.

```
                      ┌─ generate (Claude Code) ─ executable op-sequences (create/call/assert)   FR-14
 module + System Map ─┤
                      └─ provision ─ clone source DB via `db` service  (or --use-existing-db)     FR-13
                                  │
   executable cases ─────────────┴─▶ runner (deterministic XML-RPC) ─▶ pass / fail / error
                                        refs table · assertions · best-effort teardown
                                  │
                                  └─▶ report: results.md + results.json + cases.json  ─▶ drop clone   FR-17
```

Unlike Phase 2 (where Claude Code *is* the analysis), Phase 3 splits **non-deterministic
generation** from **deterministic execution**: Claude Code reads the addon and emits a strict JSON
set of executable op-sequences (`generate.py`); a plain XML-RPC runner (`runner.py`) then executes
them with no LLM in the loop, so a run is reproducible. Each case is a sequence of ops sharing a
symbol table (`create`/`search` store a record id under a `ref`; later steps use `"$ref"`):

| op | does | 
|----|------|
| `create` | create a record, store its id as `ref` |
| `search` | find an existing record id (for required relations) |
| `call` | call a **public** `action_*`/`button_*` method on `ref_ids`; `expect: ok\|error` |
| `write` | write values to records |
| `assert` | read a field and compare to `equals` |

**Outcome semantics** (`models.CaseResult.status`): **pass** = behaved as asserted; **fail** = an
assertion was false or a call's expect didn't match (often a confirmed bug); **error** = an
unexpected RPC fault in setup (usually an invalid generated case, not a defect). Faults are reduced
to the meaningful exception line (`rpc._short_fault`), not raw tracebacks.

**Safety (NFR-02):** `provision.py` clones the source DB via Odoo's `db` XML-RPC service
(`OdooDbAdmin.duplicate`, needs the master password) into `<db>_sentinel_<ts>`, runs there, and
**drops it after**. Running against an existing DB requires the explicit `--use-existing-db` opt-in;
created records are unlinked best-effort per case regardless.

**UI smoke crawl (built — `ui_playwright.py`, FR-15).** `sentinel run-ui` introspects the addon's
window actions, logs into the Odoo web client once with Playwright/Chromium, then opens each action
(`/odoo/action-<id>`) in a fresh page and records what breaks: **console errors, uncaught JS
exceptions, failed 4xx/5xx requests, and Odoo error dialogs**, with a **screenshot** per page. It's
read-only browsing (no records created), so it needs no clone. Pages are classified **ok / issues /
load_error** and written to `ui_results.md` + `ui_results.json`. Driving forms/workflows end-to-end
(create via UI, click workflow buttons) is deliberately out of scope for v1 — the crawl already
surfaces broken views, missing-field contract errors, and JS exceptions. Requires
`pip install playwright` + `python -m playwright install chromium` (the `ui` extra).

**Still planned:** the **Docker sandbox** (FR-16) for fully isolated, disposable execution. The
current executors run directly against the configured Odoo.

---

## 11. Build Sequence

| Phase | Component | Key deliverable | FRs | Status |
|---|---|---|---|---|
| **1** | `odoo/` tools + `core/models` | XML-RPC client, `build_system_map` → SystemMap, `addon_scan`, understanding report | FR-01–04 | ✅ Done |
| **1** | `web/` + `static/index.html` | FastAPI + SPA: Understand button (live introspection) + chat/dashboard | FR-21–23 | ✅ Done |
| **2** | `engine/claude_code` + `engine/skill` | Headless Claude Code engine (sync+stream), Odoo-QA skill injection, subscription billing | FR-05–07 | ✅ Done |
| **2** | `audit/` + `/api/chat`, `/api/audit`, `sentinel audit` | Real gap analysis, bug findings, and test-plan generation grounded in `file:line`; two-pass structured output (`findings.json` + `test_plan.json`) | FR-08–12, FR-18–21 | ✅ Built — validated against `assetz` |
| **3** | `execute/` (generate + provision + runner + report) | RPC flow executor: Claude-generated op-sequences run over XML-RPC against a cloned DB; pass/fail/error + results report; `sentinel run-tests` | FR-13, FR-14, FR-17 | ✅ Built — validated against `assetz` |
| **3** | `execute/ui_playwright` | Playwright UI smoke crawl: console/JS/network errors + error dialogs + screenshots per view; `sentinel run-ui` | FR-15 | ✅ Built — validated against `assetz` |
| **3** | Docker sandbox | Fully isolated, disposable execution environment | FR-16, NFR-02 | ⬜ Planned |
| **—** | React frontend | Migrate the HTML/JS SPA to React/Redux | — | ⬜ Planned |

### 11.1 Per-phase acceptance highlights
- **Phase 1 (understand):** `assetz` introspects to the correct new/extended model split and counts;
  the understanding report renders — with no LLM (AC-1).
- **Phase 2 (reason):** one `/api/audit` call returns a test plan + bug/gap report whose findings each
  cite a real `file:line`/`model.method`; the run is billed to the subscription with no
  `ANTHROPIC_API_KEY` present (AC-2, AC-3).
- **Phase 2 (degradation):** with no Claude Code CLI installed, the UI still runs and chat falls back
  to the mock engine (AC-5).
- **Phase 3 (execute):** test cases run against a duplicate DB in the sandbox; production is never
  written to; results are pass/fail with evidence (AC-6).

---

## 12. Cross-Cutting Concerns

| Concern | Approach |
|---|---|
| **Read-only safety (NFR-01)** | Engine restricted to `Read,Grep,Glob`; introspection issues no writes; the skill states "never modify any file." |
| **Execution isolation (NFR-02)** | Phase 3 executes only against a duplicate DB inside a Docker sandbox with resource caps. |
| **Flat-cost reasoning (NFR-03)** | `ANTHROPIC_API_KEY` popped from the engine env (`SENTINEL_FORCE_SUBSCRIPTION=1` default) → billed to the subscription; `cost_usd` surfaced per run. |
| **Graceful degradation (NFR-07)** | No CLI → mock engine; RPC auth/error → clear message (not a crash); engine timeout → `error` event, child killed (no orphans). |
| **Grounding / accuracy (NFR-04)** | Deterministic System Map precedes reasoning; the skill demands `file:line`/`model.method` evidence and "report only what you can point to." |
| **Transparency (NFR-05)** | Every report ends with a Coverage note (what was read / not reached). |
| **Windows portability (NFR-09)** | Native `claude.exe` preferred; system prompt capped at 24k to stay under the command-line limit; cwd kept off the addon's git repo for fast startup. |
| **Extensibility (NFR-08)** | New static runner = new `Runner` subclass; new introspection facet = new fetch in `introspect.py`; new executor (Phase 3) = new tool; the **skill** is editable Markdown. |

---

## 13. Sequence Diagrams

**Understand → audit (current path):**
```
User ─POST /api/introspect─▶ FastAPI ─▶ OdooRPCClient.authenticate()
                                       └▶ build_system_map() ─▶ SystemMap (+ scan_addon)
   FastAPI ─cache _SUMMARY[module]─▶ returns counts + understanding-report markdown   (NO LLM)

User ─POST /api/audit/stream─▶ FastAPI ─build_system_prompt(skill + System Map)─▶ ClaudeCodeEngine
   engine ─`claude -p` (Read/Grep/Glob over the addon, subscription)─▶ stream text/tool events ─▶ User
   engine ─result─▶ write output/audit-<module>/test_plan.md ─▶ return markdown + cost_usd
```

**Phase 3 execution (planned):**
```
test plan ─▶ Docker sandbox: copy DB → duplicate
   ├─ RPC flow executor ─(XML-RPC on duplicate)▶ create/act/assert ─▶ case pass/fail + bugs
   └─ Playwright executor ─(web client on duplicate)▶ menus/forms/buttons ─▶ console/network/screenshot
   ─▶ Test Plan + Results doc (pass/fail + evidence)
```

---

## 14. Future Extensions (post-current)

- **Phase 3 build-out** — RPC flow executor, Playwright UI executor, Docker sandbox + duplicate DB.
- **React frontend** — migrate the HTML/JS SPA (Redux state + SSE stream helper).
- **Structured findings** — have `/api/audit` emit `core/models.Finding[]` JSON (not only Markdown)
  so results persist as `output/<run>/findings.json`.
- **Deterministic Odoo lint pass (optional)** — re-introduce a focused Python AST / ruff check over
  the addon source as a grounding signal for the engine (the removed generic linters, Odoo-scoped).
- **Persistence** — move run artifacts from `output/<run>/` JSON files to a database for history/diff.
- **Auto-fix proposals as diffs** (human-approved) — generate a patch per finding.
- **CI integration** — run on a branch, comment findings inline, fail on critical.
- **MCP packaging** — expose the Odoo tools via an MCP server for use inside other agents.

---

## 15. Traceability Matrix (summary)

| Requirement group | Realised by |
|---|---|
| Understand (FR-01–04) | `odoo/rpc`, `odoo/introspect`, `odoo/addon_scan`, `odoo/report` (§5), `/api/introspect` (§7) — **built** |
| Reason engine (FR-05–07) | `engine/claude_code`, `engine/skill` (§4, §6) — **built** |
| Bug/gap detection + test plan (FR-08–12, FR-18–20) | `audit/` two-pass runner + Odoo-QA skill via `sentinel audit` / `/api/audit` (§4, §6, §6A, §7) — **built** |
| Execute — RPC flows (FR-13, FR-14, FR-17) | `execute/` generate + provision (clone) + runner + report via `sentinel run-tests` (§10) — **built** |
| Execute — UI crawl (FR-15) | `execute/ui_playwright` Playwright smoke crawl via `sentinel run-ui` (§10) — **built** |
| Execute — sandbox (FR-16) | Docker sandbox (§10) — **planned** |
| Report + modes (FR-18–24) | `core/models`, `/api/audit[/stream]`, `/api/chat[/stream]`, `output/<run>/` (§3, §7) |
| Read-only / isolation / flat-cost (NFR-01–03) | read-only tools, Phase 3 sandbox, subscription billing (§4, §10, §12) |
| Degradation / portability / extensibility (NFR-07–09) | mock fallback, native-CLI resolution, editable skill (§4, §12) |

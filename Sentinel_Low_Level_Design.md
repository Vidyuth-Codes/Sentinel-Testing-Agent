# Sentinel — Low-Level Design

# Agentic QA & Bug-Detection Agent for Odoo

**Version:** 2.1 — auth + guided UI + deep investigation  |  **Date:** June 2026  |  **Classification:** Internal — R&D
**Status:** Active — Phase 1 & 2 built & running
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
UI renders chat + a System Map dashboard, behind an **authentication layer**. Target-instance
*execution* (Phase 3) runs against a **duplicate database** inside a **Docker sandbox**.

```
┌───────────────────────────────────────────────────────────────┐
│ FRONTEND  (web UI — this repo)                                 │
│   auth overlay (login / first-run setup)                       │
│   mode-picker chat · System Map dashboard · PDF report         │
│   HTML/JS today · React/Redux planned                          │
└───────────────┬───────────────────────────────────────────────┘
                │  HTTP (FastAPI) + SSE
┌───────────────▼───────────────────────────────────────────────┐
│ BACKEND  (FastAPI — src/sentinel/web/app.py)                  │
│   /api/auth/*        → authentication (stdlib only, no deps)  │
│   /api/introspect    → deterministic Odoo tools  (NO LLM)     │
│   /api/chat[/stream], /api/audit[/stream] → Claude Code       │
│   /api/investigate[/stream], /api/flow[/stream] → Claude Code │
└───────┬───────────────────────────────────┬───────────────────┘
        │ deterministic tools               │ reasoning engine
┌───────▼────────────────────────┐   ┌──────────▼────────────────┐
│ Odoo tools (src/sentinel/odoo) │   │ Claude Code engine         │
│  rpc · introspect (System Map) │   │ (engine/claude_code.py)    │
│  addon_scan (AST) · context    │   │  headless `claude -p`      │
│  investigate · deployment      │   │  Read/Grep/Glob (read-only)│
│  · report                      │   │  guided by the Odoo-QA     │
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
│   ├── cli.py                 ← `sentinel web | introspect | scan-addons | audit | run-tests | run-ui`
│   ├── paths.py               ← output dir resolution (output/<run>/)
│   ├── core/
│   │   └── models.py          ← Finding, CodeLocation, Evidence, RunResult + taxonomy
│   ├── odoo/                  ← DETERMINISTIC Odoo tools (no LLM) — the Understand layer
│   │   ├── rpc.py             ← read-only XML-RPC client
│   │   ├── introspect.py      ← build_system_map(): live instance → SystemMap
│   │   ├── addon_scan.py      ← AST scan of addon source on disk; cross-check vs live
│   │   ├── schema.py          ← SystemMap + OdooModelInfo/Field/View/Action/Access/Rule/Cron/…
│   │   ├── context.py         ← summarize_system_map(): compact System Map brief for the engine
│   │   ├── investigate.py     ← 2-hop live-data diagnosis (stock moves + invoice lines)
│   │   ├── deployment.py      ← instance-wide scan: split installed modules into custom vs core
│   │   └── report.py          ← System Map → Markdown understanding report + JSON
│   ├── engine/               ← REASONING layer — Claude Code on subscription
│   │   ├── claude_code.py     ← ClaudeCodeEngine: headless `claude -p`, sync + streaming
│   │   └── skill.py           ← load Odoo-QA skill + assemble system prompt (+ System Map)
│   ├── audit/                ← Phase 2 — the structured audit (two-pass)
│   │   ├── runner.py          ← generate_report (pass 1) + structure_report (pass 2) + persist
│   │   └── models.py          ← TestPlan / RequirementCoverage / AuditTestCase / AuditOutcome
│   ├── execute/              ← Phase 3 — RPC flow executor + UI smoke crawl
│   │   ├── generate.py        ← Claude Code → executable op-sequences
│   │   ├── provision.py       ← clone source DB via the `db` service (or existing-DB opt-in)
│   │   ├── runner.py          ← deterministic XML-RPC executor (refs, asserts, teardown)
│   │   ├── ui_playwright.py    ← Playwright crawl (console/JS/network/screenshots)
│   │   ├── report.py          ← results.{md,json} + cases.json + ui_results.{md,json}
│   │   └── models.py          ← ExecStep / ExecCase / CaseResult / ExecReport / UIPageResult
│   └── web/
│       ├── app.py             ← FastAPI: /api/auth/*, /api/config, /api/introspect,
│       │                         /api/chat[/stream], /api/audit[/stream],
│       │                         /api/investigate[/stream], /api/flow[/stream],
│       │                         /api/deployment, /api/deployment/overview
│       ├── auth.py            ← stdlib-only auth: pbkdf2_hmac passwords, HMAC-SHA256 tokens,
│       │                         per-user session isolation, admin user management
│       └── static/index.html  ← single-page UI (mode-picker chat + System Map dashboard)
├── data/                      ← runtime data (users.json) — created on first run, git-ignored
├── skills/odoo-qa/SKILL.md    ← testing playbook (anti-hallucination rules + auto-discovery)
├── tests/unit/                ← pytest suite (Odoo layer: System Map counts + LLM brief)
└── output/                    ← run artifacts — git-ignored
```

> **Cleanup note.** The retired generic/metered-API modules — `llm/` (raw-Anthropic client), `plan/`
> (metered-API planner), `ingest/` + `pipeline.py` + `report/` + `static/` (the generic
> stack-detect → lint → report audit), and the `sentinel audit`/`plan` CLI commands — **have been
> removed**. `summarize_system_map` was relocated to `odoo/context.py`; `core/models.py` is
> retained as the `Finding` schema.

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
    rule_id: str | None
```

`RunResult` wraps a run: `project_ref`, `project_map`, `test_plan`, `findings`, `coverage`
(tool/layer → `"ran"` | `"skipped: …"`), timestamps, and `severity_rollup()`.

### 3.2 `SystemMap` — `src/sentinel/odoo/schema.py`

The agent's model of "what the addon built", produced by introspection (FR-02).

```python
class OdooField(BaseModel):
    name; string; ttype; required; readonly; store; relation; related; compute; help
    owned_by_addon: bool

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
names. Produces an `AddonScan` used to cross-check the live System Map against the source. (FR-03)

### 5.4 `report.py` — understanding report
`render_system_map_markdown(smap, scan)` / `write_system_map(...)` render the System Map (+ optional
scan) into a Markdown understanding report and JSON. (FR-04)

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

### Skill — anti-hallucination rules (FR-11, NFR-05)

The skill enforces two **hard rules** that cannot be overridden:

**Never hallucinate.** Before stating anything about a model, field, method, or state value:
- If addon source available → find the file and read it first.
- If source NOT available → answer only from System Map and say so explicitly.
- If unsure whether a field exists → grep before mentioning it.

**Auto-discover before answering.** When a question references a model not yet read:
1. Look up the file path in the System Map.
2. Read that file fully.
3. Grep for `@api.depends`, `@api.constrains`, `action_*`, compute methods.
4. Follow `_inherit` one level deep.
5. Only then answer.

These rules were added after observing the agent giving hedged, imprecise answers in support
scenarios — "I can't confirm whether…" — when the answer was available in the data it had
already fetched.

---

## 6A. The Two-Pass Structured Audit (Phase 2) — `src/sentinel/audit/`

A one-shot audit must yield both a **human Markdown report** and **machine-readable structure**
(`Finding[]` + a test plan) per FR-18/19/20. `audit/runner.py` does this in two engine passes so the
expensive code-reading happens once:

**Pass 1 — `generate_report`.** Claude Code reads the addon (`--add-dir`) with the Odoo-QA skill +
System Map as system prompt and writes the Markdown report (`REPORT_PROMPT`: requirement-coverage
table, rpc/ui test cases, grounded findings with `file:line` evidence).

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

**`_source_dir()` — addons root support.** The audit runner accepts either a single addon folder
(contains `__manifest__.py` directly) or an **addons root** folder (a child directory contains
`__manifest__.py`). Both resolve correctly. This covers the case where the user points Sentinel at
`C:\path\to\addons` (a folder containing multiple addon subfolders) rather than the specific addon
inside it.

```python
def _source_dir(addons: str | None) -> str | None:
    if not addons: return None
    p = Path(addons)
    if not p.is_dir(): return None
    if (p / "__manifest__.py").exists(): return addons          # single addon
    if any((child / "__manifest__.py").exists()                 # addons root
           for child in p.iterdir() if child.is_dir()):
        return addons
    return None
```

**Persistence** (`_save`): `output/audit-<module>-<timestamp>/` gets `report.md`, `findings.json`
(`Finding[]`), and `test_plan.json`.

The CLI (`sentinel audit`) and the non-streaming `/api/audit` call `run_full_audit` (both passes). The
streaming `/api/audit/stream` runs pass 1 live to the UI, then calls `structure_report` server-side and
emits a final `summary` event (counts, rollup, saved paths, cost).

---

## 6B. Live-Data Investigation — per-record diagnosis (`src/sentinel/odoo/investigate.py`)

The **support/troubleshooting** capability: a functional user asks, in plain language, about a
*specific live record* — *"why does S00437 still show 0 delivered?"* — and Sentinel reads that
record's real data and explains what happened. This is the forensic, record-level analysis.

**Architecture — Sentinel queries, Claude Code reasons:**

```
question ─▶ extract_references()  ─ pull "S00437" / "INV/2026/00010" / "WH/OUT/00032" from the text
         ─▶ resolve_record()      ─ search business models (sale.order, account.move, stock.picking…) by name
         ─▶ fetch_record_graph()  ─ read the record + related rows (1 hop) + 2-hop expansion
         ─▶ render_graph()        ─ compact text bundle (capped ~16k)
         ─▶ Claude Code reasons over the data (INVESTIGATE_SYSTEM) ─▶ plain-language diagnosis
```

**2-hop expansion (added for investigation precision).**  The first hop fetches related records
(pickings, invoices). The second hop immediately expands each picking into its **stock moves**
(fields: `product_id`, `sale_line_id`, `qty_done`, `state`) and each invoice into its **account
move lines** (product lines only). This gives the engine:

- `sale_line_id = False` on a stock move → the move is **orphaned** (not linked to any sale line)
- `product_id` on every move and invoice line → the **variant** actually shipped / billed
- invoice lines per invoice → which products were billed on which invoice

```python
def _fetch_moves(client, picking_ids):
    return client.search_read("stock.move",
        [["picking_id", "in", picking_ids]],
        ["product_id", "sale_line_id", "qty_done", "state", "name"])

def _fetch_invoice_lines(client, invoice_ids):
    return client.search_read("account.move.line",
        [["move_id", "in", invoice_ids], ["product_id", "!=", False]],
        ["product_id", "quantity", "price_unit", "move_id", "name"])
```

**`INVESTIGATE_SYSTEM` precision rules.** The prompt instructs the engine:
- Cite EXACT record names, product IDs, user names, UTC timestamps.
- Read `sale_line_id` on every stock move — if False/None the move is **ORPHANED**.
- Read `product_id` on every stock move and invoice line — this is the VARIANT.
- Build timeline from chatter authors and dates.
- **NEVER** hedge with "I can't prove" if the answer is in the data.
- **NEVER** invent record names, IDs, quantities, or events.

Output structure: Root cause → Complete timeline → Records involved → Data integrity issues → What to do.

Limits: `max_related_rows=20`, `max_messages=80` (up from 12/40 before 2-hop was added — the deep
data requires more tokens).

- **Read-only** — only `search_read`/`read`; never writes. Needs no source code.
- Exposed at `POST /api/investigate/stream` — used by Logic/UI Gaps mode.

---

## 6C. Deployment Scan — what's custom across the instance (`src/sentinel/odoo/deployment.py`)

For a **heavily-customised deployment** (many tailored modules) the first question is *"what has been
custom-built here?"*. `scan_deployment` reads `ir.module.module` and splits installed modules into
**custom/non-standard** vs **core Odoo**.

The classifier combines two signals, because **author alone is unreliable** — partner developers
frequently leave the scaffold's `author = "Odoo S.A."`. A module is treated as **core Odoo only if**
its author is Odoo/OCA **AND** its version is the 4-part `series.x.y` form (e.g. `18.0.1.3`). Custom
partner modules keep the scaffold's **5-part** version (`18.0.1.0.5`), so they're caught even with a
faked author. Validated on a real client instance.

Exposed at `POST /api/deployment` (the list) + `POST /api/deployment/overview` (an engine narrative
grouping the customisations by business area). Read-only.

---

## 6D. Authentication — `src/sentinel/web/auth.py` (FR-25–29, NFR-10)

Sentinel is a multi-user service. `auth.py` provides a complete authentication system using **stdlib
only** (zero new dependencies):

**Storage.** User accounts are stored in `data/users.json` as a list of objects:
```json
{"username": "admin", "hashed": "<pbkdf2_hmac hash>", "role": "admin"}
```

**Password hashing.** `hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 390000)`. Salt and
hash stored as hex; brute-forcing is impractical.

**Session tokens.** On successful login, a 32-byte cryptographically random token is generated,
stored in `_SESSIONS` (in-memory dict `{token: username}`), and returned to the client as a cookie.
Token validation uses `hmac.compare_digest` (constant-time, resistant to timing attacks).

**First-run setup.** On startup, if `data/users.json` doesn't exist or contains no users, the
`/api/auth/status` endpoint returns `{"first_run": true}`. The frontend shows a setup form;
`/api/auth/setup` creates the admin account and seeds the file.

**Per-user session isolation.** The FastAPI dependency `get_current_user()` extracts the username
from the validated token. The caches `_SUMMARY` and `_SESSION` are keyed by `(username, module)` so
sessions, conversation history, and audit state are fully isolated between concurrent users (FR-28).

**Endpoints:**

| Endpoint | Method | Behaviour |
|---|---|---|
| `/api/auth/status` | GET | Returns `{logged_in, first_run, username}` |
| `/api/auth/login` | POST | Verify credentials → set `sentinel_token` cookie |
| `/api/auth/logout` | POST | Delete token from `_SESSIONS`, clear cookie |
| `/api/auth/setup` | POST | Create first admin (only works when no users exist) |
| `/api/auth/users` | GET | List all users (admin only) |
| `/api/auth/users` | POST | Create a new user (admin only) |
| `/api/auth/users/{username}` | DELETE | Delete a user (admin only; cannot delete self) |

---

## 7. Web / API Layer — `src/sentinel/web/app.py` (FastAPI)

`FastAPI(title="Sentinel — Odoo Testing Agent")`. A single `ClaudeCodeEngine` instance is shared;
two in-memory caches key off `(username, module)`: `_SUMMARY` (System Map brief) and `_SESSION`
(Claude Code session id for multi-turn continuity).

| Endpoint | Layer | Behaviour | FRs |
|---|---|---|---|
| `GET /` | — | Serves `static/index.html`. | — |
| `GET /api/config` | — | Version, connection defaults, and whether the engine is `claude-code` or `mock`. | — |
| `GET /api/auth/status` | auth | Returns login state + first_run flag. | FR-25, FR-26 |
| `POST /api/auth/login` | auth | Authenticate → session cookie. | FR-25, FR-27 |
| `POST /api/auth/logout` | auth | Clear session. | FR-25 |
| `POST /api/auth/setup` | auth | Create first admin (first-run only). | FR-26 |
| `GET /api/auth/users` | auth | List accounts (admin). | FR-29 |
| `POST /api/auth/users` | auth | Add account (admin). | FR-29 |
| `DELETE /api/auth/users/{u}` | auth | Delete account (admin). | FR-29 |
| `POST /api/introspect` | deterministic | Connect → `build_system_map` → optional `scan_addon`; caches summary; returns counts + understanding report markdown. **No LLM.** | FR-01–04 |
| `POST /api/chat` | reasoning | `run_sync` with skill+System Map, addon as `code_dir`, cached session. Falls back to `_mock_reply` if engine unavailable. | FR-22, NFR-07 |
| `POST /api/chat/stream` | reasoning | SSE variant — streams `text`/`tool`/`result` events. | FR-23 |
| `POST /api/audit` | reasoning | Two-pass `run_full_audit` (§6A): saves `report.md` + `findings.json` + `test_plan.json`; returns markdown, finding count, severity rollup, cost, and saved paths. | FR-18–21 |
| `POST /api/audit/stream` | reasoning | SSE variant — streams pass 1 live, then runs pass 2 server-side and emits a final `summary` event. | FR-18–21, FR-23 |
| `POST /api/overview` | reasoning | Functional overview of the module (what it does for users, key capabilities, who uses it). Uses `_OVERVIEW_SYSTEM` prompt — no source required. | FR-04 |
| `POST /api/investigate/stream` | data+reasoning | Per-record 2-hop diagnosis (§6B): resolve reference → fetch live data graph → engine explains. Read-only. | FR-30, support |
| `POST /api/flow/stream` | data+reasoning | Explain a flow grounded in real example records; hypothetical example if none exist. Read-only. | FR-31, support |
| `POST /api/deployment` | deterministic | Instance-wide scan (§6C): custom vs core Odoo modules. | support |
| `POST /api/deployment/overview` | reasoning | Engine narrative grouping custom modules by business area. | support |

Re-introspecting a module **resets** its cached session (`_SESSION.pop`) so a fresh understanding
starts a fresh conversation. SSE responses set `Cache-Control: no-cache` / `X-Accel-Buffering: no`.

---

## 8. Frontend — `src/sentinel/web/static/index.html`

A **single-page app** with light ("Hotel Gold") and dark ("Hotel Night") themes (vanilla HTML/JS,
`marked.js` for Markdown). The UI has three states:

### 8.1 Authentication overlay

On page load, `boot()` calls `/api/auth/status`:
- `first_run: true` → shows the admin setup form (username + password fields → `/api/auth/setup`).
- `logged_in: false` → shows the login form (→ `/api/auth/login`).
- `logged_in: true` → proceeds to the main UI.

The admin panel (accessible by admin users) adds/removes accounts via the `/api/auth/users` endpoints.

### 8.2 Connection bar

Always visible once logged in. Fields: Odoo URL, database, user, password, module name, addon path,
SSL verify toggle. Defaults populated from `/api/config`. Connection settings are saved to
`localStorage` so they survive page refresh.

The addon path field accepts either a **single addon folder** (with `__manifest__.py`) or an
**addons root folder** (a parent folder containing multiple addon subfolders). Both are correctly
resolved by `_source_dir()` in the backend.

### 8.3 Mode-picker chat UI

After login and connection, `boot()` calls `showModePicker()` — a chat card with five mode buttons:

| Mode | Button label | Routes to | Behaviour |
|------|-------------|-----------|-----------|
| **Understand** | "Understand a module" | `/api/introspect` → `/api/overview` | If module field pre-filled → introspects immediately. If user types a module name in chat → sets the module field and introspects. |
| **Logic / UI Gaps** | "Logic / UI Gaps" | `/api/investigate/stream` | Routes the typed question as the investigation query. |
| **Code Errors** | "Code Errors" | `/api/audit/stream` | Checks addon path is filled first; if not, prompts. If path is filled and the message is empty, triggers the full audit. |
| **Report** | "Report" | (scope picker + PDF) | Opens a scope picker (whole chat / last conversation / new topic). Generates a PDF-printable Markdown report via the browser print window. Detects "report on X" phrases in typed text and auto-triggers. |
| **General Question** | "General Question" | `/api/flow/stream` | Routes the question to flow-explanation grounded in real records. |

The mode picker card **disappears** when a mode is selected (`_lastPickerCard` tracks the DOM element
and removes it). The mode badge in the panel title and the input placeholder text update to reflect
the active mode. The **↺ Switch** button re-presents the mode picker without wiping conversation
history or session state (FR-22).

### 8.4 `send()` routing

The `send()` function is the single message handler. It branches by `currentMode`:

```js
async function send() {
    const m = $('msg').value.trim();
    if (!currentMode) { showModePicker(); return; }
    if (!m) { if (currentMode === 'errors') return startErrorScan(); return; }
    switch (currentMode) {
        case 'understand':
            // type a module name → set module field + introspect
            $('module').value = m; saveConn();
            understand(); break;
        case 'gaps':
            await streamRun('/api/investigate/stream', {…, question: m}); break;
        case 'errors':
            if (!addons) { addBot('⚠️ Please fill in addon source path'); return; }
            await streamRun('/api/chat/stream', {…, message: m}); break;
        case 'report':
            return sendAsReport(m);    // detects "report on X" phrases
        case 'general':
            await streamRun('/api/flow/stream', {…, question: m}); break;
    }
}
```

### 8.5 Stream cancellation (Stop button)

An **AbortController** is created when a stream starts and stored globally. The Stop button (`id="stopBtn"`)
calls `controller.abort()`. The SSE reader catches the `AbortError` and closes cleanly. Once the stream
ends (naturally or cancelled), the Stop button becomes a Send button again. (FR-34)

### 8.6 PDF report generation

`makeReport()` calls `cleanReportContent()` to strip any Coverage sections and suggestion
paragraphs from the content, then opens `window.print()`. The browser's print dialog produces a
text-extractable PDF — no server-side rendering required. (FR-32)

### 8.7 Overview prompt — functional capabilities framing

`_OVERVIEW_SYSTEM` in `app.py` instructs the engine to describe what the module **does for users**
(functional capabilities), not the structural new/extended counts. Output structure:

- `## 📦 What this module does` — 2–3 sentences: what business process it enables
- `## ⚙️ Key capabilities` — 5–8 bullets: concrete user-facing capabilities
- `## 👥 Who uses it and when` — 1–2 sentences: roles and business context

### 8.8 System Map dashboard

The right pane renders: System Map counts (models, views, fields, security), the understanding
report Markdown, and a model list. Clicking a module name in the deployment overview introspects
that module directly.

**Planned upgrade (roadmap):** migrate to **React** (with Redux for run/chat/findings state and a
stream helper for SSE) once Phase 2 stabilises. The HTML/JS UI is sufficient until then. (Req §13 Q4)

---

## 9. Removed Legacy (record of the cleanup)

| Removed | Was | Why it went |
|---|---|---|
| `llm/client.py` | Raw `anthropic` SDK wrapper (metered API, `.env` key, mock mode) | Reasoning moved to the Claude Code CLI (subscription); the SDK path and the API key are obsolete. |
| `plan/` | Metered-API test-plan / coverage generator | Gap analysis + test-plan generation is now Claude Code's job via `/api/audit`. |
| `ingest/` + `pipeline.py` + `report/` | Generic stack-detect → lint → audit pipeline | Generic multi-stack auditing is out of scope (Odoo only). |
| `static/` (base, engine, runners/*) | Deterministic generic linters | Only ever invoked by the removed generic pipeline. |
| `sentinel audit`/`plan` CLI (old) | Entry points for the two old pipelines | Backing code was removed; the CLI is now `web` / `introspect` / `scan-addons` / `audit` (new). |
| `tests/sample_app/` + generic tests | Generic React+FastAPI fixture | Tested only the removed generic path. |
| Action buttons (Understand, Scan Modules, Diagnose, Test Plan, Report) | Frontend HTML buttons in the toolbar / composer | Replaced by the mode-picker chat card and ↺ Switch button. |

---

## 10. Phase 3 — Execute + Report

Phase 3 turns reasoning into **executed results** against a database that is never production
(FR-13–17, NFR-02). The **RPC flow executor is built** (`src/sentinel/execute/`); the Playwright UI
executor is also built; the Docker sandbox is still planned.

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

**Safety (NFR-02):** `provision.py` clones the source DB via Odoo's `db` XML-RPC service
(`OdooDbAdmin.duplicate`, needs the **master password** — the `admin_passwd` from `odoo.conf`, not
the Odoo login password) into `<db>_sentinel_<ts>`, runs there, and **drops it after**. Running
against an existing DB requires the explicit `--use-existing-db` opt-in.

**UI smoke crawl (built — `ui_playwright.py`, FR-15).** `sentinel run-ui` introspects the addon's
window actions, logs into the Odoo web client once with Playwright/Chromium, then opens each action
in a fresh page and records: **console errors, uncaught JS exceptions, failed 4xx/5xx requests, and
Odoo error dialogs**, with a **screenshot** per page. Read-only (no records created), so needs no
clone. Pages are classified **ok / issues / load_error**. Requires `pip install playwright` +
`python -m playwright install chromium` (the `ui` extra).

**Still planned:** the **Docker sandbox** (FR-16) for fully isolated, disposable execution.

---

## 11. Build Sequence

| Phase | Component | Key deliverable | FRs | Status |
|---|---|---|---|---|
| **1** | `odoo/` tools + `core/models` | XML-RPC client, `build_system_map` → SystemMap, `addon_scan`, understanding report | FR-01–04 | ✅ Done |
| **1** | `web/auth.py` + login UI | Login page, first-run setup, per-user session isolation, admin user management | FR-25–29, NFR-10 | ✅ Done |
| **1** | `web/` + `static/index.html` | FastAPI + SPA: mode-picker chat, auth overlay, PDF report | FR-21–23, FR-32–34 | ✅ Done |
| **2** | `engine/claude_code` + `engine/skill` | Headless Claude Code engine (sync+stream), Odoo-QA skill, subscription billing | FR-05–07 | ✅ Done |
| **2** | `audit/` + `/api/chat`, `/api/audit`, `sentinel audit` | Real gap analysis, bug findings, test-plan generation; two-pass structured output | FR-08–12, FR-18–21 | ✅ Built |
| **2** | `odoo/investigate.py` + `/api/investigate/stream` | 2-hop live-data diagnosis (stock moves + invoice lines); precision INVESTIGATE_SYSTEM | FR-30, NFR-04 | ✅ Built |
| **2** | `/api/flow/stream` | Flow explanation grounded in real records | FR-31 | ✅ Built |
| **3** | `execute/` (generate + provision + runner + report) | RPC flow executor: Claude-generated op-sequences against cloned DB; `sentinel run-tests` | FR-13, FR-14, FR-17 | ✅ Built |
| **3** | `execute/ui_playwright` | Playwright UI smoke crawl; `sentinel run-ui` | FR-15 | ✅ Built |
| **3** | Docker sandbox | Fully isolated, disposable execution environment | FR-16, NFR-02 | ⬜ Planned |
| **—** | React frontend | Migrate HTML/JS SPA to React/Redux | — | ⬜ Planned |

### 11.1 Per-phase acceptance highlights
- **Phase 1 (understand):** introspects correctly; functional overview describes capabilities, not counts; auth gating works (AC-1, AC-2).
- **Phase 2 (reason):** audit returns test plan + bug/gap report with `file:line` evidence; billed to subscription with no API key (AC-3, AC-4).
- **Phase 2 (chat):** mode picker presents 5 modes; each routes correctly; ↺ Switch works without wiping history (AC-5).
- **Phase 2 (investigation):** record diagnosis with 2-hop data expansion produces exact IDs, timestamps, user names (AC-6).
- **Phase 2 (degradation):** no CLI → mock engine with clear message (AC-8).
- **Phase 3 (execute):** test cases run against a duplicate DB; production never written to (AC-9).

---

## 12. Cross-Cutting Concerns

| Concern | Approach |
|---|---|
| **Read-only safety (NFR-01)** | Engine restricted to `Read,Grep,Glob`; introspection issues no writes; the skill states "never modify any file." |
| **Execution isolation (NFR-02)** | Phase 3 executes only against a duplicate DB (Docker sandbox planned). |
| **Flat-cost reasoning (NFR-03)** | `ANTHROPIC_API_KEY` popped from the engine env → billed to the subscription; `cost_usd` surfaced per run. |
| **Graceful degradation (NFR-07)** | No CLI → mock engine; RPC auth/error → clear message; engine timeout → `error` event, child killed. |
| **Grounding / accuracy (NFR-04)** | Deterministic System Map precedes reasoning; skill demands `file:line`/`model.method` evidence; anti-hallucination rules forbid inventing field names or state values; auto-discovery protocol reads source before answering. |
| **Transparency (NFR-05)** | Agent states which models were read and which weren't; investigation output cites exact record IDs and timestamps; no hedging when the data is present. |
| **Authentication security (NFR-10)** | pbkdf2_hmac password hashes; HMAC-SHA256 session tokens; constant-time comparison; no plaintext in storage or logs; stdlib only. |
| **Windows portability (NFR-09)** | Native `claude.exe` preferred; system prompt capped at 24k; cwd kept off the addon's git repo for fast startup. |
| **Extensibility (NFR-08)** | New introspection facet = new fetch in `introspect.py`; new executor = new tool in `execute/`; the **skill** is editable Markdown. |

---

## 13. Sequence Diagrams

**Understand → audit (current path):**
```
User ─POST /api/introspect─▶ FastAPI ─▶ auth check (get_current_user)
                                       └▶ OdooRPCClient.authenticate()
                                       └▶ build_system_map() ─▶ SystemMap (+ scan_addon)
   FastAPI ─cache _SUMMARY[(user,module)]─▶ returns counts + understanding-report markdown (NO LLM)

User ─POST /api/audit/stream─▶ FastAPI ─build_system_prompt(skill + System Map)─▶ ClaudeCodeEngine
   engine ─`claude -p` (Read/Grep/Glob over the addon, subscription)─▶ stream text/tool events ─▶ User
   engine ─result─▶ write output/audit-<module>/{report.md, findings.json, test_plan.json} ─▶ return markdown + cost_usd
```

**Investigation (Logic / UI Gaps mode):**
```
User types "S00437 shows 0 delivered" ─POST /api/investigate/stream─▶ FastAPI
   └▶ extract_references("S00437")
   └▶ resolve_record(client, "S00437") → sale.order record
   └▶ fetch_record_graph(client, "sale.order", id)
       ├─ 1-hop: fetch pickings, invoices, linked records
       └─ 2-hop: _fetch_moves(picking_ids) → stock.move (product_id, sale_line_id, qty_done)
                 _fetch_invoice_lines(invoice_ids) → account.move.line (product_id, quantity)
   └▶ render_graph() → compact text bundle (~16k)
   └▶ ClaudeCodeEngine (INVESTIGATE_SYSTEM) ─▶ stream precise diagnosis with exact IDs + timeline
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

- **Phase 3 completion** — Docker sandbox (fully isolated, disposable execution environment).
- **React frontend** — migrate the HTML/JS SPA (Redux state + SSE stream helper).
- **Structured findings in UI** — render `core/models.Finding[]` JSON in the dashboard (counts, filters).
- **Deterministic Odoo lint pass (optional)** — focused Python AST / ruff check as a grounding signal.
- **Persistence** — move run artifacts from `output/<run>/` JSON files to a database.
- **Auto-fix proposals as diffs** (human-approved) — generate a patch per finding.
- **CI integration** — run on a branch, comment findings inline, fail on critical.
- **MCP packaging** — expose the Odoo tools via an MCP server for use inside other agents.

---

## 15. Traceability Matrix (summary)

| Requirement group | Realised by |
|---|---|
| Understand (FR-01–04) | `odoo/rpc`, `odoo/introspect`, `odoo/addon_scan`, `odoo/report` (§5), `/api/introspect` (§7) — **built** |
| Authentication (FR-25–29, NFR-10) | `web/auth.py`, `/api/auth/*` (§6D, §7) — **built** |
| Reason engine (FR-05–07) | `engine/claude_code`, `engine/skill` (§4, §6) — **built** |
| Bug/gap detection + test plan (FR-08–12, FR-18–20) | `audit/` two-pass runner + Odoo-QA skill via `sentinel audit` / `/api/audit` (§6A, §7) — **built** |
| Live-data investigation (FR-30) | `odoo/investigate.py` with 2-hop expansion, `/api/investigate/stream` (§6B) — **built** |
| Flow explanation (FR-31) | `/api/flow/stream` (§7) — **built** |
| Mode-picker UI (FR-22, FR-32–34) | `static/index.html` mode picker, ↺ Switch, AbortController stop, scope picker, PDF via print (§8) — **built** |
| Execute — RPC flows (FR-13, FR-14, FR-17) | `execute/` generate + provision + runner + report via `sentinel run-tests` (§10) — **built** |
| Execute — UI crawl (FR-15) | `execute/ui_playwright` Playwright smoke crawl via `sentinel run-ui` (§10) — **built** |
| Execute — sandbox (FR-16) | Docker sandbox (§10) — **planned** |
| Read-only / isolation / flat-cost (NFR-01–03) | read-only tools, Phase 3 sandbox, subscription billing (§4, §10, §12) |
| Degradation / portability / extensibility (NFR-07–09) | mock fallback, native-CLI resolution, editable skill (§4, §12) |

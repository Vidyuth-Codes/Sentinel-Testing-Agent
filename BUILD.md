# Sentinel — Build & Run Guide

Implementation companion to the [Requirement Document](Sentinel_Requirement_Document.md)
and [Low-Level Design](Sentinel_Low_Level_Design.md). Sentinel is an **Odoo 18 Enterprise** QA
agent whose reasoning runs on **Claude Code** (flat subscription). Built in **3 phases**.

> **Architecture note.** Earlier drafts of this guide described a generic LangGraph + pgvector +
> metered-API pipeline. That design is **retired** — see the LLD's supersession note. The reasoning
> engine is now **Claude Code**, and the scope is **Odoo only**.

---

## Phase status

| Phase | Scope | Status |
|-------|-------|--------|
| **Phase 1 — Understand + Frontend** | Odoo XML-RPC tools (connect, introspect → System Map), addon source scan, understanding report, web UI (chat + dashboard + PDF report download), **auth system** (login, per-user sessions, admin user management). The deterministic, no-LLM foundation. | ✅ **Done — runnable** |
| **Phase 2 — Reason via Claude Code** | `/api/chat` + `/api/audit` + `sentinel audit` driven by the Claude Code engine + the Odoo-QA skill: reads the addon + System Map and produces gap analysis, bug findings, and the test plan; a two-pass audit emits a human report **and** structured `findings.json` + `test_plan.json`. Deep investigation via 2-hop data fetch (stock moves + invoice lines). | ✅ **Built** |
| **Phase 3 — Execute + Report** | RPC **flow executor** (`sentinel run-tests`): Claude-generated op-sequences run over XML-RPC against a **cloned DB** (or existing, opt-in). **Playwright UI crawl** (`sentinel run-ui`): logs into the web client and opens each view, capturing console/JS/network errors + screenshots. Both emit reports. **Docker sandbox** still planned. | 🟡 Partial |

---

## Phase 1 — what's built

```
src/sentinel/
  odoo/                      DETERMINISTIC Odoo tools (no LLM) — the Understand layer
    rpc.py                   read-only XML-RPC client (auth, search_read, fields_get, execute_kw)
    introspect.py            build_system_map(): live instance → SystemMap
    addon_scan.py            AST scan of addon source on disk; cross-check vs live
    schema.py                SystemMap + OdooModelInfo/Field/View/Action/Access/Rule/Cron/…
    report.py                System Map → Markdown "understanding report" + JSON
    investigate.py           2-hop live-data record diagnosis (stock moves + invoice lines)
    deployment.py            whole-deployment module scan
  engine/                    REASONING layer — Claude Code on subscription
    claude_code.py           ClaudeCodeEngine: headless `claude -p`, sync + streaming, read-only
    skill.py                 load Odoo-QA skill + assemble system prompt (+ System Map)
  odoo/context.py            summarize_system_map(): compact System Map brief for the engine
  web/
    app.py                   FastAPI: /api/auth/*, /api/config, /api/introspect, /api/chat[/stream],
                             /api/audit[/stream], /api/investigate[/stream], /api/flow[/stream]
    auth.py                  stdlib-only auth: pbkdf2_hmac passwords, HMAC-SHA256 tokens,
                             per-user session isolation, admin user management
    static/index.html        single-page UI (mode-picker chat + System Map dashboard + PDF report)
  core/models.py             Finding, RunResult + taxonomy (the structured-findings schema for Phase 2)
  cli.py                     sentinel web | introspect | scan-addons | audit | run-tests | run-ui
skills/odoo-qa/SKILL.md      the testing playbook injected as Claude Code's system prompt
                             (anti-hallucination rules + auto-model-discovery protocol)
tests/unit/                  pytest suite (Odoo layer: System Map counts + the LLM brief)
```

> The **Odoo-specific** logic (wrong `@api.depends`, ineffective `@api.constrains`, illegal `state`
> transitions, broken view/method contracts, over-broad access rules) is **Claude Code's** job, guided
> by `skills/odoo-qa/SKILL.md` and grounded in the System Map + addon source scan.

---

## Setup

```powershell
# use your project directory
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

**Reasoning engine (Phase 2) — one-time Claude Code setup:**

```powershell
npm install -g @anthropic-ai/claude-code
claude          # sign in with the Claude subscription (NO ANTHROPIC_API_KEY needed)
```

**UI executor (Phase 3 `run-ui`) — one-time Playwright setup:**

```powershell
.\.venv\Scripts\python.exe -m pip install ".[ui]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

With the CLI installed and signed in, `/api/config` reports `engine: claude-code`; otherwise the UI
falls back to a **mock** engine so the frontend is alive before Claude Code is connected.

---

## Run it

### Web UI (primary)

```powershell
# 1. start your Odoo instance (separate terminal, from the addon's project directory)

# 2. start Sentinel's web UI:
.\.venv\Scripts\sentinel.exe web        # -> http://127.0.0.1:8800
```

Open `http://127.0.0.1:8800`. On **first run**, a setup screen prompts for an admin username and
password — this creates the admin account. Subsequent visits show the login page.

After logging in, fill in the connection bar (Odoo URL, database, credentials, module name, and
optionally the addon source path), then choose a mode from the chat card:

| Mode | What it does |
|------|--------------|
| **Understand a module** | Introspects the live instance and builds a System Map; type a module name in chat to switch modules on the fly |
| **Logic / UI Gaps** | Runs live-data investigation — describe a record problem in plain language |
| **Code Errors** | Triggers a full source audit; requires addon path to be filled |
| **Report** | Scope picker (whole chat / last topic / new topic) + PDF generation |
| **General Question** | Routes to the flow-explanation endpoint grounded in real records |

Press **↺ Switch** at any time to return to the mode picker.

Connection field defaults can be set via environment variables:
```powershell
$env:SENTINEL_ODOO_URL      = "http://localhost:8069"
$env:SENTINEL_ODOO_DB       = "my_db"
$env:SENTINEL_ODOO_USER     = "admin"
$env:SENTINEL_ODOO_PASSWORD = "admin"
$env:SENTINEL_MODULE        = "my_module"
$env:SENTINEL_ADDONS        = "C:\path\to\addons"   # single addon folder OR addons root folder
```

### Introspect from the CLI (no LLM)

```powershell
.\.venv\Scripts\sentinel.exe introspect --url http://localhost:8069 --db <db> `
    --user admin --password admin --module <module> --addons C:\path\to\addon --print
# writes the System Map to output/introspect-<module>-<ts>/
```

### Static scan of an addon's source

```powershell
.\.venv\Scripts\sentinel.exe scan-addons C:\path\to\addon
```

### Full Claude Code audit (Phase 2)

```powershell
# from source only:
.\.venv\Scripts\sentinel.exe audit --module <module> --addons C:\path\to\addon

# richer — introspect a live instance first for System Map context:
.\.venv\Scripts\sentinel.exe audit --module <module> --addons C:\path\to\addon `
    --db <db> --url http://localhost:8069 --user admin --password admin
```

Writes `output/audit-<module>-<timestamp>/`: `report.md` (human report), `findings.json` (`Finding[]`),
and `test_plan.json`. Requires the Claude Code CLI installed + signed in; billed to the subscription.

### Run RPC flow tests (Phase 3)

Claude Code generates executable RPC cases, which run over XML-RPC against a **cloned** database
(safe — the source DB is never written to), then the clone is dropped:

```powershell
# clone <db>, run, drop (needs the odoo.conf master password — admin_passwd):
.\.venv\Scripts\sentinel.exe run-tests --module <module> --addons C:\path\to\addon `
    --db <db> --master-pw YOUR_MASTER_PW --max-cases 8

# or run directly against a disposable DB (writes to it; records best-effort cleaned up):
.\.venv\Scripts\sentinel.exe run-tests --module <module> --addons C:\path\to\addon `
    --db <db> --use-existing-db

# focus generation on confirming a prior audit's findings:
.\.venv\Scripts\sentinel.exe run-tests --module <module> --addons C:\path\to\addon `
    --db <db> --master-pw YOUR_MASTER_PW --seed-findings output\audit-<module>-<ts>\findings.json
```

Writes `output/exectest-<module>-<timestamp>/`: `results.md`, `results.json`, `cases.json`. Each case
is **pass** (behaved as asserted), **fail** (behaved differently — often a confirmed bug), or
**error** (invalid setup). The master password can also be set via `SENTINEL_ODOO_MASTER`.

> **Note on `--master-pw`:** this is the Odoo `admin_passwd` from `odoo.conf`, not the Odoo admin
> login password. The two are different credentials.

### Run the UI smoke crawl (Phase 3)

Logs into the Odoo web client with Playwright and opens each of the addon's views, capturing console
errors, uncaught JS, failed 4xx/5xx requests, Odoo error dialogs, and a screenshot per page. Read-only
(no records created), so no clone is needed:

```powershell
.\.venv\Scripts\sentinel.exe run-ui --module <module> --db <db> --max-pages 12
# watch it drive the browser:
.\.venv\Scripts\sentinel.exe run-ui --module <module> --db <db> --headed
```

Writes `output/ui-<module>-<timestamp>/`: `ui_results.md`, `ui_results.json`, and `screenshots/`. Each
page is **ok**, **issues** (console/JS/5xx/error-dialog), or **load_error**.

> The CLI commands are: `web` / `introspect` / `scan-addons` / `audit` / `run-tests` / `run-ui`.

---

## Test

```powershell
.\.venv\Scripts\python.exe -m pytest -q          # unit tests
```

The suite covers the deterministic Odoo layer: the System Map's model split and field counts, and
the compact System Map brief (`summarize_system_map`) that feeds the engine.

---

## Phase 2 — status

**Built:**
- Headless Claude Code engine (sync + streaming), Odoo-QA skill injection, subscription billing.
- `/api/chat[/stream]` real reasoning with per-module conversation continuity.
- `/api/investigate[/stream]` live-data record investigation with 2-hop data expansion:
  stock moves (product variant + `sale_line_id`) and invoice lines fetched for every picking/invoice
  in the record graph. Produces precise, actionable diagnoses with exact IDs, timestamps, user names.
- `/api/flow[/stream]` flow-explanation endpoint grounded in real records (General Question mode).
- Two-pass audit (`audit/runner.py`): pass 1 writes the human report; pass 2 extracts structured
  `Finding[]` + `TestPlan`. Exposed via `sentinel audit`, `/api/audit`, `/api/audit/stream`.
- Artifacts saved to `output/audit-<module>/`: `report.md`, `findings.json`, `test_plan.json`.
- Anti-hallucination rules in `skills/odoo-qa/SKILL.md`: auto-model-discovery protocol,
  hard rules against hedging or inventing data.
- `_source_dir()` accepts a single addon folder (has `__manifest__.py`) **or** an addons root
  folder (a child directory has `__manifest__.py`).

**Optional follow-ups (not blocking Phase 2):**
- Render the structured `TestPlan`/`findings.json` in the web UI (counts, filters) — backend already
  returns them.
- Deterministic Odoo lint pass (a focused Python AST / ruff check) as an extra grounding signal.

Requires: the Claude Code CLI signed in to the subscription. **No `ANTHROPIC_API_KEY`, no Postgres,
no pgvector.**

---

## Phase 3 — status

**Built:**
- DB provisioning (`execute/provision.py`): clone the source DB via Odoo's `db` service (master
  password) and drop it after; or `--use-existing-db` for a disposable DB. The source DB stays untouched.
- Executable-case generation (`execute/generate.py`): Claude Code emits op-sequences
  (create/search/call/write/assert) grounded in the real models, methods, and required fields.
- Deterministic XML-RPC executor (`execute/runner.py`): ref table, assertions, best-effort teardown;
  classifies each case **pass / fail / error**. Exposed via `sentinel run-tests`.
- Results report (`execute/report.py`): `results.md` + `results.json` + `cases.json`.
- **UI smoke crawl** (`execute/ui_playwright.py`, `sentinel run-ui`): Playwright logs into the web
  client and opens each window action, capturing console/JS/network errors, error dialogs, and
  screenshots; pages classified ok / issues / load_error → `ui_results.md` + `ui_results.json`.

**Still planned:**
1. **Docker sandbox** — fully isolated, disposable execution environment. Requires Docker.
2. **UI form/workflow driving** (beyond the smoke crawl) — create records via the UI and click
   workflow buttons. Fragile; the crawl already catches most frontend defects.

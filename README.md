# Sentinel — Agentic QA & Bug-Detection Agent for Odoo

**Version:** 0.2 (Claude Code architecture)  |  **Target:** Odoo 18 Enterprise
**Status:** Phases 1 & 2 built; Phase 3 RPC flow + Playwright UI executors built; live-data **record investigation** built; **auth + guided UI** built (Docker sandbox pending)

> **Guided mode-based UI:** instead of raw buttons, Sentinel opens with a five-option mode picker
> in chat. The user chooses what they want to do — Understand, find Logic/UI Gaps, scan Code Errors,
> get a Report, or ask a General Question — and Sentinel adapts its behaviour and routing accordingly.
>
> **Live-data record investigation:** with just a staging link (no source), describe a problem in
> plain language — *"S00437 shows 0 delivered despite two completed deliveries"* — and Sentinel reads
> that record's live data (state, stock moves with sale_line_id, invoice lines, field-change history,
> chatter) and produces a precise, actionable diagnosis with exact IDs, timestamps, and user names.
>
> **Whole-deployment view:** for a heavily-customised instance, the General Question mode and
> Scan Modules capability list every custom/non-standard module, summarise the customisations by
> area, and let you drill into any module in depth.

---

## What Sentinel does

Point it at an Odoo module (code + a running instance, or just a staging link). It **understands
what was developed and configured**, then **finds bugs and logic gaps** across backend and
frontend, and produces a **test plan + bug/gap report**. It works as an **interactive chat** and
as a one-shot **audit**.

Any staging link or local addon given at runtime is the target — there is no fixed default module.

---

## Architecture — Claude Code as the engine (not the raw API)

The "brain" is **Claude Code**, driven headlessly via the `claude -p` CLI (as a subprocess —
not the Agent SDK, whose control-protocol handshake hangs on this Windows setup), running on the
team's **Claude Code subscription** — not metered per-token API calls. We only build the parts
Claude Code can't know on its own: the **Odoo tools** and the **QA skill**.

```
┌───────────────────────────────────────────────────────────────┐
│ FRONTEND  (web UI — this repo, built)                          │
│   mode-picker chat · System Map dashboard · PDF report         │
│   auth overlay (login / first-run setup) · user management     │
└───────────────┬───────────────────────────────────────────────┘
                │  HTTP (FastAPI) + SSE
┌───────────────▼───────────────────────────────────────────────┐
│ BACKEND  (FastAPI — src/sentinel/web/app.py)                  │
│   /api/auth/*        → authentication (stdlib only, no deps)  │
│   /api/introspect    → deterministic Odoo tools (no LLM)      │
│   /api/chat[/stream] → REASONING via Claude Code              │
└───────┬───────────────────────────────────┬───────────────────┘
        │ deterministic tools               │ reasoning engine
┌───────▼────────────────────┐   ┌──────────▼────────────────────┐
│ Odoo tools (built)         │   │ Claude Code                    │
│  rpc · introspect · scan   │   │  reads code (Read/Grep/Glob) + │
│  investigate · deployment  │   │  reasons over System Map +     │
└────────────────────────────┘   │  guided by the Odoo-QA skill   │
                                  └────────────────────────────────┘
   SUBSCRIPTION billing (flat) ── no ANTHROPIC_API_KEY needed
```

**Why this beats the raw API** (the decision the team made):

| | Raw Claude API | **Claude Code (chosen)** |
|---|---|---|
| Billing | Metered per-token | **Flat subscription** you already pay for |
| Code reading, agent loop, retries, RAG | We build + maintain | **Built in — free** |
| Reading a real repo | chunk + embed | **Navigates like a developer** (open/grep/follow) |
| Frontend | custom | custom **or** reuse Claude Code's UIs |
| Unblocked today | needs API credits | **Yes — no key** |

---

## The 3-phase build plan

| Phase | Goal | Status |
|-------|------|--------|
| **Phase 1 — Understand + Frontend** | Odoo RPC tools (connect, introspect → System Map), addon source scan, **web UI** (chat + dashboard). The deterministic, no-LLM foundation. | ✅ **Built & running** |
| **Phase 2 — Reason via Claude Code** | `/api/chat` + `/api/audit` + `sentinel audit` driven by the **Claude Code engine** + the **Odoo-QA skill**: reads the code + System Map and produces gap analysis, bug findings, and the **test plan** — a two-pass run that emits a human report **and** structured `findings.json` + `test_plan.json`. | ✅ **Built** |
| **Phase 3 — Execute + Report** | RPC **flow executor** (`sentinel run-tests`) + **Playwright UI crawl** (`sentinel run-ui`) built. **Docker sandbox** still planned. | 🟡 Partial |

---

## What's built right now

```
src/sentinel/
  odoo/         rpc · introspect (System Map) · addon_scan · context · report
                investigate (2-hop live-data diagnosis) · deployment   (deterministic tools)
  engine/       claude_code (headless `claude -p`) · skill (Odoo-QA playbook)   (reasoning)
  audit/        runner (two-pass: report → structured findings) · models         (Phase 2 audit)
  execute/      generate · provision (clone) · runner (XML-RPC) · ui_playwright · report   (Phase 3)
  web/          app.py (FastAPI) · auth.py (stdlib auth, per-user sessions) · static/index.html
  core/         models.py — the Finding schema
  cli.py        sentinel web | introspect | scan-addons | audit | run-tests | run-ui
tests/unit/     Odoo-layer + audit-parser + executor tests
skills/odoo-qa/SKILL.md   the testing playbook (anti-hallucination rules + auto-discovery protocol)
```

Key capabilities in the current build:
- **Mode-picker chat UI** — five guided modes (Understand, Logic/UI Gaps, Code Errors, Report, General Question) with a ↺ Switch button; no raw action buttons
- **Authentication** — login page, first-run admin setup, per-user session isolation, admin user management
- **Understand mode** — type a module name to introspect it directly from chat; auto-triggers if module field is pre-filled
- **Logic/UI Gaps mode** — routes to live-data investigation (`/api/investigate/stream`)
- **Code Errors mode** — checks addon path, triggers full source audit
- **Report mode** — scope picker (whole chat / last conversation / new topic), auto-PDF on any "report on X" phrase
- **General Question mode** — routes to flow explanation (`/api/flow/stream`) grounded in real records
- **Deep investigation** — stock moves (product variant + `sale_line_id`) and invoice lines fetched automatically; precise timeline from chatter
- **Addons root support** — path can point to a single addon folder or a root folder containing multiple addons

---

## Run it

```powershell
# 1. Create virtual environment and install
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 2. Install and sign in to Claude Code (reasoning engine — one-time)
npm install -g @anthropic-ai/claude-code
claude          # sign in with the Claude subscription (NO ANTHROPIC_API_KEY needed)

# 3. Start Sentinel
.\.venv\Scripts\sentinel.exe web        # -> http://127.0.0.1:8800
```

Open `http://127.0.0.1:8800`. On first run, create an admin account. Fill in the connection
bar (Odoo URL, database, user, password, module name, and optionally the addon source path),
then select **Understand a module** from the mode picker.

Connection defaults can be set via environment variables:
```powershell
$env:SENTINEL_ODOO_URL      = "http://localhost:8069"
$env:SENTINEL_ODOO_DB       = "my_db"
$env:SENTINEL_ODOO_USER     = "admin"
$env:SENTINEL_MODULE        = "my_module"
$env:SENTINEL_ADDONS        = "C:\path\to\addons"
```

---

## Document index

| Document | Contents |
|----------|----------|
| [BUILD.md](BUILD.md) | Setup, run, test, and the per-phase build detail |
| [Sentinel_Requirement_Document.md](Sentinel_Requirement_Document.md) | Requirements (v2.0 — aligned to the Odoo + Claude Code design) |
| [Sentinel_Low_Level_Design.md](Sentinel_Low_Level_Design.md) | LLD (v2.0 — aligned to the Odoo + Claude Code design) |

## Tech stack

Python · FastAPI (backend + web UI) · **Claude Code** (headless `claude -p`, reasoning) ·
XML-RPC (Odoo) · stdlib-only auth (pbkdf2_hmac + HMAC-SHA256 tokens) · Playwright (Phase 3 UI crawl) ·
Docker sandbox (Phase 3, planned) — frontend is HTML/JS today, can move to React.

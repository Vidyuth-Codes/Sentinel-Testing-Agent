# Sentinel — Agentic QA & Bug-Detection Agent for Odoo

**Version:** 0.2 (Claude Code architecture)  |  **Target:** Odoo 18 Enterprise
**Status:** Phases 1 & 2 built; Phase 3 RPC flow + Playwright UI executors built; live-data **record diagnosis** built (Docker sandbox pending)

> **Functional/support mode:** with just a staging link (no source), an end user can ask plain-language
> questions ("why does S00437 still show 0 delivered?") and click **🔍 Diagnose** — Sentinel reads that
> record's live data (state, related deliveries/invoices, field-change history, chatter) and explains
> what happened and why, in plain terms. Read-only.
>
> **Flow explanations from real data:** ask "explain the flow of bills" (or sales orders, deliveries…)
> and the chat auto-grounds the walkthrough in **actual records** — how many are at each stage, named
> examples, and one record's full journey — falling back to an illustrative example only if none exist.
>
> **Whole-deployment view:** for a heavily-customised instance, **Scan Modules** lists every custom/
> non-standard module (detecting partner modules even when their author is faked as "Odoo S.A.", via
> the tell-tale 5-part version), summarises the customisations by area, and lets you click any module
> to Understand it in depth.

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
│   chat · System Map dashboard · report viewer · PDF download   │
└───────────────┬───────────────────────────────────────────────┘
                │  HTTP (FastAPI)
┌───────────────▼───────────────────────────────────────────────┐
│ BACKEND  (FastAPI — this repo)                                 │
│   /api/introspect → deterministic Odoo tools (no LLM)         │
│   /api/chat       → REASONING via Claude Code (subscription)  │
└───────┬───────────────────────────────────┬───────────────────┘
        │ deterministic tools               │ reasoning engine
┌───────▼────────────────────┐   ┌──────────▼────────────────────┐
│ Odoo tools (built)         │   │ Claude Code                    │
│  rpc · introspect · scan   │   │  reads code (Read/Grep/Bash) + │
│  later: flow + UI executors│   │  calls Odoo tools + reasons    │
└────────────────────────────┘   │  guided by the "Odoo-QA" skill │
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

We keep a custom **frontend** either way (see below) — Claude Code changes the *engine*,
not the UI.

---

## The 3-phase build plan (revised)

| Phase | Goal | Status |
|-------|------|--------|
| **Phase 1 — Understand + Frontend** | Odoo RPC tools (connect, introspect → System Map), addon source scan, **web UI** (chat + dashboard). The deterministic, no-LLM foundation. | ✅ **Built & running** |
| **Phase 2 — Reason via Claude Code** | `/api/chat` + `/api/audit` + `sentinel audit` driven by the **Claude Code engine** (headless `claude -p`) + the **Odoo-QA skill**: reads the code + System Map and produces gap analysis, bug findings, and the **test plan** — a two-pass run that emits a human report **and** structured `findings.json` + `test_plan.json`. | ✅ **Built** |
| **Phase 3 — Execute + Report** | RPC **flow executor** (`sentinel run-tests`): Claude Code generates executable op-sequences (create → call action → assert), run over XML-RPC against a **cloned DB** (or existing, opt-in). **Playwright UI crawl** (`sentinel run-ui`): logs into the web client and opens each view, capturing console/JS/network errors + screenshots. Both produce pass/fail reports. **Docker sandbox** still planned. | 🟡 Partial |

> The old API-centric Phase 2 (`sentinel/llm`, prompts, `.env` key) is **retired** — the
> reasoning moves to Claude Code. The Odoo introspection/scan already built is **reused** as
> Claude Code's tools.

---

## What's built right now

```
src/sentinel/
  odoo/         rpc · introspect (System Map) · addon_scan · context · report · investigate · deployment   (deterministic tools)
  engine/       claude_code (headless `claude -p`) · skill (Odoo-QA playbook)   (reasoning)
  audit/        runner (two-pass: report → structured findings) · models         (Phase 2 audit)
  execute/      generate · provision (clone) · runner (XML-RPC) · ui_playwright · report   (Phase 3)
  web/          app.py (FastAPI) + static/index.html (chat + dashboard + PDF report download)
  core/         models.py — the Finding schema the audit populates
  cli.py        sentinel web | introspect | scan-addons | audit | run-tests | run-ui
tests/unit/     Odoo-layer + audit-parser + executor tests
```

The web UI's **Understand** button calls the real Odoo introspection live; the **chat**
panel is wired to Claude Code (with a mock fallback so the UI is alive before Claude Code
is connected). The **Report** button streams a functional flow report and auto-downloads it
as a text-extractable PDF.

---

## Run it

```powershell
cd C:\Users\vidyu\Desktop\sentinel-testing-agent
.\.venv\Scripts\python.exe -m pip install -e .

# 1. start your Odoo instance (separate terminal, from the addon's project directory)

# 2. start Sentinel's web UI:
.\.venv\Scripts\sentinel.exe web        # -> http://127.0.0.1:8800
```

Open `http://127.0.0.1:8800`, fill in the connection fields (Odoo URL, database, user, password,
module name, and optionally the addon source path), then click **Understand** — the System Map
fills in from the live instance. Then chat with it, or run a one-shot audit.

To power the chat with real reasoning, install and sign in to the Claude Code CLI — the engine
auto-detects it (no flag needed) and `/api/config` then reports `engine: claude-code`:
```powershell
npm install -g @anthropic-ai/claude-code
claude          # sign in with the Claude subscription (NO ANTHROPIC_API_KEY needed)
```
Optional overrides: `SENTINEL_CLAUDE_PATH` (point at a specific `claude` binary) ·
`SENTINEL_FORCE_SUBSCRIPTION=0` (keep `ANTHROPIC_API_KEY` in the engine env instead of stripping it).

Connection defaults can be set via environment variables:
```powershell
$env:SENTINEL_ODOO_URL    = "http://localhost:8069"
$env:SENTINEL_ODOO_DB     = "my_db"
$env:SENTINEL_ODOO_USER   = "admin"
$env:SENTINEL_MODULE      = "my_module"
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
XML-RPC (Odoo) · later Playwright + Docker sandbox (Phase 3 execution) — frontend is HTML/JS
today, can move to React.

# Sentinel — Requirement Document

# Agentic QA & Bug-Detection Agent for Odoo

**Version:** 2.1 — auth + guided UI + deep investigation  |  **Date:** June 2026  |  **Classification:** Internal — R&D
**Status:** Active — aligned to the built system
**Prepared by:** Vidyuth  |  **Author:** Vidyuth

**Companion document:** [`Sentinel_Low_Level_Design.md`](Sentinel_Low_Level_Design.md)

> **Supersedes v1.0.** Version 1.0 described a generic, "point it at any frontend/backend
> project" agent orchestrated with LangGraph, a pgvector RAG index, and metered Claude API
> calls. That design is **retired**. Sentinel is now an **Odoo 18 Enterprise** QA agent whose
> reasoning runs on **Claude Code** (flat subscription), not the raw API. This document is the
> *what* and *why*; the *how* lives in the Low-Level Design.

---

## 1. Purpose

Build a **conversational QA & bug-detection agent** ("Sentinel") that is pointed at an **Odoo 18
Enterprise module** — its **addon source code** plus a **running Odoo instance** — and:

1. **Understands** what the module developed and configured (models, fields, views, security,
   crons, automations) by introspecting the live instance and scanning the source;
2. **Finds bugs and logic gaps** across the **backend (Python)** and **frontend (OWL/JS/XML)**;
3. Produces a **test plan** and a **bug/gap report** a developer can act on immediately.
4. Supports **multi-user access** via an authentication system so multiple team members can use
   the same Sentinel instance with isolated sessions.

It works as an **interactive chat** and as a **one-shot audit**.

The target is whatever Odoo module (local addon or staging instance) the user points Sentinel at
at runtime — there is no fixed or default module.

---

## 2. Background & Motivation

Odoo modules — especially custom addons built rapidly — accumulate defects that no single tool
catches: linters miss Odoo-specific logic bugs (wrong `@api.depends`, constraints that don't
block the bad case, illegal `state` transitions); unit tests are rarely written for custom
addons; and manual QA of 60+ models is slow and inconsistent. A single agent that **reads the
code like a developer**, **knows the live system's shape**, and **reasons over both** closes that
gap.

**Design stance.** The most reliable systems are *neuro-symbolic*: deterministic tooling
(live RPC introspection and an AST scan of the addon source) runs first and **grounds** the LLM,
which then reasons with full context. Every finding is tied to a concrete `file:line` or `model.method`, and
a human stays in the loop. Sentinel adopts this stance.

**Why Claude Code as the engine.** The "brain" is **Claude Code** (driven headlessly via the
`claude -p` CLI), running on the team's **Claude Code subscription** — not metered per-token API
calls. Claude Code already provides the agent loop, retries, and — crucially — the ability to
**read a real repo like a developer** (open/grep/follow references) instead of chunk-and-embed
RAG. We build only the parts Claude Code can't know on its own: the **Odoo tools** and the
**Odoo-QA skill** (the testing playbook).

| | Raw Claude API (retired) | **Claude Code (chosen)** |
|---|---|---|
| Billing | Metered per-token | **Flat subscription** already paid for |
| Code reading, agent loop, retries | We build + maintain | **Built in** |
| Reading a real addon | chunk + embed (pgvector) | **Navigates like a developer** (Read/Grep/Glob) |
| Unblocked today | needs API credits | **Yes — no API key needed** |

---

## 3. Goals & Non-Goals

### 3.1 Goals
- **G1 — Understand** an Odoo module from a live instance (System Map) + source scan, with no LLM.
- **G2 — Detect defects** across **both** backend (Python/ORM) and frontend (OWL/JS/XML), plus
  integration/contract and security/access issues.
- **G3 — Ground every finding** in a concrete location (`models/asset.py:123` or
  `<module>.<model>.action_confirm`) with the offending snippet — no vague claims.
- **G4 — Two modes:** interactive **chat** (guided via mode picker) and a **one-shot audit** that
  emits a test plan + report.
- **G5 — Low false positives** through deterministic grounding + "report only what you can point to."
- **G6 — Flat-cost reasoning** on the Claude Code subscription (no `ANTHROPIC_API_KEY` required).
- **G7 — Read-only by default:** Sentinel inspects the addon and the instance but never modifies them.
- **G8 — Multi-user access** via authentication (login, per-user session isolation, admin user management).

### 3.2 Non-Goals (current scope)
- **NG1** — Sentinel does **not** auto-fix, auto-commit, or modify the addon. It suggests; humans apply.
- **NG2** — Not a replacement for a full human security audit / pen-test.
- **NG3** — No load/stress testing at production scale (lightweight perf smells only).
- **NG4** — **Odoo only.** Generic "any project" auditing is out of scope; the old generic
  static-analysis path has been removed (see LLD §9).
- **NG5** — Does not run write/flow execution against the **production** database; Phase 3 executes
  only against a **duplicate DB** in a sandbox.

---

## 4. Personas

| Persona | Need | How they use Sentinel |
|---------|------|------------------------|
| **Odoo developer** | Catch ORM/logic bugs before deploying a custom addon | Understand the module, chat to drill into a model, run a full audit on the branch |
| **Tech lead / reviewer** | Find issues a PR review misses | Audit a module; chat about `@api.depends`/state-machine hotspots |
| **QA / functional consultant** | Reproducible defect list + a concrete test plan with steps | One-shot audit → test plan (rpc/ui cases) + bug report |
| **Project owner** | "Is this module sound? Where are the risks?" | Chat: "is the asset disposal flow safe?", reads the summary |

---

## 5. Scope

### 5.1 In scope
- **Inputs:** a path to the **addon source** (a folder with `__manifest__.py` directly, or an
  addons root folder whose child directories contain `__manifest__.py`) **and/or** connection
  details for a **running Odoo 18 instance** (URL, db, user, password/API key) plus the addon's
  **technical name**. Source path is optional — with only a staging link, Sentinel works in
  UI-flow and live-data mode.
- **Understand layer (deterministic, no LLM):** XML-RPC introspection of the live instance into a
  **System Map**; AST scan of the addon source; an "understanding report."
- **Reasoning layer (Claude Code):** requirement/gap analysis, backend + frontend bug findings,
  and test-plan generation — guided by the **Odoo-QA skill**.
- **Execution layer (Phase 3, planned):** RPC **flow executor** and **Playwright UI executor** that
  run real flows against a **duplicate database** in a sandbox, producing a Test Plan + Results doc.
- **Outputs:** a live **chat** answer, a **Markdown** test plan + bug/gap report, structured
  **JSON**, and (Phase 3) screenshots / run artifacts.

### 5.2 Out of scope — see Non-Goals
Non-Odoo projects, production-DB write execution, automatic code fixes, mobile-native apps,
formal compliance certification.

---

## 6. Functional Requirements

> IDs are stable references used throughout the LLD acceptance criteria. Requirements are grouped
> by the three build phases (see §12).

### 6.1 Understand — introspection & source scan (Phase 1, built)
- **FR-01** — Connect to a live Odoo 18 instance over **XML-RPC** (authenticate; read-only) and
  report the server version.
- **FR-02** — Build a **System Map** of the target module: which **models** it *created* vs
  *extended*, **fields** (type, required, relation, compute, store), **views**, **actions**,
  **menus**, **access rules** (`ir.model.access`), **record rules**, **scheduled actions (crons)**,
  **automations**, and **sequences** — attributing each to the addon via `ir.model.data`.
- **FR-03** — **Statically scan** the addon source on disk (`__manifest__.py`, models, fields,
  decorators, methods) and **cross-check** it against the live System Map.
- **FR-04** — Render an **understanding report** (Markdown + JSON) summarising the module's
  functional capabilities — what it does for users, key capabilities, and who uses it — rather than
  structural new/extended counts.

### 6.2 Authentication & multi-user access (Phase 1, built)
- **FR-25** — Provide a **login page** so access requires authentication; unauthenticated requests
  are rejected.
- **FR-26** — On **first run** (no users exist), present a setup screen to create an admin account.
- **FR-27** — Store user credentials securely: passwords hashed with **pbkdf2_hmac** (HMAC-SHA256
  tokens for sessions); no new dependencies (stdlib only).
- **FR-28** — Maintain **per-user session isolation**: conversation history, module context, and
  audit state are keyed by `(username, module)` so concurrent users don't interfere.
- **FR-29** — Admin users can **add and delete non-admin accounts** from the UI.

### 6.3 Reason — Claude Code engine (Phase 2, built)
- **FR-05** — Drive **Claude Code** headlessly (`claude -p`) with **read-only tools**
  (`Read`, `Grep`, `Glob`) so it inspects the addon but never edits it.
- **FR-06** — Inject the **Odoo-QA skill** (the testing playbook) and the **System Map summary** as
  the system prompt so Claude Code orients before reading source.
- **FR-07** — Bill reasoning to the **Claude Code subscription**: when no `ANTHROPIC_API_KEY` is
  present the run uses the signed-in subscription, not metered API.
- **FR-08** — **Find backend defects** Odoo-specifically: wrong/incomplete `@api.depends`,
  ineffective `@api.constrains`, unguarded/illegal `state` transitions in `action_*`/`button_*`
  methods, mutable default args, `sudo()` that bypasses access control, SQL string-formatting /
  `eval`/`exec` on input, N+1 queries / `search` in loops, `create`/`write` overrides that break
  `super()` / `@api.model_create_multi`.
- **FR-09** — **Find frontend defects** (OWL/JS/XML): views referencing fields/methods that don't
  exist on the model, broken `domain`/`attrs`, buttons calling absent methods, JS reading fields the
  view doesn't load, unhandled promise rejections.
- **FR-10** — **Find integration/contract & security issues:** a view/JS expecting a field or method
  the backend doesn't define (or vice-versa); `ir.model.access` / record rules that grant
  write/unlink too broadly or fail to isolate companies/users.
- **FR-11** — **Ground every finding** in a concrete location (`file.py:line` or `model.method`)
  with the offending snippet; prefer **fewer real findings** over many speculative ones. The skill
  enforces **auto-model-discovery**: before stating anything about a model or field, read its source
  file; never invent field names or state values.
- **FR-12** — Generate a **test plan**: (1) a **requirement-coverage** table (intended behaviours →
  covered / partial / gap) and (2) concrete **test cases**, each with id, title, type
  (functional/workflow/ui/security/validation), **channel** (`rpc` or `ui`), priority,
  preconditions, numbered steps, and expected result — favouring the module's real `action_*`
  methods and state transitions.
- **FR-30** — **Live-data investigation** (`/api/investigate/stream`): given a plain-language
  problem description (e.g. "S00437 shows 0 delivered despite two completed deliveries"), fetch
  the matching record and expand it **2 hops**: stock pickings → stock moves (product variant +
  `sale_line_id`); invoices → invoice lines. Return a precise, actionable diagnosis with exact
  record IDs, user names, and UTC timestamps.
- **FR-31** — **Flow explanation** (`/api/flow/stream`): given a general question about the Odoo
  project (e.g. "explain the purchase order flow"), ground the answer in real records and module
  source where available.

### 6.4 Execute — run real flows (Phase 3, planned)
- **FR-13** — Provision a **duplicate database** (copy of the target DB) so execution never touches
  production data.
- **FR-14** — **RPC flow executor:** run data/logic test cases over XML-RPC (create records, call
  `action_*` methods, assert resulting `state`/field values) against the duplicate DB.
- **FR-15** — **Playwright UI executor:** drive the Odoo web client for `ui` test cases — navigate
  menus/views, submit forms, click workflow buttons — capturing console errors, failed network
  requests (4xx/5xx), uncaught exceptions, and screenshots.
- **FR-16** — Run the target instance and its duplicate DB inside a **Docker sandbox** with
  resource caps, so execution is isolated and reproducible.
- **FR-17** — Produce a full **Test Plan + Results** document: each case marked pass/fail with
  evidence, plus the bugs surfaced during execution.

### 6.5 Report & interaction modes
- **FR-18** — Assign each finding a **category**, **layer** (backend/frontend/integration),
  **severity** (critical/high/medium/low/info), and **confidence** (0–1).
- **FR-19** — Produce a **report** with: summary, severity rollup, per-finding detail (title,
  description, location, evidence snippet, impact, suggested fix), and the **test plan**.
- **FR-20** — Export reports as **Markdown** and **JSON**; persist artifacts under `output/<run>/`.
- **FR-21** — **One-shot audit mode:** a single call runs the full understand→reason flow and
  returns a test plan + bug/gap report.
- **FR-22** — **Guided chat mode:** the user selects one of five modes via a **chat mode picker**:
  Understand, Logic/UI Gaps, Code Errors, Report, General Question. Each mode routes to the
  appropriate backend endpoint. A **↺ Switch** button re-presents the mode picker without wiping
  conversation history.
- **FR-23** — Within a mode, behaviour is consistent: Understand introspects; Logic/UI Gaps runs
  live investigation; Code Errors triggers a source audit; Report opens a scope picker then
  generates a PDF; General Question routes to flow explanation. All modes **stream progress** in
  real time (text deltas + tool activity) over Server-Sent Events.
- **FR-24** — Persist every run (inputs, System Map summary, findings, test plan, cost) under
  `output/<run>/`.
- **FR-32** — **Report scope picker:** before generating a report, Sentinel asks the user whether
  to cover the whole chat, the last conversation, or a new custom topic. The resulting report is
  printable as a text-extractable PDF via the browser print window.
- **FR-33** — **Report phrase auto-detection:** if the user types a phrase such as "give me a
  report on X" in Report mode, Sentinel automatically generates a report on that topic without
  requiring separate button presses.
- **FR-34** — **Stop/cancel:** the user can cancel any running stream at any time.

---

## 7. Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| **NFR-01 — Read-only safety** | Understand + Reason are strictly read-only: RPC introspection issues no writes; Claude Code is restricted to `Read`/`Grep`/`Glob`. The addon source and live instance are never modified. |
| **NFR-02 — Execution isolation (Phase 3)** | Flow/UI execution runs only against a **duplicate DB** inside a **Docker sandbox** with CPU/memory/time caps — never the production database. |
| **NFR-03 — Flat-cost reasoning** | Reasoning runs on the Claude Code **subscription**; `ANTHROPIC_API_KEY` is removed from the engine's environment so runs are billed to the subscription, not metered API. No per-token budget to manage. |
| **NFR-04 — Accuracy** | Findings are grounded in real `file:line`/`model.method` evidence; the agent reports only defects it can point to. Target: high precision over recall (few false positives). |
| **NFR-05 — Transparency** | Every finding cites evidence. The agent must read source files before making claims; if source is unavailable it states that explicitly rather than inventing data. |
| **NFR-06 — Performance** | Deterministic introspection of a large module (~65 models) completes in seconds; chat responses begin streaming within a few seconds; a full audit completes within the engine timeout (default 600s sync / 1200s streamed). |
| **NFR-07 — Resilience / graceful degradation** | A missing piece degrades gracefully: no Claude Code CLI → the UI falls back to a **mock** engine; an introspection/auth error returns a clear message rather than crashing; an engine timeout ends the run with an error event and no orphaned process. |
| **NFR-08 — Extensibility** | New Odoo introspection facets, new static runners, and (Phase 3) new executors can be added without rewriting the engine. The Odoo-QA **skill** is editable Markdown — the testing playbook changes without code changes. |
| **NFR-09 — Portability** | Runs on the developer's Windows workstation; the engine resolves the native `claude.exe` and keeps the command line under the Windows length limit. |
| **NFR-10 — Authentication security** | Passwords stored as pbkdf2_hmac hashes; sessions authenticated via HMAC-SHA256 tokens; no plaintext credentials in storage or logs. Zero new dependencies (stdlib only). |

---

## 8. Defect Taxonomy (what Sentinel looks for)

The detection↔reporting contract. Each finding maps to one **primary** category and one **layer**,
plus a severity and a confidence.

| Category | Layer(s) | Odoo examples |
|----------|----------|---------------|
| **Functional bug** | BE / FE | Action produces wrong result, record not created/updated, button does nothing |
| **Logic error / gap** | BE | Wrong/incomplete `@api.depends`; compute reads a field not in depends; ineffective `@api.constrains`; illegal/unguarded `state` transition; mutable default arg; `== None`/`== False` vs `is` |
| **Integration / contract** | BE↔FE | View/JS references a field or method the model doesn't define (or vice-versa); broken `domain`/`attrs` |
| **Security / access** | BE / FE | `sudo()` bypassing access control; SQL string-formatting injection; `eval`/`exec` on input; `ir.model.access`/record rules too broad or not isolating companies/users |
| **UI** | FE (OWL/JS/XML) | Button calls a method absent from the model; JS reads a field the view doesn't load; unhandled promise rejection; console errors |
| **Performance** | BE | N+1 queries / `search` inside a loop; unbounded `search()` |
| **Code quality** | BE / FE | Bare `except:`, `except: pass`, dead code, lint violations |

---

## 9. Severity & Confidence Model

**Severity** (impact if real):
- **Critical** — data loss/corruption, security/access breach, core workflow unusable.
- **High** — core feature broken or wrong; access weakness with a realistic exploit.
- **Medium** — non-core defect with a workaround; noticeable logic/UX issue.
- **Low** — minor cosmetic, edge-case, or quality issue.
- **Info** — observation / suggestion, not a defect.

**Confidence** (likelihood it's a true positive), 0.0–1.0:
- Deterministic tool hit or (Phase 3) reproduced at runtime → high (≥ 0.8).
- Claude Code reasoning grounded in a cited snippet → moderate-to-high based on how directly the
  evidence proves the defect.
- The agent is instructed to **lower confidence when unsure and not invent issues**.

---

## 10. High-Level User Flows

**A. Guided chat (mode picker)**
```
User → open Sentinel (login required)
  → mode picker appears in chat
  → User selects a mode:
      Understand        → introspect live instance + scan source → System Map + functional overview
      Logic / UI Gaps   → describe a record problem → live-data investigation with 2-hop expansion
      Code Errors       → addon path validated → full source audit via Claude Code
      Report            → scope picker → generate PDF-printable report
      General Question  → flow explanation grounded in real records
  → User presses ↺ Switch at any time to return to mode picker without losing chat history
```

**B. Understand — type module in chat**
```
User → selects "Understand a module" mode
  → if module name is pre-filled → Sentinel introspects immediately
  → if user types a module name in chat → Sentinel sets module field + introspects
  → System Map fills in; functional overview streamed to chat
```

**C. One-shot audit**
```
User → connects (URL, db, user, password, module name, optional addon path)
  → Code Errors mode → triggers full audit
  → Sentinel: System Map context + Odoo-QA skill → Claude Code reads the addon
  → returns a test plan (requirement coverage + rpc/ui cases) and a bug/gap report
  → saved to output/audit-<module>/
```

**D. Execute (Phase 3)**
```
Test plan → provision duplicate DB in a Docker sandbox
  → RPC flow executor runs data/logic cases; Playwright executor runs ui cases
  → Test Plan + Results doc: pass/fail per case + bugs, with screenshots/logs
```

---

## 11. Acceptance Criteria

- **AC-1 (Understand)** — Given a module name + a live instance, Sentinel builds a System Map and
  renders a functional understanding report describing what the module does for users — with **no
  LLM** involved (FR-01…FR-04).
- **AC-2 (Auth)** — First run prompts for admin setup; subsequent visits require login; sessions are
  isolated per user; admin can add/delete accounts (FR-25…FR-29, NFR-10).
- **AC-3 (Reason)** — A one-shot audit returns a test plan (requirement-coverage table + concrete
  rpc/ui cases) **and** a bug/gap report whose findings each cite a real `file:line`/`model.method`
  with the offending snippet (FR-05…FR-12, FR-18…FR-21).
- **AC-4 (Subscription)** — Reasoning runs with **no `ANTHROPIC_API_KEY`** present and is billed to
  the Claude Code subscription; cost is reported back (FR-07, NFR-03).
- **AC-5 (Chat)** — The mode picker presents 5 modes; each mode routes correctly; ↺ Switch
  returns to the picker without wiping history; streams text + tool activity (FR-22, FR-23, NFR-06).
- **AC-6 (Investigation)** — Given a record identifier in Logic/UI Gaps mode, Sentinel fetches the
  record, its stock moves (with `sale_line_id`), and its invoice lines, producing a diagnosis with
  exact IDs and timestamps (FR-30, NFR-04).
- **AC-7 (Report)** — Report mode presents a scope picker; generates a PDF-printable Markdown
  report; detects "report on X" phrases and triggers automatically (FR-32, FR-33).
- **AC-8 (Graceful degradation)** — With no Claude Code CLI installed, the UI still runs and the
  chat falls back to the mock engine with a clear message; an introspection/auth failure surfaces a
  clear error instead of crashing (NFR-07).
- **AC-9 (Execute — Phase 3)** — Test cases run against a **duplicate DB** in a sandbox; the
  production database is never written to; results are reported pass/fail with evidence
  (FR-13…FR-17, NFR-02).

---

## 12. Phasing

| Phase | Goal | Status |
|-------|------|--------|
| **Phase 1 — Understand + Frontend** | Odoo RPC tools (connect, introspect → System Map), addon source scan, web UI (mode-picker chat + dashboard), auth system (login, per-user sessions, admin user management). The deterministic, no-LLM foundation. | ✅ Built & running |
| **Phase 2 — Reason via Claude Code** | `/api/chat` + `/api/audit` + `/api/investigate` + `/api/flow` driven by the Claude Code engine + the Odoo-QA skill: reads the code + System Map and produces gap analysis, bug findings, and the test plan; 2-hop deep investigation; anti-hallucination rules in skill. | ✅ Built |
| **Phase 3 — Execute + Report** | RPC **flow executor** (`sentinel run-tests`) + **Playwright UI smoke crawl** (`sentinel run-ui`) built: executable op-sequences over XML-RPC against a **cloned DB**, plus a web-client crawl capturing console/JS/network errors + screenshots; both emit reports. **Docker sandbox** still planned. | 🟡 Partial |

---

## 13. Open Questions / Decisions for LLD

| # | Question | Current assumption (refine in LLD) |
|---|----------|-----------------------------------|
| Q1 | How is the duplicate DB provisioned in Phase 3? | Copy the target DB inside the Docker sandbox; tear down after the run |
| Q2 | Drive Playwright directly vs via an MCP server? | Direct Playwright wrapper, exposed as a tool Claude Code drives |
| Q3 | Where does requirement text come from for coverage analysis? | Inferred from the System Map + addon by Claude Code; optional requirement docs as input later |
| Q4 | When to move the HTML/JS UI to React? | After Phase 2 stabilises; the current HTML/JS SPA is sufficient until then |
| Q5 | How are findings persisted long-term? | Phase 1/2: JSON + Markdown under `output/<run>/`; a database is deferred |

---

## 14. Glossary

| Term | Meaning |
|------|---------|
| **Addon / module** | An Odoo package (folder with `__manifest__.py`) — the unit Sentinel tests. |
| **System Map** | Sentinel's model of "what the addon built": models, fields, views, security, crons, automations, derived from live RPC introspection. |
| **Introspection** | Reading the live Odoo instance's metadata over XML-RPC (no LLM). |
| **Claude Code** | Anthropic's coding agent, driven headlessly here; the reasoning engine, billed to a subscription. |
| **Odoo-QA skill** | The Markdown playbook (`skills/odoo-qa/SKILL.md`) telling Claude Code how to test an Odoo module. |
| **Finding** | A single normalised defect/observation with category, layer, severity, confidence, location, evidence. |
| **Record rule / access rule** | Odoo row-level (`ir.rule`) and model-level (`ir.model.access`) security definitions. |
| **Duplicate DB** | A throwaway copy of the target database used for Phase 3 execution so production is never touched. |
| **Neuro-symbolic** | Combining deterministic tools (introspection, linters) with LLM reasoning. |
| **Mode picker** | The chat card shown at startup (and on ↺ Switch) listing the five interaction modes. |
| **2-hop expansion** | Fetching stock moves from pickings and invoice lines from invoices, giving the investigation agent full product/quantity/linkage data. |

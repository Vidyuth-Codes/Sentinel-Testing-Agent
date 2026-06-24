"""FastAPI app powering the Sentinel UI."""

from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path

import json as _json

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from sentinel import __version__
from sentinel.audit import run_full_audit, structure_report
from sentinel.audit.runner import _source_dir, report_prompt
from sentinel.engine import ClaudeCodeEngine, build_system_prompt
from sentinel.engine.claude_code import EngineUnavailable
from sentinel.odoo import OdooRPCClient, build_system_map, scan_addon
from sentinel.odoo.context import summarize_system_map
from sentinel.odoo.deployment import render_deployment, scan_deployment
from sentinel.odoo.investigate import (
    build_flow_system, build_investigation_system, extract_references, fetch_flow_examples,
    fetch_record_graph, narrow_by_question, render_flow_examples, render_graph, resolve_flow,
    resolve_record,
)
from sentinel.odoo.report import render_system_map_markdown
from sentinel.odoo.rpc import OdooAuthError, OdooRPCError
from sentinel.web import auth

app = FastAPI(title="Sentinel — Odoo Testing Agent", version=__version__)

_STATIC = Path(__file__).parent / "static"
_ENGINE = ClaudeCodeEngine()

# Per-user, per-module session state — keyed by (username, module) or (username, db).
_SUMMARY: dict[tuple[str, str], str] = {}
_SESSION: dict[tuple[str, str], str] = {}
_DEPLOY: dict[tuple[str, str], str] = {}

_DEFAULTS = {
    "url":      os.environ.get("SENTINEL_ODOO_URL", "http://localhost:8069"),
    "db":       os.environ.get("SENTINEL_ODOO_DB", ""),
    "user":     os.environ.get("SENTINEL_ODOO_USER", "admin"),
    "password": os.environ.get("SENTINEL_ODOO_PASSWORD", ""),
    "module":   os.environ.get("SENTINEL_MODULE", ""),
    "addons":   os.environ.get("SENTINEL_ADDONS", None),
}


# --- screenshot helper --------------------------------------------------------

_SCREENSHOTS_DIR = Path("output") / "screenshots"


def _save_screenshot(image_b64: str) -> str | None:
    """Decode a data-URL or raw base64 image, save to disk, return the absolute path."""
    try:
        # strip the data-URL prefix if present: "data:image/png;base64,..."
        raw = image_b64.split(",", 1)[-1] if "," in image_b64 else image_b64
        data = base64.b64decode(raw)
        _SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        ext = "png"
        if image_b64.startswith("data:image/"):
            mime_part = image_b64.split(";")[0].split("/")[-1]
            ext = mime_part if mime_part in ("png", "jpg", "jpeg", "gif", "webp") else "png"
        path = _SCREENSHOTS_DIR / f"screenshot_{uuid.uuid4().hex[:10]}.{ext}"
        path.write_bytes(data)
        return str(path.resolve())
    except Exception:  # noqa: BLE001
        return None


def _with_screenshot(question: str, image_b64: str | None) -> tuple[str, list[str]]:
    """If image provided, save it and return augmented prompt + extra_dirs list."""
    if not image_b64:
        return question, []
    img_path = _save_screenshot(image_b64)
    if not img_path:
        return question, []
    note = (
        f"[SCREENSHOT ATTACHED: The user has pasted a screenshot. "
        f"It is saved at: {img_path} — use the Read tool to read it first "
        f"and understand what is shown before answering.]\n\n"
    )
    return note + question, [str(_SCREENSHOTS_DIR.resolve())]


# --- auth dependency ----------------------------------------------------------

def get_current_user(authorization: str = Header(default="")) -> dict:
    token = authorization.removeprefix("Bearer ").strip()
    user = auth.verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token. Please log in again.")
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


# --- public routes (no auth) --------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (_STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/auth/status")
def auth_status() -> dict:
    """Returns whether any users exist — used by the UI to show login vs first-run setup."""
    return {"has_users": auth.users_exist()}


class LoginReq(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(req: LoginReq) -> dict:
    user = auth.authenticate(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    token = auth.create_token(user["username"], user["role"])
    return {"token": token, "username": user["username"], "role": user["role"]}


@app.post("/api/auth/setup")
def setup(req: LoginReq) -> dict:
    """Create the first admin account. Only callable when no users exist yet."""
    if auth.users_exist():
        raise HTTPException(status_code=403, detail="Setup already complete. Please log in.")
    auth.create_user(req.username, req.password, role="admin")
    token = auth.create_token(req.username, "admin")
    return {"token": token, "username": req.username, "role": "admin"}


# --- auth-protected user management (admin only) ------------------------------

@app.get("/api/auth/users")
def get_users(user: dict = Depends(require_admin)) -> dict:
    return {"users": auth.list_users()}


class CreateUserReq(BaseModel):
    username: str
    password: str
    role: str = "user"


@app.post("/api/auth/users")
def create_user(req: CreateUserReq, user: dict = Depends(require_admin)) -> dict:
    if not auth.create_user(req.username, req.password, req.role):
        raise HTTPException(status_code=400, detail="Username already exists.")
    return {"ok": True}


@app.delete("/api/auth/users/{username}")
def delete_user(username: str, user: dict = Depends(require_admin)) -> dict:
    if username == user["username"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")
    if not auth.delete_user(username):
        raise HTTPException(status_code=404, detail="User not found.")
    return {"ok": True}


# --- config (auth-protected) --------------------------------------------------

@app.get("/api/config")
def config(user: dict = Depends(get_current_user)) -> dict:
    return {
        "version": __version__,
        "defaults": _DEFAULTS,
        "engine": "claude-code" if _ENGINE.available() else "mock",
        "cli_path": _ENGINE.cli_path,
        "username": user["username"],
        "role": user["role"],
    }


# --- introspection (deterministic, no LLM) ------------------------------------

class IntrospectReq(BaseModel):
    url: str = _DEFAULTS["url"]
    db: str = _DEFAULTS["db"]
    user: str = _DEFAULTS["user"]
    password: str = _DEFAULTS["password"]
    module: str = _DEFAULTS["module"]
    addons: str | None = _DEFAULTS["addons"]
    verify_ssl: bool = True


def _db_hint(url: str, verify_ssl: bool) -> str:
    try:
        from sentinel.odoo.rpc import OdooDbAdmin
        dbs = OdooDbAdmin(url, "", verify_ssl=verify_ssl).list()
        if dbs:
            return "  → Available databases on this server: " + ", ".join(dbs)
    except Exception:
        pass
    return ("  → The database name looks wrong (it's usually NOT the URL subdomain). Find it in "
            "Odoo: Settings → activate developer mode (shows the db), or on the Odoo.sh branch page.")


def _module_check(client: OdooRPCClient, module: str, counts: dict) -> tuple[str | None, list[str]]:
    if counts.get("new_models") or counts.get("extended_models"):
        return None, []
    try:
        found = client.search_read("ir.module.module", [["name", "=", module]], ["name"], limit=1)
        if found:
            return (f"Module '{module}' is installed but defines no models/views of its own in this "
                    "instance — there's nothing to map (some apps only add data, not schema)."), []
        like = client.search_read(
            "ir.module.module",
            [["state", "=", "installed"], "|", ["name", "ilike", module], ["shortdesc", "ilike", module]],
            ["name", "shortdesc"], limit=10,
        )
        sugg = [f"{m['name']} — {m.get('shortdesc') or ''}".strip() for m in like]
        return (f"No installed module has the technical name '{module}'. That looks like the app's "
                "display name — use the lowercase technical name instead (Settings → Apps in developer mode)."), sugg
    except OdooRPCError:
        return (f"'{module}' produced an empty System Map — double-check the technical module name."), []


@app.post("/api/introspect")
def introspect(req: IntrospectReq, current_user: dict = Depends(get_current_user)) -> dict:
    client = OdooRPCClient(req.url, req.db, req.user, req.password, verify_ssl=req.verify_ssl)
    try:
        version = client.version().get("server_version")
        client.authenticate()
        smap = build_system_map(client, req.module)
    except OdooAuthError as exc:
        return {"ok": False, "error": str(exc)}
    except OdooRPCError as exc:
        msg = str(exc)
        if "does not exist" in msg.lower() or "database" in msg.lower():
            msg += _db_hint(req.url, req.verify_ssl)
        return {"ok": False, "error": msg}

    scan = None
    if req.addons:
        addon_path = Path(req.addons)
        if (addon_path / "__manifest__.py").exists():
            scan = scan_addon(req.addons)
        else:
            # Addons root — find the subfolder matching the module name
            candidate = addon_path / req.module
            if (candidate / "__manifest__.py").exists():
                scan = scan_addon(str(candidate))

    counts = smap.counts()
    warning, suggestions = _module_check(client, req.module, counts)
    key = (current_user["username"], req.module)
    _SUMMARY[key] = summarize_system_map(smap, scan)
    _SESSION.pop(key, None)  # fresh understanding → fresh conversation

    return {
        "ok": True,
        "server_version": version,
        "uid": client.uid,
        "counts": counts,
        "warning": warning,
        "suggestions": suggestions,
        "markdown": render_system_map_markdown(smap, scan),
        "models": [
            {"model": m.model, "name": m.name, "fields": m.n_fields, "new": m.owned_by_addon}
            for m in sorted(smap.models, key=lambda x: (not x.owned_by_addon, x.model))
        ],
    }


# --- deployment scan ----------------------------------------------------------

class DeploymentReq(BaseModel):
    url: str = _DEFAULTS["url"]
    db: str = _DEFAULTS["db"]
    user: str = _DEFAULTS["user"]
    password: str = _DEFAULTS["password"]
    verify_ssl: bool = True


@app.post("/api/deployment")
def deployment(req: DeploymentReq, current_user: dict = Depends(get_current_user)) -> dict:
    client = OdooRPCClient(req.url, req.db, req.user, req.password, verify_ssl=req.verify_ssl)
    try:
        client.authenticate()
        scan = scan_deployment(client)
    except (OdooAuthError, OdooRPCError) as exc:
        return {"ok": False, "error": str(exc)}
    key = (current_user["username"], req.db)
    _DEPLOY[key] = render_deployment(scan)
    return {
        "ok": True,
        "total": scan["total"],
        "custom_count": len(scan["custom"]),
        "standard_count": scan["standard_count"],
        "modules": scan["custom"],
    }


@app.post("/api/deployment/overview")
def deployment_overview(req: DeploymentReq, current_user: dict = Depends(get_current_user)) -> dict:
    rendered = _DEPLOY.get((current_user["username"], req.db))
    if not rendered:
        return {"ok": False, "error": "Run the deployment scan first."}
    if not _ENGINE.available():
        return {"ok": False, "engine": "mock", "error": "Claude Code not installed."}
    system = (
        "You are a product analyst. Given the list of an Odoo instance's CUSTOM (client-developed) "
        "modules below, write a short overview of what has been built/customised for this client. "
        "Group the modules by business area (Sales, Accounting, Inventory/Logistics, Manufacturing, "
        "HR, Reporting, etc.), and under each give a one-line plain-language description of what those "
        "customisations do. End with a one-line note on where the heaviest customisation is. Do NOT "
        "analyse code. Markdown, concise.\n\n# CUSTOM MODULES\n" + rendered
    )
    try:
        result = _ENGINE.run_sync("Summarise what has been custom-built for this client.",
                                  system_prompt=system, max_turns=2, timeout=240)
    except EngineUnavailable as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "engine": "claude-code", "markdown": result.text, "cost_usd": result.cost_usd}


# --- app overview -------------------------------------------------------------

_OVERVIEW_SYSTEM = (
    "You are a functional consultant describing what an Odoo module DOES for its users on THIS specific "
    "instance. Base EVERYTHING only on the System Map below (live introspection). Do NOT describe what "
    "the standard Odoo app of the same name does in general — describe what THIS module enables users "
    "to do on this instance. Focus on business capabilities, user workflows, and functional value. "
    "Do not analyse code or list bugs. Do not mention counts of models/fields.\n\n"
    "Use EXACTLY this Markdown structure:\n"
    "## 📦 What this module does\n"
    "(2–3 sentences: what business process or capability this module enables on this instance)\n"
    "## ⚙️ Key capabilities\n"
    "(5–8 bullets — each describing a concrete user-facing capability, workflow, or business rule "
    "this module provides. Name the actual models, views, actions, and menus from the System Map "
    "as evidence but frame each bullet around what the USER can do, not what the code defines. "
    "If a capability comes from Python logic not visible in the map, say so briefly.)\n"
    "## 👥 Who uses it and when\n"
    "(1–2 sentences: which roles use this module and in what business context)\n\n"
    "Ground every bullet in the System Map. If the module's footprint is small, be concise — "
    "describe only what is actually evidenced and note if deeper behaviour lives in Python logic. "
    "NEVER pad with generic Odoo features that aren't evidenced in the map."
)


class OverviewReq(BaseModel):
    module: str = _DEFAULTS["module"]


@app.post("/api/overview")
def overview(req: OverviewReq, current_user: dict = Depends(get_current_user)) -> dict:
    summary = _SUMMARY.get((current_user["username"], req.module), "")
    if not summary:
        return {"ok": False, "error": "Run Understand first."}
    if not _ENGINE.available():
        return {"ok": False, "engine": "mock", "error": "Claude Code not installed."}
    try:
        result = _ENGINE.run_sync(
            "Write the business overview for this Odoo module from the System Map in your instructions.",
            system_prompt=_OVERVIEW_SYSTEM + "\n\n# SYSTEM MAP\n" + summary,
            max_turns=2, timeout=180,
        )
    except EngineUnavailable as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "engine": "claude-code", "markdown": result.text, "cost_usd": result.cost_usd}


# --- reasoning (Claude Code engine, with mock fallback) -----------------------

class ChatReq(BaseModel):
    message: str
    module: str = _DEFAULTS["module"]
    addons: str | None = _DEFAULTS["addons"]
    image_b64: str | None = None


@app.post("/api/chat")
def chat(req: ChatReq, current_user: dict = Depends(get_current_user)) -> dict:
    key = (current_user["username"], req.module)
    summary = _SUMMARY.get(key, "")
    if not _ENGINE.available():
        return {"engine": "mock", "reply": _mock_reply(req.message, summary)}
    src = _source_dir(req.addons)
    try:
        result = _ENGINE.run_sync(
            req.message,
            code_dir=src,
            system_prompt=build_system_prompt(summary, has_source=src is not None),
            resume=_SESSION.get(key),
        )
        if result.session_id:
            _SESSION[key] = result.session_id
        return {"engine": "claude-code", "reply": result.text, "cost_usd": result.cost_usd}
    except EngineUnavailable as exc:
        return {"engine": "mock", "reply": f"_(Claude Code unavailable: {exc})_\n\n" + _mock_reply(req.message, summary)}


# --- one-shot audit -----------------------------------------------------------

class AuditReq(BaseModel):
    module: str = _DEFAULTS["module"]
    addons: str | None = _DEFAULTS["addons"]


@app.post("/api/audit")
def audit(req: AuditReq, current_user: dict = Depends(get_current_user)) -> dict:
    summary = _SUMMARY.get((current_user["username"], req.module), "")
    if not _ENGINE.available():
        return {"engine": "mock", "ok": False,
                "error": "Claude Code not installed — install @anthropic-ai/claude-code to run a real audit."}
    try:
        outcome = run_full_audit(_ENGINE, module=req.module, addons=req.addons, summary=summary)
    except EngineUnavailable as exc:
        return {"engine": "mock", "ok": False, "error": str(exc)}
    return {
        "engine": "claude-code", "ok": True,
        "markdown": outcome.markdown,
        "findings": len(outcome.findings),
        "rollup": outcome.severity_rollup(),
        "test_cases": len(outcome.test_plan.test_cases),
        "coverage": outcome.test_plan.coverage_rollup(),
        "structured": outcome.structured,
        "cost_usd": outcome.cost_usd,
        "saved_to": outcome.saved,
    }


# --- streaming variants (SSE) -------------------------------------------------

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


def _sse(obj: dict) -> str:
    return "data: " + _json.dumps(obj) + "\n\n"


@app.post("/api/chat/stream")
def chat_stream(req: ChatReq, current_user: dict = Depends(get_current_user)) -> StreamingResponse:
    key = (current_user["username"], req.module)
    summary = _SUMMARY.get(key, "")

    def gen():
        if not _ENGINE.available():
            yield _sse({"type": "text", "text": _mock_reply(req.message, summary)})
            yield _sse({"type": "result"})
            return
        src = _source_dir(req.addons)
        prompt, extra_dirs = _with_screenshot(req.message, req.image_b64)
        try:
            for ev in _ENGINE.run_stream(
                prompt, code_dir=src, extra_dirs=extra_dirs,
                system_prompt=build_system_prompt(summary, has_source=src is not None),
                resume=_SESSION.get(key),
            ):
                if ev.get("type") == "result" and ev.get("session_id"):
                    _SESSION[key] = ev["session_id"]
                yield _sse(ev)
        except EngineUnavailable as exc:
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.post("/api/audit/stream")
def audit_stream(req: AuditReq, current_user: dict = Depends(get_current_user)) -> StreamingResponse:
    summary = _SUMMARY.get((current_user["username"], req.module), "")

    def gen():
        if not _ENGINE.available():
            yield _sse({"type": "error", "message": "Claude Code not installed — cannot run an audit."})
            return
        acc: list[str] = []
        result_text: str | None = None
        cost: float | None = None
        src = _source_dir(req.addons)
        try:
            for ev in _ENGINE.run_stream(
                report_prompt(src is not None), code_dir=src,
                system_prompt=build_system_prompt(summary, has_source=src is not None), timeout=1800,
            ):
                if ev.get("type") == "text":
                    acc.append(ev["text"])
                elif ev.get("type") == "result":
                    result_text = ev.get("result")
                    cost = ev.get("cost_usd")
                yield _sse(ev)

            report_md = (result_text or "".join(acc)).strip()
            if report_md:
                yield _sse({"type": "status", "message": "structuring + verifying findings…"})
                outcome = structure_report(_ENGINE, module=req.module, report_md=report_md,
                                           addons=req.addons, pass1_cost=cost)
                yield _sse({
                    "type": "summary", "ok": True,
                    "findings": len(outcome.findings),
                    "rollup": outcome.severity_rollup(),
                    "verification": outcome.verification,
                    "test_cases": len(outcome.test_plan.test_cases),
                    "coverage": outcome.test_plan.coverage_rollup(),
                    "structured": outcome.structured,
                    "cost_usd": outcome.cost_usd,
                    "saved_to": outcome.saved,
                })
        except EngineUnavailable as exc:
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


# --- per-record investigation -------------------------------------------------

class InvestigateReq(BaseModel):
    url: str = _DEFAULTS["url"]
    db: str = _DEFAULTS["db"]
    user: str = _DEFAULTS["user"]
    password: str = _DEFAULTS["password"]
    module: str = _DEFAULTS["module"]
    verify_ssl: bool = True
    question: str
    record: str | None = None
    image_b64: str | None = None


@app.post("/api/investigate/stream")
def investigate_stream(req: InvestigateReq,
                       current_user: dict = Depends(get_current_user)) -> StreamingResponse:
    def gen():
        if not _ENGINE.available():
            yield _sse({"type": "error", "message": "Claude Code not installed — cannot investigate."})
            return
        client = OdooRPCClient(req.url, req.db, req.user, req.password, verify_ssl=req.verify_ssl)
        try:
            client.authenticate()
        except (OdooAuthError, OdooRPCError) as exc:
            yield _sse({"type": "error", "message": str(exc)})
            return

        refs = [req.record] if req.record else extract_references(req.question)
        if not refs:
            yield _sse({"type": "text", "text": "I couldn't spot a record reference in your question. "
                        "Please mention it — e.g. *S00437*, *INV/2026/00010*, or *WH/OUT/00032*."})
            return
        matches = []
        for tok in refs:
            try:
                matches = resolve_record(client, tok)
            except OdooRPCError:
                matches = []
            if matches:
                break
        if not matches:
            yield _sse({"type": "text", "text": f"No record found matching `{', '.join(refs)}`. "
                        "Double-check the reference (the Number, or the Reference field on the document)."})
            return
        matches = narrow_by_question(req.question, matches)
        if len(matches) > 1:
            opts = "; ".join(f"**{m['label']}** {m['name']}" for m in matches[:6])
            yield _sse({"type": "text", "text": f"That reference matches several documents: {opts}. "
                        "Say which (e.g. add 'the bill', 'the purchase order', or 'the delivery')."})
            return

        m = matches[0]
        yield _sse({"type": "status", "message": f"reading {m['label']} {m['name']} from the live database…"})
        try:
            graph = fetch_record_graph(client, m["model"], m["id"])
        except OdooRPCError as exc:
            yield _sse({"type": "error", "message": f"could not read the record: {exc}"})
            return

        sysp = build_investigation_system(render_graph(graph))
        prompt, extra_dirs = _with_screenshot(req.question, req.image_b64)
        try:
            for ev in _ENGINE.run_stream(prompt, code_dir=None, extra_dirs=extra_dirs,
                                         system_prompt=sysp, timeout=900):
                yield _sse(ev)
        except EngineUnavailable as exc:
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


# --- flow explanation ---------------------------------------------------------

class FlowReq(BaseModel):
    url: str = _DEFAULTS["url"]
    db: str = _DEFAULTS["db"]
    user: str = _DEFAULTS["user"]
    password: str = _DEFAULTS["password"]
    module: str = _DEFAULTS["module"]
    verify_ssl: bool = True
    question: str
    image_b64: str | None = None


@app.post("/api/flow/stream")
def flow_stream(req: FlowReq, current_user: dict = Depends(get_current_user)) -> StreamingResponse:
    def gen():
        if not _ENGINE.available():
            yield _sse({"type": "error", "message": "Claude Code not installed — cannot explain flows."})
            return
        client = OdooRPCClient(req.url, req.db, req.user, req.password, verify_ssl=req.verify_ssl)
        try:
            client.authenticate()
        except (OdooAuthError, OdooRPCError) as exc:
            yield _sse({"type": "error", "message": str(exc)})
            return

        prompt, extra_dirs = _with_screenshot(req.question, req.image_b64)
        target = resolve_flow(req.question)
        if not target:
            sysp = ("You are explaining an Odoo flow to a functional user, step by step in plain language. "
                    "You do not have live records for this topic. Explain it generally with one clear "
                    "illustrative example, and mention that live examples are available for: vendor bills, "
                    "customer invoices, sales orders, purchase orders, deliveries, payments, manufacturing "
                    "orders. Do not reference code.")
            for ev in _ENGINE.run_stream(prompt, code_dir=None, extra_dirs=extra_dirs,
                                         system_prompt=sysp, timeout=600):
                yield _sse(ev)
            return

        model, domain, label = target
        yield _sse({"type": "status", "message": f"pulling real {label} from the live database…"})
        try:
            ex = fetch_flow_examples(client, model, domain, label)
        except OdooRPCError as exc:
            yield _sse({"type": "error", "message": f"could not read {label}: {exc}"})
            return

        has = bool(ex["samples"])
        note = (f"_(grounding in {ex['total']} live {label.lower()})_" if has
                else f"_(no {label.lower()} found — using an illustrative example)_")
        yield _sse({"type": "text", "text": note})
        sysp = build_flow_system(label, render_flow_examples(ex), has_examples=has)
        try:
            for ev in _ENGINE.run_stream(prompt, code_dir=None, extra_dirs=extra_dirs,
                                         system_prompt=sysp, timeout=900):
                yield _sse(ev)
        except EngineUnavailable as exc:
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


def _mock_reply(message: str, summary: str) -> str:
    head = summary.splitlines()[:2]
    return (
        "*(mock engine — Claude Code not installed on this machine yet)*\n\n"
        f"You asked: **{message}**\n\n"
        "Install the Claude Code CLI (`npm install -g @anthropic-ai/claude-code`, then `claude` "
        "to sign in with the subscription) and this panel will read the addon source + the System "
        "Map and answer for real — finding bugs, logic gaps, and drafting the test plan, billed "
        "to the subscription.\n\n"
        f"Context I currently hold:\n> {' '.join(head) if head else 'run Understand first.'}"
    )

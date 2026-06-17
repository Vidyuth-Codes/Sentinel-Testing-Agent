"""Phase 3 UI executor — a Playwright smoke crawl of the Odoo web client.

Logs into Odoo once, then opens each of the addon's window actions in a fresh page and records
what breaks: console errors, uncaught JS exceptions, failed (4xx/5xx) requests, and any Odoo
error dialog — with a screenshot per page. This is read-only browsing (no records created), so it
runs safely against the live DB without cloning.

Driving Odoo forms/workflows end-to-end (create via UI, click workflow buttons) is intentionally
out of scope for this first version — the smoke crawl already surfaces broken views, missing-field
contract errors, and JS exceptions, which are the bulk of frontend defects.
"""

from __future__ import annotations

from pathlib import Path

from sentinel.execute.models import UIPageResult, UIReport


class PlaywrightUnavailable(Exception):
    """Playwright (or its browser) isn't installed."""


def _import_pw():
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
        return sync_playwright
    except ImportError as exc:
        raise PlaywrightUnavailable(
            "Playwright isn't installed. Run:\n"
            "  .\\.venv\\Scripts\\python.exe -m pip install playwright\n"
            "  .\\.venv\\Scripts\\python.exe -m playwright install chromium"
        ) from exc


def _short_url(u: str) -> str:
    # keep the path (+ a little), drop scheme/host/query noise
    s = u.split("://", 1)[-1]
    s = s[s.find("/"):] if "/" in s else s
    return s.split("?", 1)[0][:80]


def _error_dialog(page) -> str | None:
    # ONLY real error surfaces — not ordinary wizard modals (which use .o_dialog/.modal-body and
    # would otherwise be flagged as false positives).
    for sel in (".o_error_dialog", ".o_dialog_error", ".o_notification.border-danger",
                ".o_notification_bar.bg-danger"):
        try:
            el = page.query_selector(sel)
            if el:
                txt = (el.inner_text() or "").strip()
                if txt:
                    return txt[:400]
        except Exception:  # noqa: BLE001
            pass
    return None


def _login(page, url: str, db: str, user: str, password: str) -> None:
    page.goto(f"{url}/web/login?db={db}", wait_until="domcontentloaded", timeout=30000)
    try:
        page.fill("input[name='login']", user, timeout=10000)
        page.fill("input[name='password']", password, timeout=10000)
        page.click("button[type='submit']")
        # The Odoo web client keeps bus/long-poll connections open, so it never reaches
        # "networkidle" — wait for the web-client DOM to appear instead.
        page.wait_for_selector(".o_web_client, .o_main_navbar, .o_home_menu", timeout=25000)
    except Exception as exc:  # noqa: BLE001
        if "/web/login" in page.url:
            raise PlaywrightUnavailable("Odoo login failed — check user/password/db.") from exc
        raise PlaywrightUnavailable(f"could not reach the web client after login: {str(exc)[:160]}") from exc
    if "/web/login" in page.url:
        raise PlaywrightUnavailable("Odoo login failed — check user/password/db.")


def _visit(ctx, url: str, act: dict, shots: Path, settle_ms: int) -> UIPageResult:
    page = ctx.new_page()
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed: list[str] = []

    page.on("console", lambda m: console_errors.append(m.text[:300]) if m.type == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)[:300]))

    def _on_response(resp):
        try:
            if resp.status >= 400:
                failed.append(f"{resp.status} {resp.request.method} {_short_url(resp.url)}")
        except Exception:  # noqa: BLE001
            pass

    page.on("response", _on_response)

    target = f"{url}/odoo/action-{act['id']}"
    status = "ok"
    dialog = None
    try:
        page.goto(target, wait_until="domcontentloaded", timeout=30000)
        # wait for the web client to mount the action (never networkidle — see _login)
        try:
            page.wait_for_selector(".o_action_manager, .o_content", timeout=15000)
        except Exception:  # noqa: BLE001 — view may have failed to render; errors are captured anyway
            pass
        page.wait_for_timeout(settle_ms)
        dialog = _error_dialog(page)
    except Exception as exc:  # noqa: BLE001
        status = "load_error"
        page_errors.append(f"navigation: {str(exc)[:200]}")

    shot_path: str | None = str(shots / f"action-{act['id']}.png")
    try:
        page.screenshot(path=shot_path, full_page=False)
    except Exception:  # noqa: BLE001
        shot_path = None

    has_5xx = any(f.startswith("5") for f in failed)
    if status != "load_error" and (console_errors or page_errors or dialog or has_5xx):
        status = "issues"

    page.close()
    return UIPageResult(
        action_id=act["id"], name=act.get("name") or f"action {act['id']}",
        model=act.get("model"), url=target, status=status,
        console_errors=console_errors[:10], page_errors=page_errors[:10],
        failed_requests=failed[:15], error_dialog=dialog, screenshot=shot_path,
    )


def run_ui_crawl(
    *, url: str, db: str, user: str, password: str, module: str, actions: list[dict],
    out_dir: Path, headless: bool = True, max_pages: int = 12, settle_ms: int = 2500,
    progress=None,
) -> UIReport:
    sync_playwright = _import_pw()
    out_dir.mkdir(parents=True, exist_ok=True)
    shots = out_dir / "screenshots"
    shots.mkdir(exist_ok=True)

    pages: list[UIPageResult] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900}, ignore_https_errors=True)
        try:
            login_page = ctx.new_page()
            _login(login_page, url, db, user, password)
            login_page.close()
            for act in actions[:max_pages]:
                if progress:
                    progress(f"visiting action {act['id']} — {act.get('name') or ''}")
                pages.append(_visit(ctx, url, act, shots, settle_ms))
        finally:
            ctx.close()
            browser.close()

    return UIReport(module=module, url=url, db=db, pages=pages)

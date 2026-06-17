"""Sentinel CLI — Odoo QA & bug-detection agent.

    sentinel web                                          launch the web UI (chat + System Map)
    sentinel introspect --db DB --module M [--addons P]   build a System Map (live RPC, no LLM)
    sentinel scan-addons <addon_path>                     statically scan an addon's source
    sentinel audit --module M --addons P [--db DB ...]    full Claude Code audit → findings + test plan
    sentinel run-tests --module M --addons P --db DB ...   Phase 3: execute RPC flows on a cloned DB
    sentinel run-ui --module M --db DB ...                  Phase 3: Playwright UI smoke crawl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sentinel import __version__


def _cmd_introspect(args: argparse.Namespace) -> int:
    from sentinel.odoo import OdooRPCClient, build_system_map, scan_addon
    from sentinel.odoo.rpc import OdooAuthError, OdooRPCError
    from sentinel.odoo.report import render_system_map_markdown, write_system_map
    from sentinel.paths import run_dir

    client = OdooRPCClient(args.url, args.db, args.user, args.password)
    print(f"Sentinel v{__version__} - introspecting module '{args.module}' on {args.url} (db={args.db})")
    try:
        ver = client.version().get("server_version")
        client.authenticate()
        print(f"  connected: Odoo {ver}  (uid={client.uid})")
        smap = build_system_map(client, args.module)
    except (OdooAuthError, OdooRPCError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    scan = scan_addon(args.addons) if args.addons else None

    c = smap.counts()
    print(f"  new models: {c['new_models']}  extended: {c['extended_models']}  "
          f"fields(owned): {c['fields_owned']}  views: {c['views']}  actions: {c['actions']}")
    print(f"  access rules: {c['access_rules']}  record rules: {c['record_rules']}  "
          f"crons: {c['scheduled_actions']}  automations: {c['automations']}")

    out_dir = Path(args.out) if args.out else run_dir(f"introspect-{args.module}")
    paths = write_system_map(smap, scan, out_dir)
    print(f"  system map: {paths['markdown']}")
    print(f"  json:       {paths['json']}")
    if args.print:
        print()
        print(render_system_map_markdown(smap, scan))
    return 0


def _cmd_scan_addons(args: argparse.Namespace) -> int:
    from sentinel.odoo import scan_addon

    path = Path(args.path)
    if not (path / "__manifest__.py").exists():
        print(f"error: no __manifest__.py in {path} (point at the addon root)", file=sys.stderr)
        return 2
    scan = scan_addon(path)
    print(f"Sentinel v{__version__} - scanned addon '{scan.technical_name}' ({scan.manifest_name} v{scan.version})")
    for k, v in scan.counts().items():
        print(f"  {k.replace('_', ' '):<20} {v}")
    if args.json:
        print()
        print(scan.model_dump_json(indent=2))
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    from sentinel.audit import run_full_audit
    from sentinel.engine import ClaudeCodeEngine
    from sentinel.odoo import summarize_system_map

    if not (Path(args.addons) / "__manifest__.py").exists():
        print(f"error: no __manifest__.py in {args.addons} (point --addons at the addon root)", file=sys.stderr)
        return 2

    engine = ClaudeCodeEngine()
    if not engine.available():
        print("error: Claude Code CLI not found. Install it and sign in:\n"
              "  npm install -g @anthropic-ai/claude-code\n  claude", file=sys.stderr)
        return 3

    # Optional: introspect a live instance first for System Map context (richer audit).
    summary = ""
    if args.db:
        from sentinel.odoo import OdooRPCClient, build_system_map, scan_addon
        from sentinel.odoo.rpc import OdooAuthError, OdooRPCError
        client = OdooRPCClient(args.url, args.db, args.user, args.password)
        try:
            client.authenticate()
            smap = build_system_map(client, args.module)
            scan = scan_addon(args.addons)
            summary = summarize_system_map(smap, scan)
            print(f"  System Map: {smap.counts()['new_models']} new models, "
                  f"{smap.counts()['fields_owned']} fields (context loaded)")
        except (OdooAuthError, OdooRPCError) as exc:
            print(f"  note: introspection skipped ({exc}); auditing from source only.")

    print(f"Sentinel v{__version__} - auditing '{args.module}' via Claude Code (this may take a few minutes)…")
    outcome = run_full_audit(engine, module=args.module, addons=args.addons, summary=summary)

    roll = outcome.severity_rollup()
    sev = "  ".join(f"{k}:{v}" for k, v in roll.items() if v) or "no findings"
    cov = outcome.test_plan.coverage_rollup()
    print(f"  findings: {len(outcome.findings)}  ({sev})")
    print(f"  test cases: {len(outcome.test_plan.test_cases)}  "
          f"coverage: {cov['covered']} ok / {cov['partial']} partial / {cov['gap']} gap")
    if not outcome.structured:
        print("  note: structured extraction failed — Markdown report saved, findings.json may be empty.")
    if outcome.cost_usd:
        print(f"  cost: ${outcome.cost_usd:.4f} (subscription)")
    for name, path in outcome.saved.items():
        if name != "dir":
            print(f"  {name}: {path}")
    return 0


def _cmd_run_tests(args: argparse.Namespace) -> int:
    import json
    from datetime import datetime

    from sentinel.engine import ClaudeCodeEngine
    from sentinel.execute import (
        ExecCaseSet, ExecReport, generate_cases, master_password, provision, run_cases,
        teardown, write_report,
    )
    from sentinel.odoo import OdooRPCClient, build_system_map, scan_addon, summarize_system_map
    from sentinel.odoo.rpc import OdooAuthError, OdooRPCError
    from sentinel.paths import run_dir

    if not (Path(args.addons) / "__manifest__.py").exists():
        print(f"error: no __manifest__.py in {args.addons}", file=sys.stderr)
        return 2

    engine = ClaudeCodeEngine()
    if not args.cases and not engine.available():
        print("error: generating test cases needs the Claude Code CLI (or pass --cases FILE).", file=sys.stderr)
        return 3

    print(f"Sentinel v{__version__} - RPC flow execution for '{args.module}' on {args.url}")

    # System Map summary from the SOURCE db (read-only) to ground case generation.
    summary = ""
    try:
        src = OdooRPCClient(args.url, args.db, args.user, args.password)
        src.authenticate()
        summary = summarize_system_map(build_system_map(src, args.module), scan_addon(args.addons))
    except (OdooAuthError, OdooRPCError) as exc:
        print(f"  note: System Map context skipped ({exc})")

    # Provision a safe database (clone by default; existing only with explicit opt-in).
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    try:
        prov = provision(args.url, args.db, use_existing=args.use_existing_db,
                         master_pw=master_password(args.master_pw), stamp=stamp)
    except OdooRPCError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if prov.cloned:
        print(f"  cloned '{prov.source_db}' -> '{prov.db}' (throwaway)")
    else:
        print(f"  ⚠ executing against EXISTING db '{prov.db}' (you opted in with --use-existing-db)")

    try:
        # Test cases: load from file, or generate with Claude Code.
        if args.cases:
            cases = ExecCaseSet(**json.loads(Path(args.cases).read_text(encoding="utf-8")))
            print(f"  loaded {len(cases.cases)} cases from {args.cases}")
        else:
            seed = None
            if args.seed_findings and Path(args.seed_findings).exists():
                data = json.loads(Path(args.seed_findings).read_text(encoding="utf-8"))
                seed = "\n".join(f"- {f.get('title')} ({(f.get('location') or {}).get('file')})"
                                 for f in data[: args.max_cases])
            print("  generating executable cases via Claude Code…")
            cases, _ = generate_cases(engine, module=args.module, addons=args.addons,
                                      summary=summary, max_cases=args.max_cases, seed=seed)
            print(f"  generated {len(cases.cases)} cases")

        if not cases.cases:
            print("  no executable cases — nothing to run.")
            return 0

        client = OdooRPCClient(args.url, prov.db, args.user, args.password)
        client.authenticate()
        results = run_cases(client, cases.cases)
        report = ExecReport(module=args.module, url=args.url, db=prov.db,
                            source_db=prov.source_db, cloned=prov.cloned, results=results)
        paths = write_report(report, cases, run_dir(f"exectest-{args.module}"))

        roll = report.rollup()
        print(f"  results: ✅ {roll['pass']} passed · ❌ {roll['fail']} failed · ⚠ {roll['error']} errored")
        for c in report.results:
            if c.status != "pass":
                print(f"    {c.status.upper():5} {c.id} — {c.title}: {c.message}")
        print(f"  report: {paths['results_md']}")
    except (OdooRPCError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        msg = teardown(prov, keep=args.keep_clone)
        if msg:
            print(f"  {msg}")
    return 0


def _cmd_run_ui(args: argparse.Namespace) -> int:
    from sentinel.execute import PlaywrightUnavailable, run_ui_crawl, write_ui_report
    from sentinel.odoo import OdooRPCClient, build_system_map
    from sentinel.odoo.rpc import OdooAuthError, OdooRPCError
    from sentinel.paths import run_dir

    client = OdooRPCClient(args.url, args.db, args.user, args.password)
    print(f"Sentinel v{__version__} - UI smoke crawl for '{args.module}' on {args.url}")
    try:
        client.authenticate()
        smap = build_system_map(client, args.module)
    except (OdooAuthError, OdooRPCError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    actions = [{"id": a.id, "name": a.name, "model": a.res_model}
               for a in smap.actions if (a.type or "").endswith("act_window") and a.id]
    if not actions:
        print("  no window actions found for this module — nothing to crawl.")
        return 0
    print(f"  {len(actions)} window actions found; crawling up to {args.max_pages}…")

    out = run_dir(f"ui-{args.module}")
    try:
        report = run_ui_crawl(
            url=args.url, db=args.db, user=args.user, password=args.password,
            module=args.module, actions=actions, out_dir=out,
            headless=not args.headed, max_pages=args.max_pages, settle_ms=args.settle,
            progress=lambda m: print(f"    - {m}"),
        )
    except PlaywrightUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    roll = report.rollup()
    print(f"  pages: ✅ {roll['ok']} clean · ❌ {roll['issues']} issues · ⚠ {roll['load_error']} load-error")
    for p in report.pages:
        if p.status != "ok":
            sig = (p.error_dialog or (p.page_errors[0] if p.page_errors else None)
                   or (p.console_errors[0] if p.console_errors else None)
                   or (p.failed_requests[0] if p.failed_requests else p.status))
            print(f"    {p.status.upper():10} {p.name} (#{p.action_id}): {str(sig)[:120]}")
    paths = write_ui_report(report, out)
    print(f"  report: {paths['ui_results_md']}")
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    import uvicorn

    print(f"Sentinel v{__version__} - web UI at http://{args.host}:{args.port}")
    uvicorn.run("sentinel.web.app:app", host=args.host, port=args.port, reload=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentinel", description="Sentinel — Odoo QA & bug-detection agent")
    parser.add_argument("--version", action="version", version=f"sentinel {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- Web UI ---
    web = sub.add_parser("web", help="Launch the Sentinel web UI (chat + System Map dashboard)")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8800)
    web.set_defaults(func=_cmd_web)

    # --- Odoo: introspect a live instance ---
    intro = sub.add_parser("introspect", help="Introspect a live Odoo module (build a System Map)")
    intro.add_argument("--url", default="http://localhost:8069", help="Odoo base URL")
    intro.add_argument("--db", required=True, help="Odoo database name")
    intro.add_argument("--user", default="admin", help="Odoo login (default: admin)")
    intro.add_argument("--password", default="admin", help="Odoo password or API key (default: admin)")
    intro.add_argument("--module", required=True, help="Technical name of the addon to map (e.g. assetz)")
    intro.add_argument("--addons", help="Path to the addon source (enables static cross-check)")
    intro.add_argument("--out", help="Output dir (default: <repo>/output/introspect-<module>-<ts>)")
    intro.add_argument("--print", action="store_true", help="Print the System Map markdown to stdout")
    intro.set_defaults(func=_cmd_introspect)

    # --- Odoo: static scan of addon source ---
    scan = sub.add_parser("scan-addons", help="Statically scan an Odoo addon's source on disk")
    scan.add_argument("path", help="Path to the addon root (folder with __manifest__.py)")
    scan.add_argument("--json", action="store_true", help="Print the full scan JSON")
    scan.set_defaults(func=_cmd_scan_addons)

    # --- Odoo: full Claude Code audit (Phase 2) ---
    aud = sub.add_parser("audit", help="Run a full Claude Code audit -> findings.json + test_plan.json")
    aud.add_argument("--module", required=True, help="Technical name of the addon (e.g. assetz)")
    aud.add_argument("--addons", required=True, help="Path to the addon source (folder with __manifest__.py)")
    aud.add_argument("--db", help="Odoo database name — if given, introspect first for System Map context")
    aud.add_argument("--url", default="http://localhost:8069", help="Odoo base URL (with --db)")
    aud.add_argument("--user", default="admin", help="Odoo login (with --db)")
    aud.add_argument("--password", default="admin", help="Odoo password or API key (with --db)")
    aud.set_defaults(func=_cmd_audit)

    # --- Odoo: execute RPC flows (Phase 3) ---
    rt = sub.add_parser("run-tests", help="Execute RPC flow tests against a cloned DB (Phase 3)")
    rt.add_argument("--module", required=True, help="Technical name of the addon (e.g. assetz)")
    rt.add_argument("--addons", required=True, help="Path to the addon source (folder with __manifest__.py)")
    rt.add_argument("--db", required=True, help="Source Odoo database (cloned by default; never written to)")
    rt.add_argument("--url", default="http://localhost:8069", help="Odoo base URL")
    rt.add_argument("--user", default="admin", help="Odoo login")
    rt.add_argument("--password", default="admin", help="Odoo password or API key")
    rt.add_argument("--use-existing-db", action="store_true",
                    help="Run against --db directly (it WILL be written to). Only for a disposable DB.")
    rt.add_argument("--master-pw", help="Odoo master password for cloning (or set SENTINEL_ODOO_MASTER)")
    rt.add_argument("--keep-clone", action="store_true", help="Do not drop the cloned DB after the run")
    rt.add_argument("--max-cases", type=int, default=8, help="Max test cases to generate (default 8)")
    rt.add_argument("--cases", help="Run a pre-made executable-cases JSON instead of generating")
    rt.add_argument("--seed-findings", help="A findings.json to focus case generation on confirming")
    rt.set_defaults(func=_cmd_run_tests)

    # --- Odoo: UI smoke crawl (Phase 3, Playwright) ---
    ui = sub.add_parser("run-ui", help="Crawl the addon's views in the Odoo web client (Playwright)")
    ui.add_argument("--module", required=True, help="Technical name of the addon (e.g. assetz)")
    ui.add_argument("--db", required=True, help="Odoo database name")
    ui.add_argument("--url", default="http://localhost:8069", help="Odoo base URL")
    ui.add_argument("--user", default="admin", help="Odoo login")
    ui.add_argument("--password", default="admin", help="Odoo password or API key")
    ui.add_argument("--max-pages", type=int, default=12, help="Max window actions to visit (default 12)")
    ui.add_argument("--settle", type=int, default=2500, help="ms to wait after load for the SPA to settle")
    ui.add_argument("--headed", action="store_true", help="Show the browser window (default: headless)")
    ui.set_defaults(func=_cmd_run_ui)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; make our (occasionally non-ASCII) output safe.
    for stream in (sys.stdout, sys.stderr):
        reconfig = getattr(stream, "reconfigure", None)
        if reconfig:
            try:
                reconfig(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

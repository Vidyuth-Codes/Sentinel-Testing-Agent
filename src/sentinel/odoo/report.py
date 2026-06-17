"""Render a SystemMap (+ optional static AddonScan) into a Markdown understanding
report. This is the Phase 1 deliverable: "here is what your addon developed and
configured" — the foundation the Phase 2 test-plan/gap analysis builds on.
"""

from __future__ import annotations

from pathlib import Path

from sentinel.odoo.addon_scan import AddonScan
from sentinel.odoo.schema import SystemMap


def render_system_map_markdown(smap: SystemMap, scan: AddonScan | None = None) -> str:
    c = smap.counts()
    lines: list[str] = []
    lines.append(f"# Odoo System Map — `{smap.module}`")
    lines.append("")
    lines.append(f"**Instance:** {smap.url}  ·  **DB:** `{smap.db}`  ·  **Server:** {smap.server_version}  ")
    lines.append(f"**Generated:** {smap.generated_at.isoformat(timespec='seconds')}  ")
    lines.append(f"**Installed modules:** {smap.modules_installed}  ·  **Depends on:** {', '.join(smap.module_depends) or '-'}")
    lines.append("")

    # --- summary ---
    lines.append("## What this addon developed & configured")
    lines.append("")
    lines.append("| Element | Count |")
    lines.append("|---------|-------|")
    for k, v in c.items():
        lines.append(f"| {k.replace('_', ' ').capitalize()} | {v} |")
    lines.append("")

    # --- new models ---
    lines.append("## New models (created by addon)")
    lines.append("")
    new_models = [m for m in smap.models if m.owned_by_addon]
    if new_models:
        lines.append("| Model | Label | Fields | Key relational fields |")
        lines.append("|-------|-------|--------|------------------------|")
        for m in sorted(new_models, key=lambda x: x.model):
            rels = [f"{f.name}→{f.relation}" for f in m.fields if f.relation][:4]
            lines.append(f"| `{m.model}` | {m.name or ''} | {m.n_fields} | {', '.join(rels) or '-'} |")
    else:
        lines.append("_None._")
    lines.append("")

    # --- extended models ---
    lines.append("## Extended models (addon added fields to existing models)")
    lines.append("")
    ext = [m for m in smap.models if not m.owned_by_addon and m.n_fields_owned]
    if ext:
        lines.append("| Model | Fields added by addon |")
        lines.append("|-------|------------------------|")
        for m in sorted(ext, key=lambda x: x.model):
            added = ", ".join(f.name for f in m.fields if f.owned_by_addon)
            lines.append(f"| `{m.model}` | {added} |")
    else:
        lines.append("_None._")
    lines.append("")

    # --- automations & crons (business logic that runs on its own) ---
    lines.append("## Scheduled actions & automations")
    lines.append("")
    if smap.crons or smap.automations:
        for cr in smap.crons:
            lines.append(f"- ⏰ **cron** `{cr.name}` on `{cr.model or '-'}` every {cr.interval} "
                         f"({'active' if cr.active else 'inactive'})")
        for au in smap.automations:
            lines.append(f"- ⚙️ **automation** `{au.name}` on `{au.model or '-'}` (trigger: {au.trigger})")
    else:
        lines.append("_None._")
    lines.append("")

    # --- security ---
    lines.append("## Security (access rights & record rules)")
    lines.append("")
    if smap.access:
        lines.append("| Access | Model | Group | R | W | C | U |")
        lines.append("|--------|-------|-------|---|---|---|---|")
        for a in smap.access:
            def b(x):
                return "✓" if x else "·"
            lines.append(f"| {a.name or ''} | `{a.model or '-'}` | {a.group} | "
                         f"{b(a.read)} | {b(a.write)} | {b(a.create)} | {b(a.unlink)} |")
    else:
        lines.append("_No addon-owned access rules._")
    lines.append("")
    if smap.rules:
        lines.append("**Record rules:**")
        for r in smap.rules:
            scope = "global" if r.global_rule else ", ".join(r.groups) or "(no groups)"
            lines.append(f"- `{r.name}` on `{r.model}` [{scope}] domain: `{r.domain}`")
        lines.append("")

    # --- actions & menus ---
    lines.append("## Actions & menus")
    lines.append("")
    lines.append(f"- Window/server/report/client actions: **{len(smap.actions)}**")
    lines.append(f"- Menu items: **{len(smap.menus)}**")
    lines.append(f"- Views (form/list/kanban/search/...): **{len(smap.views)}**")
    lines.append("")

    # --- static code cross-check ---
    if scan is not None:
        sc = scan.counts()
        lines.append("## Static code cross-check (source on disk)")
        lines.append("")
        lines.append(f"Manifest: **{scan.manifest_name}** v{scan.version}  ·  "
                     f"Python deps: {', '.join(scan.python_deps) or '-'}")
        lines.append("")
        lines.append("| From source | Count |")
        lines.append("|-------------|-------|")
        for k, v in sc.items():
            lines.append(f"| {k.replace('_', ' ').capitalize()} | {v} |")
        lines.append("")
        # surface business logic worth testing
        actions = [(m.name or m.class_name, x.name) for m in scan.model_classes for x in m.methods if x.kind == "action"]
        if actions:
            lines.append("**Action/button methods (candidate functional tests):**")
            for model, method in sorted(actions)[:40]:
                lines.append(f"- `{model}` → `{method}()`")
            if len(actions) > 40:
                lines.append(f"- … and {len(actions) - 40} more")
            lines.append("")

    lines.append("---")
    lines.append("> Phase 1 output: system understanding. Next (Phase 2): map requirements to "
                 "this surface, generate the test plan, and flag gaps.")
    lines.append("")
    return "\n".join(lines)


def write_system_map(smap: SystemMap, scan: AddonScan | None, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md = out / "system_map.md"
    js = out / "system_map.json"
    md.write_text(render_system_map_markdown(smap, scan), encoding="utf-8")
    js.write_text(smap.model_dump_json(indent=2), encoding="utf-8")
    return {"markdown": md, "json": js}

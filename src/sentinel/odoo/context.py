"""Condense a SystemMap (+ static AddonScan) into a compact text brief for Claude Code.

We never dump all ~1,400 fields — we summarise the business surface: models with
their key fields + any state/workflow field, the action/button methods (the real
operations to test), crons, and security groups. This brief is injected into the
Claude Code system prompt alongside the Odoo-QA skill (see `engine/skill.py`).
"""

from __future__ import annotations

from sentinel.odoo.addon_scan import AddonScan
from sentinel.odoo.schema import OdooModelInfo, SystemMap

_NOISE_FIELDS = {
    "id", "create_uid", "create_date", "write_uid", "write_date",
    "__last_update", "display_name", "sequence",
}
_NOISE_PREFIXES = ("activity_", "message_", "website_message_", "rating_", "my_activity_")


def _interesting_fields(model: OdooModelInfo, limit: int = 12) -> list[str]:
    out = []
    for f in model.fields:
        if f.name in _NOISE_FIELDS or f.name.startswith(_NOISE_PREFIXES):
            continue
        tag = f.ttype or "?"
        if f.relation:
            tag += f"→{f.relation}"
        if f.required:
            tag += "*"
        if f.compute:
            tag += " (computed)"
        out.append(f"{f.name}:{tag}")
        if len(out) >= limit:
            break
    return out


def _state_field(model: OdooModelInfo) -> str | None:
    for f in model.fields:
        if f.name in ("state", "status", "stage_id") and (f.ttype in ("selection", "many2one")):
            return f.name
    return None


def summarize_system_map(smap: SystemMap, scan: AddonScan | None = None, *, max_models: int = 80) -> str:
    lines: list[str] = []
    c = smap.counts()
    lines.append(f"ODOO MODULE: {smap.module}  (server {smap.server_version}, db {smap.db})")
    lines.append(
        f"Surface: {c['new_models']} new models, {c['extended_models']} extended, "
        f"{c['fields_owned']} fields, {c['views']} views, {c['actions']} actions, "
        f"{c['scheduled_actions']} crons, {c['record_rules']} record rules."
    )
    lines.append("")

    # action/button methods per model from source (the real operations to test)
    actions_by_model: dict[str, list[str]] = {}
    if scan:
        for cls in scan.model_classes:
            key = cls.name or (cls.inherit[0] if cls.inherit else cls.class_name)
            acts = [m.name for m in cls.methods if m.kind == "action"]
            if acts:
                actions_by_model.setdefault(key, []).extend(acts)

    lines.append("NEW MODELS (model — label — key fields — workflow field — actions):")
    for m in sorted((m for m in smap.models if m.owned_by_addon), key=lambda x: x.model)[:max_models]:
        state = _state_field(m)
        acts = actions_by_model.get(m.model, [])
        lines.append(
            f"- {m.model} — {m.name or ''} — fields[{m.n_fields}]: "
            f"{', '.join(_interesting_fields(m))}"
            + (f" — WORKFLOW:{state}" if state else "")
            + (f" — ACTIONS: {', '.join(sorted(set(acts))[:12])}" if acts else "")
        )
    lines.append("")

    ext = [m for m in smap.models if not m.owned_by_addon and m.n_fields_owned]
    if ext:
        lines.append("EXTENDED MODELS (existing Odoo models the addon adds to):")
        for m in ext:
            added = ", ".join(f.name for f in m.fields if f.owned_by_addon)
            lines.append(f"- {m.model}: +{added}")
        lines.append("")

    if smap.crons:
        lines.append("SCHEDULED ACTIONS:")
        for cr in smap.crons:
            lines.append(f"- {cr.name} on {cr.model} every {cr.interval} ({'on' if cr.active else 'off'})")
        lines.append("")

    groups = sorted({a.group for a in smap.access if a.group})
    if groups:
        lines.append(f"SECURITY GROUPS: {', '.join(groups)}")
        lines.append("")

    return "\n".join(lines)

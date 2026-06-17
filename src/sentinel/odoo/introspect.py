"""Build a SystemMap from a live Odoo instance via RPC.

Strategy: `ir.model.data` records every DB object a module created/configured
(keyed by `module`). We pull all rows for the target addon, group them by target
model, then fetch details — giving a precise picture of exactly what the addon
*developed and configured*, separate from inherited core behaviour.
"""

from __future__ import annotations

from collections import defaultdict

from sentinel.odoo.rpc import OdooRPCClient
from sentinel.odoo.schema import (
    OdooAccess,
    OdooAction,
    OdooAutomation,
    OdooCron,
    OdooField,
    OdooMenu,
    OdooModelInfo,
    OdooRule,
    OdooSequence,
    OdooView,
    SystemMap,
)

_ACTION_MODELS = {
    "ir.actions.act_window": "act_window",
    "ir.actions.server": "server",
    "ir.actions.report": "report",
    "ir.actions.client": "client",
}


def _clean(rows: list[dict]) -> list[dict]:
    """Odoo returns `False` for empty values; convert to None so optional string
    fields validate. Booleans are re-cast with bool() at the call sites, and m2o
    helpers treat None the same as the old False — so this is safe across types.
    """
    return [{k: (None if v is False else v) for k, v in row.items()} for row in rows]


def _m2o_id(value) -> int | None:
    """Odoo m2o reads come back as [id, display] or False."""
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return None


def _m2o_name(value) -> str | None:
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return value[1]
    return None


def build_system_map(client: OdooRPCClient, module: str) -> SystemMap:
    ver = client.version().get("server_version")

    # --- module metadata --------------------------------------------------
    mods = client.search_read(
        "ir.module.module",
        [["name", "=", module]],
        ["name", "state", "dependencies_id"],
    )
    depends: list[str] = []
    if mods:
        dep_ids = mods[0].get("dependencies_id") or []
        if dep_ids:
            deps = client.search_read(
                "ir.module.module.dependency", [["id", "in", dep_ids]], ["name"]
            )
            depends = sorted(d["name"] for d in deps)
    installed_count = client.execute_kw(
        "ir.module.module", "search_count", [[["state", "=", "installed"]]]
    )

    smap = SystemMap(
        url=client.url, db=client.db, module=module,
        server_version=ver, modules_installed=installed_count, module_depends=depends,
    )

    # --- everything the addon owns (via ir.model.data) --------------------
    data_rows = client.search_read(
        "ir.model.data", [["module", "=", module]], ["model", "res_id", "name"]
    )
    owned: dict[str, set[int]] = defaultdict(set)
    for row in data_rows:
        owned[row["model"]].add(row["res_id"])

    # ir.model id -> technical name (resolve m2o references everywhere).
    # `modules` lists every module that defines/extends the model — used below to tell a model
    # the addon truly *created* from a core model it merely *extends*.
    all_models = _clean(client.search_read("ir.model", [], ["id", "model", "name", "transient", "modules"]))
    model_by_id = {m["id"]: m for m in all_models}

    owned_model_ids = owned.get("ir.model", set())
    owned_model_techs = {
        model_by_id[i]["model"] for i in owned_model_ids if i in model_by_id
    }

    # --- fields: owned fields + surface models ----------------------------
    owned_field_ids = owned.get("ir.model.fields", set())
    owned_fields = (
        client.search_read("ir.model.fields", [["id", "in", list(owned_field_ids)]], ["model"])
        if owned_field_ids else []
    )
    surface_models = set(owned_model_techs) | {f["model"] for f in owned_fields}

    if surface_models:
        field_rows = _clean(client.search_read(
            "ir.model.fields",
            [["model", "in", sorted(surface_models)]],
            ["id", "name", "field_description", "ttype", "required", "readonly",
             "store", "relation", "related", "compute", "help", "model"],
        ))
        by_model: dict[str, list[OdooField]] = defaultdict(list)
        for f in field_rows:
            by_model[f["model"]].append(
                OdooField(
                    name=f["name"],
                    string=f.get("field_description"),
                    ttype=f.get("ttype"),
                    required=bool(f.get("required")),
                    readonly=bool(f.get("readonly")),
                    store=bool(f.get("store")),
                    relation=f.get("relation") or None,
                    related=f.get("related") or None,
                    compute=bool(f.get("compute")),
                    help=(f.get("help") or None),
                    owned_by_addon=f["id"] in owned_field_ids,
                )
            )
        for tech in sorted(surface_models):
            meta = next((m for m in all_models if m["model"] == tech), {})
            # "new" only if the addon CREATED the model — i.e. it's the sole module that defines it.
            # If several modules touch the model (e.g. core `account.move`), the addon merely extends it.
            mods = {x.strip() for x in (meta.get("modules") or "").split(",") if x.strip()}
            is_new = (tech in owned_model_techs) and (mods == {module} if mods else True)
            smap.models.append(
                OdooModelInfo(
                    model=tech,
                    name=meta.get("name"),
                    transient=bool(meta.get("transient")),
                    owned_by_addon=is_new,
                    fields=sorted(by_model.get(tech, []), key=lambda x: x.name),
                )
            )

    # --- views ------------------------------------------------------------
    for v in _read_ids(client, "ir.ui.view", owned.get("ir.ui.view"),
                       ["name", "model", "type", "mode", "inherit_id"]):
        smap.views.append(OdooView(
            id=v["id"], name=v.get("name"), model=v.get("model"),
            type=v.get("type"), mode=v.get("mode"), inherit_id=_m2o_id(v.get("inherit_id")),
        ))

    # --- actions (across action types) -----------------------------------
    for data_model, label in _ACTION_MODELS.items():
        fields = ["name"]
        if data_model == "ir.actions.act_window":
            fields += ["res_model", "view_mode"]
        for a in _read_ids(client, data_model, owned.get(data_model), fields):
            smap.actions.append(OdooAction(
                id=a["id"], name=a.get("name"), type=label,
                res_model=a.get("res_model"), view_mode=a.get("view_mode"),
            ))

    # --- menus ------------------------------------------------------------
    for mn in _read_ids(client, "ir.ui.menu", owned.get("ir.ui.menu"),
                        ["complete_name", "parent_id", "action"]):
        smap.menus.append(OdooMenu(
            id=mn["id"], name=mn.get("complete_name"),
            parent=_m2o_name(mn.get("parent_id")),
            action=mn.get("action") if isinstance(mn.get("action"), str) else _m2o_name(mn.get("action")),
        ))

    # --- access rights ----------------------------------------------------
    for ac in _read_ids(client, "ir.model.access", owned.get("ir.model.access"),
                        ["name", "model_id", "group_id",
                         "perm_read", "perm_write", "perm_create", "perm_unlink"]):
        mid = _m2o_id(ac.get("model_id"))
        smap.access.append(OdooAccess(
            name=ac.get("name"),
            model=model_by_id.get(mid, {}).get("model") if mid else None,
            group=_m2o_name(ac.get("group_id")) or "(public)",
            read=bool(ac.get("perm_read")), write=bool(ac.get("perm_write")),
            create=bool(ac.get("perm_create")), unlink=bool(ac.get("perm_unlink")),
        ))

    # --- record rules -----------------------------------------------------
    for rl in _read_ids(client, "ir.rule", owned.get("ir.rule"),
                        ["name", "model_id", "groups", "domain_force",
                         "perm_read", "perm_write", "perm_create", "perm_unlink", "global"]):
        mid = _m2o_id(rl.get("model_id"))
        group_ids = rl.get("groups") or []
        group_names = []
        if group_ids:
            grs = client.search_read("res.groups", [["id", "in", group_ids]], ["full_name"])
            group_names = sorted(g.get("full_name") or "" for g in grs)
        smap.rules.append(OdooRule(
            name=rl.get("name"),
            model=model_by_id.get(mid, {}).get("model") if mid else None,
            groups=group_names, domain=rl.get("domain_force"),
            read=bool(rl.get("perm_read")), write=bool(rl.get("perm_write")),
            create=bool(rl.get("perm_create")), unlink=bool(rl.get("perm_unlink")),
            global_rule=bool(rl.get("global")),
        ))

    # --- scheduled actions (cron) ----------------------------------------
    for cr in _read_ids(client, "ir.cron", owned.get("ir.cron"),
                        ["name", "model_id", "active", "interval_number", "interval_type"]):
        mid = _m2o_id(cr.get("model_id"))
        smap.crons.append(OdooCron(
            id=cr["id"], name=cr.get("name"),
            model=model_by_id.get(mid, {}).get("model") if mid else None,
            active=bool(cr.get("active")),
            interval=f"{cr.get('interval_number')} {cr.get('interval_type')}",
        ))

    # --- automations (base.automation; may be absent) ---------------------
    try:
        for au in _read_ids(client, "base.automation", owned.get("base.automation"),
                            ["name", "model_id", "trigger"]):
            mid = _m2o_id(au.get("model_id"))
            smap.automations.append(OdooAutomation(
                id=au["id"], name=au.get("name"),
                model=model_by_id.get(mid, {}).get("model") if mid else None,
                trigger=au.get("trigger"),
            ))
    except Exception:  # noqa: BLE001 — base_automation not installed; skip
        pass

    # --- sequences --------------------------------------------------------
    for sq in _read_ids(client, "ir.sequence", owned.get("ir.sequence"),
                        ["name", "code", "prefix"]):
        smap.sequences.append(OdooSequence(
            id=sq["id"], name=sq.get("name"), code=sq.get("code"), prefix=sq.get("prefix"),
        ))

    return smap


def _read_ids(client: OdooRPCClient, model: str, ids: set[int] | None, fields: list[str]) -> list[dict]:
    if not ids:
        return []
    return _clean(client.search_read(model, [["id", "in", list(ids)]], fields))

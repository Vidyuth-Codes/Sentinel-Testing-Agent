"""Data models describing what an Odoo addon developed & configured (the SystemMap).

Populated by `introspect.build_system_map()` (live RPC) and cross-checked against
`addon_scan.scan_addon()` (static source). This is the agent's understanding of
"what we built" — the basis for test-plan generation and gap analysis.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pydantic import BaseModel, Field


class OdooField(BaseModel):
    name: str
    string: str | None = None
    ttype: str | None = None
    required: bool = False
    readonly: bool = False
    store: bool = True
    relation: str | None = None  # target model for relational fields
    related: str | None = None
    compute: bool = False
    help: str | None = None
    owned_by_addon: bool = False  # defined/added by the target addon


class OdooModelInfo(BaseModel):
    model: str  # technical name, e.g. "assetz.asset"
    name: str | None = None
    transient: bool = False
    owned_by_addon: bool = False  # True = new model created by addon; False = extended
    fields: list[OdooField] = Field(default_factory=list)

    @property
    def n_fields(self) -> int:
        return len(self.fields)

    @property
    def n_fields_owned(self) -> int:
        return sum(1 for f in self.fields if f.owned_by_addon)


class OdooView(BaseModel):
    id: int
    name: str | None = None
    model: str | None = None
    type: str | None = None  # form|list|kanban|search|...
    mode: str | None = None  # primary|extension
    inherit_id: int | None = None


class OdooAction(BaseModel):
    id: int
    name: str | None = None
    type: str | None = None  # ir.actions.act_window|server|report|client
    res_model: str | None = None
    view_mode: str | None = None


class OdooMenu(BaseModel):
    id: int
    name: str | None = None
    parent: str | None = None
    action: str | None = None


class OdooAccess(BaseModel):
    name: str | None = None
    model: str | None = None
    group: str | None = None
    read: bool = False
    write: bool = False
    create: bool = False
    unlink: bool = False


class OdooRule(BaseModel):
    name: str | None = None
    model: str | None = None
    groups: list[str] = Field(default_factory=list)
    domain: str | None = None
    read: bool = False
    write: bool = False
    create: bool = False
    unlink: bool = False
    global_rule: bool = False


class OdooCron(BaseModel):
    id: int
    name: str | None = None
    model: str | None = None
    active: bool = True
    interval: str | None = None  # e.g. "1 days"


class OdooAutomation(BaseModel):
    id: int
    name: str | None = None
    model: str | None = None
    trigger: str | None = None


class OdooSequence(BaseModel):
    id: int
    name: str | None = None
    code: str | None = None
    prefix: str | None = None


class SystemMap(BaseModel):
    url: str
    db: str
    module: str
    server_version: str | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    modules_installed: int = 0
    module_depends: list[str] = Field(default_factory=list)

    models: list[OdooModelInfo] = Field(default_factory=list)
    views: list[OdooView] = Field(default_factory=list)
    actions: list[OdooAction] = Field(default_factory=list)
    menus: list[OdooMenu] = Field(default_factory=list)
    access: list[OdooAccess] = Field(default_factory=list)
    rules: list[OdooRule] = Field(default_factory=list)
    crons: list[OdooCron] = Field(default_factory=list)
    automations: list[OdooAutomation] = Field(default_factory=list)
    sequences: list[OdooSequence] = Field(default_factory=list)

    @property
    def owned_models(self) -> list[str]:
        return sorted(m.model for m in self.models if m.owned_by_addon)

    @property
    def extended_models(self) -> list[str]:
        return sorted(m.model for m in self.models if not m.owned_by_addon)

    def counts(self) -> dict[str, int]:
        return {
            "new_models": len(self.owned_models),
            "extended_models": len(self.extended_models),
            "fields_owned": sum(m.n_fields_owned for m in self.models),
            "views": len(self.views),
            "actions": len(self.actions),
            "menus": len(self.menus),
            "access_rules": len(self.access),
            "record_rules": len(self.rules),
            "scheduled_actions": len(self.crons),
            "automations": len(self.automations),
            "sequences": len(self.sequences),
        }

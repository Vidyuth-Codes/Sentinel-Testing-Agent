"""Static scan of an Odoo addon's source on disk.

Complements live introspection: reads the *code* (Python models + manifest +
security CSV + XML view files) so the agent can cross-check "what's coded" vs
"what's deployed", and reason about business logic (compute/constrains/actions)
that RPC introspection alone doesn't reveal.
"""

from __future__ import annotations

import ast
import csv
from pathlib import Path

from pydantic import BaseModel, Field


class AddonMethod(BaseModel):
    name: str
    kind: str  # compute | constrains | onchange | action | api_model | plain
    decorators: list[str] = Field(default_factory=list)


class AddonModelClass(BaseModel):
    class_name: str
    name: str | None = None  # _name
    inherit: list[str] = Field(default_factory=list)  # _inherit
    file: str = ""
    fields: dict[str, str] = Field(default_factory=dict)  # field_name -> ttype (Char/Many2one/...)
    methods: list[AddonMethod] = Field(default_factory=list)
    sql_constraints: int = 0

    @property
    def is_new_model(self) -> bool:
        return bool(self.name)


class AddonScan(BaseModel):
    path: str
    technical_name: str
    manifest_name: str | None = None
    version: str | None = None
    depends: list[str] = Field(default_factory=list)
    python_deps: list[str] = Field(default_factory=list)
    model_classes: list[AddonModelClass] = Field(default_factory=list)
    access_rows: int = 0
    view_files: int = 0
    data_files: int = 0
    wizard_files: int = 0
    report_files: int = 0
    controllers: int = 0

    def counts(self) -> dict[str, int]:
        new_models = sum(1 for m in self.model_classes if m.is_new_model)
        return {
            "model_classes": len(self.model_classes),
            "new_models": new_models,
            "inherited_classes": len(self.model_classes) - new_models,
            "fields_in_code": sum(len(m.fields) for m in self.model_classes),
            "methods": sum(len(m.methods) for m in self.model_classes),
            "compute_methods": sum(1 for m in self.model_classes for x in m.methods if x.kind == "compute"),
            "constraints": sum(1 for m in self.model_classes for x in m.methods if x.kind == "constrains"),
            "action_methods": sum(1 for m in self.model_classes for x in m.methods if x.kind == "action"),
            "access_rows": self.access_rows,
            "view_files": self.view_files,
            "wizard_files": self.wizard_files,
        }


def _str_or_list(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def _decorator_names(node: ast.AST) -> list[str]:
    names = []
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        # api.depends / api.constrains / api.onchange / api.model
        if isinstance(target, ast.Attribute):
            names.append(target.attr)
        elif isinstance(target, ast.Name):
            names.append(target.id)
    return names


def _classify_method(name: str, decorators: list[str]) -> str:
    if "depends" in decorators or name.startswith("_compute_"):
        return "compute"
    if "constrains" in decorators:
        return "constrains"
    if "onchange" in decorators:
        return "onchange"
    if name.startswith("action_") or name.startswith("button_"):
        return "action"
    if "model" in decorators or "model_create_multi" in decorators:
        return "api_model"
    return "plain"


def _scan_python_file(path: Path, root: Path) -> list[AddonModelClass]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except (OSError, SyntaxError):
        return []
    classes: list[AddonModelClass] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        cls = AddonModelClass(class_name=node.name, file=str(path.relative_to(root)))
        for stmt in node.body:
            # _name / _inherit
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                tgt = stmt.targets[0].id
                if tgt == "_name":
                    vals = _str_or_list(stmt.value)
                    cls.name = vals[0] if vals else None
                elif tgt == "_inherit":
                    cls.inherit = _str_or_list(stmt.value)
                elif tgt == "_sql_constraints" and isinstance(stmt.value, (ast.List, ast.Tuple)):
                    cls.sql_constraints = len(stmt.value.elts)
                # field assignment: x = fields.Type(...)
                elif isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Attribute):
                    f = stmt.value.func
                    if isinstance(f.value, ast.Name) and f.value.id == "fields":
                        cls.fields[tgt] = f.attr
            # methods
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decs = _decorator_names(stmt)
                cls.methods.append(AddonMethod(
                    name=stmt.name, kind=_classify_method(stmt.name, decs), decorators=decs,
                ))
        # only keep classes that look like Odoo models (have _name or _inherit or fields)
        if cls.name or cls.inherit or cls.fields:
            classes.append(cls)
    return classes


def _read_manifest(addon_root: Path) -> dict:
    mf = addon_root / "__manifest__.py"
    if not mf.exists():
        return {}
    try:
        return ast.literal_eval(mf.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, SyntaxError):
        return {}


def scan_addon(addon_path: str | Path) -> AddonScan:
    root = Path(addon_path).resolve()
    manifest = _read_manifest(root)

    scan = AddonScan(
        path=str(root),
        technical_name=root.name,
        manifest_name=manifest.get("name"),
        version=manifest.get("version"),
        depends=list(manifest.get("depends", [])),
        python_deps=list((manifest.get("external_dependencies", {}) or {}).get("python", [])),
    )

    for py in sorted(root.rglob("*.py")):
        if "__pycache__" in py.parts or py.name in {"__init__.py", "__manifest__.py"}:
            continue
        scan.model_classes.extend(_scan_python_file(py, root))

    # security access rows
    access_csv = root / "security" / "ir.model.access.csv"
    if access_csv.exists():
        try:
            with access_csv.open(encoding="utf-8", errors="replace", newline="") as fh:
                scan.access_rows = max(0, sum(1 for _ in csv.reader(fh)) - 1)  # minus header
        except OSError:
            pass

    scan.view_files = len(list((root / "views").glob("*.xml"))) if (root / "views").exists() else 0
    scan.data_files = len(list((root / "data").glob("*.xml"))) if (root / "data").exists() else 0
    scan.wizard_files = len(list((root / "wizard").glob("*.py"))) if (root / "wizard").exists() else 0
    scan.report_files = len(list((root / "report").glob("*.xml"))) if (root / "report").exists() else 0
    scan.controllers = len(list((root / "controllers").glob("*.py"))) if (root / "controllers").exists() else 0
    return scan

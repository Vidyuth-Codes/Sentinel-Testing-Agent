"""Instance-wide scan: what has been *custom-built* for this client.

For a heavily-customised deployment (vs a single addon), the useful first view is "which of the
installed modules are the client's own developments, not standard Odoo/OCA". We read
`ir.module.module` and split modules by **author** — official Odoo and OCA modules have known
authors; everything else is treated as a custom/partner development. Read-only.

From the list a user can then pick one module and run the normal single-module Understand.
"""

from __future__ import annotations

import re

from sentinel.odoo.rpc import OdooRPCClient

# Authors that mark a module as official Odoo / community (OCA).
_STD_AUTHOR_RX = re.compile(r"odoo|openerp|\boca\b|community association", re.I)


def _is_official(author: str, version: str) -> bool:
    """Core Odoo modules have an Odoo author AND a 4-part `series.x.y` version (e.g. 18.0.1.3).
    Custom/partner modules often FAKE the author as 'Odoo S.A.', but the module scaffold leaves a
    5-part version (e.g. 18.0.1.0.5) — so we require BOTH signals to call a module standard.
    """
    std_author = bool(author and _STD_AUTHOR_RX.search(author))
    four_part = version.count(".") == 3   # "18.0.1.3" → 3 dots → 4 segments
    return std_author and four_part


def scan_deployment(client: OdooRPCClient) -> dict:
    """List installed modules, split into custom/non-standard vs core Odoo.

    Custom = anything not clearly core Odoo (partner modules — even those mislabelled with an Odoo
    author — plus OCA/third-party). The `author` field in each entry lets the caller tell partner
    modules from OCA ones.
    """
    rows = client.search_read(
        "ir.module.module", [["state", "=", "installed"]],
        ["name", "shortdesc", "author", "installed_version", "application", "license"],
        order="name",
    )
    custom, standard = [], []
    for m in rows:
        author = (m.get("author") or "").strip()
        version = (m.get("installed_version") or "").strip()
        entry = {
            "name": m["name"],
            "summary": m.get("shortdesc") or "",
            "author": author or "(no author)",
            "version": version,
            "application": bool(m.get("application")),
        }
        (standard if _is_official(author, version) else custom).append(entry)
    return {"total": len(rows), "custom": custom, "standard_count": len(standard)}


def render_deployment(scan: dict, *, max_modules: int = 80) -> str:
    """Compact text of the custom modules for the engine to summarise."""
    custom = scan["custom"][:max_modules]
    lines = [
        f"CUSTOM / NON-STANDARD MODULES: {len(scan['custom'])} of {scan['total']} installed "
        f"({scan['standard_count']} are standard Odoo/OCA).",
        "",
    ]
    for m in custom:
        tag = " [APP]" if m["application"] else ""
        summary = f" — {m['summary']}" if m["summary"] else ""
        lines.append(f"- {m['name']}{tag}{summary}  (author: {m['author']}, v{m['version']})")
    return "\n".join(lines)

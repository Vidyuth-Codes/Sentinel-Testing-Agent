"""Odoo integration layer for Sentinel.

- `rpc.OdooRPCClient`  — authenticate + execute_kw against a live Odoo instance
- `introspect`         — build a SystemMap of what an addon developed/configured
- `addon_scan`         — static scan of the addon source on disk
- `schema`             — SystemMap / OdooModelInfo / OdooField ... data models
"""

from sentinel.odoo.rpc import OdooAuthError, OdooRPCClient
from sentinel.odoo.introspect import build_system_map
from sentinel.odoo.addon_scan import scan_addon
from sentinel.odoo.context import summarize_system_map

__all__ = [
    "OdooRPCClient",
    "OdooAuthError",
    "build_system_map",
    "scan_addon",
    "summarize_system_map",
]

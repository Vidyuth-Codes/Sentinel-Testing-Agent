"""Provision a safe database for the RPC flow executor.

Default: clone the source DB via Odoo's `db` service into a throwaway copy, run against
that, then drop it. The source (often production) is never written to. Running against an
existing DB is allowed only with explicit opt-in (`use_existing=True`) — the caller is
responsible for that being a disposable database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from sentinel.odoo.rpc import OdooDbAdmin, OdooRPCError


@dataclass
class Provisioned:
    db: str  # the database to execute against
    source_db: str
    cloned: bool
    _admin: OdooDbAdmin | None = None


def master_password(explicit: str | None = None) -> str | None:
    return explicit or os.environ.get("SENTINEL_ODOO_MASTER")


def provision(
    url: str, source_db: str, *, use_existing: bool, master_pw: str | None, stamp: str,
) -> Provisioned:
    if use_existing:
        return Provisioned(db=source_db, source_db=source_db, cloned=False)

    if not master_pw:
        raise OdooRPCError(
            "cloning needs the Odoo master password — pass --master-pw or set "
            "SENTINEL_ODOO_MASTER (or run with --use-existing-db to target a disposable DB directly)."
        )
    admin = OdooDbAdmin(url, master_pw)
    target = f"{source_db}_sentinel_{stamp}"
    if admin.exists(target):
        raise OdooRPCError(f"clone target '{target}' already exists — drop it or retry.")
    admin.duplicate(source_db, target)  # may take a few seconds
    return Provisioned(db=target, source_db=source_db, cloned=True, _admin=admin)


def teardown(prov: Provisioned, *, keep: bool) -> str | None:
    """Drop the clone unless told to keep it. Returns a status string for logging."""
    if not prov.cloned:
        return None
    if keep:
        return f"kept clone '{prov.db}'"
    if prov._admin:
        prov._admin.drop(prov.db)
        return f"dropped clone '{prov.db}'"
    return None

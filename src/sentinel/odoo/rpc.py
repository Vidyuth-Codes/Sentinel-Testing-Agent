"""Thin XML-RPC client for Odoo (External API).

Uses the stdlib `xmlrpc.client` against Odoo's `/xmlrpc/2/common` and
`/xmlrpc/2/object` endpoints — no extra dependencies. The `password` may be a
real user password *or* an Odoo API key (Odoo 14+), they're interchangeable here.

Introspection (Phase 1) is read-only (search_read / fields_get). Write helpers
(create / write / unlink) and the `db` admin service (duplicate / drop) are used by
the Phase 3 RPC flow executor, which only ever runs against a throwaway/cloned
database (see `sentinel.execute`).
"""

from __future__ import annotations

import re
import ssl
import xmlrpc.client
from typing import Any
from urllib.parse import urlparse


def _ssl_context(verify_ssl: bool):
    """Return an SSL context. When verify_ssl is False, skip cert + hostname checks —
    needed for staging/dev instances (e.g. *.dev.odoo.com) whose TLS cert doesn't match
    the host. Only meaningful for https URLs; ignored for http (localhost)."""
    if verify_ssl:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _base_url(url: str) -> str:
    """Normalise a user-entered URL to the XML-RPC base (scheme://host[:port]).

    Odoo's XML-RPC endpoints live at the domain ROOT (`/xmlrpc/2/...`), so we drop any
    path the user pasted — e.g. the web-client path `/odoo` or `/web`. A missing scheme
    is added (http for localhost, https otherwise).
    """
    u = (url or "").strip()
    if "://" not in u:
        local = u.startswith(("localhost", "127.0.0.1"))
        u = ("http://" if local else "https://") + u
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}" if p.netloc else u.rstrip("/")


class OdooAuthError(Exception):
    """Authentication against the Odoo instance failed."""


class OdooRPCError(Exception):
    """An RPC call returned a fault."""


def _short_fault(fault_string: str) -> str:
    """Odoo faults often carry a full Python traceback; surface the meaningful last line
    (the UserError/ValidationError message) rather than the whole stack."""
    lines = [ln for ln in (fault_string or "").splitlines() if ln.strip()]
    if not lines:
        return fault_string
    for ln in reversed(lines):
        if re.search(r"(Error|Warning|Exception):", ln):
            return ln.strip()
    return lines[-1].strip()


class OdooRPCClient:
    def __init__(self, url: str, db: str, username: str, password: str, *, verify_ssl: bool = True):
        self.url = _base_url(url)
        self.db = db
        self.username = username
        self.password = password
        self.uid: int | None = None
        ctx = _ssl_context(verify_ssl)
        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True, context=ctx)
        self._object = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True, context=ctx)

    # --- connection -------------------------------------------------------

    def version(self) -> dict:
        try:
            return self._common.version()
        except (xmlrpc.client.Error, OSError) as exc:
            raise OdooRPCError(
                f"could not reach Odoo's XML-RPC API at {self.url}/xmlrpc/2/common — {exc}. "
                "Check the URL is the instance base (no /odoo or /web path) and that XML-RPC is enabled."
            ) from exc

    def authenticate(self) -> int:
        try:
            uid = self._common.authenticate(self.db, self.username, self.password, {})
        except xmlrpc.client.Fault as exc:
            raise OdooRPCError(f"authentication failed: {_short_fault(exc.faultString)}") from exc
        except (xmlrpc.client.Error, OSError) as exc:
            raise OdooRPCError(f"authentication request failed: {exc}") from exc
        if not uid:
            raise OdooAuthError(
                f"login failed for user '{self.username}' on db '{self.db}' "
                f"(check credentials / API key)"
            )
        self.uid = uid
        return uid

    # --- generic execute --------------------------------------------------

    def execute_kw(
        self,
        model: str,
        method: str,
        args: list | None = None,
        kwargs: dict | None = None,
    ) -> Any:
        if self.uid is None:
            self.authenticate()
        try:
            return self._object.execute_kw(
                self.db, self.uid, self.password, model, method, args or [], kwargs or {}
            )
        except xmlrpc.client.Fault as exc:
            raise OdooRPCError(f"{model}.{method} failed: {_short_fault(exc.faultString)}") from exc
        except (xmlrpc.client.ProtocolError, OSError) as exc:
            raise OdooRPCError(f"{model}.{method} transport error: {exc}") from exc

    # --- convenience reads ------------------------------------------------

    def search_read(
        self,
        model: str,
        domain: list | None = None,
        fields: list[str] | None = None,
        limit: int | None = None,
        order: str | None = None,
    ) -> list[dict]:
        kwargs: dict[str, Any] = {}
        if fields is not None:
            kwargs["fields"] = fields
        if limit is not None:
            kwargs["limit"] = limit
        if order is not None:
            kwargs["order"] = order
        return self.execute_kw(model, "search_read", [domain or []], kwargs)

    def fields_get(self, model: str, attributes: list[str] | None = None) -> dict:
        kwargs = {"attributes": attributes} if attributes else {}
        return self.execute_kw(model, "fields_get", [], kwargs)

    def read(self, model: str, ids: list[int], fields: list[str] | None = None) -> list[dict]:
        kwargs = {"fields": fields} if fields else {}
        return self.execute_kw(model, "read", [ids], kwargs)

    def search(self, model: str, domain: list | None = None, limit: int | None = None) -> list[int]:
        kwargs = {"limit": limit} if limit is not None else {}
        return self.execute_kw(model, "search", [domain or []], kwargs)

    # --- writes (Phase 3 — used only against a throwaway/cloned DB) --------

    def create(self, model: str, values: dict) -> int:
        return self.execute_kw(model, "create", [values])

    def write(self, model: str, ids: list[int], values: dict) -> bool:
        return self.execute_kw(model, "write", [ids, values])

    def unlink(self, model: str, ids: list[int]) -> bool:
        return self.execute_kw(model, "unlink", [ids])


class OdooDbAdmin:
    """Wraps Odoo's `db` service (`/xmlrpc/2/db`) for duplicate / drop / list.

    Requires the instance master password (the `admin_passwd` in odoo.conf), which
    is separate from any user login. Used only to provision a throwaway test DB.
    """

    def __init__(self, url: str, master_pw: str, *, verify_ssl: bool = True):
        self.url = _base_url(url)
        self.master_pw = master_pw
        self._db = xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/db", allow_none=True, context=_ssl_context(verify_ssl)
        )

    def list(self) -> list[str]:
        try:
            return self._db.list()
        except (xmlrpc.client.Error, OSError) as exc:
            raise OdooRPCError(f"could not list databases: {exc}") from exc

    def exists(self, name: str) -> bool:
        return name in self.list()

    def duplicate(self, source: str, target: str) -> None:
        try:
            self._db.duplicate_database(self.master_pw, source, target)
        except xmlrpc.client.Fault as exc:
            raise OdooRPCError(f"could not duplicate '{source}' -> '{target}': {exc.faultString}") from exc

    def drop(self, name: str) -> None:
        try:
            self._db.drop(self.master_pw, name)
        except xmlrpc.client.Fault as exc:
            raise OdooRPCError(f"could not drop '{name}': {exc.faultString}") from exc

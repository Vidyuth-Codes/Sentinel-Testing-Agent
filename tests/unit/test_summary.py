"""Smoke tests for the deterministic Odoo layer: SystemMap counts + the LLM brief."""

from sentinel.odoo.context import summarize_system_map
from sentinel.odoo.schema import OdooField, OdooModelInfo, OdooCron, SystemMap


def _sample_map() -> SystemMap:
    asset = OdooModelInfo(
        model="assetz.asset",
        name="Asset",
        owned_by_addon=True,
        fields=[
            OdooField(name="name", ttype="char", required=True, owned_by_addon=True),
            OdooField(name="state", ttype="selection", owned_by_addon=True),
            OdooField(name="value", ttype="float", owned_by_addon=True),
            OdooField(name="create_date", ttype="datetime"),  # noise — should be filtered
        ],
    )
    partner_ext = OdooModelInfo(
        model="res.partner",
        name="Contact",
        owned_by_addon=False,
        fields=[OdooField(name="asset_count", ttype="integer", owned_by_addon=True)],
    )
    return SystemMap(
        url="http://localhost:8069",
        db="assetz_db",
        module="assetz",
        server_version="18.0",
        models=[asset, partner_ext],
        crons=[OdooCron(id=1, name="Depreciate", model="assetz.asset", interval="1 days")],
    )


def test_scan_deployment_splits_custom_vs_standard():
    from sentinel.odoo.deployment import render_deployment, scan_deployment

    class Fake:
        def search_read(self, model, domain, fields, order=None, limit=None):
            return [
                {"name": "sale", "shortdesc": "Sales", "author": "Odoo S.A.", "installed_version": "18.0.1.2", "application": True},
                {"name": "account_accountant", "shortdesc": "Accounting", "author": "Odoo S.A.", "installed_version": "18.0.1.1", "application": True},
                # partner module FAKING an Odoo author, but with the tell-tale 5-part version → custom
                {"name": "invoice_approval", "shortdesc": "Invoice Approval", "author": "Odoo S.A.", "installed_version": "18.0.1.0.5", "application": False},
                {"name": "stock_account_customisation", "shortdesc": "Stock cust", "author": "Odoo S.A.", "installed_version": "18.0.1.0.15", "application": False},
                # non-Odoo author, even with a 4-part version → custom
                {"name": "studio_customization", "shortdesc": "Studio", "author": "Lighting Division", "installed_version": "18.0.1.0", "application": False},
            ]

    s = scan_deployment(Fake())
    names = [m["name"] for m in s["custom"]]
    assert s["total"] == 5 and s["standard_count"] == 2
    assert "invoice_approval" in names and "stock_account_customisation" in names   # 5-part version → custom
    assert "studio_customization" in names                                          # non-Odoo author → custom
    assert "sale" not in names and "account_accountant" not in names                # Odoo + 4-part → standard
    txt = render_deployment(s)
    assert "invoice_approval" in txt and "CUSTOM" in txt


def test_base_url_normalisation():
    from sentinel.odoo.rpc import _base_url
    assert _base_url("https://x.dev.odoo.com/odoo") == "https://x.dev.odoo.com"
    assert _base_url("https://x.dev.odoo.com/odoo/") == "https://x.dev.odoo.com"
    assert _base_url("http://localhost:8069") == "http://localhost:8069"
    assert _base_url("http://localhost:8069/web") == "http://localhost:8069"
    assert _base_url("x.dev.odoo.com") == "https://x.dev.odoo.com"   # scheme added (remote → https)
    assert _base_url("localhost:8069") == "http://localhost:8069"     # localhost → http


def test_counts_split_new_vs_extended():
    smap = _sample_map()
    c = smap.counts()
    assert c["new_models"] == 1
    assert c["extended_models"] == 1
    assert c["fields_owned"] == 4  # 3 on the new model + 1 added to res.partner
    assert smap.owned_models == ["assetz.asset"]
    assert smap.extended_models == ["res.partner"]


def test_summary_brief_is_grounded_and_denoised():
    brief = summarize_system_map(_sample_map())
    assert "ODOO MODULE: assetz" in brief
    assert "assetz.asset" in brief
    assert "WORKFLOW:state" in brief          # state field detected as the workflow field
    assert "create_date" not in brief         # noise field filtered out
    assert "res.partner: +asset_count" in brief
    assert "Depreciate" in brief              # scheduled action surfaced

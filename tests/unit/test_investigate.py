"""Tests for the per-record investigation helpers (no live instance)."""

from sentinel.odoo.investigate import (
    _has_value, build_investigation_system, extract_references, render_graph,
)


def test_extract_references():
    assert extract_references("why does S00437 still show 0 delivered?") == ["S00437"]
    refs = extract_references("INV/2026/00010 and WH/OUT/00032 both look wrong")
    assert "INV/2026/00010" in refs and "WH/OUT/00032" in refs
    assert extract_references("the delivery did not update the order") == []   # no reference token


def test_resolve_flow_maps_keywords_to_models():
    from sentinel.odoo.investigate import resolve_flow
    assert resolve_flow("explain the flow of bills")[0] == "account.move"
    assert resolve_flow("explain the flow of bills")[1] == [["move_type", "=", "in_invoice"]]
    assert resolve_flow("sales order processing flow")[0] == "sale.order"
    assert resolve_flow("how do deliveries work")[0] == "stock.picking"
    assert resolve_flow("explain the asset request workflow") is None   # not a mapped business object


def test_build_flow_system_switches_on_examples():
    from sentinel.odoo.investigate import build_flow_system
    s_has = build_flow_system("Vendor Bills", "FLOW: ...\nCOUNTS...", has_examples=True)
    s_none = build_flow_system("Vendor Bills", "FLOW: ...", has_examples=False)
    assert "REAL records" in s_has
    assert "NO records" in s_none and "illustrative" in s_none


def test_or_domain_searches_multiple_reference_fields():
    from sentinel.odoo.investigate import _or_domain
    assert _or_domain(["name"], "=", "X") == [["name", "=", "X"]]
    assert _or_domain(["name", "ref"], "=", "X") == ["|", ["name", "=", "X"], ["ref", "=", "X"]]
    assert _or_domain(["name", "ref", "origin"], "ilike", "X") == [
        "|", "|", ["name", "ilike", "X"], ["ref", "ilike", "X"], ["origin", "ilike", "X"]]


def test_narrow_by_question_prefers_named_doc_type():
    from sentinel.odoo.investigate import narrow_by_question
    matches = [
        {"model": "purchase.order", "label": "Purchase Order", "id": 1, "name": "P00441"},
        {"model": "account.move", "label": "Bill", "id": 2, "name": "Draft Bill"},
    ]
    assert narrow_by_question("explain this vendor bill", matches)[0]["model"] == "account.move"
    assert narrow_by_question("explain the purchase order", matches)[0]["model"] == "purchase.order"
    assert len(narrow_by_question("explain this", matches)) == 2          # no hint → leave both
    assert len(narrow_by_question("x", matches[:1])) == 1                 # single match → unchanged


def test_has_value_keeps_zero():
    assert _has_value(0) and _has_value(0.0)        # a delivered-qty of 0 must be shown
    assert not _has_value(False) and not _has_value(None) and not _has_value("")


def test_render_graph_shows_state_zero_qty_timeline_and_chatter():
    g = {
        "model": "sale.order", "id": 1, "name": "S001",
        "scalars": {"Status": "sale", "Delivered": "0.0"},
        "related": [{"label": "Order Lines", "relation": "sale.order.line", "count": 1,
                     "rows": [{"id": 5, "display_name": "Widget", "qty_delivered": 0.0}]}],
        "tracking": [{"field_desc": "Status", "old_value_char": "draft", "new_value_char": "sale"}],
        "messages": [{"date": "2026-01-01", "author_id": [9, "Alice"], "body": "<p>Confirmed</p>"}],
    }
    txt = render_graph(g)
    assert "S001" in txt and "Status: sale" in txt
    assert "Order Lines" in txt and "qty_delivered=0.0" in txt   # the 0 is preserved
    assert "Status: draft → sale" in txt                          # tracking timeline
    assert "Alice" in txt and "Confirmed" in txt                  # chatter, html-stripped


def test_build_investigation_system_caps_data():
    s = build_investigation_system("x" * 20000, max_chars=5000)
    assert "data truncated" in s and len(s) < 8000

"""Per-record live-data investigation (read-only).

Given a human reference like ``S00437`` (or an explicit ``model``/``id``), pull the record's
current field values, its **chatter** (`mail.message`) and **field-change history**
(`mail.tracking.value`), and the key related records one hop away (lines, deliveries, invoices,
payments…). The result is rendered to a compact text bundle that Claude Code reasons over to
answer "what happened to this record / why is it in this state" — no source code needed.

Everything here is `search_read`/`read` only — it never writes.
"""

from __future__ import annotations

import re

from sentinel.odoo.rpc import OdooRPCClient, OdooRPCError

# Business documents users typically ask about, with the fields a reference might live in.
# (Users often paste the *Reference* — e.g. a bill's `ref` — not the internal Number/`name`,
#  especially for drafts whose `name` is still "/".)
CANDIDATE_MODELS: list[tuple[str, str, list[str]]] = [
    ("sale.order", "Sales Order", ["name", "client_order_ref"]),
    ("purchase.order", "Purchase Order", ["name", "partner_ref"]),
    ("account.move", "Invoice / Bill / Journal Entry", ["name", "ref", "payment_reference"]),
    ("stock.picking", "Transfer / Delivery", ["name", "origin"]),
    ("account.payment", "Payment", ["name", "ref"]),
    ("mrp.production", "Manufacturing Order", ["name", "origin"]),
    ("repair.order", "Repair Order", ["name"]),
    ("project.task", "Task", ["name"]),
    ("crm.lead", "Lead / Opportunity", ["name"]),
    ("helpdesk.ticket", "Helpdesk Ticket", ["name"]),
    ("purchase.requisition", "Purchase Agreement", ["name"]),
    ("stock.move", "Stock Move", ["reference", "origin"]),
]


def _or_domain(fields: list[str], op: str, token: str) -> list:
    """Build an Odoo OR domain over several fields: e.g. ['|', ['name','=',X], ['ref','=',X]]."""
    terms = [[f, op, token] for f in fields]
    return (["|"] * (len(terms) - 1)) + terms if len(terms) > 1 else terms


# When one reference matches several doc types, the user's wording usually says which they mean.
_MODEL_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("vendor bill", "bill", "invoice", "credit note", "journal entry", "journal"), "account.move"),
    (("purchase order", "purchase", " po "), "purchase.order"),
    (("sales order", "sale order", "quotation", "sales"), "sale.order"),
    (("delivery", "transfer", "picking", "shipment", "receipt"), "stock.picking"),
    (("payment",), "account.payment"),
    (("manufacturing", "production", " mo "), "mrp.production"),
]


def narrow_by_question(question: str, matches: list[dict]) -> list[dict]:
    """If a reference matched several record types, prefer the one the question names."""
    if len(matches) <= 1:
        return matches
    q = f" {(question or '').lower()} "
    for keywords, model in _MODEL_HINTS:
        if any(k in q for k in keywords):
            sub = [m for m in matches if m["model"] == model]
            if sub:
                return sub
    return matches

_NOISE_FIELDS = {
    "__last_update", "display_name", "create_uid", "write_uid", "message_ids",
    "message_follower_ids", "message_partner_ids", "activity_ids", "website_message_ids",
    "message_main_attachment_id", "access_token", "access_url", "my_activity_date_deadline",
}
_SCALAR_TYPES = {"char", "text", "selection", "integer", "float", "monetary", "boolean",
                 "date", "datetime", "many2one"}
# Technical/computed/widget fields that add noise to a functional diagnosis.
_NOISE_RX = re.compile(
    r"(^has_|_count$|^activity_|^message_|^website_|^my_activity|json|popover|^access_|"
    r"format$|^is_.*_available$|^show_.*button$|_html$|qr_code|^rating_)", re.I)
_HTML_TAG = re.compile(r"<[^>]+>")
# Match document references in free text: S01671, INV/2026/00010, WH/OUT/00032, F2/OUT/02716, PO00045…
# A letter-led token of [A-Z0-9] groups joined by / _ - ; the digit requirement is applied below.
_REF_RX = re.compile(r"\b([A-Z][A-Z0-9]*(?:[/_-][A-Z0-9]+)*)\b")


def extract_references(text: str) -> list[str]:
    """Pull likely Odoo document references out of a free-text question."""
    seen, out = set(), []
    for tok in _REF_RX.findall(text or ""):
        if any(ch.isdigit() for ch in tok) and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


INVESTIGATE_SYSTEM = (
    "You are Sentinel, an Odoo support analyst helping a FUNCTIONAL user understand what happened to "
    "a specific record. You are given that record's LIVE DATA below — current values, related "
    "documents, field-change history, and chatter. Answer the user's question using ONLY this data; "
    "never invent records, values, or events. Write in plain language for a non-developer:\n"
    "1. **Answer** — directly answer their question in 1–2 sentences.\n"
    "2. **What happened** — a short timeline from the change history / chatter (who did what, when), "
    "when it's relevant.\n"
    "3. **Why** — the root cause, tied to the actual data (e.g. a delivery whose move isn't linked to "
    "the order line; a status that blocks the next step; a quantity that never propagated).\n"
    "4. **The records involved** — name the specific related documents / links.\n"
    "5. **What to do / check** — concrete next steps for the user.\n"
    "If the data isn't enough to be sure, say exactly which extra record or field you'd need to see. "
    "Do NOT mention source code or file:line — this is a live-data investigation."
)


def build_investigation_system(graph_text: str, max_chars: int = 16000) -> str:
    data = graph_text if len(graph_text) <= max_chars else graph_text[:max_chars] + "\n…[data truncated]"
    return INVESTIGATE_SYSTEM + "\n\n# LIVE RECORD DATA\n" + data


# --- flow explanation grounded in real records -------------------------------

# A flow keyword → (model, extra domain, label). Checked in order; first match wins.
_FLOW_MAP: list[tuple[tuple[str, ...], str, list, str]] = [
    (("vendor bill", "vendor bills", "supplier invoice", "bill", "bills"),
     "account.move", [["move_type", "=", "in_invoice"]], "Vendor Bills"),
    (("customer invoice", "customer invoices", "invoice", "invoices"),
     "account.move", [["move_type", "=", "out_invoice"]], "Customer Invoices"),
    (("credit note", "credit notes", "refund"),
     "account.move", [["move_type", "in", ["out_refund", "in_refund"]]], "Credit Notes"),
    (("sales order", "sale order", "sales orders", "quotation", "quotations", "sales"),
     "sale.order", [], "Sales Orders"),
    (("purchase order", "purchase orders", "rfq", "request for quotation", "purchase"),
     "purchase.order", [], "Purchase Orders"),
    (("delivery", "deliveries", "transfer", "transfers", "picking", "shipment", "receipt"),
     "stock.picking", [], "Deliveries / Transfers"),
    (("payment", "payments"), "account.payment", [], "Payments"),
    (("manufacturing", "production order", "manufacturing order"), "mrp.production", [], "Manufacturing Orders"),
]


def resolve_flow(question: str) -> tuple[str, list, str] | None:
    """Map a free-text flow question to (model, domain, label), e.g. 'flow of bills' → account.move bills."""
    q = f" {(question or '').lower()} "
    for keywords, model, domain, label in _FLOW_MAP:
        if any(k in q for k in keywords):
            return model, domain, label
    return None


def fetch_flow_examples(client: OdooRPCClient, model: str, domain: list, label: str,
                        *, max_samples: int = 8) -> dict:
    """Real records to ground a flow explanation: counts per state + recent samples + one detailed example."""
    counts: dict[str, int] = {}
    try:
        for g in client.execute_kw(model, "read_group", [domain, ["id"], ["state"]]):
            counts[g.get("state") or "?"] = g.get("__count") or g.get("state_count") or 0
    except OdooRPCError:
        pass
    fields = _existing_fields(client, model, [
        "display_name", "state", "payment_state", "partner_id", "amount_total",
        "invoice_date", "date", "date_order", "scheduled_date", "create_date"])
    samples = []
    try:
        samples = client.search_read(model, domain, fields or ["display_name"],
                                     limit=max_samples, order="create_date desc")
    except OdooRPCError:
        pass
    detail = None
    if samples:
        try:
            detail = fetch_record_graph(client, model, samples[0]["id"])
        except OdooRPCError:
            detail = None
    return {"label": label, "model": model, "counts": counts, "samples": samples,
            "detail": detail, "total": sum(counts.values()) or len(samples)}


def render_flow_examples(ex: dict) -> str:
    lines = [f"FLOW: {ex['label']}  ({ex['model']})", ""]
    if ex["counts"]:
        lines.append("COUNTS BY STATE: " + ", ".join(f"{k}: {v}" for k, v in ex["counts"].items()))
    if ex["samples"]:
        lines += ["", "RECENT EXAMPLE RECORDS:"]
        for s in ex["samples"]:
            bits = [f"{k}={s[k][1] if isinstance(s[k], (list, tuple)) and len(s[k]) > 1 else s[k]}"
                    for k in s if k != "id" and _has_value(s.get(k))]
            lines.append("  - " + ", ".join(bits))
    if ex["detail"]:
        lines += ["", "DETAILED EXAMPLE (most recent):", render_graph(ex["detail"])]
    return "\n".join(lines)


def build_flow_system(label: str, examples_text: str, has_examples: bool, max_chars: int = 15000) -> str:
    intro = (
        f"You are explaining the '{label}' flow in THIS Odoo instance to a functional (non-developer) "
        "user. Walk the happy path step by step in plain language (a short numbered sequence). "
    )
    if has_examples:
        intro += (
            "Ground each step in the REAL records below: cite example record names and how many are at each "
            "stage (e.g. 'N are still in Draft, like JX…'), and use the detailed example to illustrate one "
            "record's journey. "
        )
    else:
        intro += (
            "There are NO records of this type in the instance yet, so explain the flow with ONE clear, "
            "realistic example you invent — and say explicitly that it's illustrative, not live data. "
        )
    intro += ("Note where behaviour may be customised on this instance (would need the source or a specific "
              "record to confirm). Do NOT reference code or file:line.\n\n# LIVE DATA\n")
    data = examples_text if len(examples_text) <= max_chars else examples_text[:max_chars] + "\n…[truncated]"
    return intro + data


def _has_value(v) -> bool:
    """Truthy-ish, but KEEPS 0 / 0.0 (a delivered-qty of 0 is exactly what users ask about).
    Drops only None, False, '' and empty collections."""
    return v is not None and v is not False and v != "" and v != []


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    txt = _HTML_TAG.sub(" ", s)
    return re.sub(r"\s+", " ", txt).strip()


def _existing_fields(client: OdooRPCClient, model: str, wanted: list[str]) -> list[str]:
    """Intersect a wish-list with the model's real fields (schemas vary across Odoo versions)."""
    try:
        meta = client.fields_get(model, ["type"])
    except OdooRPCError:
        return []
    return [f for f in wanted if f in meta]


def resolve_record(client: OdooRPCClient, token: str) -> list[dict]:
    """Find business records whose reference matches `token`. Exact `name` matches win;
    falls back to a fuzzy match. Returns [{model, label, id, name}]."""
    token = (token or "").strip()
    if not token:
        return []
    exact, fuzzy = [], []
    for model, label, ref_fields in CANDIDATE_MODELS:
        try:
            rows = client.search_read(model, _or_domain(ref_fields, "=", token),
                                      ["id", "display_name"], limit=5)
            for r in rows:
                exact.append({"model": model, "label": label, "id": r["id"],
                              "name": r.get("display_name") or token})
            if not rows:
                rows = client.search_read(model, _or_domain(ref_fields, "ilike", token),
                                          ["id", "display_name"], limit=3)
                for r in rows:
                    fuzzy.append({"model": model, "label": label, "id": r["id"],
                                  "name": r.get("display_name") or token})
        except OdooRPCError:
            continue  # model not installed / field absent / not readable — skip
    return exact or fuzzy


def fetch_record_graph(client: OdooRPCClient, model: str, rec_id: int,
                       *, max_related_rows: int = 12, max_messages: int = 40) -> dict:
    """Pull the record's current state + related rows (1 hop) + chatter + tracking history."""
    meta = client.fields_get(model, ["string", "type", "relation"])
    rec = client.read(model, [rec_id])[0]

    scalars: dict[str, str] = {}
    relations: list[dict] = []  # x2many fields → expand one hop
    for fname, val in rec.items():
        if fname in _NOISE_FIELDS or fname == "id":
            continue
        info = meta.get(fname, {})
        ftype = info.get("type")
        if ftype in ("one2many", "many2many"):
            if val and not _NOISE_RX.search(fname):
                relations.append({"field": fname, "label": info.get("string") or fname,
                                  "relation": info.get("relation"), "ids": val})
        elif ftype in _SCALAR_TYPES and _has_value(val) and not _NOISE_RX.search(fname):
            if ftype == "many2one":
                scalars[info.get("string") or fname] = (val[1] if isinstance(val, (list, tuple)) and len(val) > 1 else str(val))
            else:
                scalars[info.get("string") or fname] = str(val)

    # related rows one hop (display_name + a couple of state-ish fields if present)
    related_out: list[dict] = []
    for r in relations:
        rel_model, ids = r.get("relation"), r["ids"][:max_related_rows]
        rows = []
        if rel_model and ids:
            wanted = _existing_fields(client, rel_model,
                                      ["display_name", "state", "product_id", "product_uom_qty",
                                       "quantity", "qty_delivered", "qty_invoiced", "amount_total",
                                       "price_subtotal", "move_type", "sale_line_id", "purchase_line_id",
                                       "picking_type_id", "date", "date_done", "parent_state"])
            try:
                rows = client.search_read(rel_model, [["id", "in", ids]], wanted or ["display_name"])
            except OdooRPCError:
                rows = []
        related_out.append({"label": r["label"], "relation": rel_model,
                            "count": len(r["ids"]), "rows": rows})

    # chatter
    msgs = []
    try:
        msgs = client.search_read(
            "mail.message", [["model", "=", model], ["res_id", "=", rec_id]],
            ["date", "author_id", "message_type", "subtype_id", "body", "tracking_value_ids"],
            order="date asc", limit=max_messages,
        )
    except OdooRPCError:
        pass

    # tracking values (field changes) referenced by those messages
    tv_ids = [tid for m in msgs for tid in (m.get("tracking_value_ids") or [])]
    tracking: list[dict] = []
    if tv_ids:
        tv_fields = _existing_fields(
            client, "mail.tracking.value",
            ["mail_message_id", "field_id", "field", "field_desc",
             "old_value_char", "new_value_char", "old_value_integer", "new_value_integer",
             "old_value_float", "new_value_float", "old_value_datetime", "new_value_datetime",
             "old_value_monetary", "new_value_monetary"],
        )
        try:
            tracking = client.search_read("mail.tracking.value", [["id", "in", tv_ids]], tv_fields)
        except OdooRPCError:
            tracking = []

    return {
        "model": model, "id": rec_id,
        "name": rec.get("display_name") or rec.get("name") or f"{model}#{rec_id}",
        "scalars": scalars, "related": related_out, "messages": msgs, "tracking": tracking,
    }


def _tracking_str(tv: dict) -> str:
    field = tv.get("field_desc") or tv.get("field") or (
        tv["field_id"][1] if isinstance(tv.get("field_id"), (list, tuple)) and len(tv["field_id"]) > 1 else "field")
    def pick(prefix):
        for suf in ("char", "monetary", "float", "integer", "datetime"):
            v = tv.get(f"{prefix}_value_{suf}")
            if v not in (False, None, ""):
                return str(v)
        return "∅"
    return f"{field}: {pick('old')} → {pick('new')}"


def render_graph(graph: dict) -> str:
    """Compact, LLM-friendly text bundle of the record's neighbourhood + history."""
    lines = [f"RECORD: {graph['name']}  ({graph['model']} #{graph['id']})", ""]
    lines.append("CURRENT VALUES:")
    for k, v in graph["scalars"].items():
        lines.append(f"  - {k}: {v}")
    lines.append("")

    for rel in graph["related"]:
        if not rel["rows"] and not rel["count"]:
            continue
        lines.append(f"RELATED — {rel['label']} ({rel['count']}):")
        for row in rel["rows"]:
            bits = [f"{k}={row[k][1] if isinstance(row[k], (list, tuple)) and len(row[k]) > 1 else row[k]}"
                    for k in row if k != "id" and _has_value(row.get(k))]
            lines.append("    • " + ", ".join(bits))
        lines.append("")

    if graph["tracking"]:
        lines.append("FIELD-CHANGE HISTORY (tracked):")
        for tv in graph["tracking"]:
            lines.append("  - " + _tracking_str(tv))
        lines.append("")

    if graph["messages"]:
        lines.append("CHATTER / LOG (oldest first):")
        for m in graph["messages"]:
            who = m["author_id"][1] if isinstance(m.get("author_id"), (list, tuple)) and len(m["author_id"]) > 1 else "system"
            body = _strip_html(m.get("body"))[:200]
            line = f"  - [{m.get('date')}] {who}"
            if body:
                line += f": {body}"
            lines.append(line)
    return "\n".join(lines)

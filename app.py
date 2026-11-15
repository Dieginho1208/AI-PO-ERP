"""Operator review console (Streamlit).

    streamlit run app.py
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

import config
import database
from extractor import ExtractionError
from models import PurchaseOrder, Status
from pipeline import Pipeline

MANUAL_MINUTES_PER_PO = 12  # typical manual keying + checking time, used for the ROI tile

st.set_page_config(page_title="AI PO → ERP Validator", page_icon="🧾", layout="wide")
st.markdown("""
<style>
  .flag-box {background:#fdecea;border-left:4px solid #d93025;padding:.55rem .8rem;
             border-radius:6px;margin:.25rem 0;color:#7a1c14;font-size:.92rem}
  .ok-box   {background:#e6f4ea;border-left:4px solid #1e8e3e;padding:.55rem .8rem;
             border-radius:6px;color:#0d5323}
  .pill {display:inline-block;padding:.1rem .55rem;border-radius:999px;font-size:.75rem;
         font-weight:600;margin-left:.4rem}
  .pill-review{background:#d93025;color:#fff}.pill-ok{background:#1e8e3e;color:#fff}
  .pill-mode{background:#1a73e8;color:#fff}
</style>""", unsafe_allow_html=True)


@st.cache_resource
def get_pipeline() -> Pipeline:
    return Pipeline()


pipe = get_pipeline()


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("🧾 PO → ERP Validator")
    if pipe.extractor.mode == "live":
        st.markdown(f"<span class='pill pill-mode'>LIVE · {config.ANTHROPIC_MODEL}</span>", unsafe_allow_html=True)
    else:
        st.markdown("<span class='pill pill-mode'>DEMO MODE · mock payloads</span>", unsafe_allow_html=True)
        st.caption("No `ANTHROPIC_API_KEY` found. Extraction uses pre-cached payloads for the bundled sample PDFs.")

    st.subheader("1 · Ingest purchase orders")
    uploads = st.file_uploader("Drop PO PDFs", type=["pdf"], accept_multiple_files=True)
    if st.button("Process uploaded PDFs", disabled=not uploads, use_container_width=True):
        for up in uploads:
            try:
                pipe.process_pdf(up.getvalue(), filename=up.name)
            except ExtractionError as exc:
                st.error(f"{up.name}: {exc}")
        st.rerun()

    if st.button("▶ Run bundled sample POs", type="primary", use_container_width=True):
        for pdf in sorted(config.SAMPLE_PDFS_DIR.glob("*.pdf")):
            pipe.process_pdf(pdf)
        st.rerun()

    st.divider()
    if st.button("Reset processed orders", use_container_width=True):
        database.clear_orders()
        for f in config.ERP_IMPORT_DIR.glob("*.csv"):
            f.unlink()
        st.rerun()
    st.caption(f"Price tolerance ±{config.PRICE_TOLERANCE:.0%} · match threshold {config.MATCH_CONFIDENCE_THRESHOLD:.0f}%")


# --------------------------------------------------------------------- KPIs
orders = database.list_orders()
approved = [o for o in orders if o["status"] == Status.APPROVED.value]
review = [o for o in orders if o["status"] == Status.REQUIRES_REVIEW.value]
auto_approved = [o for o in approved if not o["reviewed_by"]]

st.header("Purchase Order Reconciliation")
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Processed", len(orders))
k2.metric("Approved", len(approved),
          f"{len(auto_approved) / len(orders):.0%} touchless" if orders else None)
k3.metric("Requires review", len(review), delta_color="inverse")
k4.metric("Value exported", f"${sum(o['order_total'] for o in approved):,.0f}")
k5.metric("Operator time saved", f"{len(auto_approved) * MANUAL_MINUTES_PER_PO} min",
          help=f"Auto-approved POs × {MANUAL_MINUTES_PER_PO} min of manual keying avoided")

if not orders:
    st.info("No orders yet. Click **▶ Run bundled sample POs** in the sidebar to see the full flow.")
    st.stop()


# ------------------------------------------------------------------ helpers
def reconciliation_frame(result: dict) -> pd.DataFrame:
    rows = []
    for ln in result["lines"]:
        src = ln["source"]
        rows.append({
            "Line": ln["line_no"], "PO SKU": src["sku"] or "—", "PO Description": src["description"],
            "Qty": src["qty"], "PO Price": src["unit_price"], "ERP SKU": ln["internal_sku"] or "—",
            "ERP Description": ln["erp_description"] or "—", "ERP Price": ln["erp_unit_price"],
            "Var %": ln["price_variance_pct"], "Match": ln["match_method"], "Confidence": ln["confidence"],
        })
    return pd.DataFrame(rows)


def styled(df: pd.DataFrame, flags: list[dict]):
    red = "background-color:#fdecea;color:#b3261e;font-weight:600"
    cells: dict[int, set[str]] = {}
    for f in flags:
        if f.get("line_no"):
            cols = {"Unit_Price": ["PO Price", "Var %"], "SKU": ["ERP SKU", "ERP Description", "Confidence"],
                    "Qty": ["Qty"]}.get(f.get("field"), [])
            cells.setdefault(f["line_no"], set()).update(cols)

    def _row(row):
        hit = cells.get(row["Line"], set())
        return [red if c in hit else "" for c in row.index]

    return (df.style.apply(_row, axis=1)
            .format({"PO Price": "{:,.2f}", "ERP Price": lambda v: "—" if pd.isna(v) else f"{v:,.2f}",
                     "Var %": lambda v: "—" if pd.isna(v) else f"{v:+.2f}%", "Confidence": "{:.0f}%",
                     "Qty": "{:g}"}))


def order_title(o: dict) -> str:
    po = o["po_number"] or "⚠ NO PO #"
    return f"{po} · {o['customer_name']} · ${o['order_total']:,.2f} · {o['source_file']}"


# --------------------------------------------------------------------- tabs
t_review, t_approved, t_all = st.tabs([f"🔴 Requires review ({len(review)})",
                                       f"✅ Approved ({len(approved)})", "📋 All orders"])

with t_review:
    if not review:
        st.markdown("<div class='ok-box'>Queue is empty. Every processed PO reconciled.</div>", unsafe_allow_html=True)
    for o in review:
        res = o["result"]
        with st.expander(order_title(o), expanded=True):
            for f in res["flags"]:
                st.markdown(f"<div class='flag-box'><b>{f['code']}</b> · {f['message']}</div>", unsafe_allow_html=True)

            st.markdown("**Reconciliation vs. ERP**")
            st.dataframe(styled(reconciliation_frame(res), res["flags"]), hide_index=True, use_container_width=True)

            cands = [(ln["line_no"], c) for ln in res["lines"] for c in ln["candidates"][:2]
                     if any(fl.get("line_no") == ln["line_no"] and fl["field"] == "SKU" for fl in res["flags"])]
            if cands:
                st.caption("Catalog suggestions for flagged lines (type the SKU in the editor below):  " +
                           " · ".join(f"L{n}: `{c['internal_sku']}` {c['description']} (${c['unit_price']}, {c['score']:.0f}%)"
                                      for n, c in cands))

            st.markdown("**Quick edit**")
            order = res["order"]
            c1, c2, c3 = st.columns([1, 2, 3])
            po_num = c1.text_input("PO Number", order["po_number"] or "", key=f"po_{o['id']}")
            cust = c2.text_input("Customer", res["erp_customer_name"] or order["customer_name"], key=f"cu_{o['id']}")
            ship = c3.text_input("Ship To", order["ship_to"] or "", key=f"st_{o['id']}")
            items_df = pd.DataFrame(order["line_items"]).rename(columns={
                "sku": "SKU", "description": "Description", "qty": "Qty", "uom": "UOM", "unit_price": "Unit_Price"})
            edited = st.data_editor(items_df, key=f"ed_{o['id']}", hide_index=True, use_container_width=True,
                                    num_rows="dynamic")

            b1, b2, _ = st.columns([1, 1, 3])
            revalidate = b1.button("Re-validate", key=f"rv_{o['id']}", type="primary")
            override = b2.button("Approve with override", key=f"ov_{o['id']}")
            if revalidate or override:
                try:
                    new_order = PurchaseOrder.model_validate({
                        "PO_Number": po_num, "Customer_Name": cust, "Ship_To": ship or None,
                        "Line_Items": [{k: (None if pd.isna(v) else v) for k, v in r.items()}
                                       for r in edited.to_dict("records")],
                    })
                except Exception as exc:  # pydantic ValidationError
                    st.error(f"Invalid edit: {exc}")
                else:
                    out = pipe.revalidate(o["id"], new_order, o["source_file"], o["extraction_mode"],
                                          reviewer="operator", force_approve=override)
                    if out.result.status == Status.APPROVED:
                        st.toast(f"Approved · exported {Path(out.csv_path).name}", icon="✅")
                    else:
                        st.toast(f"Still {len(out.result.flags)} flag(s)", icon="⚠️")
                    st.rerun()

with t_approved:
    for o in approved:
        with st.expander(order_title(o) + ("  · reviewed" if o["reviewed_by"] else "  · touchless")):
            st.dataframe(styled(reconciliation_frame(o["result"]), []), hide_index=True, use_container_width=True)
            if o["csv_path"] and Path(o["csv_path"]).exists():
                p = Path(o["csv_path"])
                st.download_button(f"⬇ {p.name}", p.read_bytes(), file_name=p.name, mime="text/csv",
                                   key=f"dl_{o['id']}")
                st.caption(f"Written to `{config.ERP_IMPORT_DIR.name}/{p.name}`")

with t_all:
    st.dataframe(pd.DataFrame([{
        "ID": o["id"], "Status": o["status"], "PO": o["po_number"] or "—", "Customer": o["customer_name"],
        "Customer ID": o["customer_id"] or "—", "Flags": o["flag_count"], "Total": o["order_total"],
        "Source": o["source_file"], "Mode": o["extraction_mode"], "Updated": o["updated_at"],
    } for o in orders]), hide_index=True, use_container_width=True)

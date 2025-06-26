"""
app.py  –  Import-PO JSON generator
Drag-and-drop an Excel/CSV ➜ get a ZIP with one JSON file per PO
"""

import streamlit as st
import pandas as pd
import io, zipfile, json

st.set_page_config(page_title="Import-PO Generator", layout="wide")
st.title("Excel / CSV → Import-PO JSON Payloads")

uploaded = st.file_uploader(
    "➕  Drop an Excel (.xlsx / .xls) or CSV file",
    type=["xlsx", "xls", "csv"],
)
dedup = st.checkbox(
    "Remove duplicate rows on (Column D + Column E)",
    value=True,
    help="Ticked = keep only unique combinations of column-D and column-E within each PO.",
)

# ───────────────────────── helper functions ──────────────────────────
def fmt_date(v: str) -> str:
    """Return 'YYYY-MM-DD' or empty string."""
    if not v or str(v).strip().lower() in ("nan", "none"):
        return ""
    dt = pd.to_datetime(v, errors="coerce")
    return "" if pd.isna(dt) else dt.strftime("%Y-%m-%d")


def pick(df: pd.DataFrame, *names: str) -> str | None:
    """Return the first column name that exists in df.columns."""
    return next((n for n in names if n in df.columns), None)


# ───────────────────────── main logic ────────────────────────────
if uploaded:
    # ── Load file into a single dataframe (string dtype)
    file_suffix = uploaded.name.split(".")[-1].lower()
    if file_suffix in ("csv", "tsv"):
        df = pd.read_csv(uploaded, dtype=str)
    else:
        book = pd.ExcelFile(uploaded)
        df = pd.concat(
            [book.parse(sheet, dtype=str) for sheet in book.sheet_names],
            ignore_index=True,
        )

    df.columns = [
        str(c).strip() if str(c).strip() else f"Unnamed_{i}"
        for i, c in enumerate(df.columns)
    ]

    po_col = pick(df, "Purchase Order Number", "PO Number", df.columns[0])
    col_d, col_e = df.columns[3], df.columns[4]  # Column D + E for dedup rule

    # apply dedup if requested
    if dedup:
        df = (
            df.groupby(po_col, group_keys=False)
            .apply(lambda g: g.drop_duplicates(subset=[col_d, col_e]))
            .reset_index(drop=True)
        )

    # ── Build JSON payloads
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for po, grp in df.groupby(po_col):
            f = grp.iloc[0]

            # optional header columns
            header_cols = {
                "po_date": pick(grp, "Purchase Order Date", "PO Date"),
                "need": pick(
                    grp,
                    "Purchase Need/Ship Date",
                    "Need Date",
                    "Need/Ship Date",
                ),
                "due": pick(grp, "Purchase Due Date", "Due Date"),
                "open_qty": pick(grp, "Open Quantity"),
                "buyer": pick(grp, "Buyer Code"),
                "sup_num": pick(grp, "Supplier Number"),
                "sup_name": pick(grp, "Alternate Supplier Name", "Supplier Name"),
                "sup_addr1": pick(grp, "Supplier Address1"),
                "sup_addr2": pick(grp, "Supplier Address2"),
                "sup_city": pick(grp, "Supplier City", "City"),
                "sup_post": pick(grp, "Supplier Postal Code", "Postal Code"),
                "sup_ctry": pick(grp, "Supplier Country", "Country"),
                "ship_ctry": pick(grp, "Country of Ship To", "Ship To Country"),
                "ems": pick(grp, "EMS Unit Level 1 Name", "EMS Unit"),
                "inco": pick(grp, "Header Incoterms", "Incoterms"),
            }

            payload = {
                "reference_number": str(po),
                "customer_reference_number": "RTS_SLB",
                "description": "",
                "group_code": "WMWHSE80",
                "order_date": fmt_date(f.get(header_cols["po_date"])),
                "type": str(f.get(header_cols["ems"], "")),
                "expected_delivery_date": fmt_date(f.get(header_cols["need"])),
                "order_due_date": fmt_date(f.get(header_cols["due"])),
                "open_quantity": int(
                    pd.to_numeric(
                        grp[header_cols["open_qty"]], errors="coerce"
                    )
                    .fillna(0)
                    .sum()
                )
                if header_cols["open_qty"]
                else 0,
                "buyer_details": {
                    "buyer_code": str(f.get(header_cols["buyer"], ""))
                },
                "seller_details": {
                    "seller_code": str(f.get(header_cols["sup_num"], "")),
                    "name": str(f.get(header_cols["sup_name"], "")),
                    "address_line_1": str(f.get(header_cols["sup_addr1"], "")),
                    "address_line_2": str(f.get(header_cols["sup_addr2"], "")),
                    "city": str(f.get(header_cols["sup_city"], "")),
                    "state": "",
                    "country": str(f.get(header_cols["sup_ctry"], "")),
                    "postal_code": str(f.get(header_cols["sup_post"], "")),
                    "phone_number": "",
                    "tax_number": "",
                    "email": "",
                },
                "notes": "",
                "inco_terms": str(f.get(header_cols["inco"], "")),
                "payment_terms": "",
                "origin_country": str(f.get(header_cols["sup_ctry"], "")),
                "destination_country": str(f.get(header_cols["ship_ctry"], "")),
                "pieces_detail": [],
            }

            # optional line-item columns
            line_cols = {
                "line": pick(grp, "PO Line Number", "Line", col_d),
                "part": pick(grp, "Part Number", col_e),
                "need": header_cols["need"],
                "due": header_cols["due"],
                "order_qty": pick(grp, "Order Quantity"),
                "open_qty": header_cols["open_qty"],
                "desc": pick(grp, "Part Description", "Description"),
                "uom": pick(grp, "UOM", "Unit"),
                "cost": pick(grp, "Item Unit Cost", "Unit Cost"),
            }

            for _, r in grp.iterrows():
                qty_raw = (
                    r.get(line_cols["order_qty"])
                    or r.get(line_cols["open_qty"], 0)
                )
                try:
                    qty = float(qty_raw)
                except Exception:
                    qty = 0

                piece = {
                    "reference_number": str(r.get(line_cols["line"])),
                    "customer_reference_number": str(r.get(line_cols["line"])),
                    "notes": "",
                    "alternate_unit": "",
                    "order_due_date": fmt_date(r.get(line_cols["due"])),
                    "expected_delivery_date": fmt_date(r.get(line_cols["need"])),
                    "stock_number": str(r.get(line_cols["part"])),
                    "is_chemical": False,
                    "is_dangerous_good": False,
                    "product_code": f"{r.get(line_cols['line'])}--{r.get(line_cols['part'])}",
                    "description": str(r.get(line_cols["desc"], "")),
                    "quantity": qty,
                    "item_unit": str(r.get(line_cols["uom"], "")),
                    "unit_price": 0,
                    "unit_cost": float(r.get(line_cols["cost"], 0) or 0),
                    "weight": 0,
                    "volume": 0,
                    "length": 0,
                    "width": 0,
                    "height": 0,
                    "source_location": None,
                    "hs_code": "",
                }
                payload["pieces_detail"].append(piece)

            zf.writestr(
                f"PO_{payload['reference_number']}.json",
                json.dumps(payload, indent=2),
            )

    st.success("✅  Finished!")
    st.download_button(
        "⬇️  Download PO_payloads.zip",
        data=zip_buffer.getvalue(),
        file_name="PO_payloads.zip",
        mime="application/zip",
    )

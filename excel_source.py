"""
Excel / CSV PO-line reader (the original app.py input), mapped onto the same
`PurchaseOrder` model the PDF parser produces.

Column names are resolved through the configurable alias lists in
config["excel_columns"] (first alias present wins, matched case- and
whitespace-insensitively). Nothing is looked up by column position.
"""

from __future__ import annotations

import io
import re

import pandas as pd

from po_model import POLine, PurchaseOrder, clean_str, parse_date, parse_datetime, to_float


def read_table(data: bytes, filename: str) -> pd.DataFrame:
    """Load every sheet (or the CSV) as strings, with clean, unique headers."""
    suffix = filename.rsplit(".", 1)[-1].lower()
    if suffix in ("csv", "tsv"):
        df = pd.read_csv(io.BytesIO(data), dtype=str, sep="\t" if suffix == "tsv" else ",")
    else:
        book = pd.ExcelFile(io.BytesIO(data))
        df = pd.concat([book.parse(s, dtype=str) for s in book.sheet_names], ignore_index=True)
    df.columns = [
        str(c).strip() if str(c).strip() and not str(c).startswith("Unnamed:") else f"Unnamed_{i}"
        for i, c in enumerate(df.columns)
    ]
    return df


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", str(name)).strip().lower()


def resolve_columns(df: pd.DataFrame, aliases: dict[str, list[str]]) -> dict[str, str | None]:
    by_norm = {_norm(c): c for c in df.columns}
    return {
        field: next((by_norm[_norm(a)] for a in names if _norm(a) in by_norm), None)
        for field, names in aliases.items()
    }


def parse_po_table(
    df: pd.DataFrame, aliases: dict[str, list[str]], source_file: str = "", dayfirst: bool = False
) -> list[PurchaseOrder]:
    cols = resolve_columns(df, aliases)
    if not cols.get("po_number"):
        raise ValueError(
            "No PO-number column found. Looked for: " + ", ".join(aliases.get("po_number", []))
            + ". Add your column name under 'Excel/CSV column names' in the sidebar."
        )

    def get(row, field):
        col = cols.get(field)
        return clean_str(row[col]) if col else ""

    df = df.copy()
    df["_po"] = df[cols["po_number"]].map(clean_str)
    blank = int((df["_po"] == "").sum())
    df = df[df["_po"] != ""]

    orders: list[PurchaseOrder] = []
    for po_number, grp in df.groupby("_po", sort=False):
        first = grp.iloc[0]
        po = PurchaseOrder(
            po_number=po_number,
            order_date=parse_date(get(first, "order_date"), dayfirst),
            order_type=get(first, "order_type"),
            description=get(first, "po_description"),
            buyer=get(first, "buyer"),
            currency=get(first, "currency"),
            supplier_number=get(first, "supplier_number"),
            supplier_name=get(first, "supplier_name"),
            supplier_address_lines=[a for a in (get(first, "supplier_address1"), get(first, "supplier_address2")) if a],
            supplier_city=get(first, "supplier_city"),
            supplier_postal_code=get(first, "supplier_postal_code"),
            supplier_country=get(first, "supplier_country"),
            delivery_location=get(first, "delivery_location"),
            notes=get(first, "notes"),
            ship_to_country=get(first, "ship_to_country"),
            incoterm=get(first, "incoterm"),
            payment_terms=get(first, "payment_terms"),
            source_file=source_file,
        )
        if blank and not orders:
            po.warnings.append(f"{blank} row(s) in {source_file} had no PO number and were ignored")
        if not cols.get("item_number"):
            po.warnings.append("No item/part-number column found - SKUs will be empty")

        for seq, (_, row) in enumerate(grp.iterrows(), start=1):
            line_no = get(row, "line_number")
            if not line_no:
                line_no = str(seq)
                po.warnings.append(f"Row {seq} of PO {po_number}: no line number, using {seq}")
            order_qty, open_qty = get(row, "quantity"), get(row, "open_quantity")
            po.lines.append(
                POLine(
                    line_number=line_no,
                    item_number=get(row, "item_number"),
                    supplier_item=get(row, "supplier_item"),
                    description=get(row, "line_description"),
                    quantity=to_float(order_qty if order_qty else open_qty),
                    open_quantity=to_float(open_qty) if open_qty else None,
                    uom=get(row, "uom"),
                    unit_price=to_float(get(row, "unit_price")),
                    promise_date=parse_date(get(row, "promise_date"), dayfirst),
                    need_by_date=parse_date(get(row, "need_by_date"), dayfirst),
                    promise_at=parse_datetime(get(row, "promise_date"), dayfirst),
                    need_by_at=parse_datetime(get(row, "need_by_date"), dayfirst),
                )
            )
        orders.append(po)
    return orders

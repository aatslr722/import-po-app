"""
Parse Oracle "Approved Standard Purchase Order" PDFs (ADES print layout).

Why geometry instead of plain text:
    PyMuPDF's text order inside the line-item table is unreliable - the line
    number / quantity / price cells are vertically centred, so they land in the
    middle of the description text, and sometimes on the same text line as
    "Item No:". Instead we

    1. find the table's horizontal border lines (each line item is one row of
       the table, bounded by lines that cross the Quantity column),
    2. bucket every word inside a row by its x-position into the
       Line / Description / Quantity / UOM / Price / Total columns, and
    3. read the description cell's sub-rows by their labels
       ("Item No:", "Promise date:", "Note to Supplier ..." / "Supplier Note:").

    Items that break across a page continue on the next page in a row that has
    no line number; those fragments are merged back into the previous item.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

from po_model import POLine, PurchaseOrder, parse_date, parse_datetime, split_city_line, to_float

ROW_TOLERANCE = 3.0  # words whose vertical centres are this close share a row
_NUMBER = re.compile(r"^-?[\d,]*\.?\d+$")
_FOOTER = re.compile(r"^\d+\s+of\s+\d+$")
_TOTAL_ROW = re.compile(r"^Total\s*(Amount|\(Without)", re.I)


class PDFParseError(ValueError):
    pass


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


def _words(page) -> list[Word]:
    return [Word(*w[:5]) for w in page.get_text("words")]


def _rows(words: list[Word], tol: float = ROW_TOLERANCE) -> list[list[Word]]:
    """Group words into visual rows (sorted top-to-bottom, left-to-right)."""
    rows: list[list[Word]] = []
    centre = 0.0
    for w in sorted(words, key=lambda w: (w.yc, w.x0)):
        if rows and abs(w.yc - centre) <= tol:
            rows[-1].append(w)
            centre = sum(x.yc for x in rows[-1]) / len(rows[-1])
        else:
            rows.append([w])
            centre = w.yc
    return [sorted(r, key=lambda w: w.x0) for r in rows]


def _text(row: list[Word]) -> str:
    return " ".join(w.text for w in row)


def _find(words: list[Word], text: str, min_x: float = -1.0, max_x: float = 1e9) -> Word | None:
    """Top-most word with exactly this text inside the x-range."""
    hits = [w for w in words if w.text == text and min_x < w.x0 < max_x]
    return min(hits, key=lambda w: (w.y0, w.x0), default=None)


def _horizontal_borders(page, probe_x: float) -> list[float]:
    """y-positions of horizontal table lines that cross x = probe_x."""
    ys: list[float] = []
    for d in page.get_drawings():
        for item in d["items"]:
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 0.5 and min(p1.x, p2.x) < probe_x < max(p1.x, p2.x):
                    ys.append(p1.y)
            elif item[0] == "re":
                r = item[1]
                if r.height < 1.5 and r.x0 < probe_x < r.x1:
                    ys.append(r.y0)
    merged: list[float] = []
    for y in sorted(ys):
        if not merged or y - merged[-1] > 2:
            merged.append(y)
    return merged


# ───────────────────────────── header ─────────────────────────────
def _labelled_rows(rows: list[list[Word]], labels: list[str]) -> dict[str, list[str]]:
    """Read 'Label: value' rows; unlabelled rows continue the previous label."""
    pattern = re.compile(r"^(" + "|".join(re.escape(l) for l in labels) + r")\s*:\s*(.*)$")
    out: dict[str, list[str]] = {}
    current = None
    for row in rows:
        text = _text(row)
        m = pattern.match(text)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
            if m.group(2).strip():
                out[current].append(m.group(2).strip())
        elif current:
            out[current].append(text)
    return out


def _parse_header(page, po: PurchaseOrder) -> dict:
    words = _words(page)
    supplier = _find(words, "Supplier:", max_x=100)
    delivery = _find(words, "Delivery", min_x=200)
    incoterm = _find(words, "Incoterm")
    quantity = _find(words, "Quantity")
    if not (supplier and incoterm and quantity):
        raise PDFParseError(
            "This does not look like an Oracle 'Standard Purchase Order' print "
            "(could not find the Supplier / Incoterm / Quantity headings)."
        )
    split_x = (delivery.x0 if delivery else page.rect.width / 2) - 2

    # Title "Standard Purchase Order" -> order type STANDARD
    title_words = sorted((w for w in words if w.y1 < 70), key=lambda w: w.x0)
    for i, w in enumerate(title_words[1:-1], start=1):
        if w.text == "Purchase" and title_words[i + 1].text == "Order":
            po.order_type = title_words[i - 1].text.upper()
            break

    # Letterhead (buying entity) address: left column above "Supplier:"
    po.buyer_address_lines = [
        _text(r) for r in _rows([w for w in words if w.x1 <= split_x and 90 < w.yc < supplier.y0 - 2])
    ]

    # Right-hand box: PO , Rev # / Approved Date / Buyer / Email / PO Description
    rev = _find(words, "Rev", min_x=split_x - 20)
    if rev:
        rev_row = [w for w in words if abs(w.yc - rev.yc) <= ROW_TOLERANCE + 1]
        hash_w = next((w for w in rev_row if w.text == "#" and w.x0 > rev.x0), rev)
        value_x = min((w.x0 for w in rev_row if w.x0 > hash_w.x1), default=hash_w.x1 + 10) - 1
        box = [w for w in words if w.x0 >= split_x - 20 and rev.yc - 3 <= w.yc < supplier.y0]
        label_val: list[list[str]] = []
        for row in _rows(box):
            label = " ".join(w.text for w in row if w.x0 < value_x)
            value = " ".join(w.text for w in row if w.x0 >= value_x)
            if label:
                label_val.append([label, value])
            elif label_val:
                label_val[-1][1] = f"{label_val[-1][1]} {value}".strip()
        for label, value in label_val:
            key = re.sub(r"\s+", " ", label.replace(" ,", ",")).strip().lower()
            if key.startswith("po, rev"):
                m = re.match(r"(\S+)\s*,\s*(\S*)", value)
                if m:
                    po.po_number, po.revision = m.group(1), m.group(2)
            elif key.startswith("approved date"):
                po.order_date = parse_date(value)
            elif key.startswith("buyer"):
                po.buyer = value.split("/")[0].strip()
            elif key == "email":
                po.buyer_email = value
            elif key.startswith("po description"):
                po.description = value
    if not po.po_number:
        m = re.search(r"\bPO\s+(\d+)\b", page.get_text())
        if m:
            po.po_number = m.group(1)

    # Supplier block (left column) and delivery / bill-to (right column)
    left = [w for w in words if w.x1 <= split_x + 2 and supplier.y1 < w.yc < incoterm.y0 - 2]
    sup = _labelled_rows(
        _rows(left),
        ["No.", "Name", "Contact Name", "Email", "Phone", "Address", "Vendor Phone", "Vendor Fax"],
    )
    po.supplier_number = " ".join(sup.get("No.", []))
    po.supplier_name = " ".join(sup.get("Name", []))
    po.supplier_contact = " ".join(sup.get("Contact Name", []))
    po.supplier_email = " ".join(sup.get("Email", []))
    po.supplier_phone = " ".join(sup.get("Phone", []) or sup.get("Vendor Phone", []))
    raw_addr = [a for a in sup.get("Address", []) if a.strip(" ,")]
    if raw_addr:
        po.supplier_country = raw_addr[-1].strip(" ,")
        body = raw_addr[:-1]
        # Oracle prints "CITY , STATE , POSTAL" as the line before the country.
        if len(body) >= 2 and "," in body[-1]:
            city, state, postal = split_city_line(body[-1])
            if city:
                po.supplier_city, po.supplier_state, po.supplier_postal_code = city, state, postal
                body = body[:-1]
        po.supplier_address_lines = [b.strip(" ,") for b in body]

    right = [w for w in words if w.x0 >= split_x and supplier.y0 - 2 < w.yc < incoterm.y0 - 2]
    right_rows = _rows(right)
    bill_idx = next((i for i, r in enumerate(right_rows) if _text(r).startswith("Bill to")), len(right_rows))
    dlv = _labelled_rows(right_rows[1:bill_idx], ["Organization", "Location", "Address"])
    po.delivery_organization = " ".join(dlv.get("Organization", []))
    po.delivery_location = " ".join(dlv.get("Location", []))
    po.ship_to_lines = [a for a in dlv.get("Address", []) if a.strip(" ,")]
    bill_lines = [re.sub(r"^Address\s*:\s*", "", _text(r)).strip() for r in right_rows[bill_idx + 1:]]
    bill_lines = [b for b in bill_lines if b]
    po.bill_to_lines = bill_lines
    if bill_lines:
        po.ship_to_country = bill_lines[-1].split(",")[-1].strip()

    # Incoterm / Mode of Transportation / Payment Terms mini-table
    header_row = [w for w in words if abs(w.yc - incoterm.yc) <= ROW_TOLERANCE]
    mode = next((w for w in header_row if w.text == "Mode"), None)
    payment = next((w for w in header_row if w.text == "Payment"), None)
    # Everything between the incoterm values and the line-item table header
    # (its top border) is the Notes block.
    probe_x = (quantity.x0 + quantity.x1) / 2
    table_top = max((y for y in _horizontal_borders(page, probe_x) if y < quantity.yc), default=quantity.y0)
    below = _rows([w for w in words if incoterm.y1 + 1 < w.yc < table_top])
    if below and mode and payment:
        transport_end = max(w.x1 for w in header_row if w.x0 < payment.x0)
        b1 = (incoterm.x1 + mode.x0) / 2
        b2 = (transport_end + payment.x0) / 2
        values = below[0]
        po.incoterm = " ".join(w.text for w in values if w.x0 < b1)
        po.transport_mode = " ".join(w.text for w in values if b1 <= w.x0 < b2)
        po.payment_terms = " ".join(w.text for w in values if w.x0 >= b2)

    return {"words": words, "quantity": quantity, "notes_rows": below[1:]}


# ─────────────────────────── line items ───────────────────────────
_ITEM_ROW = re.compile(r"^Item No\s*:\s*(.*?)\s*(?:Supplier item\s*:\s*(.*))?$", re.I)
_DATE_ROW = re.compile(r"^Promise date\s*:\s*(.*?)\s*(?:Need by date\s*:\s*(.*))?$", re.I)
_NOTE_ROW = re.compile(r"^(?:Note to Supplier for Line\s*#?\s*\d*\s*:|Supplier Note\s*:)\s*(.*)$", re.I)


@dataclass
class _Fragment:
    line_no: str | None
    desc_rows: list[str]
    qty: list[str]
    uom: list[str]
    amounts: list[Word]


def _build_line(frags: list[_Fragment]) -> POLine:
    line = POLine(line_number=frags[0].line_no or "")
    desc, notes, seen_item = [], [], False
    for row in (r for f in frags for r in f.desc_rows):
        if m := _ITEM_ROW.match(row):
            seen_item = True
            line.item_number = m.group(1).strip()
            line.supplier_item = (m.group(2) or "").strip()
        elif m := _DATE_ROW.match(row):
            line.promise_at = parse_datetime(m.group(1))
            line.need_by_at = parse_datetime((m.group(2) or "").strip())
            line.promise_date = parse_date(line.promise_at)
            line.need_by_date = parse_date(line.need_by_at)
        elif m := _NOTE_ROW.match(row):
            seen_item = True
            if m.group(1).strip():
                notes.append(m.group(1).strip())
        elif seen_item:
            notes.append(row)  # wrapped note text
        else:
            desc.append(row)
    line.description = " ".join(desc)
    line.note = " ".join(notes)
    qty = [q for f in frags for q in f.qty]
    line.quantity = to_float(qty[0]) if qty else 0.0
    line.uom = " ".join(u for f in frags for u in f.uom)
    amounts = sorted((w for f in frags for w in f.amounts), key=lambda w: w.x0)
    if len(amounts) >= 2:
        line.unit_price, line.line_total = to_float(amounts[0].text), to_float(amounts[-1].text)
    elif len(amounts) == 1:
        line.unit_price = to_float(amounts[0].text)
    return line


def parse_po_pdf(data: bytes, source_file: str = "") -> PurchaseOrder:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # corrupt / not a PDF
        raise PDFParseError(f"Could not open PDF: {exc}") from exc
    if doc.page_count == 0 or not doc[0].get_text().strip():
        raise PDFParseError("The PDF has no text layer (scanned image?) - OCR is not supported.")

    po = PurchaseOrder(po_number="", source_file=source_file)
    hdr = _parse_header(doc[0], po)
    words0, quantity = hdr["words"], hdr["quantity"]
    notes_rows = [_text(r) for r in hdr["notes_rows"]]
    if notes_rows:
        po.notes = re.sub(r"^(Supplier\s+)?Notes\s*:\s*", "", " ".join(notes_rows)).strip()

    # Column geometry comes from the table header on page 1; continuation pages
    # reuse it (they repeat the same table without a header row).
    uom = _find(words0, "UOM")
    item_labels = [w for w in words0 if w.text == "Item" and w.y0 > quantity.y1 + 2]
    desc_left = min((w.x0 for w in item_labels), default=quantity.x0 / 10) - 1
    qty_left = quantity.x0 - 8
    uom_left = (uom.x0 if uom else quantity.x1 + 5) - 6
    probe_x = (quantity.x0 + quantity.x1) / 2
    header_band = [w for w in words0 if abs(w.yc - quantity.yc) < 16 and w.x0 > qty_left - 300]
    po.currency = next(
        (m.group(1) for w in header_band if (m := re.fullmatch(r"\(([A-Z]{3})\)", w.text))), ""
    )

    fragments: list[_Fragment] = []
    finished = False
    for pno in range(doc.page_count):
        if finished:
            break
        page = doc[pno]
        words = words0 if pno == 0 else _words(page)
        rows = _rows(words)
        borders = _horizontal_borders(page, probe_x)

        if pno == 0:
            top = min((y for y in borders if y > quantity.yc), default=None)
        else:
            # Continuation pages: the table starts right under the repeated
            # "PO <number>" page title.
            title = next((r for r in rows if re.fullmatch(r"PO\s+\S+", _text(r))), None)
            top = (max(w.y1 for w in title) + 1) if title else None
        if top is None:
            continue

        bottom = page.rect.height
        for r in rows:
            t = _text(r)
            if r[0].yc > top and _TOTAL_ROW.match(t):
                bottom = min(w.y0 for w in r) - 0.5
                nums = [w.text for w in r if _NUMBER.match(w.text)]
                po.stated_total = to_float(nums[0]) if nums else None
                finished = True
                break
            if r[0].yc > top and _FOOTER.match(t):
                bottom = min(bottom, min(w.y0 for w in r) - 0.5)

        edges = [top] + [y for y in borders if top < y < bottom] + [bottom]
        for y_top, y_bot in zip(edges, edges[1:]):
            cell = [w for w in words if y_top < w.yc < y_bot]
            if not cell:
                continue
            line_no = next((w.text for w in cell if w.x1 < desc_left and w.text.isdigit()), None)
            desc_words = [w for w in cell if desc_left <= w.x0 < qty_left]
            frag = _Fragment(
                line_no=line_no,
                desc_rows=[_text(r) for r in _rows(desc_words)],
                qty=[w.text for w in cell if qty_left <= w.x0 < uom_left and _NUMBER.match(w.text)],
                uom=[w.text for w in cell if w.x0 >= uom_left and not _NUMBER.match(w.text)],
                amounts=[w for w in cell if w.x0 >= uom_left and _NUMBER.match(w.text)],
            )
            fragments.append(frag)

    # Group fragments into items: a fragment without a line number continues
    # the item before it (page break inside an item).
    groups: list[list[_Fragment]] = []
    for frag in fragments:
        if frag.line_no is None and groups:
            groups[-1].append(frag)
        elif frag.line_no is not None:
            groups.append([frag])
    po.lines = [_build_line(g) for g in groups]

    if not po.lines:
        po.warnings.append("No line items were found in the PDF.")
    for ln in po.lines:
        if not ln.item_number:
            po.warnings.append(f"Line {ln.line_number}: no 'Item No' printed on the PO")
    po.check_totals()
    return po

"""
Source-independent purchase-order model plus the value-cleaning helpers.

Both the PDF parser and the Excel/CSV reader produce `PurchaseOrder` objects,
and the payload builders only ever read from this model. That keeps every
"is this cell empty / numeric / a date?" decision in one place.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

_EMPTY_MARKERS = {"", "nan", "none", "null", "nat", "<na>"}


def clean_str(value) -> str:
    """Return a trimmed string; None / NaN / 'nan' / 'None' become ''."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    s = str(value).strip()
    if s.lower() in _EMPTY_MARKERS:
        return ""
    # Excel often hands back whole numbers as '12.0' – keep identifiers clean.
    if re.fullmatch(r"-?\d+\.0+", s):
        s = s.split(".")[0]
    return s


def to_float(value, default: float = 0.0) -> float:
    """Parse '1,200.50', '.7067', 12, NaN, '' ... -> finite float or `default`."""
    s = clean_str(value).replace(",", "")
    if not s:
        return default
    try:
        f = float(s)
    except ValueError:
        return default
    return f if math.isfinite(f) else default


def as_number(value: float) -> int | float:
    """12.0 -> 12, 12.5 -> 12.5 (keeps whole quantities as JSON integers)."""
    return int(value) if float(value).is_integer() else value


_DATE_FORMATS = (
    "%d-%b-%Y %H:%M:%S",  # 01-FEB-2026 08:39:36  (Oracle PO print)
    "%d-%b-%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def parse_datetime(value, dayfirst: bool = False) -> datetime | None:
    """Parse PDF / Excel date(-time) text; None when not a recognisable date."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    s = clean_str(value)
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)  # %b is case-insensitive
        except ValueError:
            pass
    # Excel serial day numbers (e.g. '46153.506') from .xls exports.
    if re.fullmatch(r"\d{5}(\.\d+)?", s):
        serial = float(s)
        if 20000 < serial < 80000:
            return datetime(1899, 12, 30) + timedelta(days=serial)
    import pandas as pd  # local import: only needed for free-form dates

    dt = pd.to_datetime(s, errors="coerce", dayfirst=dayfirst)
    return None if pd.isna(dt) else dt.to_pydatetime().replace(tzinfo=None)


def parse_date(value, dayfirst: bool = False) -> str:
    """Return 'YYYY-MM-DD' or '' for anything that is not a recognisable date."""
    dt = parse_datetime(value, dayfirst)
    return dt.strftime("%Y-%m-%d") if dt else ""


@dataclass
class POLine:
    line_number: str
    item_number: str = ""          # ADES item code -> WMS SKU
    supplier_item: str = ""
    description: str = ""
    quantity: float = 0.0
    open_quantity: float | None = None
    uom: str = ""
    unit_price: float = 0.0
    line_total: float | None = None
    promise_date: str = ""         # YYYY-MM-DD
    need_by_date: str = ""         # YYYY-MM-DD
    promise_at: datetime | None = None  # full timestamp as printed (for .NET dates)
    need_by_at: datetime | None = None
    note: str = ""


@dataclass
class PurchaseOrder:
    po_number: str
    revision: str = ""
    order_date: str = ""           # YYYY-MM-DD (approved date on the PDF)
    order_type: str = ""           # e.g. STANDARD (from the PDF title)
    description: str = ""
    buyer: str = ""
    buyer_email: str = ""
    currency: str = ""
    supplier_number: str = ""
    supplier_name: str = ""
    supplier_contact: str = ""
    supplier_email: str = ""
    supplier_phone: str = ""
    supplier_address_lines: list[str] = field(default_factory=list)
    supplier_city: str = ""
    supplier_state: str = ""
    supplier_postal_code: str = ""
    supplier_country: str = ""
    buyer_address_lines: list[str] = field(default_factory=list)  # PO letterhead block
    delivery_organization: str = ""
    delivery_location: str = ""     # rig / location code, e.g. ADES-878
    ship_to_lines: list[str] = field(default_factory=list)
    bill_to_lines: list[str] = field(default_factory=list)
    ship_to_country: str = ""
    incoterm: str = ""
    transport_mode: str = ""
    payment_terms: str = ""
    notes: str = ""
    stated_total: float | None = None  # "Total Amount" printed on the PO
    source_file: str = ""
    lines: list[POLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def check_totals(self) -> None:
        """Cross-check parsed line totals against the PO's printed total."""
        for ln in self.lines:
            if ln.line_total is None:
                continue
            expected = ln.quantity * ln.unit_price
            if abs(expected - ln.line_total) > max(0.02, abs(ln.line_total) * 0.005):
                self.warnings.append(
                    f"Line {ln.line_number}: qty {ln.quantity:g} x price {ln.unit_price:g} "
                    f"= {expected:,.2f}, but the PO shows {ln.line_total:,.2f}"
                )
        if self.stated_total is not None and all(ln.line_total is not None for ln in self.lines):
            parsed = sum(ln.line_total for ln in self.lines)
            if abs(parsed - self.stated_total) > 0.05:
                self.warnings.append(
                    f"Sum of parsed lines {parsed:,.2f} does not match the PO total "
                    f"{self.stated_total:,.2f} - some lines may have been missed"
                )


def split_city_line(line: str) -> tuple[str, str, str]:
    """Oracle prints 'CITY , STATE , POSTAL' – return (city, state, postal)."""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return parts[0], parts[1], ", ".join(p for p in parts[2:] if p)

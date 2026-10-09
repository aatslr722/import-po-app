"""
Build ImportPO and ImportSKU JSON payloads (SSA API format) from
`PurchaseOrder` objects.

Shared variables (config["common"]) feed both payloads:
    facility, storer_key     -> DataHeader.Facility / DataHeader.StorerKey (both)
    ssa_login                -> SSA.SSA_Login (both)
    requested_system         -> ApplicationHeader.RequestedSystem (both)
    lottable_validation_key  -> ImportSKU DataHeader[].LottableValidationKey

ImportSKU carries many SKUs per payload (DataHeader is a list); fields that
don't apply are left out.
"""

from __future__ import annotations

import calendar
import dataclasses
import io
import json
import random
import re
import unicodedata
import zipfile
from datetime import datetime

import pycountry

from po_model import POLine, PurchaseOrder, as_number, clean_str

MISSING_ITEM_MODES = ("placeholder", "skip", "supplier_item", "description_prefix")


# ───────────────────────── preparation ─────────────────────────
def apply_missing_item_policy(
    orders: list[PurchaseOrder], mode: str, placeholder: str = "{po}-{line}--NOSKU"
) -> None:
    """Fill blank item numbers per the configured policy (and say so).

    Runs once, before any payload is built, so ImportPO (DataLines.SKU) and
    ImportSKU (SKU / ManufacturerSKU) always carry the same value.
    """
    if mode not in MISSING_ITEM_MODES:
        raise ValueError(f"missing_item_number must be one of {MISSING_ITEM_MODES}")
    if mode == "skip":
        return
    for po in orders:
        for ln in po.lines:
            if ln.item_number:
                continue
            if mode == "placeholder":  # "{po}-{line}--NOSKU" -> "107439-3--NOSKU"
                guess = placeholder.format(line=ln.line_number, po=po.po_number)
            elif mode == "supplier_item":
                guess = ln.supplier_item
            else:  # "150-576-D-NPN / 5-3/4" O.D. Logan ..." -> "150-576-D-NPN"
                guess = ln.description.split(" / ")[0].strip() if " / " in ln.description else ""
            if guess:
                ln.item_number = guess
                po.warnings.append(f"Line {ln.line_number}: no Item No, using {mode.replace('_', ' ')} '{guess}'")


def dedupe_lines(po: PurchaseOrder) -> None:
    """Drop repeated (line number, item number) rows within one PO."""
    seen, kept = set(), []
    for ln in po.lines:
        key = (ln.line_number, ln.item_number)
        if key not in seen:
            seen.add(key)
            kept.append(ln)
    if len(kept) != len(po.lines):
        po.warnings.append(f"Removed {len(po.lines) - len(kept)} duplicate line(s)")
    po.lines = kept


# ───────────────────────── special characters ─────────────────────────
DEFAULT_ALLOWED = " .,-/():"
# Parsed fields that are not free text and must keep their exact form.
_NOT_CLEANED = {"order_date", "promise_date", "need_by_date", "source_file"}


def clean_text(text: str, allowed: str = DEFAULT_ALLOWED) -> str:
    """Keep letters, digits, spaces and the `allowed` punctuation; drop the rest.

    Accented letters are reduced to ASCII ('é' -> 'e'); runs of spaces collapse.
    """
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    kept = "".join(ch for ch in ascii_text if ch.isalnum() or ch == " " or ch in allowed)
    return re.sub(r"\s+", " ", kept).strip()


def _clean_fields(obj, allowed: str) -> None:
    for f in dataclasses.fields(obj):
        value = getattr(obj, f.name)
        if f.name in _NOT_CLEANED:
            continue
        if isinstance(value, str):
            setattr(obj, f.name, clean_text(value, allowed))
        elif f.name.endswith("_lines") and isinstance(value, list):
            # Address lines: '|' separates parts ("Block 8 | Plot 144 | 61010"),
            # so turn it into a comma instead of deleting it.
            cleaned = (clean_text(re.sub(r"\s*\|\s*", ", ", v), allowed) for v in value)
            setattr(obj, f.name, [c.strip(" ,") for c in cleaned if c.strip(" ,")])


def clean_orders(orders: list[PurchaseOrder], cfg: dict) -> None:
    """Remove special characters from the parsed PO data (header + lines).

    Runs before any payload is built, so both payloads, the SUSR2 slice, the
    50-character address wrapping and SKU de-duplication all see cleaned text.
    Values declared in the settings (Facility, StorerKey, ...) are not touched.
    """
    common = cfg["common"]
    if not common.get("remove_special_characters", True):
        return
    allowed = common.get("allowed_special_characters", DEFAULT_ALLOWED)
    for po in orders:
        _clean_fields(po, allowed)
        for ln in po.lines:
            _clean_fields(ln, allowed)


def prepare_orders(orders: list[PurchaseOrder], cfg: dict) -> None:
    """Everything that happens between parsing and payload generation."""
    clean_orders(orders, cfg)
    apply_missing_item_policy(
        orders, cfg["parsing"]["missing_item_number"], cfg["parsing"]["missing_item_placeholder"]
    )
    if cfg["po_payload"]["remove_duplicate_lines"]:
        for po in orders:
            dedupe_lines(po)


# ───────────────────────── shared helpers ─────────────────────────
_COUNTRY_ALIASES = {"UAE": "AE", "U.A.E": "AE", "USA": "US", "U.S.A": "US", "UK": "GB", "KSA": "SA"}


def country_code(name: str) -> str:
    """'United Arab Emirates' -> 'AE'. Unknown names are returned unchanged."""
    s = clean_str(name).strip(" ,.")
    if not s:
        return ""
    if s.upper() in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[s.upper()]
    try:
        return pycountry.countries.lookup(s).alpha_2
    except LookupError:
        return s


def _is_country(text: str) -> bool:
    s = clean_str(text).strip(" ,.")
    if not s or re.search(r"\d", s):
        return False
    try:
        return s.upper() in _COUNTRY_ALIASES or bool(pycountry.countries.lookup(s))
    except LookupError:
        return False


_POSTAL = re.compile(r"^\d{4,7}$")
_POSTAL_LABEL = re.compile(r"[-,|\s]*Postal\s*Code\s*[:#-]?\s*(\w+)\s*$", re.I)


def _take_postal(lines: list[str]) -> tuple[list[str], str]:
    """Pull a trailing postal code out of the address.

    'Block 8 | Plot 144 | P.O. Box 9282 | 61010' -> 'Block 8 | Plot 144 | P.O. Box 9282', '61010'
    '... PO Box 23724 -Postal Code 00000'         -> '... PO Box 23724', '00000'
    """
    for i in range(len(lines) - 1, -1, -1):
        line, postal, rest = lines[i], None, ""
        if m := _POSTAL_LABEL.search(line):
            postal, rest = m.group(1), line[: m.start()]
        else:
            parts = re.split(r"\s*[|,]\s*", line)
            last = parts[-1].strip()
            # a bare number only counts as a postal code after a separator,
            # or as the whole last line
            if _POSTAL.match(last) and (len(parts) > 1 or i == len(lines) - 1):
                postal, rest = last, line[: line.rstrip().rfind(last)]
        if postal is not None:
            rest = rest.strip(" ,|-")
            return lines[:i] + ([rest] if rest else []) + lines[i + 1:], postal
    return lines, ""


def split_address(lines: list[str]) -> tuple[list[str], str, str, str]:
    """Printed address lines -> (street lines, city, postal code, ISO country).

    Handles 'Giza, Egypt' as a last line, a bare country as the last line, a
    short digit-free line just before the country being the city ('Giza'),
    and a trailing postal code ('... | 61010').
    """
    lines = [l.strip(" ,") for l in lines if l.strip(" ,")]
    city = country = ""
    if lines:
        last = lines[-1]
        if _is_country(last):
            country, lines = country_code(last), lines[:-1]
        elif "," in last and _is_country(last.rsplit(",", 1)[1]):
            city, ctry = (x.strip() for x in last.rsplit(",", 1))
            country, lines = country_code(ctry), lines[:-1]
    lines, postal = _take_postal(lines)
    if not city and len(lines) >= 2 and not re.search(r"\d", lines[-1]) and len(lines[-1].split()) <= 3:
        city, lines = lines[-1], lines[:-1]
    return lines, city, postal, country


def _wrap(text: str, max_len: int) -> list[str]:
    """Break one address line into pieces of <= max_len, at '|', then ',', then spaces."""
    text = re.sub(r"\s*\|\s*", ", ", text.strip()).strip(" ,")
    if len(text) <= max_len:
        return [text] if text else []
    for pattern, joiner in ((r"\s*,\s*", ", "), (r"\s+", " ")):
        parts = [p for p in re.split(pattern, text) if p]
        if len(parts) > 1:
            break
    else:
        return [text[i: i + max_len] for i in range(0, len(text), max_len)]
    out, cur = [], ""
    for part in parts:
        for piece in _wrap(part, max_len) if len(part) > max_len else [part]:
            candidate = f"{cur}{joiner}{piece}" if cur else piece
            if len(candidate) <= max_len:
                cur = candidate
            else:
                out.append(cur)
                cur = piece
    return out + ([cur] if cur else [])


def fit_address(lines: list[str], slots: int, max_len: int) -> tuple[list[str], bool]:
    """Address lines -> exactly `slots` lines of <= max_len. Returns (lines, truncated)."""
    pieces = [p for line in lines for p in _wrap(line, max_len)]
    if len(pieces) > slots:  # too many: pack short neighbours together
        packed: list[str] = []
        for p in pieces:
            if packed and len(packed[-1]) + 2 + len(p) <= max_len:
                packed[-1] += ", " + p
            else:
                packed.append(p)
        pieces = packed
    truncated = len(pieces) > slots
    if truncated:
        pieces = pieces[: slots - 1] + [", ".join(pieces[slots - 1:])[:max_len].rstrip(" ,")]
    return pieces + [""] * (slots - len(pieces)), truncated


def dotnet_date(dt: datetime | None) -> str | None:
    """Printed date-time (taken as UTC) -> '/Date(1775084903000+0000)/'."""
    if dt is None:
        return None
    return f"/Date({calendar.timegm(dt.timetuple()) * 1000}+0000)/"


_PART_NO = re.compile(r"\bP/?N\s*[#:.]*\s*([A-Za-z0-9][A-Za-z0-9\-./]*)", re.I)


def manufacturer_part(ln: POLine) -> str:
    """Last 'P/N# xxx' in the description, else the supplier item."""
    found = _PART_NO.findall(ln.description)
    return found[-1].rstrip(".") if found else ln.supplier_item


def _line_value(ln: POLine) -> float:
    return ln.line_total if ln.line_total is not None else ln.quantity * ln.unit_price


class TransactionIds:
    """Random 8-digit IDs, never repeated within one generator run."""

    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.SystemRandom()
        self._used: set[int] = set()

    def next(self) -> str:
        while True:
            n = self._rng.randint(10_000_000, 99_999_999)
            if n not in self._used:
                self._used.add(n)
                return str(n)


# ───────────────────────── ImportPO ─────────────────────────
def build_po_payload(po: PurchaseOrder, cfg: dict, password: str, txn_id: str, requested_date: str) -> dict:
    common, pcfg = cfg["common"], cfg["po_payload"]
    total = po.stated_total if po.stated_total is not None else sum(_line_value(ln) for ln in po.lines)

    max_len = int(pcfg["address_line_max_length"])

    def fit(label: str, lines: list[str], slots: int) -> list[str]:
        fitted, truncated = fit_address(lines, slots, max_len)
        if truncated:
            po.warnings.append(f"{label} address is longer than {slots} x {max_len} characters - cut off")
        return fitted

    b_lines, b_city, b_postal, b_country = split_address(po.buyer_address_lines)
    bill_lines, bill_city, bill_postal, bill_country = split_address(po.bill_to_lines)
    ship_lines, ship_city, ship_postal, ship_country = split_address(po.ship_to_lines)
    if not ship_lines:  # no delivery street address printed -> same as bill-to
        ship_lines, ship_city, ship_postal = bill_lines, bill_city, bill_postal
        ship_country = ship_country or bill_country
    if not ship_country:
        ship_country = bill_country

    buyer = {
        "Ref": "",
        "Company": "",
        **dict(zip(("Address1", "Address2", "Address3", "Address4"), fit("BuyerAddress", b_lines, 4))),
        "Country": b_country,
        "City": b_city,
        "Contact": pcfg["company_name"],
        "Phone1": "",
        "Phone2": "",
        "Email": "",
        "State": "",
        "ZipCode": b_postal,
        "Fax": "",
        "W3W": "",
        "C_ID": "",
    }
    buyer.update(pcfg.get("buyer_address_overrides") or {})
    s_addr = fit("SupplierAddress", po.supplier_address_lines, 2)
    supplier = {
        "Company": "",
        "Address1": s_addr[0],
        "Address2": s_addr[1],
        "City": po.supplier_city,
        "Country": country_code(po.supplier_country),
        "Contact": po.supplier_name,
        "Phone1": po.supplier_phone,
        "C_ID": po.supplier_number,
    }
    data_header = {
        "Facility": common["facility"],
        "StorerKey": common["storer_key"],
        "ClinetSystemRef": po.po_number,  # sic - field name as the API spells it
        "EXTERNALPOKEY2": "",
        "Currency": po.currency,
        "Type": po.order_type or pcfg["type"],
        "INCOTERMS": po.incoterm,
        "SUSR1": po.delivery_location,  # rig / location
        "SUSR2": pcfg["amount_format"].format(total),  # total PO value
        "Notes": po.notes,
        "BuyerAddress": buyer,
        "SupplierAddress": supplier,
    }
    data_header.update(pcfg.get("data_header_overrides") or {})

    def extra(ref: str, lines: list[str], city: str, postal: str, country: str, overrides: dict | None) -> dict:
        a = fit(ref, lines, 4)
        out = {
            "SerialKey": int(pcfg["extra_address_serial_key"]),
            "Ref": ref,
            "Name1": pcfg["company_name"],
            "Name2": "",
            "Name3": "",
            "Name4": "",
            "Address1": a[0],
            "Address2": a[1],
            "Address3": a[2],
            "Address4": a[3],
            "Country": country,
            "City": city,
            "Postal": postal,
            "Telephone1": "",
            "Telephone2": "",
            "Email": "",
        }
        out.update(overrides or {})
        return out

    return {
        "SSA": {"SSA_Login": common["ssa_login"], "SSA_Password": password},
        "ApplicationHeader": {
            "RequestedSystem": common["requested_system"],
            "RequestedDate": requested_date,
            "TransactionID": txn_id,
        },
        "DataHeader": data_header,
        "DataLines": [_data_line(ln, pcfg) for ln in po.lines],
        "ExtraAddresses": [
            extra("BILL_TO", bill_lines, bill_city, bill_postal, bill_country, pcfg.get("bill_to_overrides")),
            extra("SHIP_TO", ship_lines, ship_city, ship_postal, ship_country, pcfg.get("ship_to_overrides")),
        ],
    }


def _data_line(ln: POLine, pcfg: dict) -> dict:
    pn = manufacturer_part(ln)
    promise = dotnet_date(ln.promise_at or ln.need_by_at)
    line = {
        "ExternLineNo": ln.line_number,
        "SKU": ln.item_number,
        "Descr": ln.description,
        "ManufacturerSKU": pcfg["manufacturer_sku_template"].format(pn=pn) if pn else "",
        "SUSR1": ln.uom,  # PO UOM
        "SUSR2": str(pcfg["conversion_factor"]),  # conversion factor
        "SUSR3": ln.uom,  # item UOM
        "SUSR4": str(as_number(ln.quantity)),
        "SUSR5": pcfg["amount_format"].format(_line_value(ln)),  # line cost
        "Qty": as_number(ln.quantity),
        "UnitCost": ln.unit_price,
        "InitalPromiseDate": promise,  # sic
        "NewPromiseDate": promise,
        "Notes": ln.note,
    }
    line.update(pcfg.get("data_line_overrides") or {})
    return line


def build_po_payloads(
    orders: list[PurchaseOrder], cfg: dict, password: str, now: datetime | None = None,
    txn_ids: TransactionIds | None = None,
) -> list[tuple[str, dict]]:
    """One ImportPO payload per purchase order -> [(po_number, payload)].

    Expects orders that went through prepare_orders() (cleaned, SKUs filled).
    """
    requested_date = (now or datetime.now()).strftime(cfg["po_payload"]["requested_date_format"])
    txn_ids = txn_ids or TransactionIds()
    return [
        (po.po_number, build_po_payload(po, cfg, password, txn_ids.next(), requested_date))
        for po in orders
    ]


# ───────────────────────── ImportSKU ─────────────────────────
# Every field an ImportSKU DataHeader entry can carry, in the order the API
# documents them; settings["sku_payload"]["fields"] picks which are sent.
def _sku_field_values(ln: POLine, cfg: dict) -> dict:
    common, scfg = cfg["common"], cfg["sku_payload"]
    desc = ln.description
    if scfg["description_max_length"]:
        desc = desc[: int(scfg["description_max_length"])].rstrip()
    return {
        "Description": desc,
        "Facility": common["facility"],
        "SKU": ln.item_number,
        "SerialCount": str(scfg["serial_count"]),
        "StorerKey": common["storer_key"],
        "LottableValidationKey": common["lottable_validation_key"],
        "ManufacturerSKU": ln.item_number,
        "SUSR2": ln.description[: int(scfg["susr2_length"])].rstrip(),
        "SUSR3": scfg["susr3"],
        "SUSR9": ln.uom,
        "Cost": float(ln.unit_price),
        "Price": float(scfg["price"]),
        "SHELFLIFEINDICATOR": scfg["shelf_life_indicator"],
        "SHELFLIFECODETYPE": scfg["shelf_life_code_type"],
    }


SKU_FIELDS = [
    "Description", "Facility", "SKU", "SerialCount", "StorerKey", "LottableValidationKey",
    "ManufacturerSKU", "SUSR2", "SUSR3", "SUSR9", "Cost", "Price", "SHELFLIFEINDICATOR", "SHELFLIFECODETYPE",
]
DEFAULT_SKU_FIELDS = ["Description", "Facility", "SKU", "SerialCount", "StorerKey", "LottableValidationKey"]


def build_sku_entry(ln: POLine, cfg: dict) -> dict:
    """One DataHeader entry. Fields that don't apply (empty / null) are dropped."""
    scfg = cfg["sku_payload"]
    values = _sku_field_values(ln, cfg)
    entry = {f: values[f] for f in scfg.get("fields") or DEFAULT_SKU_FIELDS if f in values}
    entry.update(scfg.get("data_header_overrides") or {})
    return {k: v for k, v in entry.items() if v is not None and v != ""}


def build_sku_payloads(
    orders: list[PurchaseOrder], cfg: dict, password: str, now: datetime | None = None,
    txn_ids: TransactionIds | None = None,
) -> tuple[list[tuple[str, dict]], list[str]]:
    """ImportSKU payload(s): many SKUs per payload, in the DataHeader list.

    All distinct SKUs of the uploaded POs go into one payload, or into batches
    of `skus_per_payload` when that is set. Returns ([(label, payload)], notes).
    """
    common, scfg = cfg["common"], cfg["sku_payload"]
    requested_date = (now or datetime.now()).strftime(scfg["requested_date_format"])
    txn_ids = txn_ids or TransactionIds()

    entries, notes, seen = [], [], set()
    for po in orders:
        for ln in po.lines:
            if not ln.item_number:
                notes.append(f"PO {po.po_number} line {ln.line_number}: skipped (no Item No)")
                continue
            if scfg["unique_skus"] and ln.item_number in seen:
                continue
            seen.add(ln.item_number)
            entries.append(build_sku_entry(ln, cfg))
    if not entries:
        return [], notes

    size = int(scfg.get("skus_per_payload") or 0) or len(entries)
    batches = [entries[i: i + size] for i in range(0, len(entries), size)]
    out = []
    for n, batch in enumerate(batches, start=1):
        label = f"{len(batch)} SKUs" if len(batches) == 1 else f"Batch {n} of {len(batches)} ({len(batch)} SKUs)"
        out.append((label, {
            "ApplicationHeader": {
                "RequestedDate": requested_date,
                "RequestedSystem": common["requested_system"],
                "TransactionID": txn_ids.next(),
            },
            "DataHeader": batch,
            "SSA": {"SSA_Login": common["ssa_login"], "SSA_Password": password},
        }))
    return out, notes


# ───────────────────────── output ─────────────────────────
def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "blank"


def to_json(payload: dict) -> str:
    # allow_nan=False: fail loudly rather than emit invalid JSON (bare NaN).
    text = json.dumps(payload, indent=4, ensure_ascii=False, allow_nan=False)
    # .NET (DataContractJsonSerializer) only recognises dates written as "\/Date(...)\/".
    return re.sub(r'"/Date\((-?\d+)([+-]\d{4})?\)/"', r'"\\/Date(\1\2)\\/"', text)


def build_zip(po_payloads: list[tuple[str, dict]], sku_payloads: list[tuple[str, dict]]) -> bytes:
    buf = io.BytesIO()
    used: set[str] = set()

    def unique(path: str) -> str:
        stem, n = path[:-5], 2
        while path in used:
            path, n = f"{stem}_{n}.json", n + 1
        used.add(path)
        return path

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for po_number, p in po_payloads:
            zf.writestr(unique(f"ImportPO/PO_{_safe(po_number)}.json"), to_json(p))
        for n, (_, p) in enumerate(sku_payloads, start=1):
            name = "ImportSKU.json" if len(sku_payloads) == 1 else f"ImportSKU_batch_{n:03d}.json"
            zf.writestr(unique(f"ImportSKU/{name}"), to_json(p))
    return buf.getvalue()

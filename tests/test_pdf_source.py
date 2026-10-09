import copy

import pytest

from pdf_source import PDFParseError, parse_po_pdf


def line(po, number):
    return next(ln for ln in po.lines if ln.line_number == number)


def test_header_107439(po_107439):
    po = po_107439
    assert (po.po_number, po.revision, po.order_date) == ("107439", "0", "2026-02-01")
    assert po.buyer == "Mr. Oleh Bilinskyi"
    assert po.supplier_number == "1961"
    assert po.supplier_name == "WOODHOUSE INTERNATIONAL FZE"
    assert po.supplier_city == "DUBAI"
    assert po.supplier_country == "United Arab Emirates"
    assert po.ship_to_country == "Kuwait"
    assert (po.incoterm, po.transport_mode, po.payment_terms) == ("EXW", "AIR", "Net 90 Day")
    assert po.currency == "USD"
    assert po.description.startswith("878-61219 // OVER SHOT FISHING TOOL")
    assert po.notes == "DELIVERY TERMS: EXW WI Houston Packer DELIVERY TIME: 14-15 Weeks"


def test_lines_107439(po_107439):
    po = po_107439
    assert [ln.line_number for ln in po.lines] == ["1", "2", "3", "4", "5"]
    first = line(po, "1")
    assert first.description.startswith('150-576-D-NPN / 5-3/4" O.D. Logan Series "150"')
    assert first.description.endswith("Cutlipped Guide. (C11823).")
    assert (first.quantity, first.uom, first.unit_price, first.line_total) == (1, "EACH", 5481.12, 5481.12)
    assert (first.promise_date, first.need_by_date) == ("2026-05-30", "2026-02-05")
    assert first.item_number == ""  # this PO prints no Item No
    # line 5 sits on page 2 – still parsed, and totals reconcile
    assert line(po, "5").unit_price == 551.76
    assert po.stated_total == 7258.38
    assert not [w for w in po.warnings if "does not match" in w or " x price " in w]
    assert sum("no 'Item No'" in w for w in po.warnings) == 5


def test_header_36690(po_36690):
    po = po_36690
    assert (po.po_number, po.order_date, po.buyer) == ("36690", "2025-03-30", "Mr. Sayed Ali")
    assert po.supplier_name == "Neptunus Global Trading FZE"
    assert po.supplier_address_lines == ["RAK FREE TRADE ZONE , RAK, UAE", "P.O BOX NO-329339"]
    assert po.supplier_country == "United Arab Emirates"
    assert po.ship_to_country == "Egypt"
    assert (po.incoterm, po.transport_mode) == ("EXW", "")
    assert po.payment_terms == "Cash 30% Down payment, 70% Prior Delivery"
    assert po.notes == "ADM1 - Part list for EMD #2 Repair required - PR.# 36135"


def test_lines_36690_reconcile_with_printed_total(po_36690):
    po = po_36690
    assert len(po.lines) == 131
    assert po.lines[0].line_number == "1" and po.lines[-1].line_number == "141"
    assert round(sum(ln.line_total for ln in po.lines), 2) == po.stated_total == 196215.07
    assert po.warnings == []
    assert all(ln.item_number and ln.uom and ln.quantity > 0 for ln in po.lines)


@pytest.mark.parametrize(
    "number, item, qty, uom, price, desc",
    [
        # "Item No" and the qty/price cells share one text line
        ("5", "5060700.01.11.05051", 1, "SET", 2320.8219, "BEARING, SET MAIN CRANKSHAFT-LOWER - MFG :EMD P/N# 8455083"),
        # item split across pages 3 -> 4 (description on one page, Item No on the next)
        ("27", "5060700.01.11.01213", 400, "EACH", 0.9359,
         "FASTENER, BOLTS, 5/16 INCH SIZE, 1 1/8 HEX.HEAD - MFG: EMD - P/N# 179819"),
        # description itself split across pages 11 -> 12
        ("107", "5060700.01.11.09835", 1, "EACH", 6795.885,
         "ACC DRIVE COVER, ASM ACCESSORY DRIVE L.H.ROTATION INDUSTRIAL ENGINES - MFG: EMD - P/N# 9571255"),
        # two-row description
        ("37", "5060700.01.11.02499", 1, "EACH", 143.6511,
         "PICKUP, MAGNETIC POWER, 2 POLE FEMALE MODEL 3040A606910 - MFG : EMD - P/N # 8270383"),
    ],
)
def test_tricky_lines_36690(po_36690, number, item, qty, uom, price, desc):
    ln = line(po_36690, number)
    assert (ln.item_number, ln.quantity, ln.uom, ln.unit_price, ln.description) == (item, qty, uom, price, desc)


def test_supplier_item_and_dates_36690(po_36690):
    ln = line(po_36690, "1")
    assert ln.supplier_item == "40078993"
    assert (ln.promise_date, ln.need_by_date) == ("2025-05-24", "")


def test_rejects_non_pdf():
    with pytest.raises(PDFParseError):
        parse_po_pdf(b"not a pdf at all")

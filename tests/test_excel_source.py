"""Regression tests for the bugs in the original app.py."""

import io
import json

import pandas as pd
import pytest

from excel_source import parse_po_table, read_table
from payloads import build_po_payloads, to_json
from po_model import clean_str, parse_date, to_float

CSV = (
    "Purchase Order Number,Purchase Order Date,Supplier Number,Supplier Name,PO Line Number,"
    "Part Number,Part Description,Order Quantity,Open Quantity,UOM,Item Unit Cost\n"
    "PO-1,2026-02-01,1961,ACME,1,P-100,Widget,,3,EACH,\"1,200\"\n"
    "PO-1,2026-02-01,1961,ACME,2,P-200,,,,,\n"
    ",,,,,,,,,,\n"
)


@pytest.fixture
def orders(cfg):
    df = read_table(CSV.encode(), "lines.csv")
    return parse_po_table(df, cfg["excel_columns"], "lines.csv")


def test_blank_cells_are_empty_strings_not_nan(orders, cfg):
    [(_, payload)] = build_po_payloads(orders[:1], cfg, "")
    text = to_json(payload)  # allow_nan=False would raise on NaN
    assert '"nan"' not in text.lower() and "NaN" not in text
    line2 = payload["DataLines"][1]
    assert line2["Descr"] == "" and line2["SUSR1"] == ""


def test_empty_quantity_falls_back_to_open_quantity(orders):
    assert orders[0].lines[0].quantity == 3
    assert orders[0].lines[1].quantity == 0


def test_cost_with_thousands_separator(orders):
    assert orders[0].lines[0].unit_price == 1200.0


def test_rows_without_po_are_ignored_and_reported(orders):
    assert len(orders) == 1
    assert any("no PO number" in w for w in orders[0].warnings)


def test_columns_matched_by_name_not_position(cfg):
    # Only 3 columns – the original crashed on df.columns[3] / [4].
    df = pd.DataFrame({"PO Number": ["9"], "Part Number": ["X1"], "Order Quantity": ["2"]})
    [po] = parse_po_table(df, cfg["excel_columns"])
    assert (po.lines[0].item_number, po.lines[0].quantity, po.lines[0].line_number) == ("X1", 2, "1")


def test_custom_alias(cfg):
    df = pd.DataFrame({"Order No": ["5"], "Part Number": ["X"]})
    with pytest.raises(ValueError, match="No PO-number column"):
        parse_po_table(df, cfg["excel_columns"])
    cfg["excel_columns"]["po_number"].append("order no")
    assert parse_po_table(df, cfg["excel_columns"])[0].po_number == "5"


@pytest.mark.parametrize("value, expected", [(None, ""), (float("nan"), ""), ("nan", ""), (" 12.0 ", "12"), ("A", "A")])
def test_clean_str(value, expected):
    assert clean_str(value) == expected


@pytest.mark.parametrize("value, expected", [("1,200.50", 1200.5), (".7067", 0.7067), (float("nan"), 0.0), ("abc", 0.0)])
def test_to_float(value, expected):
    assert to_float(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [("01-FEB-2026 08:39:36", "2026-02-01"), ("2026-02-01 00:00:00", "2026-02-01"), ("46153.506", "2026-05-11"), ("", ""), ("junk", "")],
)
def test_parse_date(value, expected):
    assert parse_date(value) == expected

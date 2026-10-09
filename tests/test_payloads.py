import copy
import io
import json
import random
import zipfile
from datetime import datetime

from payloads import (
    TransactionIds,
    apply_missing_item_policy,
    build_po_payloads,
    clean_orders,
    prepare_orders,
    split_address,
    to_json,
    build_sku_payloads,
    build_zip,
)
from po_model import POLine, PurchaseOrder

# Key layout of the ImportPO sample the user supplied.
SAMPLE_PO_KEYS = {
    "top": ["SSA", "ApplicationHeader", "DataHeader", "DataLines", "ExtraAddresses"],
    "SSA": ["SSA_Login", "SSA_Password"],
    "ApplicationHeader": ["RequestedSystem", "RequestedDate", "TransactionID"],
    "DataHeader": ["Facility", "StorerKey", "ClinetSystemRef", "EXTERNALPOKEY2", "Currency", "Type", "INCOTERMS",
                   "SUSR1", "SUSR2", "Notes", "BuyerAddress", "SupplierAddress"],
    "BuyerAddress": ["Ref", "Company", "Address1", "Address2", "Address3", "Address4", "Country", "City", "Contact",
                     "Phone1", "Phone2", "Email", "State", "ZipCode", "Fax", "W3W", "C_ID"],
    "SupplierAddress": ["Company", "Address1", "Address2", "City", "Country", "Contact", "Phone1", "C_ID"],
    "DataLines": ["ExternLineNo", "SKU", "Descr", "ManufacturerSKU", "SUSR1", "SUSR2", "SUSR3", "SUSR4", "SUSR5",
                  "Qty", "UnitCost", "InitalPromiseDate", "NewPromiseDate", "Notes"],
    "ExtraAddresses": ["SerialKey", "Ref", "Name1", "Name2", "Name3", "Name4", "Address1", "Address2", "Address3",
                       "Address4", "Country", "City", "Postal", "Telephone1", "Telephone2", "Email"],
}


def po_payload(po, cfg, password="", now=None):
    """Same pipeline as the app: prepare (clean, fill SKUs, dedupe), then build."""
    po = copy.deepcopy(po)  # fixtures are shared between tests
    prepare_orders([po], cfg)
    [(_, payload)] = build_po_payloads([po], cfg, password, now=now)
    return payload

# ImportSKU sample the user supplied: many SKUs per payload, minimal fields.
SAMPLE_SKU_KEYS = {
    "top": ["ApplicationHeader", "DataHeader", "SSA"],
    "ApplicationHeader": ["RequestedDate", "RequestedSystem", "TransactionID"],
    "entry": ["Description", "Facility", "SKU", "SerialCount", "StorerKey", "LottableValidationKey"],
    "SSA": ["SSA_Login", "SSA_Password"],
}


def sku_entries(payloads):
    return [e for _, p in payloads for e in p["DataHeader"]]


def sample_line():
    return POLine(
        line_number="1",
        item_number="5061100.01.11.01082",
        description="STEERING ROTARY WITH CABLE FOR NPT75F   MFG JIANGYIN NEPTUNE MARINE APPL   P/N STEERING- NPT75F",
        quantity=2,
        uom="EACH",
        unit_price=1200.0,
    )


def test_sku_payload_matches_sample_shape_and_values(cfg):
    other = sample_line()
    other.line_number, other.item_number, other.description = "2", "5060400.01.11.06071", "SERVICE KIT"
    po = PurchaseOrder(po_number="1", lines=[sample_line(), other])
    [(label, p)], notes = build_sku_payloads([po], cfg, "secret", now=datetime(2020, 3, 24))
    assert label == "2 SKUs" and notes == []
    assert list(p) == SAMPLE_SKU_KEYS["top"]
    assert list(p["ApplicationHeader"]) == SAMPLE_SKU_KEYS["ApplicationHeader"]
    assert list(p["SSA"]) == SAMPLE_SKU_KEYS["SSA"]
    assert p["SSA"] == {"SSA_Login": "wsades", "SSA_Password": "secret"}
    assert p["ApplicationHeader"]["RequestedDate"] == "2020-03-24"
    assert p["ApplicationHeader"]["RequestedSystem"] == "ADES_ORACLE"
    assert [list(e) for e in p["DataHeader"]] == [SAMPLE_SKU_KEYS["entry"]] * 2
    first = p["DataHeader"][0]
    assert first == {
        "Description": "STEERING ROTARY WITH CABLE FOR NPT75F   MFG JIANGYIN NEPTUNE MARINE APPL   P/N STEERING- NPT75F",
        "Facility": "WMWHSE3",
        "SKU": "5061100.01.11.01082",
        "SerialCount": "0",
        "StorerKey": "ADES_GSO",
        "LottableValidationKey": "060000",
    }
    assert p["DataHeader"][1]["SKU"] == "5060400.01.11.06071"


def test_optional_sku_fields_and_empty_values_dropped(cfg):
    cfg["sku_payload"]["fields"] = ["SKU", "SUSR2", "SUSR3", "SUSR9", "Cost", "Price", "ManufacturerSKU"]
    po = PurchaseOrder(po_number="1", lines=[sample_line()])
    [(_, p)], _ = build_sku_payloads([po], cfg, "")
    # SUSR3 is blank by default -> not applicable -> left out
    assert p["DataHeader"][0] == {
        "SKU": "5061100.01.11.01082", "SUSR2": "STEERING ROTARY WI", "SUSR9": "EACH",
        "Cost": 1200.0, "Price": 0.01, "ManufacturerSKU": "5061100.01.11.01082",
    }


def test_skus_per_payload_batches(po_36690, cfg):
    cfg["sku_payload"]["skus_per_payload"] = 50
    payloads, _ = build_sku_payloads([po_36690], cfg, "")
    assert [len(p["DataHeader"]) for _, p in payloads] == [50, 50, 25]
    assert payloads[0][0] == "Batch 1 of 3 (50 SKUs)"
    ids = [p["ApplicationHeader"]["TransactionID"] for _, p in payloads]
    assert len(set(ids)) == 3


def test_common_variables_feed_both_payloads(cfg):
    cfg["common"].update(facility="WMWHSE9", storer_key="STORER_X", lottable_validation_key="999999")
    po = PurchaseOrder(po_number="77", lines=[sample_line()])
    cfg["common"].update(ssa_login="someone", requested_system="SYS_X")
    p = po_payload(po, cfg)
    [(_, sku_payload)], _ = build_sku_payloads([po], cfg, "")
    entry = sku_payload["DataHeader"][0]
    assert p["DataHeader"]["Facility"] == entry["Facility"] == "WMWHSE9"
    assert p["DataHeader"]["StorerKey"] == entry["StorerKey"] == "STORER_X"
    assert p["SSA"]["SSA_Login"] == sku_payload["SSA"]["SSA_Login"] == "someone"
    assert p["ApplicationHeader"]["RequestedSystem"] == sku_payload["ApplicationHeader"]["RequestedSystem"] == "SYS_X"
    assert entry["LottableValidationKey"] == "999999"


def test_transaction_ids_unique_8_digit():
    ids = TransactionIds(random.Random(1))
    got = [ids.next() for _ in range(5000)]
    assert len(set(got)) == 5000
    assert all(len(t) == 8 and t.isdigit() for t in got)


def test_all_skus_in_one_payload_each_once(po_36690, po_107439, cfg):
    payloads, notes = build_sku_payloads([po_36690, po_107439], cfg, "")
    assert len(payloads) == 1
    skus = [e["SKU"] for e in sku_entries(payloads)]
    assert len(skus) == len(set(skus)) == len({ln.item_number for ln in po_36690.lines}) == 125
    assert len(notes) == 5  # every line of 107439 lacks an Item No (no placeholder policy applied here)
    cfg["sku_payload"]["unique_skus"] = False
    payloads, _ = build_sku_payloads([po_36690], cfg, "")
    assert len(sku_entries(payloads)) == 131


def test_missing_item_policy_description_prefix(po_107439):
    po = copy.deepcopy(po_107439)
    apply_missing_item_policy([po], "description_prefix")
    assert [ln.item_number for ln in po.lines] == ["150-576-D-NPN", "A50402-152", "A60402", "A70402-128", "A90402-128"]


def test_placeholder_sku_is_identical_in_po_and_sku_payloads(po_107439, cfg):
    po = copy.deepcopy(po_107439)
    apply_missing_item_policy([po], cfg["parsing"]["missing_item_number"], cfg["parsing"]["missing_item_placeholder"])
    expected = [f"107439-{n}--NOSKU" for n in "12345"]
    po_skus = [ln["SKU"] for ln in po_payload(po, cfg)["DataLines"]]
    sku_payloads, notes = build_sku_payloads([po], cfg, "")
    assert po_skus == expected
    assert [e["SKU"] for e in sku_entries(sku_payloads)] == expected
    assert notes == []
    assert sku_entries(sku_payloads)[0]["Description"].startswith("150-576-D-NPN")


def test_placeholder_pattern_is_configurable(po_107439):
    po = copy.deepcopy(po_107439)
    apply_missing_item_policy([po], "placeholder", "{line}--NOSKU")
    assert po.lines[0].item_number == "1--NOSKU"


def test_placeholders_from_different_pos_do_not_collide(po_107439, cfg):
    other = copy.deepcopy(po_107439)
    other.po_number = "999"
    pos = [copy.deepcopy(po_107439), other]
    apply_missing_item_policy(pos, "placeholder", cfg["parsing"]["missing_item_placeholder"])
    sku_payloads, _ = build_sku_payloads(pos, cfg, "")
    assert len(sku_entries(sku_payloads)) == 10


def test_placeholder_leaves_real_item_numbers_alone(po_36690, cfg):
    po = copy.deepcopy(po_36690)
    apply_missing_item_policy([po], "placeholder")
    assert [ln.item_number for ln in po.lines] == [ln.item_number for ln in po_36690.lines]


def test_po_payload_matches_sample_layout(po_36690, cfg):
    p = po_payload(po_36690, cfg)
    assert list(p) == SAMPLE_PO_KEYS["top"]
    for section in ("SSA", "ApplicationHeader", "DataHeader"):
        assert list(p[section]) == SAMPLE_PO_KEYS[section]
    assert list(p["DataHeader"]["BuyerAddress"]) == SAMPLE_PO_KEYS["BuyerAddress"]
    assert list(p["DataHeader"]["SupplierAddress"]) == SAMPLE_PO_KEYS["SupplierAddress"]
    assert all(list(ln) == SAMPLE_PO_KEYS["DataLines"] for ln in p["DataLines"])
    assert [list(a) for a in p["ExtraAddresses"]] == [SAMPLE_PO_KEYS["ExtraAddresses"]] * 2


def test_po_payload_values_107439(po_107439, cfg):
    p = po_payload(po_107439, cfg, "pw", now=datetime(2026, 2, 16))
    assert p["SSA"] == {"SSA_Login": "wsades", "SSA_Password": "pw"}
    assert p["ApplicationHeader"]["RequestedDate"] == "16/02/2026"
    assert p["ApplicationHeader"]["RequestedSystem"] == "ADES_ORACLE"
    dh = p["DataHeader"]
    assert (dh["Facility"], dh["StorerKey"], dh["ClinetSystemRef"]) == ("WMWHSE3", "ADES_GSO", "107439")
    assert (dh["Currency"], dh["Type"], dh["INCOTERMS"]) == ("USD", "STANDARD", "EXW")
    assert (dh["SUSR1"], dh["SUSR2"]) == ("ADES-878", "7,258.38")
    assert dh["Notes"].startswith("DELIVERY TERMS")
    sup = dh["SupplierAddress"]
    assert (sup["Contact"], sup["C_ID"], sup["City"], sup["Country"]) == ("WOODHOUSE INTERNATIONAL FZE", "1961", "DUBAI", "AE")
    assert dh["BuyerAddress"]["Country"] == "KW"
    assert dh["BuyerAddress"]["Contact"] == "ADVANCED ENERGY SYSTEM ADES"
    line = p["DataLines"][0]
    assert (line["ExternLineNo"], line["SUSR1"], line["SUSR2"], line["SUSR3"], line["SUSR4"]) == ("1", "EACH", "1", "EACH", "1")
    assert (line["SUSR5"], line["Qty"], line["UnitCost"]) == ("5,481.12", 1, 5481.12)
    # promise date 30-MAY-2026 17:30:28 -> epoch ms, written in .NET form
    assert line["InitalPromiseDate"] == line["NewPromiseDate"] == "/Date(1780162228000+0000)/"
    assert r'"InitalPromiseDate": "\/Date(1780162228000+0000)\/"' in to_json(p)
    bill, ship = p["ExtraAddresses"]
    assert (bill["Ref"], bill["Country"], ship["Ref"], ship["Country"]) == ("BILL_TO", "KW", "SHIP_TO", "KW")
    assert bill["SerialKey"] == 9999999999 and bill["Name1"] == "ADVANCED ENERGY SYSTEM ADES"


def test_po_payload_values_36690(po_36690, cfg):
    p = po_payload(po_36690, cfg)
    dh = p["DataHeader"]
    assert (dh["SUSR1"], dh["SUSR2"]) == ("ADM-I", "196,215.07")
    assert dh["Notes"] == "ADM1 - Part list for EMD 2 Repair required - PR. 36135"  # '#' removed
    b = dh["BuyerAddress"]
    # the 56-char printed line is wrapped at a comma to stay within 50
    assert (b["Address1"], b["Address2"], b["Address3"]) == ("B5, Capital Business Park, Floor 4", "Al Sheikh Zayed City", "6th October")
    assert (b["City"], b["Country"]) == ("Giza", "EG")
    assert len(p["DataLines"]) == 131
    first = p["DataLines"][0]
    assert (first["SKU"], first["ManufacturerSKU"], first["SUSR5"]) == ("5060700.01.11.02032", "P/N:40078993NO", "16,901.69")
    assert first["Descr"] == "INJECTOR, FUEL, ECO-TIP, FOR EMD DIESEL - MFG : EMD - P/N 40078993NO"
    bill, ship = p["ExtraAddresses"]
    assert (bill["Address1"], bill["City"], bill["Country"]) == ("ADES Egypt - Legal Address", "Giza", "EG")
    # the delivery location prints no street address -> SHIP_TO mirrors BILL_TO
    assert {k: v for k, v in ship.items() if k != "Ref"} == {k: v for k, v in bill.items() if k != "Ref"}


def test_split_address_variants():
    assert split_address(["ADES Egypt - Legal Address", "B5, Park", "Giza, Egypt"]) == (["ADES Egypt - Legal Address", "B5, Park"], "Giza", "", "EG")
    assert split_address(["B5, Park, Floor 4", "6th October", "Giza", "Egypt"]) == (["B5, Park, Floor 4", "6th October"], "Giza", "", "EG")
    assert split_address(["Company W.L.L.", "Block 8 | P.O. Box 9282", "Kuwait"]) == (["Company W.L.L.", "Block 8 | P.O. Box 9282"], "", "", "KW")


def test_postal_code_is_pulled_out_of_the_address():
    lines = ["United Precision Drilling Company W.L.L.", "Block 8 | Plot 144 | East Ahmadi | P.O. Box 9282 | 61010", "Kuwait"]
    assert split_address(lines) == (
        ["United Precision Drilling Company W.L.L.", "Block 8 | Plot 144 | East Ahmadi | P.O. Box 9282"], "", "61010", "KW"
    )
    assert split_address(["Plot No. S-30229, PO Box 23724 -Postal Code 00000", "Dubai, United Arab Emirates"]) == (
        ["Plot No. S-30229, PO Box 23724"], "Dubai", "00000", "AE"
    )
    # P.O. box numbers and plot numbers are not postal codes
    assert split_address(["P.O BOX NO-329339", "Plot 144", "Kuwait"])[2] == ""


def test_postal_and_short_lines_in_po_107439(po_107439, cfg):
    p = po_payload(po_107439, cfg)
    buyer = p["DataHeader"]["BuyerAddress"]
    assert buyer["ZipCode"] == "61010"
    assert (buyer["Address1"], buyer["Address2"]) == (
        "United Precision Drilling Company W.L.L.", "Block 8, Plot 144, East Ahmadi, P.O. Box 9282"
    )
    bill, ship = p["ExtraAddresses"]
    assert bill["Postal"] == ship["Postal"] == "61010"


def test_no_address_line_over_50_characters(po_36690, po_107439, cfg):
    for po in (po_36690, po_107439):
        p = po_payload(po, cfg)
        blocks = [p["DataHeader"]["BuyerAddress"], p["DataHeader"]["SupplierAddress"], *p["ExtraAddresses"]]
        for block in blocks:
            for key, value in block.items():
                if key.startswith("Address"):
                    assert len(value) <= 50, (key, value)
    bill = po_payload(po_36690, cfg)["ExtraAddresses"][0]
    # 66-char printed line wrapped at a comma, nothing lost
    assert (bill["Address2"], bill["Address3"]) == ("B5, Capital Business Park, Floor 4", "Al Sheikh Zayed City 6th October")


def test_overlong_address_is_cut_and_reported(po_36690, cfg):
    import copy

    po = copy.deepcopy(po_36690)
    po.bill_to_lines = [("Very long street segment " * 3).strip() for _ in range(6)] + ["Egypt"]
    [(_, p)] = build_po_payloads([po], cfg, "")
    assert all(len(p["ExtraAddresses"][0][f"Address{i}"]) <= 50 for i in range(1, 5))
    assert any("BILL_TO address is longer" in w for w in po.warnings)


def _dirty_po():
    line = POLine(line_number="1", item_number="ABC-1", description='GRIP 5-3/4" #2 S&S |X| caf\u00e9 <b>', uom="EACH",
                  supplier_item="S&S/8268641", unit_price=1.0, quantity=1)
    return PurchaseOrder(po_number="9", lines=[line], notes="PR.# 36135 & co; 100%",
                         bill_to_lines=["Block 8 | Plot #144 | 61010", "Kuwait"])


def test_special_characters_are_removed_before_generation(cfg):
    po = _dirty_po()
    clean_orders([po], cfg)
    # the parsed data itself is clean before any payload exists
    assert po.lines[0].description == "GRIP 5-3/4 2 SS X cafe b"
    assert po.lines[0].supplier_item == "SS/8268641"
    assert po.notes == "PR. 36135 co 100"
    # '|' in addresses becomes a separator, so the postal code is still found
    assert po.bill_to_lines == ["Block 8, Plot 144, 61010", "Kuwait"]


def test_payloads_built_from_cleaned_data(cfg):
    po = _dirty_po()
    prepare_orders([po], cfg)
    [(_, p)] = build_po_payloads([po], cfg, "P@ss#1&")
    cfg["sku_payload"]["fields"] = ["Description", "SKU", "SUSR2"]
    [(_, sku)], _ = build_sku_payloads([po], cfg, "P@ss#1&")
    entry = sku["DataHeader"][0]
    assert p["DataLines"][0]["Descr"] == entry["Description"] == "GRIP 5-3/4 2 SS X cafe b"
    assert entry["SUSR2"] == "GRIP 5-3/4 2 SS X"  # 18 chars of the cleaned text
    assert p["DataHeader"]["Notes"] == "PR. 36135 co 100"
    assert p["ExtraAddresses"][0]["Postal"] == "61010"
    assert p["ExtraAddresses"][0]["Address1"] == "Block 8, Plot 144"
    # declared values and secrets are never cleaned
    assert p["SSA"]["SSA_Password"] == sku["SSA"]["SSA_Password"] == "P@ss#1&"
    assert p["ApplicationHeader"]["RequestedSystem"] == "ADES_ORACLE"
    assert (p["ExtraAddresses"][0]["Ref"], p["ExtraAddresses"][1]["Ref"]) == ("BILL_TO", "SHIP_TO")
    assert p["ApplicationHeader"]["RequestedDate"].count("/") == 2


def test_cleaning_can_be_switched_off(cfg):
    cfg["common"]["remove_special_characters"] = False
    po = _dirty_po()
    prepare_orders([po], cfg)
    assert po.lines[0].description == 'GRIP 5-3/4" #2 S&S |X| caf\u00e9 <b>'


def test_parsed_dates_are_not_cleaned(po_107439, cfg):
    cfg["common"]["allowed_special_characters"] = " "  # even '-' removed from text
    po = copy.deepcopy(po_107439)
    clean_orders([po], cfg)
    assert (po.order_date, po.lines[0].promise_date) == ("2026-02-01", "2026-05-30")


def test_transaction_ids_unique_across_po_and_sku(po_36690, cfg):
    ids = TransactionIds()
    pos = build_po_payloads([po_36690], cfg, "", txn_ids=ids)
    skus, _ = build_sku_payloads([po_36690], cfg, "", txn_ids=ids)
    all_ids = [p["ApplicationHeader"]["TransactionID"] for _, p in pos + skus]
    assert len(all_ids) == len(set(all_ids)) == 2  # 1 ImportPO + 1 ImportSKU (all SKUs)


def test_po_overrides(po_36690, cfg):
    cfg["po_payload"]["data_header_overrides"] = {"EXTERNALPOKEY2": "X"}
    cfg["po_payload"]["buyer_address_overrides"] = {"ZipCode": "34423"}
    cfg["po_payload"]["bill_to_overrides"] = {"Postal": "3245504"}
    p = po_payload(po_36690, cfg)
    assert p["DataHeader"]["EXTERNALPOKEY2"] == "X"
    assert p["DataHeader"]["BuyerAddress"]["ZipCode"] == "34423"
    assert p["ExtraAddresses"][0]["Postal"] == "3245504"


def test_zip_layout(po_36690, cfg):
    skus, _ = build_sku_payloads([po_36690], cfg, "")
    data = build_zip(build_po_payloads([po_36690], cfg, ""), skus)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "ImportPO/PO_36690.json" in names
    assert names == ["ImportPO/PO_36690.json", "ImportSKU/ImportSKU.json"]
    for n in names:
        json.loads(zipfile.ZipFile(io.BytesIO(data)).read(n))

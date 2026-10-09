"""
app.py  –  ImportPO + ImportSKU payload generator
Drop Oracle PO PDFs (or the Excel/CSV PO-line export) ➜ download a ZIP with
    ImportPO/PO_<po>.json    – one per purchase order
    ImportSKU/SKU_<sku>.json – one per PO line item (or per distinct SKU)

Run:  streamlit run app.py
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from excel_source import parse_po_table, read_table
from payloads import (
    MISSING_ITEM_MODES,
    TransactionIds,
    build_po_payloads,
    build_sku_payloads,
    build_zip,
    prepare_orders,
    to_json,
)
from pdf_source import PDFParseError, parse_po_pdf
from settings import HERE, USER_PATH, deep_merge, load_config, load_defaults, save_config

SECRETS_PATH = HERE / ".streamlit" / "secrets.toml"  # git-ignored

st.set_page_config(page_title="ImportPO / ImportSKU Generator", layout="wide")

LABELS = {
    "common": "Shared variables (ImportPO + ImportSKU)",
    "po_payload": "ImportPO payload",
    "sku_payload": "ImportSKU payload",
    "parsing": "Parsing",
    "facility": "Facility",
    "storer_key": "StorerKey",
    "lottable_validation_key": "LottableValidationKey (ImportSKU)",
    "ssa_login": "SSA login",
    "remove_special_characters": "Remove special characters from both payloads",
    "allowed_special_characters": "Punctuation to keep (letters, digits, spaces always kept)",
    "address_line_max_length": "Max characters per address line",
    "requested_system": "RequestedSystem",
    "type": "Type (used when the PDF title has none)",
    "company_name": "Company name (BuyerAddress.Contact, ExtraAddresses.Name1)",
    "conversion_factor": "DataLines SUSR2 (conversion factor)",
    "amount_format": "Amount format (header SUSR2 total, line SUSR5)",
    "manufacturer_sku_template": "DataLines ManufacturerSKU template ({pn})",
    "extra_address_serial_key": "ExtraAddresses SerialKey",
    "remove_duplicate_lines": "Remove duplicate lines (same line no. + item) within a PO",
    "buyer_address_overrides": "Fixed values for BuyerAddress fields (JSON)",
    "data_line_overrides": "Fixed values for every DataLines entry (JSON)",
    "bill_to_overrides": "Fixed values for the BILL_TO address (JSON)",
    "ship_to_overrides": "Fixed values for the SHIP_TO address (JSON)",
    "enabled": "Generate ImportSKU payloads",
    "requested_date_format": "RequestedDate format (strftime)",
    "description_max_length": "Max Description length (0 = no limit)",
    "susr2_length": "SUSR2 = first N characters of description",
    "susr3": "SUSR3",
    "price": "Price",
    "shelf_life_indicator": "SHELFLIFEINDICATOR",
    "shelf_life_code_type": "SHELFLIFECODETYPE",
    "one_payload_per_sku": "One payload per distinct SKU (else one per PO line)",
    "data_header_overrides": "Fixed values for any DataHeader field (JSON)",
    "missing_item_number": "When a PO line has no Item No",
    "missing_item_placeholder": "Placeholder SKU pattern ({line}, {po})",
    "excel_dayfirst": "Excel/CSV dates are day-first (DD/MM/YYYY)",
}
MISSING_ITEM_HELP = {
    "placeholder": "Generate a placeholder SKU (pattern below, e.g. 107439-3--NOSKU)",
    "skip": "Leave SKU blank – no ImportSKU payload for that line",
    "supplier_item": "Use the 'Supplier item' value",
    "description_prefix": "Use the text before ' / ' in the description",
}


# ───────────────────────── sidebar: settings ─────────────────────────
def _widget(section: str, key: str, value):
    label, wkey = LABELS.get(key, key), f"cfg.{section}.{key}"
    if key == "missing_item_number":
        return st.selectbox(
            label, MISSING_ITEM_MODES, index=MISSING_ITEM_MODES.index(value),
            format_func=lambda m: MISSING_ITEM_HELP[m], key=wkey,
        )
    if isinstance(value, bool):
        return st.checkbox(label, value, key=wkey)
    if isinstance(value, int):
        return int(st.number_input(label, value=value, step=1, min_value=0, key=wkey))
    if isinstance(value, float):
        return float(st.number_input(label, value=value, format="%.4f", key=wkey))
    if isinstance(value, dict):
        raw = st.text_area(label, json.dumps(value, indent=2), height=90, key=wkey)
        try:
            parsed = json.loads(raw or "{}")
            if not isinstance(parsed, dict):
                raise ValueError("must be a JSON object")
            return parsed
        except ValueError as exc:
            st.error(f"{label}: invalid JSON ({exc}) – ignored")
            return value
    return st.text_input(label, value, key=wkey)


def _stored_password() -> str:
    """SSA_PASSWORD from the environment, Streamlit secrets, or our secrets file."""
    if os.environ.get("SSA_PASSWORD"):
        return os.environ["SSA_PASSWORD"]
    try:
        if st.secrets.get("SSA_PASSWORD"):
            return st.secrets["SSA_PASSWORD"]
    except Exception:  # no secrets.toml anywhere
        pass
    if SECRETS_PATH.exists():
        import tomllib

        return tomllib.loads(SECRETS_PATH.read_text(encoding="utf-8")).get("SSA_PASSWORD", "")
    return ""


def _password_input() -> str:
    password = st.text_input(
        "SSA password (both payloads)", value=_stored_password(), type="password", key="ssa_password",
        help="Used as SSA_Password in ImportPO and ImportSKU. Never written to config.json.",
    )
    remember = st.checkbox(
        "Remember on this computer", value=SECRETS_PATH.exists(), key="ssa_remember",
        help=f"Saves the password to {SECRETS_PATH.relative_to(HERE)} (ignored by git). Untick to forget it.",
    )
    if remember and password:
        SECRETS_PATH.parent.mkdir(exist_ok=True)
        SECRETS_PATH.write_text(f"SSA_PASSWORD = {json.dumps(password)}\n", encoding="utf-8")
    elif not remember and SECRETS_PATH.exists():
        SECRETS_PATH.unlink()
    return password


def settings_sidebar() -> tuple[dict, str]:
    if "base_cfg" not in st.session_state:
        st.session_state.base_cfg = load_config()
    base = st.session_state.base_cfg
    cfg: dict = {}

    st.sidebar.header("Settings")
    for section in ("common", "po_payload", "sku_payload", "parsing"):
        with st.sidebar.expander(LABELS[section], expanded=section == "common"):
            if section == "common":
                password = _password_input()
            cfg[section] = {k: _widget(section, k, v) for k, v in base[section].items()}

    with st.sidebar.expander("Excel/CSV column names (one alias per line)"):
        cfg["excel_columns"] = {}
        for field, aliases in base["excel_columns"].items():
            raw = st.text_area(field, "\n".join(aliases), height=68, key=f"cfg.excel.{field}")
            cfg["excel_columns"][field] = [a.strip() for a in raw.splitlines() if a.strip()]

    c1, c2 = st.sidebar.columns(2)
    if c1.button("💾 Save settings", use_container_width=True):
        save_config(cfg)
        st.session_state.base_cfg = cfg
        st.sidebar.success(f"Saved to {USER_PATH.name}")
    if c2.button("↺ Defaults", use_container_width=True):
        st.session_state.base_cfg = load_defaults()
        for k in [k for k in st.session_state if str(k).startswith("cfg.")]:
            del st.session_state[k]
        st.rerun()
    st.sidebar.download_button(
        "⬇️ Download settings (JSON)", json.dumps(cfg, indent=2), "import_po_config.json",
        "application/json", use_container_width=True,
    )
    upl = st.sidebar.file_uploader("Load settings JSON", type=["json"], key="cfg_upload")
    if upl is not None and st.session_state.get("cfg_upload_name") != upl.name + str(upl.size):
        st.session_state.cfg_upload_name = upl.name + str(upl.size)
        st.session_state.base_cfg = deep_merge(load_defaults(), json.loads(upl.getvalue()))
        for k in [k for k in st.session_state if str(k).startswith("cfg.")]:
            del st.session_state[k]
        st.rerun()
    return cfg, password


# ───────────────────────── parsing ─────────────────────────
@st.cache_data(show_spinner=False)
def _parse_pdf(data: bytes, name: str):
    return parse_po_pdf(data, name)


@st.cache_data(show_spinner=False)
def _parse_table(data: bytes, name: str, aliases_json: str, dayfirst: bool):
    return parse_po_table(read_table(data, name), json.loads(aliases_json), name, dayfirst)


def load_orders(files, cfg) -> tuple[list, list[str]]:
    orders, errors = [], []
    for f in files:
        data = f.getvalue()
        try:
            if f.name.lower().endswith(".pdf"):
                orders.append(copy.deepcopy(_parse_pdf(data, f.name)))
            else:
                orders.extend(copy.deepcopy(_parse_table(
                    data, f.name, json.dumps(cfg["excel_columns"]), cfg["parsing"]["excel_dayfirst"]
                )))
        except (PDFParseError, ValueError) as exc:
            errors.append(f"**{f.name}**: {exc}")
        except Exception as exc:  # keep the other files usable
            errors.append(f"**{f.name}**: unexpected error – {type(exc).__name__}: {exc}")
    return orders, errors


# ───────────────────────── main ─────────────────────────
cfg, password = settings_sidebar()

st.title("PO ➜ ImportPO + ImportSKU payloads")
st.caption(
    "Drop Oracle 'Standard Purchase Order' PDFs and/or the Excel/CSV PO-line export. "
    "You get one ImportPO payload per PO and ImportSKU payloads for the PO's line items."
)

uploads = st.file_uploader(
    "➕  Drop PO PDFs or an Excel (.xlsx / .xls) / CSV file",
    type=["pdf", "xlsx", "xls", "csv"],
    accept_multiple_files=True,
)
if not uploads:
    st.stop()

orders, errors = load_orders(uploads, cfg)
for e in errors:
    st.error(e)
if not orders:
    st.stop()

# Clean special characters, fill missing SKUs, drop duplicate lines - all
# before any payload is generated.
prepare_orders(orders, cfg)

# ── build (before the review, so address warnings show up in it)
try:
    now, txn_ids = datetime.now(), TransactionIds()  # one ID pool: unique across both payload types
    po_payloads = build_po_payloads(orders, cfg, password, now=now, txn_ids=txn_ids)
    sku_payloads, sku_notes = (
        build_sku_payloads(orders, cfg, password, now=now, txn_ids=txn_ids)
        if cfg["sku_payload"]["enabled"] else ([], [])
    )
    zip_bytes = build_zip(po_payloads, sku_payloads)
except (KeyError, ValueError, IndexError) as exc:
    st.error(f"Could not build payloads – check the settings: {type(exc).__name__}: {exc}")
    st.stop()

# ── review
summary = pd.DataFrame(
    [
        {
            "File": po.source_file,
            "PO": po.po_number,
            "Rev": po.revision,
            "Date": po.order_date,
            "Supplier": f"{po.supplier_number} {po.supplier_name}".strip(),
            "Lines": len(po.lines),
            "PO total": po.stated_total,
            "Parsed total": round(sum(ln.line_total or 0 for ln in po.lines), 2) if po.stated_total is not None else None,
            "Warnings": len(po.warnings),
        }
        for po in orders
    ]
)
st.subheader(f"{len(orders)} purchase order(s)")
st.dataframe(summary, hide_index=True, use_container_width=True)

all_warnings = [f"PO {po.po_number}: {w}" for po in orders for w in po.warnings]
if all_warnings:
    with st.expander(f"⚠️ {len(all_warnings)} warning(s)", expanded=any("match" in w for w in all_warnings)):
        st.markdown("\n".join(f"- {w}" for w in all_warnings))

with st.expander("Parsed line items"):
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "PO": po.po_number, "Line": ln.line_number, "Item No (SKU)": ln.item_number,
                    "Description": ln.description, "Qty": ln.quantity, "UOM": ln.uom,
                    "Unit price": ln.unit_price, "Total": ln.line_total,
                    "Promise": ln.promise_date, "Need by": ln.need_by_date, "Supplier item": ln.supplier_item,
                }
                for po in orders for ln in po.lines
            ]
        ),
        hide_index=True, use_container_width=True,
    )

c1, c2, c3 = st.columns(3)
c1.metric("ImportPO payloads", len(po_payloads))
c2.metric("ImportSKU payloads", len(sku_payloads))
c3.metric("Lines without SKU payload", len(sku_notes))
if not password:
    st.warning("SSA password is empty – enter it under 'Shared variables' in the sidebar.")

st.download_button(
    "⬇️  Download all payloads (ZIP)",
    data=zip_bytes,
    file_name=f"payloads_{datetime.now():%Y%m%d_%H%M%S}.zip",
    mime="application/zip",
    type="primary",
)


# ── view & copy a single payload
def _payload_text(payload: dict, reveal: bool) -> str:
    if reveal or not payload["SSA"]["SSA_Password"]:
        return to_json(payload)
    masked = copy.deepcopy(payload)
    masked["SSA"]["SSA_Password"] = "************"
    return to_json(masked)


def copy_button(text: str, label: str = "📋 Copy payload") -> None:
    """Clipboard button (Clipboard API, with execCommand fallback for older browsers)."""
    js_text = json.dumps(text).replace("</", "<\\/")
    components.html(
        f"""
        <button id="copy" style="font:500 14px sans-serif;padding:6px 14px;border-radius:8px;
                border:1px solid #ccc;background:#fff;cursor:pointer">{label}</button>
        <span id="msg" style="font:13px sans-serif;margin-left:8px;color:#2e7d32"></span>
        <script>
          const text = {js_text};
          document.getElementById("copy").onclick = async () => {{
            try {{ await navigator.clipboard.writeText(text); }}
            catch (e) {{
              const ta = document.createElement("textarea");
              ta.value = text; document.body.appendChild(ta); ta.select();
              document.execCommand("copy"); ta.remove();
            }}
            document.getElementById("msg").textContent = "Copied!";
            setTimeout(() => document.getElementById("msg").textContent = "", 2000);
          }};
        </script>""",
        height=42,
    )


st.subheader("View & copy payloads")
kinds = ["ImportPO"] + (["ImportSKU"] if sku_payloads else [])
kind = st.radio("Payload type", kinds, horizontal=True, key="view_kind")
items = po_payloads if kind == "ImportPO" else sku_payloads
idx = st.selectbox(
    "PO number" if kind == "ImportPO" else "SKU",
    range(len(items)),
    format_func=lambda i: items[i][0],
    key=f"view_{kind}",
)
reveal = st.toggle(
    "Include the real SSA password (for copy-paste into the API)", value=False, key="view_reveal",
    disabled=not password,
)
text = _payload_text(items[idx][1], reveal)
b1, b2 = st.columns([1, 1])
with b1:
    copy_button(text)
with b2:
    st.download_button(
        "⬇️ Download this payload", text,
        file_name=f"{'PO' if kind == 'ImportPO' else 'SKU'}_{items[idx][0]}.json",
        mime="application/json", key="dl_one",
    )
st.code(text, language="json")

# ImportPO + ImportSKU payload generator

Drop Oracle "Standard Purchase Order" PDFs (or the Excel/CSV PO-line export) and
download a ZIP with:

- `ImportPO/PO_<po>.json`, one SSA ImportPO payload per purchase order
- `ImportSKU/ImportSKU.json`, one SSA ImportSKU payload listing every distinct item on the PO lines

## Run

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m streamlit run app.py
```

Tests: `.venv/Scripts/python -m pytest -q`. The PDF tests skip if the sample
POs aren't in `tests/fixtures/`.

## Public deployment (Streamlit Community Cloud)

1. On share.streamlit.io, choose **Create app**: repo `aatslr722/import-po-app`,
   branch `main`, main file `app.py`.
2. Under **Advanced settings → Secrets**, add only `PUBLIC_MODE = true`.
   **Do not** add `SSA_PASSWORD`: on a public URL it would be filled in for, and
   downloadable by, every visitor.
3. After it deploys, open **Share** and set the app to public.

In public mode each visitor types the SSA password for their own session.
Nothing per-user is stored on the server, and settings are kept by downloading
and uploading the settings JSON.

## Settings

Defaults live in `default_config.json`. Change them in the sidebar, then
**Save settings** to write `config.json`, which overrides the defaults.
You can also download or upload the settings as JSON.

Shared variables (`common`), used by both payloads:

| Variable | ImportPO | ImportSKU |
|---|---|---|
| `facility` | `DataHeader.Facility` | `DataHeader.Facility` |
| `storer_key` | `DataHeader.StorerKey` | `DataHeader.StorerKey` |
| `ssa_login` | `SSA.SSA_Login` | `SSA.SSA_Login` |
| `requested_system` | `ApplicationHeader.RequestedSystem` | same |
| `lottable_validation_key` | – | `DataHeader.LottableValidationKey` |

Every `*_overrides` setting pins fields of that block to fixed values, e.g.
`buyer_address_overrides: {"ZipCode": "34423"}`.

**Password.** Enter it as **SSA password** under *Shared variables*. Tick
*Remember on this computer* to keep it in `.streamlit/secrets.toml`, which git
ignores; untick to delete it. It's never written to `config.json`. The
`SSA_PASSWORD` environment variable or Streamlit Cloud secrets also work.

**Special characters.** With `remove_special_characters` on (the default),
the parsed PO data is cleaned *before* any payload is generated. Every text
field keeps only letters, digits, spaces and `allowed_special_characters`
(default `. , - / ( ) :`), and accents become plain ASCII. Both payloads, the
"Parsed line items" preview, the 18-character SUSR2, the 50-character address
wrapping and SKU de-duplication therefore all use the cleaned text. In address
lines, `|` becomes `, ` instead of being deleted, so postal codes are still
found. Values you declare in settings, the password, dates and TransactionIDs
are never cleaned, because they don't come from the PO.

Pipeline: parse → `prepare_orders()` (clean → fill missing SKUs → remove
duplicate lines) → build payloads.

**Addresses.** No address line goes over `address_line_max_length` (50)
characters. Long lines are wrapped at `|` or `,` across Address1–4 (Address1–2
for the supplier), and `|` separators become `, `. A trailing postal code
(`… | 61010`, `Postal Code 00000`) goes to `ZipCode` / `Postal`. If an address
still doesn't fit, the last line is cut and a warning is shown.

**Viewing and copying.** Under *View & copy payloads*, pick any ImportPO or
ImportSKU payload to view it, copy it to the clipboard, or download it on its
own. The password shows as `************` unless you switch on *Include the
real SSA password*.

TransactionIDs are random 8-digit numbers, unique across all payloads in one
run.

## ImportPO mapping (PDF → payload)

| Payload field | Source |
|---|---|
| `ApplicationHeader.RequestedDate` | today, `po_payload.requested_date_format` (default `%d/%m/%Y`) |
| `DataHeader.ClinetSystemRef` | PO number |
| `Currency` | currency in the price column header, e.g. `(USD)` |
| `Type` | PDF title ("Standard Purchase Order" → `STANDARD`) |
| `INCOTERMS` | Incoterm |
| `SUSR1` | Delivery Location → Location (rig), e.g. `ADES-878` |
| `SUSR2` | printed total PO value, e.g. `7,258.38` |
| `Notes` | Notes / Supplier Notes |
| `BuyerAddress` | letterhead address block (top left); `Contact` = `company_name` setting |
| `SupplierAddress` | Supplier No. (`C_ID`), Name (`Contact`), address, city, ISO country |
| `DataLines[].ExternLineNo / SKU / Descr` | line no. / Item No / description |
| `DataLines[].ManufacturerSKU` | `P/N:` + last "P/N# …" in the description, else Supplier item |
| `SUSR1`, `SUSR3` / `SUSR2` | UOM / `conversion_factor` setting |
| `SUSR4` / `Qty` | quantity |
| `SUSR5` / `UnitCost` | line total / unit price |
| `InitalPromiseDate`, `NewPromiseDate` | promise date (else need-by date) as `\/Date(ms+0000)\/` |
| `ExtraAddresses` BILL_TO / SHIP_TO | Bill-to / Delivery addresses; SHIP_TO copies BILL_TO when no street address is printed |

## ImportSKU mapping

One ImportSKU payload carries every distinct SKU of the uploaded POs in its
`DataHeader` list. Set `skus_per_payload` to split them into batches.

```json
{
  "ApplicationHeader": {"RequestedDate": "2026-10-09", "RequestedSystem": "ADES_ORACLE", "TransactionID": "37719259"},
  "DataHeader": [
    {"Description": "...", "Facility": "WMWHSE3", "SKU": "5060700.01.11.02032",
     "SerialCount": "0", "StorerKey": "ADES_GSO", "LottableValidationKey": "060000"}
  ],
  "SSA": {"SSA_Login": "wsades", "SSA_Password": ""}
}
```

Each entry sends the fields picked in *ImportSKU payload → fields*. The
default is `Description, Facility, SKU, SerialCount, StorerKey,
LottableValidationKey`. Optional fields are `ManufacturerSKU` (Item No),
`SUSR2` (first 18 characters of the description), `SUSR3`, `SUSR9` (UOM),
`Cost` (unit price), `Price`, `SHELFLIFEINDICATOR` and `SHELFLIFECODETYPE`.
Empty or null values are always left out.

Lines with no printed Item No get a placeholder SKU, `{po}-{line}--NOSKU` by
default (e.g. `107439-3--NOSKU`). It's used identically as ImportPO
`DataLines[].SKU` and ImportSKU `SKU` / `ManufacturerSKU`. Pattern and
behaviour are under *Parsing* in the sidebar.

## How the PDF is read

The parser finds the line-item table by its border lines and assigns words to
columns by x-position, so wrapped descriptions and items split across pages
come out right. As a self-check, the sum of the parsed line totals is compared
with the PO's printed "Total Amount"; any mismatch shows up as a warning.
Scanned (image-only) PDFs are not supported.

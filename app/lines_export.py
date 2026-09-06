"""
Every purchase line, with what we made of it and how much to trust it.

The dashboard says the footprint is 329,620 kg CO2e. This says which 29,407 rows that is
made of: for each one the kilograms, the kg CO2e, the EAT-Lancet group, which rung of
which ladder produced each of those, and how the product was matched at all.

It is the artefact the whole product is for. A caterer can be told a number by anybody;
what they cannot get elsewhere is the ability to take any figure on any screen, open the
rows underneath it, and see that 70% of the weight was matched to a specific product and
the rest to a group average -- stated, per line, rather than averaged into a claim.

There are two ways in and ONE implementation of the sheet, deliberately. `add_sheet` puts
it in the dashboard's workbook next to the summary it explains; `workbook` wraps the same
call for a single file on the Files page. If they were written twice they would drift, and
a client comparing one against the other would be the person who found out.

Nutrition is not here, and not by omission. It is computed, because the footprint ladder
uses NEVO to match; roughly half of it is group averages, so publishing it would invite a
client to rely on a number nobody here stands behind.
"""
from __future__ import annotations

import datetime as dt
import io

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# (key in the scored row, heading, number format, width)
#
# Ordered the way a person reads it: when and where, then what, then how much, then how
# much we trust it. The provenance columns come last because they are the answer to a
# question the earlier columns provoke.
COLUMNS = [
    ("period",         "Period",              None,        10),
    ("restaurant",     "Restaurant",          None,        26),
    ("klantnr",        "Account",             None,        11),
    ("artikelnr",      "Article",             None,        11),
    ("description",    "Product",             None,        38),
    ("category",       "Supplier category",   None,        26),
    ("canon_ce",       "Barcode",             None,        16),
    ("aantal",         "Quantity",            "#,##0.##",  10),
    ("omzet",          "Spend EUR",           "#,##0.00",  12),
    ("kg",             "Kilograms",           "#,##0.###", 12),
    ("kg_eff",         "Kilograms counted",   "#,##0.###", 17),
    ("is_food",        "Food?",               None,         8),
    ("bucket",         "EAT-Lancet group",    None,        18),
    ("co2",            "kg CO2e per kg",      "#,##0.###", 15),
    ("co2_kg",         "kg CO2e",             "#,##0.##",  12),
    ("footprint_src",  "Footprint from",      None,        20),
    ("footprint_conf", "Footprint confidence", "0.00",     19),
    ("bucket_src",     "Group from",          None,        18),
    ("bucket_conf",    "Group confidence",    "0.00",      17),
    ("matched_by",     "Matched by",          None,        13),
]

HEAD_FILL = PatternFill("solid", fgColor="1A4A2A")
HEAD_FONT = Font(color="F2F8F3", bold=True, size=10)
NOTE_FONT = Font(color="3D6647", size=10)
TITLE_FONT = Font(bold=True, size=13, color="1A4A2A")


def _etag(version) -> str | None:
    if isinstance(version, dict):
        return version.get("etag")
    return version or None


def _sheet_notes(ws, client: str, label: str, rows: int, version, extra) -> int:
    """The header a reader needs before the first number. -> the row the table starts on."""
    ws["A1"] = f"{client} — {label}"
    ws["A1"].font = TITLE_FONT
    notes = [
        f"{rows:,} purchase lines. Produced {dt.datetime.now():%Y-%m-%d %H:%M} by PLANETmeal.",
        f"Catalogue version {_etag(version)}.",
        "",
        "Every line carries where its numbers came from. 'Footprint from' and 'Group from' "
        "name the rung that produced each: a curated decision, a specific RIVM product, or "
        "a group average — weakest last.",
        "Confidence is 1.00 for a decision made about this exact product and falls as the "
        "match gets coarser. A line with no weight contributes 0 kg and 0 kg CO2e; it is "
        "not dropped, it is here with zeros so the gap is visible.",
        "'Kilograms counted' is what the footprint was calculated from and is what the "
        "dashboard totals; 'Kilograms' is what the file said. They differ where a line "
        "was sold by the piece.",
        "HOW TO RECONCILE THIS WITH THE DASHBOARD. Filter Food? = food, then sum "
        "'Kilograms counted' and 'kg CO2e' — those two totals are the headline exactly. "
        "Non-food lines are in this sheet as well, and they carry a footprint of their "
        "own that the headline deliberately excludes: a client is answerable for the food "
        "they buy, and cling film is not food. Sum every row and you will get a larger "
        "number than the dashboard shows, which is why this says so here.",
        "Nutrition is deliberately absent. It is computed to help match products, but "
        "around half of it is group averages, so it is not published.",
    ]
    notes.extend(extra or ())
    r = 2
    for n in notes:
        ws.cell(row=r, column=1, value=n).font = NOTE_FONT
        r += 1
    return r + 1


def add_sheet(wb, rows: list[dict], client: str, label: str, version=None,
              title: str = "lines", extra_notes=()) -> None:
    """Put the per-line sheet into an existing workbook.

    Used both on its own and as the last sheet of the dashboard export, where it is the
    evidence for every summary sheet above it.
    """
    ws = wb.create_sheet(title[:31])
    start = _sheet_notes(ws, client, label, len(rows), version or {}, extra_notes)

    for i, (_key, head, _fmt, width) in enumerate(COLUMNS, start=1):
        c = ws.cell(row=start, column=i, value=head)
        c.fill, c.font = HEAD_FILL, HEAD_FONT
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = ws.cell(row=start + 1, column=1)

    for n, row in enumerate(rows, start=start + 1):
        period = f"{int(row.get('year') or 0):04d}-{int(row.get('month') or 0):02d}"
        for i, (key, _head, fmt, _w) in enumerate(COLUMNS, start=1):
            if key == "period":
                v = period
            elif key == "is_food":
                v = "food" if row.get("is_food") else "non-food"
            else:
                v = row.get(key)
            cell = ws.cell(row=n, column=i, value=v)
            if fmt and isinstance(v, (int, float)):
                cell.number_format = fmt

    ws.auto_filter.ref = f"A{start}:{get_column_letter(len(COLUMNS))}{start + len(rows)}"


def workbook(rows: list[dict], client: str, label: str, version=None) -> bytes:
    """-> xlsx bytes, the sheet on its own. One row per purchase line, as scored."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    add_sheet(wb, rows, client, label, version)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()

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

import charts

# (key in the scored row, heading, number format, width)
#
# Ordered the way a person reads it: when and where, then what, then how much, then how
# much we trust it. The provenance columns come last because they are the answer to a
# question the earlier columns provoke.
#
# 4 Oct 2026: "Footprint from" printed the engine's tier code -- "curated_pin",
# "rivm_group_avg" -- which said how MiSt works and not what a number was. It is now the
# dashboard's precision word, and three columns say what the footprint was MATCHED TO: a
# named RIVM product, a named group average or a named bucket average, each of which is
# listed with its value (and its members) in the sheets after this one.
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
    ("share",          "Share counted",       "0%",        13),
    ("adjustment",     "Adjustment",          None,        20),
    ("kg_eff",         "Kilograms counted",   "#,##0.###", 17),
    ("is_food",        "Food?",               None,         8),
    ("bucket",         "EAT-Lancet group",    None,        18),
    ("co2",            "kg CO2e per kg",      "#,##0.###", 15),
    ("co2_kg",         "kg CO2e",             "#,##0.##",  12),
    ("precision",      "Precision",           None,        12),
    ("reference_kind", "Matched to (type)",   None,        19),
    ("matched_to",     "Matched to",          None,        36),
    ("nevo_code",      "NEVO code",           None,        11),
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
        "Every line says what its footprint was matched to. 'Matched to' names it: a "
        "specific RIVM product (with its NEVO code), a RIVM group average, or a bucket "
        "average. 'kg CO2e per kg' is that reference's value. Every one of them is listed "
        "in the sheets after this one, and every average with the RIVM items it is the "
        "mean of.",
        "'Precision' is the word the dashboard uses: Exact (checked for this exact "
        "product), Close (matched to a similar known product), Estimated (an average for "
        "its food group). 'Group from' says how the line's EAT-Lancet group was decided.",
        "Confidence is 1.00 for a decision made about this exact product and falls as the "
        "match gets coarser. A line with no weight contributes 0 kg and 0 kg CO2e; it is "
        "not dropped, it is here with zeros so the gap is visible.",
        "'Kilograms' is what the file said was bought. 'Kilograms counted' is what the "
        "footprint was calculated from and what the dashboard totals. They differ only "
        "where MiSt has adjusted how much of a product counts: 'Share counted' says how "
        "much, and 'Adjustment' names it.",
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
            elif key == "precision":
                v = charts.grade(row.get("footprint_src"))
            elif key == "matched_to":
                v = matched_name(row)
            elif key == "nevo_code":
                v = nevo_code(row)
            else:
                v = row.get(key)
            cell = ws.cell(row=n, column=i, value=v)
            if fmt and isinstance(v, (int, float)):
                cell.number_format = fmt

    ws.auto_filter.ref = f"A{start}:{get_column_letter(len(COLUMNS))}{start + len(rows)}"


# --------------------------------------------------------------------------- matched to
def matched_name(row: dict) -> str | None:
    """What a line or a product was matched to, as a reader names it.

    A RIVM product or group by its own name; a bucket by the food-group words the dashboard
    uses ("Dairy", not "dairy"), so the bucket sheet and the dashboard agree.
    """
    ref, name = row.get("reference"), row.get("reference_name")
    if not ref:
        return None
    if str(ref).startswith("bucket:"):
        return charts.food_group_label(str(ref).split(":", 1)[1])
    return name


def nevo_code(row: dict) -> int | None:
    ref = str(row.get("reference") or "")
    if ref.startswith("rivm:") and ref[5:].isdigit():
        return int(ref[5:])
    return None


def _table(wb, title: str, notes: list[str], head: list[tuple], rows: list[list]) -> None:
    """A reference sheet: a title, a line or two saying what it is, then one table."""
    ws = wb.create_sheet(title[:31])
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    r = 2
    for n in notes:
        ws.cell(row=r, column=1, value=n).font = NOTE_FONT
        r += 1
    start = r + 1
    for i, (name, fmt, width) in enumerate(head, start=1):
        c = ws.cell(row=start, column=i, value=name)
        c.fill, c.font = HEAD_FILL, HEAD_FONT
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    for n, row in enumerate(rows, start=start + 1):
        for i, ((_name, fmt, _w), v) in enumerate(zip(head, row), start=1):
            cell = ws.cell(row=n, column=i, value=v)
            if fmt and isinstance(v, (int, float)):
                cell.number_format = fmt
    ws.freeze_panes = ws.cell(row=start + 1, column=1)
    if rows:
        ws.auto_filter.ref = f"A{start}:{get_column_letter(len(head))}{start + len(rows)}"


def add_reference_sheets(wb, rows: list[dict], averages: dict | None) -> None:
    """What the lines were matched to, with its value: the RIVM products, the group
    averages, the bucket averages, and the RIVM items every average is the mean of.

    Asked for on 4 Oct 2026, so that "Matched to: Beverages - cold soft/juice" on a line
    can be looked up -- its value, and which thirteen RIVM drinks it averages -- in the same
    file. `rows` are the lines in this workbook; the "your products" columns count them.
    `averages` is catalogue.averages(); without it only the RIVM products sheet is written.
    """
    used: dict[str, dict] = {}
    for r in rows:
        ref = r.get("reference")
        if not ref:
            continue
        u = used.setdefault(ref, dict(products=set(), kg=0.0, co2=0.0, per_kg=r.get("co2"),
                                      name=matched_name(r)))
        u["products"].add(r.get("artikelnr"))
        u["kg"] += r.get("kg_eff") or 0.0
        u["co2"] += r.get("co2_kg") or 0.0

    def mine(ref):
        u = used.get(ref)
        return (len(u["products"]), round(u["kg"], 1), round(u["co2"], 1)) if u else (0, 0.0, 0.0)

    rivm = sorted(((k, u) for k, u in used.items() if k.startswith("rivm:")),
                  key=lambda kv: -kv[1]["co2"])
    _table(wb, "RIVM products used",
           ["Every RIVM product a line in this file was matched to, with its value. "
            "RIVM 2024, distribution stage: kg CO2e per kg of product as bought.",
            "Your products / kg counted / kg CO2e: the lines in this file matched to it."],
           [("NEVO code", None, 11), ("RIVM product", None, 40),
            ("kg CO2e per kg", "#,##0.###", 15), ("your products", "#,##0", 14),
            ("kg counted", "#,##0.#", 14), ("kg CO2e", "#,##0.#", 13)],
           [[int(k[5:]) if k[5:].isdigit() else k[5:], u["name"], u["per_kg"], *mine(k)]
            for k, u in rivm])
    if not averages:
        return

    groups = averages.get("groups") or []
    _table(wb, "RIVM group averages",
           ["The group averages a product is priced with when no specific RIVM product "
            "fits. Each is the plain mean of the RIVM items listed for it in 'Average "
            "members'.",
            "Your products / kg counted / kg CO2e: the lines in this file priced with it."],
           [("group average", None, 36), ("RIVM food group it is part of", None, 32),
            ("RIVM items averaged", "#,##0", 12), ("kg CO2e per kg", "#,##0.###", 15),
            ("your products", "#,##0", 14), ("kg counted", "#,##0.#", 14),
            ("kg CO2e", "#,##0.#", 13), ("note", None, 70)],
           [[g["group_name"], g.get("parent_group"), g.get("n_items"), g.get("co2"),
             *mine(f"group:{g['group_name']}"), g.get("note")]
            for g in sorted(groups, key=lambda g: g["group_name"])])

    buckets = averages.get("buckets") or []
    _table(wb, "Bucket averages",
           ["The coarsest fallback: the mean of every RIVM item in one food group. Used "
            "only when nothing finer fits, and marked Estimated.",
            "Your products / kg counted / kg CO2e: the lines in this file priced with it."],
           [("bucket average", None, 22), ("key", None, 16),
            ("RIVM items averaged", "#,##0", 12), ("kg CO2e per kg", "#,##0.###", 15),
            ("your products", "#,##0", 14), ("kg counted", "#,##0.#", 14),
            ("kg CO2e", "#,##0.#", 13), ("note", None, 70)],
           [[charts.food_group_label(b["bucket"]), b["bucket"], b.get("n_items"), b.get("co2"),
             *mine(f"bucket:{b['bucket']}"), b.get("note")]
            for b in sorted(buckets, key=lambda b: charts.food_group_label(b["bucket"]))])

    members = []
    for g in sorted(groups, key=lambda g: g["group_name"]):
        for m in g.get("members") or []:
            members.append(["RIVM group average", g["group_name"], g.get("co2"),
                            m.get("nevo_code"), m.get("name"), m.get("co2")])
    for b in sorted(buckets, key=lambda b: charts.food_group_label(b["bucket"])):
        for m in b.get("members") or []:
            members.append(["Bucket average", charts.food_group_label(b["bucket"]), b.get("co2"),
                            m.get("nevo_code"), m.get("name"), m.get("co2")])
    _table(wb, "Average members",
           ["Every RIVM item each average is the mean of. Filter on the average's name to "
            "see what it is made of; the mean of its rows is its value."],
           [("type", None, 20), ("average", None, 34), ("average kg CO2e per kg", "#,##0.###", 16),
            ("NEVO code", None, 11), ("RIVM item", None, 40), ("kg CO2e per kg", "#,##0.###", 15)],
           members)


def workbook(rows: list[dict], client: str, label: str, version=None,
             averages: dict | None = None) -> bytes:
    """-> xlsx bytes: the lines, then what they were matched to. One row per line, as scored.

    `averages` defaults to asking the catalogue for them.
    """
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    add_sheet(wb, rows, client, label, version)
    add_reference_sheets(wb, rows, averages if averages is not None else _averages())
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


def _averages() -> dict | None:
    import catalogue
    return catalogue.averages()

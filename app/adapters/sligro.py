"""
Sligro export adapter — TU Delft's format.

THIS FILE IS THE CANONICAL DEFINITION OF THE MASS FORMULA for the app. The catalogue repo
holds a second copy in `sligro_parse.py`, used only to rebuild the historical reference
baseline. `test_mass.py` pins both to the same worked examples, so if they ever drift the
test fails rather than the numbers quietly moving.

Three things about this format are dangerous and all three are handled here.

1. `kg = Aantal x IVP x Maat x unit_factor(Eenh)`. IVP **must** be multiplied. Two
   packaging conventions coexist and this one formula covers both:
     IVP = 1  -> Maat is the FULL colli weight   'KERN KROKET 28X80G' : IVP 1, Maat 2.24 KG
     IVP > 1  -> Maat is the PER-UNIT size       'MEYERIJ VOLLE MELK' : IVP 12, Maat 1 LT
   Dropping IVP moves every number in the project by ~45%, silently.

2. The file is **cumulative year-to-date**, and the last (Aantal, Omzet) pair is the
   running total, not a month. Counting it doubles the file. Two files from the same year
   therefore overlap — which is what the pre-flight overlap check exists to catch.

3. **The files are not one shape.** Measured across all 20 TU Delft exports:
     * 8 have a header block, on row 5, 7 or 9 — the row varies;
     * 12 have no header and start at data on row 1;
     * `Januari 2025.xlsx` carries two EXTRA columns (`GBR omschrijving`, `GBR reknr.`),
       so its months start at column 18, not 17.
   A fixed column position is therefore wrong for at least one real file. Read the header
   when there is one; fall back to positions only when there is not, and say which
   happened.

Where a header exists it is far better than guessing, because it states outright what we
would otherwise infer:

    row 2   Meetperiode: van 20250101 t/m 20250531     <- the period
    row 5   Jaar / 2025                                <- the year, no filename needed
    row 7   Maand Nr.
    row 8   1    2    3    4    5    Totaal            <- which month each pair is,
    row 9   Klantnr  Naam  ...  Aantal  Omzet  ...        and which pair is the total
"""
from __future__ import annotations

import os
import re

import openpyxl
from openpyxl.utils import get_column_letter

NAME = "sligro"
LABEL = "Sligro export"
DESCRIPTION = ("The spreadsheet Sligro exports per customer. Cumulative year-to-date, one "
               "column pair per month. Read by column name where the file has a header, "
               "by position where it does not.")

# Fallback positions, used only for a file with no header row.
COL_KLANTNR, COL_RESTAURANT, COL_CITY, COL_ARTIKELNR = 0, 1, 2, 3
COL_IVP, COL_VP, COL_MAAT, COL_EENH = 4, 5, 6, 7
COL_DESCRIPTION, COL_BRAND = 8, 9
COL_ARTIKELGROEP, COL_FOODFLAG, COL_EAN_CE, COL_EAN_HE = 11, 12, 13, 14
FIRST_MONTH_COL = 17

# Header names as Sligro writes them, normalised. Several spellings exist across exports.
FIELDS = {
    "klantnr": ["klantnr"],
    "restaurant": ["naam"],
    "city": ["woonplaats"],
    "artikelnr": ["artikelnr"],
    "ivp": ["ivp"],
    "vp": ["vp"],
    "maat": ["maat"],
    "eenh": ["eenh", "eenheid"],
    "description": ["artikelomschrijving", "omschrijving"],
    "brand": ["merknaam"],
    "artikelgroep": ["artikelgroep"],
    "foodflag": ["btw"],
    "ean_ce": ["eance"],
    "ean_he": ["eanhe"],
}
QUANTITY_HEADER = "aantal"          # 'Aantal ( colli/kg )'
REVENUE_HEADER = "omzet"            # 'Omzet ( euro )'
TOTAL_LABEL = "totaal"

UNIT = {"KG": 1.0, "K": 1.0, "GR": 0.001, "G": 0.001, "LT": 1.0, "L": 1.0,
        "CL": 0.01, "ML": 0.001, "DL": 0.1}

DUTCH_MONTHS = {"januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
                "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11,
                "december": 12}

HEADER_SCAN_ROWS = 20               # how far down to look for a header block


class SligroError(Exception):
    """The file cannot be read. The message names the row and column at fault."""


def cell_ref(row: int, col_index: int) -> str:
    """'R9' — a reference a person can actually find in Excel."""
    return f"{get_column_letter(col_index + 1)}{row}"


# --------------------------------------------------------------------------- the formula
def unit_factor(eenh: str, vp: str):
    """kg per unit of Maat, or None when the line has no derivable weight."""
    f = UNIT.get(eenh)
    if f is None and vp == "KG":
        f = 1.0                        # weight-sold: Aantal is already kilograms (Maat=1)
    return f


def mass_kg(aantal, ivp, maat, eenh, vp):
    """kg = Aantal x IVP x Maat x unit_factor(Eenh).  -> (kg, known)

    known=0 marks a true piece-sold line: no weight exists in the file, so it contributes
    zero. Those lines are counted and reported, never silently dropped.
    """
    f = unit_factor(eenh, vp)
    if f is None or maat is None:
        return 0.0, 0
    return aantal * ivp * maat * f, 1


def parse_ivp(v):
    try:
        return float(str(v).strip()) or 1.0
    except Exception:
        return 1.0


def months_in_file(ncol: int) -> int:
    """How many REAL months a POSITIONAL file covers.

    The last (Aantal, Omzet) pair is the year-to-date total, so it is always dropped.
    Only used when the file has no header; a header states the months outright.
    """
    n = (ncol - FIRST_MONTH_COL) // 2
    return n - 1 if n > 1 else 1


def year_from_filename(name: str):
    """'December 2025.xlsx' -> (2025, 12). Case-insensitive. -> (year, month) or None."""
    stem = os.path.splitext(os.path.basename(name or ""))[0].strip().lower()
    m = re.search(r"([a-z]+)\s*[-_ ]\s*(\d{4})", stem)
    if m and m.group(1) in DUTCH_MONTHS:
        return int(m.group(2)), DUTCH_MONTHS[m.group(1)]
    m = re.search(r"(\d{4})\s*[-_ ]\s*([a-z]+)", stem)
    if m and m.group(2) in DUTCH_MONTHS:
        return int(m.group(1)), DUTCH_MONTHS[m.group(2)]
    return None


# --------------------------------------------------------------------------- header
def _text(v) -> str:
    """Cell text, stripped — including the literal apostrophes some exports wrap values in.

    'Januari 2026.xlsx' stores text as "'KP'" rather than "KP". Left alone that breaks
    the unit lookup (so every mass becomes unknown) and every description-based rule.
    """
    if v is None:
        return ""
    s = str(v).strip()
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        s = s[1:-1].strip()
    return s


def _textish(v) -> bool:
    """A cell holding real text, as opposed to a number or a blank."""
    return isinstance(v, str) and v.strip() != ""


def _norm(v) -> str:
    return re.sub(r"[^a-z0-9]", "", _text(v).lower()) if v is not None else ""


def _scan(ws) -> list:
    return list(ws.iter_rows(min_row=1, max_row=HEADER_SCAN_ROWS, values_only=True))


def find_header(rows: list):
    """Locate the column-name row. -> (row_number, {field: col_index}, month_cols) or None.

    `month_cols` is every column where a quantity begins, in order.
    """
    for i, row in enumerate(rows, start=1):
        norm = [_norm(v) for v in row]
        if "klantnr" not in norm or "artikelnr" not in norm:
            continue
        # The MiSt template also names a Klantnr and an Artikelnr. What it always has and a
        # Sligro export never has is a 'periode' column, because Sligro puts its periods in
        # columns rather than rows. That is the discriminator.
        if "periode" in norm:
            return None
        cols = {}
        for field, names in FIELDS.items():
            for n in names:
                if n in norm:
                    cols[field] = norm.index(n)
                    break
        month_cols = [j for j, v in enumerate(norm) if v.startswith(QUANTITY_HEADER)]
        return i, cols, month_cols
    return None


def find_month_numbers(rows: list, month_cols: list):
    """Read the 'Maand Nr.' block. -> ({col_index: month}, total_col, year)

    This is the part worth having. The file says which month each column pair is AND
    which pair is the year-to-date total, so neither has to be inferred.
    """
    labels_row = None
    for i, row in enumerate(rows):
        if any(_norm(v) == "maandnr" for v in row):
            labels_row = i + 1                        # the values sit on the next row
            break
    year = None
    for i, row in enumerate(rows):
        if any(_norm(v) == "jaar" for v in row):
            nxt = rows[i + 1] if i + 1 < len(rows) else ()
            for v in nxt:
                if v is not None and str(v).strip().isdigit() and len(str(v).strip()) == 4:
                    year = int(str(v).strip())
                    break
            break
    if labels_row is None or labels_row >= len(rows):
        return {}, None, year

    row = rows[labels_row]
    months, total_col = {}, None
    for j in month_cols:
        v = row[j] if j < len(row) else None
        s = str(v).strip() if v is not None else ""
        if _norm(s) == TOTAL_LABEL:
            total_col = j
        elif s.isdigit() and 1 <= int(s) <= 12:
            months[j] = int(s)
    return months, total_col, year


def find_quantity_start(ws, sample: int = 60):
    """Where do the (Aantal, Omzet) pairs begin, in a file with no header?

    Walk in from the RIGHT: every trailing column is a quantity or a revenue, so the block
    starts immediately after the last column that holds real text. Verified against all 20
    TU Delft exports: 18 of them start at column 17, and the two January files start at 18
    because they carry two extra columns. Assuming 17 reads the wrong column in those two.

    -> (start_index, n_pairs, trailing_orphan)
    """
    rows = [r for r in ws.iter_rows(values_only=True)
            if r and r[0] is not None and str(r[0]).strip().replace(".", "").isdigit()]
    rows = rows[:sample]
    if not rows:
        return None, 0, False
    ncol = ws.max_column
    last_text = None
    for j in range(ncol - 1, 10, -1):
        hits = sum(1 for r in rows if j < len(r) and _textish(r[j]))
        if hits / len(rows) > 0.5:
            last_text = j
            break
    if last_text is None:
        return None, 0, False
    start = last_text + 1
    span = ncol - start
    return start, span // 2, bool(span % 2)


def read_meetperiode(rows: list):
    """'Meetperiode: van 20250101 t/m 20250531' -> (2025, 1, 2025, 5) or None."""
    for row in rows:
        for v in row:
            if v is None:
                continue
            m = re.search(r"meetperiode.*?(\d{8}).*?(\d{8})", str(v), re.I)
            if m:
                a, b = m.group(1), m.group(2)
                return int(a[:4]), int(a[4:6]), int(b[:4]), int(b[4:6])
    return None


# --------------------------------------------------------------------------- adapter
def detect(path: str, filename: str | None = None) -> bool:
    """Either a header block naming Klantnr and Artikelnr, or numeric klantnr in column 0."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows = _scan(ws)
        if find_header(rows) is not None:
            return True
        if ws.max_column < FIRST_MONTH_COL + 2:
            return False
        for row in rows:
            if row and row[0] is not None and str(row[0]).strip().replace(".", "").isdigit():
                return True
        return False
    finally:
        wb.close()


def read(path: str, filename: str | None = None, year: int | None = None):
    from . import Reading                       # local import: avoids a circular import

    filename = filename or os.path.basename(path)
    notes, quirks = [], []

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    scan = _scan(ws)
    found = find_header(scan)

    # ---------------------------------------------------------------- layout
    if found:
        header_row, cols, month_cols = found
        missing = [f for f in ("klantnr", "artikelnr", "description", "ivp", "vp",
                               "maat", "eenh", "artikelgroep") if f not in cols]
        if missing:
            wb.close()
            raise SligroError(
                f"the header on row {header_row} is missing required column(s): "
                f"{', '.join(missing)}. Found: "
                f"{', '.join(sorted(cols))}.")
        if not month_cols:
            wb.close()
            raise SligroError(
                f"the header on row {header_row} has no 'Aantal' column, so there is "
                "nothing to read quantities from.")

        month_map, total_col, header_year = find_month_numbers(scan, month_cols)
        first_ref = cell_ref(header_row, month_cols[0])

        if month_map:
            # the file states its own months and its own total column
            take = dict(month_map)
            if total_col is not None:
                quirks.append(dict(
                    code="ytd_total_dropped", severity="ok",
                    message=(f"The file labels its own columns. The year-to-date total is "
                             f"column {get_column_letter(total_col + 1)} and was excluded — "
                             "counting it would have doubled this file."),
                    location=cell_ref(header_row, total_col)))
            notes.append(
                f"LAYOUT: read by column NAME from the header on row {header_row}; months "
                f"{', '.join(str(m) for m in sorted(take.values()))} taken from the "
                f"'Maand Nr.' row, not inferred.")
        else:
            # header, but no month block: fall back to dropping the last pair
            usable = month_cols[:-1] if len(month_cols) > 1 else month_cols
            take = {c: i + 1 for i, c in enumerate(usable)}
            quirks.append(dict(
                code="ytd_total_dropped", severity="ok",
                message=("This file has a header but no 'Maand Nr.' row, so the months were "
                         "counted from the first quantity column and the last pair was "
                         "treated as the year-to-date total and excluded."),
                location=first_ref))

        if year is None and header_year:
            year = header_year
            notes.append(f"Year {year} read from the 'Jaar' row inside the file.")
        period = read_meetperiode(scan)
        if period:
            notes.append(f"The file states its measurement period: "
                         f"{period[0]}-{period[1]:02d} to {period[2]}-{period[3]:02d}.")
            if year is None:
                year = period[0]
        if month_cols[0] != FIRST_MONTH_COL:
            quirks.append(dict(
                code="nonstandard_layout", severity="info",
                message=(f"This export puts its first quantity in column "
                         f"{get_column_letter(month_cols[0] + 1)}, not the usual column R. "
                         "Reading by column name rather than by position, so it is handled."),
                location=first_ref))
        data_from = header_row + 1
    else:
        # ---------------------------------------------------------------- no header
        cols = dict(klantnr=COL_KLANTNR, restaurant=COL_RESTAURANT, city=COL_CITY,
                    artikelnr=COL_ARTIKELNR, ivp=COL_IVP, vp=COL_VP, maat=COL_MAAT,
                    eenh=COL_EENH, description=COL_DESCRIPTION, brand=COL_BRAND,
                    artikelgroep=COL_ARTIKELGROEP, foodflag=COL_FOODFLAG,
                    ean_ce=COL_EAN_CE, ean_he=COL_EAN_HE)
        start, npairs, orphan = find_quantity_start(ws)
        if start is None or npairs < 1:
            wb.close()
            raise SligroError(
                "this file has no header row and no recognisable block of quantity columns, "
                "so there is no safe way to tell which columns hold the months. Re-export it "
                "WITH the header row and it can be read by column name instead.")
        nmonths = npairs - 1 if npairs > 1 else 1
        take = {start + 2 * (m - 1): m for m in range(1, nmonths + 1)}
        data_from = 1
        notes.append(
            f"LAYOUT: no header row, so columns were located by structure; quantities "
            f"start at column {get_column_letter(start + 1)}.")
        quirks.append(dict(
            code="positional_read", severity="warning",
            message=(f"This file has no header row. The quantity columns were located by "
                     f"structure: they begin at column {get_column_letter(start + 1)} and "
                     f"run to the end, giving {npairs} pair(s) — {nmonths} month(s) plus the "
                     "year-to-date total, which was excluded. That is a deduction, not a "
                     "statement by the file. An export WITH its header row is safer, because "
                     "it names its own columns and its own months."),
            location=cell_ref(1, start)))
        if start != FIRST_MONTH_COL:
            quirks.append(dict(
                code="nonstandard_layout", severity="warning",
                message=(f"This export carries extra columns: its quantities start at column "
                         f"{get_column_letter(start + 1)}, not the usual column R. Reading "
                         f"column R would have taken the wrong values entirely."),
                location=cell_ref(1, start)))
        if orphan:
            quirks.append(dict(
                code="odd_column_count", severity="warning",
                message=(f"There is one column left over after pairing quantities with "
                         f"revenues (column {get_column_letter(ws.max_column)}). It was "
                         "ignored. Worth confirming the export is complete."),
                location=cell_ref(1, ws.max_column - 1)))

    # ---------------------------------------------------------------- the year
    if year is None:
        guess = year_from_filename(filename)
        if guess is None:
            wb.close()
            raise SligroError(
                f"cannot tell which year {filename!r} covers. This export carries no 'Jaar' "
                "row and no measurement period, so the year has to come from the filename — "
                "keep the original name (for example 'December 2025.xlsx'), or state the "
                "year explicitly.")
        year = guess[0]
        notes.append(f"Year {year} taken from the filename; this file does not carry one.")

    # ---------------------------------------------------------------- rows
    def get(r, field):
        j = cols.get(field)
        return r[j] if j is not None and j < len(r) else None

    prod, lines, problems, source_rows = {}, [], [], []
    for n, r in enumerate(ws.iter_rows(min_row=data_from, values_only=True), start=data_from):
        kl = get(r, "klantnr")
        if kl is None or not _text(kl).replace(".", "").isdigit():
            continue
        art = get(r, "artikelnr")
        if art is None:
            problems.append(dict(row=n, cell=cell_ref(n, cols["artikelnr"]),
                                 problem="no article number on this row"))
            continue
        art = _text(art)

        if art not in prod:
            ce, he = _text(get(r, "ean_ce")), _text(get(r, "ean_he"))
            maat_v = get(r, "maat")
            prod[art] = (
                art,
                _text(get(r, "description")),
                _text(get(r, "brand")),
                _text(get(r, "artikelgroep")),
                _text(get(r, "ivp")),
                _text(get(r, "vp")).upper(),
                maat_v if isinstance(maat_v, (int, float)) else None,
                _text(get(r, "eenh")).upper(),
                ce if ce.isdigit() and ce != "0" else "",
                he if he.isdigit() and he != "0" else "",
                _text(get(r, "foodflag")))

        maat = get(r, "maat")
        maat = maat if isinstance(maat, (int, float)) else None
        eenh = _text(get(r, "eenh")).upper()
        vp = _text(get(r, "vp")).upper()
        ivp = parse_ivp(_text(get(r, "ivp")))

        for ci, month in sorted(take.items(), key=lambda kv: kv[1]):
            a = r[ci] if ci < len(r) else None
            o = r[ci + 1] if ci + 1 < len(r) else None
            if not isinstance(a, (int, float)) or a == 0:
                continue
            kg, known = mass_kg(a, ivp, maat, eenh, vp)
            lines.append((year, month,
                          _text(kl),
                          _text(get(r, "restaurant")),
                          _text(get(r, "city")),
                          art, float(a),
                          float(o) if isinstance(o, (int, float)) else 0.0,
                          kg, known, "complete"))
            source_rows.append(n)
    wb.close()

    if not lines:
        where = (f"quantities were read from column "
                 f"{get_column_letter(min(take) + 1)} onwards" if take else "no month columns were found")
        raise SligroError(
            f"no purchase lines with a quantity were found — {where}. If this is a Sligro "
            "export, its columns may not be where they are expected; re-exporting WITH the "
            "header row lets us read it by column name instead.")

    quirks.append(dict(
        code="cumulative_file", severity="info",
        message=(f"Cumulative year-to-date export covering {len(take)} month(s) of {year}. "
                 "It also contains every earlier month of the same year, so if any of those "
                 "are already loaded the overlap check below will say so.")))

    return Reading(adapter=NAME, filename=filename, products=prod, lines=lines,
                   year=year, row_problems=problems, notes=notes, quirks=quirks,
                   source_rows=source_rows)

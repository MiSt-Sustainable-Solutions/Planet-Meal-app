"""
Sligro export adapter — TU Delft's format.

THIS FILE IS THE CANONICAL DEFINITION OF THE MASS FORMULA for the app. The catalogue repo
holds a second copy in `sligro_parse.py`, used only to rebuild the historical reference
baseline. `test_mass.py` pins both to the same worked examples, so if they ever drift the
test fails rather than the numbers quietly moving.

Two things about this format are dangerous and both are handled here:

1. `kg = Aantal x IVP x Maat x unit_factor(Eenh)`. IVP **must** be multiplied. Two
   packaging conventions coexist and this one formula covers both:
     IVP = 1  -> Maat is the FULL colli weight   'KERN KROKET 28X80G' : IVP 1, Maat 2.24 KG
     IVP > 1  -> Maat is the PER-UNIT size       'MEYERIJ VOLLE MELK' : IVP 12, Maat 1 LT
   Dropping IVP moves every number in the project by ~45%, silently.

2. The file is **cumulative year-to-date**, and the LAST (Aantal, Omzet) pair is the
   running total, not a month. Verified on 200/200 rows. Counting it doubles the file.
   Two files from the same year therefore overlap — which is what the pre-flight overlap
   check exists to catch.

The sheet does not carry its year anywhere. It comes from the filename
('December 2025.xlsx'). A renamed file is refused rather than guessed at, because guessing
wrong files a whole year of purchases under the wrong year.
"""
from __future__ import annotations

import os
import re

import openpyxl

NAME = "sligro"
LABEL = "Sligro export"
DESCRIPTION = ("The spreadsheet Sligro exports per customer. Cumulative year-to-date, "
               "one column pair per month, year taken from the filename.")

# fixed column positions, verified against all 20 exports
COL_KLANTNR, COL_RESTAURANT, COL_CITY, COL_ARTIKELNR = 0, 1, 2, 3
COL_IVP, COL_VP, COL_MAAT, COL_EENH = 4, 5, 6, 7
COL_DESCRIPTION, COL_BRAND = 8, 9
COL_ARTIKELGROEP, COL_FOODFLAG, COL_EAN_CE, COL_EAN_HE = 11, 12, 13, 14
FIRST_MONTH_COL = 17

UNIT = {"KG": 1.0, "K": 1.0, "GR": 0.001, "G": 0.001, "LT": 1.0, "L": 1.0,
        "CL": 0.01, "ML": 0.001, "DL": 0.1}

DUTCH_MONTHS = {"januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
                "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11,
                "december": 12}


class SligroError(Exception):
    pass


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
    """How many REAL months a file covers.

    The last (Aantal, Omzet) pair is the year-to-date total, so it is always dropped.
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


# --------------------------------------------------------------------------- adapter
def detect(path: str, filename: str | None = None) -> bool:
    """A Sligro export has no header row, numeric klantnr in column 0, and month pairs."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        if ws.max_column < FIRST_MONTH_COL + 2:
            return False
        for row in ws.iter_rows(min_row=1, max_row=25, values_only=True):
            if row and row[0] is not None and str(row[0]).strip().replace(".", "").isdigit():
                return True
        return False
    finally:
        wb.close()


def read(path: str, filename: str | None = None, year: int | None = None):
    from . import Reading                       # local import: avoids a circular import

    filename = filename or os.path.basename(path)
    notes, quirks = [], []

    if year is None:
        guess = year_from_filename(filename)
        if guess is None:
            raise SligroError(
                f"cannot tell which year {filename!r} covers. A Sligro export does not carry "
                "its year inside the sheet, so it comes from the filename — keep the original "
                "name (for example 'December 2025.xlsx'), or state the year explicitly.")
        year = guess[0]
        notes.append(f"Year {year} taken from the filename; the sheet does not carry one.")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    nmonths = months_in_file(ws.max_column)

    prod, lines = {}, []
    for r in ws.iter_rows(values_only=True):
        if r[COL_KLANTNR] is None or not str(r[COL_KLANTNR]).strip().replace(".", "").isdigit():
            continue
        art = str(r[COL_ARTIKELNR]).strip()
        if art not in prod:
            ce = str(r[COL_EAN_CE]).strip() if r[COL_EAN_CE] else ""
            he = str(r[COL_EAN_HE]).strip() if r[COL_EAN_HE] else ""
            prod[art] = (
                art,
                str(r[COL_DESCRIPTION]).strip() if r[COL_DESCRIPTION] else "",
                str(r[COL_BRAND]).strip() if r[COL_BRAND] else "",
                str(r[COL_ARTIKELGROEP]).strip() if r[COL_ARTIKELGROEP] else "",
                str(r[COL_IVP]).strip() if r[COL_IVP] else "",
                str(r[COL_VP]).strip().upper() if r[COL_VP] else "",
                r[COL_MAAT] if isinstance(r[COL_MAAT], (int, float)) else None,
                str(r[COL_EENH]).strip().upper() if r[COL_EENH] else "",
                ce if ce.isdigit() and ce != "0" else "",
                he if he.isdigit() and he != "0" else "",
                str(r[COL_FOODFLAG]).strip() if r[COL_FOODFLAG] else "")
        maat = r[COL_MAAT] if isinstance(r[COL_MAAT], (int, float)) else None
        eenh = str(r[COL_EENH]).strip().upper() if r[COL_EENH] else ""
        vp = str(r[COL_VP]).strip().upper() if r[COL_VP] else ""
        ivp = parse_ivp(r[COL_IVP])
        for m in range(1, nmonths + 1):
            ci = FIRST_MONTH_COL + 2 * (m - 1)
            a = r[ci]
            o = r[ci + 1] if ci + 1 < len(r) else None
            if not isinstance(a, (int, float)) or a == 0:
                continue
            kg, known = mass_kg(a, ivp, maat, eenh, vp)
            lines.append((year, m,
                          str(r[COL_KLANTNR]).strip(),
                          str(r[COL_RESTAURANT]).strip() if r[COL_RESTAURANT] else "",
                          str(r[COL_CITY]).strip() if r[COL_CITY] else "",
                          art, float(a),
                          float(o) if isinstance(o, (int, float)) else 0.0,
                          kg, known, "complete"))
    wb.close()

    if not lines:
        raise SligroError("the file contains no purchase lines with a quantity")

    quirks.append(dict(
        code="ytd_total_dropped", severity="ok",
        message=(f"Cumulative year-to-date export covering {nmonths} month(s) of {year}. "
                 "The final Aantal/Omzet column pair is the year-to-date total, not a "
                 "month, and was excluded — counting it would have doubled this file.")))
    quirks.append(dict(
        code="cumulative_file", severity="info",
        message=("Because Sligro exports are cumulative, this file also contains every "
                 "earlier month of the same year. If any of those are already loaded, the "
                 "overlap check below will say so.")))

    return Reading(adapter=NAME, filename=filename, products=prod, lines=lines,
                   year=year, notes=notes, quirks=quirks)

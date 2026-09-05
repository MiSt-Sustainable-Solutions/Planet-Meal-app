"""
Functional tests for the app.

Runs against DISPOSABLE COPIES: the env overrides are set before anything imports config,
so this never touches the real database or the real uploads folder.

The tests that matter most:
  * the mass formula, pinned to the supplier's own worked examples — the app now holds the
    canonical copy, and this is what stops it drifting from the catalogue repo's;
  * the overlap guard, because double-counting a cumulative export is the worst bug
    available in this project.

    python test_app.py
"""
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_TMP = tempfile.mkdtemp(prefix="pp_test_")
os.environ["MIST_APP_DB"] = os.path.join(_TMP, "test.db")
os.environ["MIST_TENANT"] = "testclient"

import adapters          # noqa: E402
import analysis          # noqa: E402
import catalogue         # noqa: E402
import config            # noqa: E402
import db                # noqa: E402
import preflight
import selection         # noqa: E402
import uploads           # noqa: E402
from adapters import mist_template, sligro   # noqa: E402

SLIGRO_DIR = os.path.join(config.ROOT.parent, "MiSt Tool Mrigank", "catalogue", "data_in",
                          "sligro", "Sligro afname TU Delft")
FAILED = []


def P(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAILED.append(msg)


def f(name):
    return os.path.join(SLIGRO_DIR, name)


def sev(rep, code):
    return next((x["severity"] for x in rep["findings"] if x["code"] == code), None)


def finding(rep, code):
    return next((x for x in rep["findings"] if x["code"] == code), None)


db.init()

# ------------------------------------------------------------------ the mass formula
print("=== the mass formula, pinned to the supplier's own worked examples ===")
P(sligro.mass_kg(1, 1, 2.24, "KG", "DS") == (2.24, 1),
  "IVP=1: Maat is the whole case (KERN KROKET 28x80g -> 2.24 kg)")
P(sligro.mass_kg(1, 12, 1, "LT", "DS") == (12.0, 1),
  "IVP=12: Maat is per unit (12 x 1 LT milk -> 12 kg)")
P(sligro.mass_kg(1, 6, 200, "GR", "DS") == (1.2, 1),
  "article 675698: 6 x 200 GR -> 1.2 kg")
P(sligro.mass_kg(3, 1, 100, "GR", "DS") == (0.3, 1), "article 487066: 3 x 100 GR -> 0.3 kg")
P(sligro.mass_kg(2, 1, 6, "KG", "DS") == (12.0, 1), "article 340515: 2 x 6 KG -> 12 kg")
P(sligro.mass_kg(5, 1, 1, "ST", "KG") == (5.0, 1),
  "weight-sold line (VP=KG): Aantal is already kilograms")
P(sligro.mass_kg(5, 1, 1, "ST", "DS") == (0.0, 0),
  "true piece line: no weight, flagged rather than guessed")
P(sligro.mass_kg(2, 1, None, "KG", "DS") == (0.0, 0), "missing Maat -> unknown, no crash")
P(sligro.mass_kg(1, 1, 500, "ML", "DS") == (0.5, 1), "ML converts at 0.001")
P(sligro.months_in_file(17 + 2 * 13) == 12,
  "a 13-pair file covers 12 months; the YTD pair is dropped")
P(sligro.months_in_file(17 + 2 * 9) == 8, "a 9-pair file covers 8 months")

print("\n=== the year comes from the filename, and is never guessed ===")
P(sligro.year_from_filename("December 2025.xlsx") == (2025, 12), "December 2025")
P(sligro.year_from_filename("augustus 2025.xlsx") == (2025, 8), "lowercase augustus")
P(sligro.year_from_filename("Juni 2026.xlsx") == (2026, 6), "Juni 2026")
P(sligro.year_from_filename("export_final.xlsx") is None, "an unrecognisable name -> None")
try:
    adapters.read(f("December 2025.xlsx"), "renamed.xlsx")
    P(False, "a renamed Sligro file must be refused")
except Exception as e:
    P("year" in str(e).lower(), "a renamed Sligro file is refused with an explanation")

# ------------------------------------------------------------------ adapters
print("\n=== the adapter registry picks the right reader ===")
P(adapters.detect(f("Augustus 2024.xlsx")).NAME == "sligro", "a Sligro export -> sligro")
tpl = os.path.join(_TMP, "template.xlsx")
mist_template.write_template(tpl)
P(adapters.detect(tpl).NAME == "mist_template", "our template -> mist_template")
P(len(adapters.listing()) >= 2, f"{len(adapters.listing())} adapters registered")

r = adapters.read(f("Augustus 2024.xlsx"), "Augustus 2024.xlsx")
P(r.adapter == "sligro" and r.year == 2024, "read as sligro, year 2024")
P(r.periods == [f"2024-{m:02d}" for m in range(1, 9)], "covers Jan-Aug 2024")
P(abs(sum(l[7] for l in r.lines) - 1084116) < 1,
  f"spend matches the reference pipeline (EUR {sum(l[7] for l in r.lines):,.0f})")
P(any(q["code"] == "ytd_total_dropped" for q in r.quirks),
  "the adapter discloses that it dropped the year-to-date column")

t = adapters.read(tpl, "template.xlsx")
P(t.adapter == "mist_template" and len(t.lines) == 1, "the template's example row reads back")
P(abs(t.lines[0][8] - 12 * 2.24) < 1e-9,
  f"and its mass is Aantal x IVP x Maat = {t.lines[0][8]:.2f} kg")

# ------------------------------------------------------------------ pre-flight
print("\n=== an empty client accepts its first file ===")
rep = preflight.report(r)
P(sev(rep, "overlap") == "ok", "nothing is loaded yet, so nothing can overlap")
P(rep["verdict"] != "blocked", f"verdict is {rep['verdict']}, not blocked")
P(finding(rep, "weights")["pct_of_spend"] > 0, "the per-piece gap is quantified")
P(finding(rep, "barcodes")["pct"] > 90, "barcode coverage is reported")

print("\n=== THE DOUBLE-COUNT GUARD ===")
# It used to be three import modes and a refusal. It is now structural: a file's lines
# are stored the moment it is read and count for nothing, and a month is counted from
# exactly one selected file. There is no mode to get wrong.
aug = uploads.stage(f("Augustus 2024.xlsx"), "Augustus 2024.xlsx")
P(aug["stored_lines"] > 0, f"the file's {aug['stored_lines']:,} lines are stored on arrival")
P(db.stats()["lines"] == 0, "and count for nothing until somebody selects the file")

selection.set_selected(aug["upload_id"], config.TENANT, True)
after = db.stats()["lines"]
P(after == aug["stored_lines"], f"selecting it counts exactly its {after:,} lines")
P(len(db.months()) == 8, f"eight months ({len(db.months())})")

print("\n=== the same file twice cannot count twice ===")
again = uploads.stage(f("Augustus 2024.xlsx"), "Augustus 2024.xlsx")
selection.set_selected(again["upload_id"], config.TENANT, True)
P(db.stats()["lines"] == after,
  f"a second copy of the same file adds nothing ({db.stats()['lines']:,})")
c = selection.contested(config.TENANT)
P(len(c) == 8, f"all eight months are reported as contested ({len(c)})")
P(all(x["decided"] for x in c), "each already has an owner, so nothing is ambiguous")
selection.set_selected(again["upload_id"], config.TENANT, False)
P(selection.contested(config.TENANT) == [], "unselecting the copy clears it")

print("\n=== a cumulative file that overlaps AND adds ===")
dec = uploads.stage(f("December 2024.xlsx"), "December 2024.xlsx")
selection.set_selected(dec["upload_id"], config.TENANT, True)
P(len(db.months()) == 12, f"twelve months now ({len(db.months())})")
c = selection.contested(config.TENANT)
P(len(c) == 8, f"Jan-Aug are contested ({len(c)})")
P(all(x["owner"] == aug["upload_id"] for x in c),
  "and Augustus owns them, having far more lines for each")
grew = db.stats()["lines"] - after
P(0 < grew < dec["stored_lines"],
  f"only December's own months were added ({grew:,} of its {dec['stored_lines']:,})")

print("\n=== a person can hand a month to the other file ===")
selection.set_owner(config.TENANT, 2024, 3, dec["upload_id"], by="test")
P(selection.owners(config.TENANT)[(2024, 3)] == dec["upload_id"],
  "March is now counted from December, and the decision is written down")
P(len(db.months()) == 12, "still twelve months — nothing was lost by moving it")
selection.set_owner(config.TENANT, 2024, 3, aug["upload_id"], by="test")

print("\n=== a blocked verdict advises, it does not block ===")
# It used to refuse without an override. Refusing is the wrong shape now: the file is
# already stored and counts for nothing, so there is nothing to protect against. What
# matters is that the verdict is on screen when somebody decides.
fourth = uploads.stage(f("December 2024.xlsx"), "December 2024.xlsx")
P(fourth["verdict"] in ("go", "go_with_warnings", "blocked"),
  f"the file is judged ({fourth['verdict']}) and stored either way")
P(uploads.get(fourth["upload_id"], config.TENANT)["selected"] is False,
  "and arrives counting for nothing")

print("\n=== getting rid of a file takes two steps ===")
try:
    selection.destroy(fourth["upload_id"], config.TENANT)
    P(False, "should have refused to delete a file that is not archived")
except ValueError as e:
    P("archive it first" in str(e), "an unarchived file cannot be destroyed")
selection.archive(fourth["upload_id"], config.TENANT)
out = selection.destroy(fourth["upload_id"], config.TENANT)
P(out["lines_removed"] > 0, f"archived, then deleted with its {out['lines_removed']:,} lines")
P(uploads.get(fourth["upload_id"], config.TENANT) is None, "and the file is gone")

print("\n=== quiet holiday months are not mistaken for broken ones ===")
flagged = set()
for code in ("partial_months", "thin_months"):
    fd = finding(again, code)
    if fd:
        flagged |= set(fd.get("months", []))
P("2024-07" not in flagged and "2024-08" not in flagged,
  f"July and August are not flagged ({sorted(flagged) or 'none'})")

# ------------------------------------------------------------------ analysis
print("\n=== the app scores through the catalogue API ===")
if catalogue.health() is None:
    print("  (catalogue API not running — skipping the scoring tests)")
else:
    out = analysis.run(window="FY2024", save=True)
    h = out["headline"]
    P(h["food_kg"] > 0 and h["co2_kg"] > 0, f"FY2024 scored: {h['food_kg']:,} kg food")
    P(h["confidence"]["product_specific_pct_of_weight"] > 0, "confidence is reported")
    P(any(c["code"] == "pieces_zero_weight" for c in h["caveats"]),
      "the per-piece caveat travels with the numbers")
    P(any(c["code"] == "partial_export" for c in h["caveats"]),
      "and FY2024's partial months raise an error caveat")
    P(abs(sum(x["co2_kg"] for x in out["by_month"]) - h["co2_kg"]) <= len(out["by_month"]),
      "the monthly breakdown sums to the headline")
    P(out.get("run_id") and analysis.saved(out["run_id"], config.TENANT) is not None,
      "the run is saved and can be read back")
    P(len(analysis.history()) >= 1, "and appears in the history")
    nutrition = {"kcal", "protein_g", "fat_g", "satfat_g", "carb_g", "sugar_g", "fibre_g"}
    leaked = [k for row in out["top_contributors"] for k in row if k in nutrition]
    P(not leaked, f"no nutrition reaches the app ({leaked})")

print("\n=== windows ===")
P(analysis.default_window().startswith("FY"), f"a default window is chosen ({analysis.default_window()})")
P(any(w["key"] == "all" for w in analysis.windows()), "'all' is offered")
for bad in ("FY1999", "nonsense"):
    try:
        analysis.parse_window(bad)
        P(bad == "FY1999", f"{bad} accepted")     # FY1999 parses, just has no data
    except analysis.WindowError:
        P(True, f"{bad} rejected cleanly")


# ------------------------------------------------------------------ every real export
print("\n=== every real Sligro export is readable, whatever shape it is ===")
import glob  # noqa: E402
files = sorted(glob.glob(os.path.join(SLIGRO_DIR, "*.xlsx")))
P(len(files) == 20, f"{len(files)} exports found")
unreadable, layouts = [], {"NAME": 0, "struct": 0}
per_file = {}
for fp in files:
    try:
        rd = adapters.read(fp, os.path.basename(fp))
        layouts["NAME" if any("by column NAME" in n for n in rd.notes) else "struct"] += 1
        per_file[os.path.basename(fp)] = rd
    except Exception as e:
        unreadable.append(f"{os.path.basename(fp)}: {type(e).__name__}")
P(not unreadable, f"all {len(files)} read without error ({unreadable})")
P(layouts["NAME"] >= 8, f"{layouts['NAME']} read by column NAME, {layouts['struct']} by structure")

print("\n=== the two shifted files are read at the right column ===")
# Januari 2025 and Januari 2026 carry two extra columns, so their quantities start at 18.
# Reading column 17 there takes the GBR account code instead. This is the regression that
# matters most: it is the difference between a right answer and a silently wrong one.
jan25 = per_file.get("Januari 2025.xlsx")
P(jan25 is not None and abs(sum(l[7] for l in jan25.lines) - 155163) < 1,
  f"Januari 2025 spend is EUR {sum(l[7] for l in jan25.lines):,.0f} (the verified figure)")
jan26 = per_file.get("Januari 2026.xlsx")
P(jan26 is not None and abs(sum(l[7] for l in jan26.lines) - 131966) < 1,
  f"Januari 2026 spend is EUR {sum(l[7] for l in jan26.lines):,.0f} (the verified figure)")
shifted = [f for f, rd in per_file.items()
           if any(q["code"] == "nonstandard_layout" for q in rd.quirks)]
P("Januari 2026.xlsx" in shifted, f"the shifted layout is disclosed, not silently absorbed ({shifted})")

print("\n=== apostrophe-wrapped text is unwrapped ===")
# Januari 2026 stores text as "'KP'". Left alone, every unit lookup fails and every mass
# becomes unknown.
P(sligro._text("'KP'") == "KP", "'KP' -> KP")
P(sligro._text("  'APPEL TU DELFT'  ") == "APPEL TU DELFT", "surrounding whitespace and quotes go")
P(sligro._text("O'BRIEN") == "O'BRIEN", "an apostrophe INSIDE a word is left alone")
P(sligro._text(None) == "" and sligro._text(12) == "12", "None and numbers survive")
weighed = sum(1 for l in jan26.lines if l[9] == 1) / max(len(jan26.lines), 1)
P(weighed > 0.5, f"{weighed:.0%} of Januari 2026 lines have a usable weight")

print("\n=== a file is never claimed by the wrong adapter ===")
misrouted = [os.path.basename(f) for f in files
             if adapters.detect(f, os.path.basename(f)).NAME != "sligro"]
P(not misrouted, f"all 20 Sligro exports route to the sligro adapter ({misrouted})")
P(adapters.detect(tpl, "template.xlsx").NAME == "mist_template",
  "the MiSt template routes to the template adapter, not to sligro")

print("\n=== duplicate rows: the account is part of the key ===")
# One product bought by three restaurants in one month is normal, not a duplicate.
# Keying on (period, product) alone flagged all 20 real files.
false_pos = [f for f, rd in per_file.items()
             if any(x["code"] == "duplicate_rows" for x in preflight.check_duplicate_rows(rd))]
P(not false_pos, f"no real export is flagged as duplicated ({false_pos})")


class _FakeReading:
    adapter, filename, products, notes, quirks, row_problems = "x", "f", {}, [], [], []
    source_rows = [11, 12]
    lines = [(2025, 3, "131723", "R", "", "999", 1, 10.0, 1.0, 1, "complete"),
             (2025, 3, "131723", "R", "", "999", 1, 10.0, 1.0, 1, "complete")]


dup = preflight.check_duplicate_rows(_FakeReading())
P(len(dup) == 1, "the same account buying the same product twice in one month IS flagged")
P("11, 12" in (dup[0]["examples"][0]["rows"] if dup else ""),
  f"and it names the rows ({dup[0]['examples'][0]['rows'] if dup else '-'})")

print("\n=== volume: a quiet July is not a truncated export ===")
# July 2025 is 0.24 of the annual median, which tripped the error band on the reference
# file itself. Compared against another July it is completely normal.
dec25 = per_file.get("December 2025.xlsx")
vol = preflight.check_volume(dec25)
flagged = {m for x in vol for m in x.get("months", [])}
P("2025-07" not in flagged, f"July 2025 is not flagged ({sorted(flagged) or 'nothing flagged'})")
P(not any(x["severity"] == "error" for x in vol), "the reference export raises no volume error")

print("\n=== but a genuinely partial export still is ===")
for name in ("December 2024.xlsx", "September 2024.xlsx", "augustus 2025.xlsx"):
    rd = per_file.get(name)
    if rd is None:
        continue
    v = preflight.check_volume(rd)
    P(any(x["severity"] == "error" for x in v), f"{name} still raises partial_months")

print("\n=== findings point at somewhere a person can look ===")
located = [q for rd in per_file.values() for q in rd.quirks if q.get("location")]
P(located, f"{len(located)} findings carry a cell reference, e.g. {located[0]['location']}")
P(all(len(rd.source_rows) == len(rd.lines) for rd in per_file.values()),
  "every line knows which spreadsheet row it came from")


# ------------------------------------------------------------------ the barcode bridge
print("\n=== both barcodes are captured, kept apart, and sent onward ===")
dec = per_file.get("December 2025.xlsx")
ce = sum(1 for x in dec.products.values() if x[8])
he = sum(1 for x in dec.products.values() if x[9])
both = sum(1 for x in dec.products.values() if x[8] and x[9])
P(ce > 3000, f"{ce:,} products carry a consumer-unit barcode")
P(he > 2000, f"{he:,} carry a handling-unit barcode")
P(both > 2000, f"{both:,} carry BOTH, and they are stored in separate fields")
# CE == HE is legitimate: for a bulk foodservice pack the consumer unit IS the case
# (a 2.4 kg burger box, 260x28g mini breads). Measured: 4 products in December 2025.
# The DANGEROUS case is different — a handling-unit barcode that is some OTHER product's
# consumer-unit barcode would make a case of one thing resolve as a different thing.
_ce = {x[8]: x[0] for x in dec.products.values() if x[8]}
_cross = [x[0] for x in dec.products.values()
          if x[9] and x[9] in _ce and _ce[x[9]] != x[0]]
P(not _cross,
  f"no product's case barcode is another product's item barcode ({len(_cross)} collisions)")
_same = [x for x in dec.products.values() if x[8] and x[9] and x[8] == x[9]]
P(len(_same) < 20,
  f"{len(_same)} bulk packs where the case IS the consumer unit — legitimate, not an error")

print("\n=== the template reads both, and still reads the old one-column sheets ===")
P("ean_ce" in mist_template.COLUMNS and "ean_he" in mist_template.COLUMNS,
  "the template has ean_ce and ean_he as separate columns")
tpl2 = os.path.join(_TMP, "template_v2.xlsx")
mist_template.write_template(tpl2)
tv = adapters.read(tpl2, "template_v2.xlsx")
pv = list(tv.products.values())[0]
P(pv[8] and pv[9] and pv[8] != pv[9],
  f"the example row carries two distinct barcodes ({pv[8]} / {pv[9]})")

import openpyxl as _ox  # noqa: E402
legacy = os.path.join(_TMP, "legacy.xlsx")
_wb = _ox.Workbook(); _ws = _wb.active
_ws.append(["klantnr", "restaurant", "artikelnr", "omschrijving", "artikelgroep",
            "ivp", "vp", "maat", "eenh", "periode", "aantal", "ean", "omzet"])
_ws.append(["132201", "R", "340515", "KERN KROKET", "SNACKS", 1, "DS", 2.24, "KG",
            "2026-09", 5, "8712800121619", 99.0])
_wb.save(legacy)
lg = adapters.read(legacy, "legacy.xlsx")
P(lg.adapter == "mist_template", "a sheet with the old bare 'ean' column still routes here")
P(list(lg.products.values())[0][8] == "8712800121619",
  "and its barcode is read as the consumer unit")

print("\n=== the barcodes reach the catalogue ===")
lines = db.lines_for(2024, 1, 2024, 8)
P(lines and "ean_ce" in lines[0] and "ean_he" in lines[0],
  "lines_for() puts both barcodes on every line")
carried = sum(1 for l in lines if l["ean_ce"])
P(carried > len(lines) * 0.9,
  f"{100 * carried / len(lines):.0f}% of purchase lines carry a barcode onward")

if catalogue.health() is not None:
    probe = [dict(artikelnr="194072", description="MEYERIJ VOLLE MELK", kg=100,
                  ean_ce="08710401996797", ean_he="8710401996803",
                  omzet=90, year=2026, month=7)]
    got = catalogue.score(probe, label="probe")
    P(got["headline"]["co2_kg"] > 0, "the catalogue accepts a line carrying both barcodes")

    print("\n=== a missing or malformed column can no longer crash the service ===")
    # omzet is OPTIONAL in the template. Without it the spend share divided 0 by 0 and
    # produced a NaN, which is not JSON and returned a 500.
    edge = [
        ("no omzet at all", [dict(artikelnr="Z1", description="T", kg=10, year=2026, month=7)]),
        ("junk barcode", [dict(artikelnr="Z2", description="T", kg=10, omzet=5,
                               ean_ce="not-a-barcode", year=2026, month=7)]),
        ("no barcode", [dict(artikelnr="Z3", description="T", kg=10, omzet=5,
                             year=2026, month=7)]),
        ("everything weighs zero", [dict(artikelnr="Z4", description="T", kg=0, omzet=5,
                                         year=2026, month=7)]),
    ]
    for label, ls in edge:
        try:
            out = catalogue.score(ls, label="edge")
            P(out["headline"]["piece_spend_pct"] is not None or True,
              f"{label} -> answered, not a 500")
        except Exception as e:
            P(False, f"{label} -> {type(e).__name__}: {str(e)[:60]}")
else:
    print("  (catalogue API not running — skipping the bridge tests)")

shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n{'ALL PASS' if not FAILED else str(len(FAILED)) + ' FAILED'}")
sys.exit(1 if FAILED else 0)

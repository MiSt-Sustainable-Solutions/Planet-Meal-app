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
import preflight         # noqa: E402
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
staged = uploads.stage(f("Augustus 2024.xlsx"), "Augustus 2024.xlsx")
P(staged["verdict"] != "blocked", "the first file stages cleanly")
P(db.stats()["lines"] == 0, "staging writes nothing into the client's data")

res = uploads.commit(staged["upload_id"], "new_only")
after = db.stats()["lines"]
P(after == res["imported_lines"] > 0, f"committed {res['imported_lines']:,} lines")
P(len(res["imported_months"]) == 8, "all 8 months were new")

again = uploads.stage(f("Augustus 2024.xlsx"), "Augustus 2024.xlsx")
P(again["verdict"] == "blocked", "the SAME file is now blocked")
P(sev(again, "overlap") == "error", "because every month already exists")
ov = finding(again, "overlap")
P(len(ov["already_loaded"]) == 8 and not ov["new_months"], "and the clash is named exactly")

for mode in ("all", "new_only"):
    try:
        uploads.commit(again["upload_id"], mode)
        P(False, f"mode {mode} must refuse")
    except uploads.CommitError as e:
        P(True, f"mode {mode} refuses: {str(e)[:52]}...")
P(db.stats()["lines"] == after, "and the client's data did not move")

print("\n=== a cumulative file on top of an earlier one ===")
dec = uploads.stage(f("December 2024.xlsx"), "December 2024.xlsx")
P(sev(dec, "overlap") == "error", "December 2024 re-supplies Jan-Aug, so it overlaps")
ovd = finding(dec, "overlap")
P(sorted(ovd["new_months"]) == ["2024-09", "2024-10", "2024-11", "2024-12"],
  f"but Sep-Dec are genuinely new ({ovd['new_months']})")
P(sev(dec, "partial_months") == "error", "and those months are a partial export")
res2 = uploads.commit(dec["upload_id"], "new_only", override=True)
P(len(res2["imported_months"]) == 4 and len(res2["skipped_months"]) == 8,
  "new_only imported the 4 new months and skipped the 8 duplicates")
P(len(db.months()) == 12, "the client now holds 12 months, not 20")

print("\n=== replace overwrites rather than appends ===")
before = db.stats()["lines"]
third = uploads.stage(f("Augustus 2024.xlsx"), "Augustus 2024.xlsx")
uploads.commit(third["upload_id"], "replace", override=True)
P(db.stats()["lines"] == before, f"replace left the count unchanged ({db.stats()['lines']:,})")

print("\n=== a blocked file cannot be committed by accident ===")
fourth = uploads.stage(f("December 2024.xlsx"), "December 2024.xlsx")
try:
    uploads.commit(fourth["upload_id"], "new_only")
    P(False, "a blocked upload must not commit without an override")
except uploads.CommitError as e:
    P("blocked" in str(e) and "override" in str(e), "it refuses, and says how to force it")
P(uploads.discard(fourth["upload_id"]) is True, "a staged upload can be discarded")

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
    P(out.get("run_id") and analysis.saved(out["run_id"]) is not None,
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

shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n{'ALL PASS' if not FAILED else str(len(FAILED)) + ' FAILED'}")
sys.exit(1 if FAILED else 0)

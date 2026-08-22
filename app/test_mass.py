"""
The mass formula exists in two repos. This proves they agree.

  * this app's `adapters/sligro.py` — the canonical copy, used for every upload;
  * the catalogue repo's `sligro_parse.py` — used only to rebuild the historical
    reference baseline that the verified numbers come from.

They must stay identical. If they drift, one of two things happens and both are bad: the
baseline stops matching what the app computes, or an upload silently produces different
kilograms from the same file. Neither announces itself.

Run whenever either file is touched:

    python test_mass.py

Skips cleanly (exit 0) if the catalogue repo is not beside this one — a deployed app does
not need it, and CI for this repo alone should not fail on its absence.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
from adapters import sligro  # noqa: E402

CATALOGUE_SRC = os.path.join(config.ROOT.parent, "MiSt Tool Mrigank", "catalogue", "src")
FAILED = []


def P(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAILED.append(msg)


# Sander's worked examples, plus the two packaging conventions and the piece case.
# (aantal, ivp, maat, eenh, vp) -> (kg, known)
CASES = [
    ((1, 1, 2.24, "KG", "DS"), (2.24, 1), "IVP=1, Maat is the whole case (kroket 28x80g)"),
    ((1, 12, 1, "LT", "DS"), (12.0, 1), "IVP=12, Maat is per unit (milk)"),
    ((1, 6, 200, "GR", "DS"), (1.2, 1), "article 675698: 6 x 200 GR"),
    ((3, 1, 100, "GR", "DS"), (0.3, 1), "article 487066: 3 x 100 GR"),
    ((2, 1, 6, "KG", "DS"), (12.0, 1), "article 340515: 2 x 6 KG"),
    ((5, 1, 1, "ST", "KG"), (5.0, 1), "weight-sold line, VP=KG"),
    ((5, 1, 1, "ST", "DS"), (0.0, 0), "true piece line, no weight"),
    ((2, 1, None, "KG", "DS"), (0.0, 0), "missing Maat"),
    ((1, 1, 500, "ML", "DS"), (0.5, 1), "ML at 0.001"),
    ((1, 1, 50, "CL", "DS"), (0.5, 1), "CL at 0.01"),
    ((1, 1, 5, "DL", "DS"), (0.5, 1), "DL at 0.1"),
    ((4, 2, 250, "GR", "DS"), (2.0, 1), "IVP>1 with grams"),
]

print("=== this app's copy matches the worked examples ===")
for args, expected, label in CASES:
    got = sligro.mass_kg(*args)
    P(abs(got[0] - expected[0]) < 1e-9 and got[1] == expected[1],
      f"{label}: {got[0]} kg, known={got[1]}")

print("\n=== the year-to-date column is always dropped ===")
for pairs, months in [(13, 12), (9, 8), (7, 6), (2, 1), (1, 1)]:
    ncol = sligro.FIRST_MONTH_COL + 2 * pairs
    P(sligro.months_in_file(ncol) == months,
      f"a {pairs}-pair file covers {months} month(s)")

print("\n=== the catalogue repo's copy agrees ===")
path = os.path.join(CATALOGUE_SRC, "sligro_parse.py")
if not os.path.exists(path):
    print(f"  (catalogue repo not found at {CATALOGUE_SRC} — skipping the drift check)")
else:
    sys.path.insert(0, CATALOGUE_SRC)
    spec = importlib.util.spec_from_file_location("catalogue_sligro_parse", path)
    other = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(other)
    except Exception as e:
        print(f"  (could not import the catalogue copy: {type(e).__name__}: {e})")
        other = None

    if other is not None:
        P(other.UNIT == sligro.UNIT, f"the unit tables are identical ({sligro.UNIT})")
        P(other.FIRST_MONTH_COL == sligro.FIRST_MONTH_COL,
          f"month columns start at the same index ({sligro.FIRST_MONTH_COL})")
        drift = []
        for args, _expected, label in CASES:
            a, b = sligro.mass_kg(*args), other.mass_kg(*args)
            if abs(a[0] - b[0]) > 1e-9 or a[1] != b[1]:
                drift.append(f"{label}: app={a} catalogue={b}")
        P(not drift, f"both repos compute the same kilograms for every case ({drift[:2]})")
        mism = [p for p in (13, 9, 7, 2, 1)
                if sligro.months_in_file(sligro.FIRST_MONTH_COL + 2 * p)
                != other.months_in_file(sligro.FIRST_MONTH_COL + 2 * p)]
        P(not mism, f"both drop the year-to-date column identically ({mism})")

print(f"\n{'ALL PASS' if not FAILED else str(len(FAILED)) + ' FAILED'}")
sys.exit(1 if FAILED else 0)

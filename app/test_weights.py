"""
What the app tells the catalogue about a line with no weight (1 Nov 2026, weights.py).

Sligro describes the same article differently from one yearly file to the next: 400792,
40 pizza bases, is "DS, Maat 40, Eenh ST" in the 2023 file -- no weight -- and "DS, Maat
12.6, Eenh KG" in 2025. Each line must arrive at the catalogue with its case as ITS OWN
file describes it, and with this client's kg per case from the file that weighs it. The
pre-flight must say so, and flag a case that weighs three times its history.

    python test_weights.py        (no catalogue needed)
"""
import os
import sys
import tempfile
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_TMP = tempfile.mkdtemp(prefix="pp_weights_")
os.environ["MIST_APP_DB"] = os.path.join(_TMP, "weights.db")
os.environ["MIST_TENANT"] = "acme"
os.environ["MIST_SECRET_KEY"] = "test-only-key"

import auth       # noqa: E402
import db         # noqa: E402
import preflight  # noqa: E402
import store      # noqa: E402
import weights    # noqa: E402

FAILED = []


def P(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAILED.append(msg)


def _fresh():
    if not store.IS_POSTGRES:
        return
    con = store.connect()
    for t in ("month_owner", "analysis_run", "upload_line", "upload_product", "upload",
              "purchase_line", "product", "app_user", "tenant"):
        con.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    con.commit()
    con.close()


_fresh()
db.init()
auth.init()
auth.add_tenant("acme", "Acme University", "")

PIZZA = "400792"
EGGS = "28088"
# two files: 2023 sells both by the piece, 2025 weighs them
FILES = {
    "u2023": {PIZZA: ("1", "DS", 40, "ST"), EGGS: ("1", "DS", 90, "ST")},
    "u2025": {PIZZA: ("1", "DS", 12.6, "KG"), EGGS: ("1", "DS", 5.22, "KG")},
}
con = db.connect()
for uid, prods in FILES.items():
    con.execute("INSERT INTO upload (id, tenant, filename) VALUES (?,?,?)", (uid, "acme", uid))
    for art, (ivp, vp, maat, eenh) in prods.items():
        con.execute("INSERT INTO upload_product (upload_id, artikelnr, description, category, "
                    "ivp, vp, maat, eenh) VALUES (?,?,?,?,?,?,?,?)",
                    (uid, art, "PIZZABODEM" if art == PIZZA else "EIEREN M 90ST", "", ivp, vp, maat, eenh))
for art, (ivp, vp, maat, eenh) in FILES["u2025"].items():
    con.execute("INSERT INTO product (tenant, artikelnr, description, ivp, vp, maat, eenh, "
                "first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                ("acme", art, "x", ivp, vp, maat, eenh, "2026-01-01", "2026-01-01"))
LINES = [  # (year, month, artikelnr, aantal, kg, kg_known, upload)
    (2023, 10, PIZZA, 2, 0.0, 0, "u2023"),
    (2023, 10, EGGS, 9, 0.0, 0, "u2023"),
    (2025, 10, PIZZA, 3, 37.8, 1, "u2025"),
    (2025, 10, EGGS, 2, 10.44, 1, "u2025"),
]
for y, m, art, n, kg, known, uid in LINES:
    con.execute("INSERT INTO purchase_line VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("acme", y, m, "K1", "Aula", "Delft", art, n, 10.0, kg, known, "complete", uid))
con.commit()
con.close()
db.invalidate()

print("=== a line goes out with its OWN file's case, and the client's kg per case ===")
old = {l["artikelnr"]: l for l in db.lines_from_upload("u2023", "acme")}
P(old[PIZZA]["eenh"] == "ST" and old[PIZZA]["maat"] == 40,
  f"the 2023 pizza line says what the 2023 file said: Maat 40, ST ({old[PIZZA]['maat']}, {old[PIZZA]['eenh']})")
P(abs(old[PIZZA]["pack_kg"] - 12.6) < 1e-9, f"and carries 12.6 kg per case from the 2025 file ({old[PIZZA]['pack_kg']})")
P(abs(old[EGGS]["pack_kg"] - 5.22) < 1e-9, f"eggs: 5.22 kg per case ({old[EGGS]['pack_kg']})")
P(old[PIZZA]["kg"] == 0 and old[PIZZA]["kg_known"] == 0, "the file's own kg and kg_known are untouched")
P("source_upload" not in old[PIZZA], "and the file id is not sent to the catalogue")
new = {l["artikelnr"]: l for l in db.lines_from_upload("u2025", "acme")}
P(new[PIZZA]["pack_kg"] == 0 and new[PIZZA]["eenh"] == "KG", "a weighed line needs nothing filled")

print("\n=== a file that weighs an old purchase changes that period's answer ===")
before = db.window_fingerprint(2023, 9, 2024, 8, "acme")
con = db.connect()
con.execute("UPDATE purchase_line SET kg=50.4 WHERE artikelnr=? AND year=2025", (PIZZA,))
con.commit()
con.close()
db.invalidate()
weights.invalidate()
P(db.window_fingerprint(2023, 9, 2024, 8, "acme") != before,
  "the 2023/24 fingerprint moves when the 2025 file's kg per case does")

print("\n=== the pre-flight says what it fills, and what looks wrong ===")
reading = SimpleNamespace(
    lines=[(2026, 1, "K1", "Aula", "Delft", PIZZA, 1, 52.0, 0.0, 0, "complete"),
           (2026, 1, "K1", "Aula", "Delft", EGGS, 1, 30.0, 52.2, 1, "complete")],
    products={PIZZA: (PIZZA, "PIZZABODEM", "", "", "1", "DS", 40, "ST", "", "", ""),
              EGGS: (EGGS, "EIEREN M 90ST", "", "", "1", "DS", 52.2, "KG", "", "", "")})
found = {f["code"]: f for f in preflight.check_pack_weights(reading, "acme")}
P("weights_filled" in found and found["weights_filled"]["products"] == 1,
  "the per-piece pizza line is reported as taking its weight from another file")
P("pack_changed" in found and found["pack_changed"]["products"] == 2,
  "both articles changed between pieces and kilograms across files")
P("pack_jumped" in found and found["pack_jumped"]["examples"][0]["artikelnr"] == EGGS,
  "52.2 kg for a case of eggs that weighed 5.22 is flagged (a misread, ten times over)")
P(found.get("pack_jumped", {}).get("severity") == "warning", "as a warning, before the file is counted")

print(f"\n{len(FAILED)} FAILED" if FAILED else "\nall passed")
sys.exit(1 if FAILED else 0)

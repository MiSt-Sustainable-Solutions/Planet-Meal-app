"""
Undoing an import must remove exactly what that import added. Nothing more.

The obvious implementation deletes by month range, and it is wrong. Two files can cover
the same month -- a corrected re-export, a second site, a supplier splitting a period --
and deleting "everything in July" to undo one of them takes the other with it. Silently,
because the total simply gets smaller and nobody knows what it should have been.

Every purchase line records the upload it came from, so the correct implementation
deletes by that. This proves it: two imports over OVERLAPPING months with recognisable
volumes, undo one, and check the other is untouched to the kilogram.

It also checks the boundaries around it -- that a staged upload cannot be undone, that a
committed one cannot be discarded, that one client cannot undo another's import, and
that an undone file can be committed again -- because a recovery path that only works in
the happy case is not a recovery path.

    python test_uncommit.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_TMP = tempfile.mkdtemp(prefix="pp_uncommit_")
os.environ["MIST_APP_DB"] = os.path.join(_TMP, "uncommit_test.db")
os.environ["MIST_TENANT"] = "alpha"
os.environ["MIST_SECRET_KEY"] = "test-only-key-not-a-real-secret"

import auth        # noqa: E402
import db          # noqa: E402
import store       # noqa: E402
import uploads     # noqa: E402

FAILED = []


def P(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAILED.append(msg)


def _fresh_database():
    if not store.IS_POSTGRES:
        return
    con = store.connect()
    for t in ("analysis_run", "upload_line", "upload_product", "upload",
              "purchase_line", "product", "app_user", "tenant"):
        con.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    con.commit()
    con.close()


_fresh_database()
db.init()
auth.init()
auth.add_tenant("alpha", "Alpha University", "Alpha Catering")
auth.add_tenant("beta", "Beta College", "Beta Food")


def stage(uid, tenant, months, kg, filename):
    """A staged upload, built directly. test_app owns whether parsing works."""
    con = db.connect()
    con.execute("INSERT INTO upload VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
                (uid, tenant, filename, "", "sligro", 2025, "2025-01-01T00:00:00",
                 json.dumps([f"2025-{m:02d}" for m in months]), len(months), 1, 100.0,
                 "ok", json.dumps(dict(verdict="ok", findings=[], summary={},
                                       filename=filename, adapter="sligro",
                                       lines=len(months), products=1, spend_eur=100.0,
                                       periods=[f"2025-{m:02d}" for m in months]))))
    for m in months:
        con.execute("INSERT INTO upload_line VALUES (?,2025,?,'K1','CANTEEN','Delft',"
                    "'194072',10,100.0,?,1,'complete')", (uid, m, kg))
    con.execute("INSERT INTO upload_product VALUES (?,'194072','MELK','','ZUIVEL',"
                "'1','1',1.0,'KG','871',' ','F')", (uid,))
    con.commit()
    con.close()
    db.invalidate()


def kg_for(tenant):
    con = db.connect()
    v = con.execute("SELECT COALESCE(SUM(kg),0) FROM purchase_line WHERE tenant=?",
                    (tenant,)).fetchone()[0]
    con.close()
    return round(v, 1)


# Deliberately OVERLAPPING months with volumes that cannot be confused: a month-range
# delete would take both, and the total would look plausible either way.
stage("upl-A", "alpha", [5, 6, 7], 100.0, "first.xlsx")     # 3 x 100 = 300 kg
stage("upl-B", "alpha", [7, 8], 1000.0, "second.xlsx")      # 2 x 1000 = 2000 kg
stage("upl-C", "beta", [5, 6], 50.0, "beta.xlsx")           # 2 x 50 = 100 kg

print("=== two imports that share a month ===")
uploads.commit("upl-A", "all", tenant="alpha")
P(kg_for("alpha") == 300.0, f"first file imported ({kg_for('alpha'):,.0f} kg)")
uploads.commit("upl-B", "all", override=True, tenant="alpha")
P(kg_for("alpha") == 2300.0, f"second file imported on top ({kg_for('alpha'):,.0f} kg)")
uploads.commit("upl-C", "all", tenant="beta")
P(kg_for("beta") == 100.0, "and another client has their own")

print("\n=== undoing one leaves the other exactly as it was ===")
out = uploads.uncommit("upl-B", "alpha")
P(out["removed_lines"] == 2, f"2 lines removed, the ones that import added")
P(kg_for("alpha") == 300.0,
  f"alpha is back to exactly the first file ({kg_for('alpha'):,.0f} kg, not 0)")
P(kg_for("beta") == 100.0, "beta is untouched")

con = db.connect()
july = con.execute("SELECT COALESCE(SUM(kg),0) FROM purchase_line "
                   "WHERE tenant='alpha' AND year=2025 AND month=7").fetchone()[0]
con.close()
P(round(july, 1) == 100.0,
  f"July still holds the first file's 100 kg — the shared month survived")

print("\n=== the file is staged again, not deleted ===")
rep = uploads.get("upl-B", "alpha")
P(rep is not None, "the upload still exists")
P(not rep["committed"], "and is back to staged")
con = db.connect()
n = con.execute("SELECT COUNT(*) FROM upload_line WHERE upload_id='upl-B'").fetchone()[0]
con.close()
P(n == 2, "its parsed lines are still there, so it can be imported again")

print("\n=== and it can be ===")
uploads.commit("upl-B", "all", override=True, tenant="alpha")
P(kg_for("alpha") == 2300.0, f"re-imported cleanly ({kg_for('alpha'):,.0f} kg)")

print("\n=== the refusals ===")
try:
    uploads.uncommit("upl-B", "beta")
    P(False, "beta should not be able to undo alpha's import")
except uploads.CommitError:
    P(kg_for("alpha") == 2300.0, "another client cannot undo this one's import")

uploads.uncommit("upl-B", "alpha")
try:
    uploads.uncommit("upl-B", "alpha")
    P(False, "undoing an already-undone upload should refuse")
except uploads.CommitError as e:
    P("never committed" in str(e), "undoing a staged upload refuses, and says why")

try:
    uploads.discard("upl-A", "alpha")
    P(False, "discarding a committed upload should refuse")
except uploads.CommitError:
    P(kg_for("alpha") == 300.0, "a committed upload still cannot be discarded outright")

print("\n=== undo, then discard: the two steps compose ===")
uploads.uncommit("upl-A", "alpha")
P(kg_for("alpha") == 0.0, "undone")
P(uploads.discard("upl-A", "alpha") is True, "and now it can be discarded")
P(uploads.get("upl-A", "alpha") is None, "the file is gone")

print("\n=== products survive, because other imports need them ===")
con = db.connect()
prods = con.execute("SELECT COUNT(*) FROM product WHERE tenant='alpha'").fetchone()[0]
con.close()
P(prods == 1, "the product row is still there after every line was removed")

print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED"))
sys.exit(1 if FAILED else 0)

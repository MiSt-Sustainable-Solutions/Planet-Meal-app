"""
Only the files you selected count, and a month is supplied by exactly one of them.

This replaces the three import modes. Under the old model every file was poured into one
merged history, and new_only / replace / all existed to stop the same month landing
twice; choosing wrongly doubled a year. Here a file is held whether or not it counts, and
double counting is not something the app guards against -- it is something the data
cannot express, because a month has one owner.

The case that forces ownership to exist is TU Delft's own, and it is the shape this test
is built around:

    "Augustus 2024"  covers Jan-Aug, 20,773 lines
    "December 2024"  covers Jan-Dec,  1,203 lines

Select both and eight months count twice. Select either alone and months are lost. So the
overlap is reported, a default owner is written down where it can be seen, and a person
can move it.

    python test_selection.py
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_TMP = tempfile.mkdtemp(prefix="pp_sel_")
os.environ["MIST_APP_DB"] = os.path.join(_TMP, "sel.db")
os.environ["MIST_TENANT"] = "acme"
os.environ["MIST_SECRET_KEY"] = "test-only-key"

import auth        # noqa: E402
import db          # noqa: E402
import selection   # noqa: E402
import store       # noqa: E402

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
auth.add_tenant("acme", "Acme", "")

T = "acme"


def add_file(uid, name, months, lines_per_month, selected=0):
    """A file and the purchase lines it supplied. Volumes differ so a winner is decidable."""
    import json
    con = db.connect()
    per = [f"2024-{m:02d}" for m in months]
    store.upsert(con, "upload",
                 ["id", "tenant", "filename", "stored_path", "adapter", "year",
                  "uploaded_at", "periods", "lines", "products", "spend_eur", "verdict",
                  "report_json", "committed_at", "commit_mode", "commit_note",
                  "selected", "archived_at"],
                 [(uid, T, name, "", "sligro", 2024, "2026-01-01T00:00:00",
                   json.dumps(per), len(months) * lines_per_month, 1, 100.0, "go",
                   json.dumps(dict(verdict="go", findings=[], summary={}, filename=name,
                                   adapter="sligro", lines=len(months) * lines_per_month,
                                   products=1, spend_eur=100.0, periods=per)),
                   None, None, None, selected, None)],
                 conflict=["id"])
    for m in months:
        for i in range(lines_per_month):
            con.execute(
                "INSERT INTO purchase_line (tenant, year, month, klantnr, restaurant, "
                "city, artikelnr, aantal, omzet, kg, kg_known, quality, source_upload) "
                "VALUES (?,2024,?,'K1','CANTEEN','Delft','194072',1,10.0,10.0,1,"
                "'complete',?)", (T, m, uid))
    con.commit()
    con.close()
    db.invalidate()


def kg():
    return round(db.stats(T)["lines"] and
                 sum(m["kg"] for m in db.months(T)), 1)


def held_months():
    return [m["period"] for m in db.months(T)]


# Augustus covers Jan-Aug heavily; December covers Jan-Dec thinly. The real shape.
add_file("aug", "Augustus 2024.xlsx", range(1, 9), 20)
add_file("dec", "December 2024.xlsx", range(1, 13), 1)

print("=== a file that is not selected counts for nothing ===")
P(held_months() == [], "nothing is counted before anything is selected")
P(db.stats(T)["lines"] == 0, "and the totals are zero, not a merged history")

print("\n=== selecting one file counts exactly that file ===")
selection.set_selected("aug", T, True)
P(held_months() == [f"2024-{m:02d}" for m in range(1, 9)],
  f"Augustus supplies Jan-Aug ({len(held_months())} months)")
P(db.stats(T)["lines"] == 160, f"160 lines counted, not 172 ({db.stats(T)['lines']})")
P(selection.contested(T) == [], "nothing is contested")

print("\n=== selecting the second reveals the overlap ===")
selection.set_selected("dec", T, True)
c = selection.contested(T)
P(len(c) == 8, f"eight months are contested ({len(c)})")
P(all(x["decided"] for x in c), "each one already has an owner written down")
P({x["owner"] for x in c} == {"aug"},
  "and the default is the file with the most lines for that month")

print("\n=== but nothing is counted twice ===")
P(held_months() == [f"2024-{m:02d}" for m in range(1, 13)],
  "all twelve months are now supplied")
n = db.stats(T)["lines"]
P(n == 160 + 4, f"160 from Augustus + 4 from December's own months = 164, got {n}")
P(n != 172, "not 172 — the eight shared months were not added on top")

print("\n=== a person can move a month to the other file ===")
selection.set_owner(T, 2024, 3, "dec", by="mrigank")
after = db.stats(T)["lines"]
P(after == 164 - 20 + 1, f"March now comes from December: {after} lines")
own = selection.owners(T)
P(own[(2024, 3)] == "dec", "the decision is recorded")
selection.set_selected("dec", T, False)
P(selection.contested(T) == [], "unselecting removes the contest")
P(selection.owners(T) == {}, "and the owner rows go with it — none outlives its reason")

print("\n=== a file that supplies nothing selected cannot own a month ===")
try:
    selection.set_owner(T, 2024, 3, "dec")
    P(False, "should have refused")
except ValueError as e:
    P("not selected" in str(e), "refused, and says why")

print("\n=== archiving is the first of two steps ===")
selection.set_selected("dec", T, True)
selection.archive("dec", T)
P(db.stats(T)["lines"] == 160, "an archived file stops counting")
con = db.connect()
still = con.execute("SELECT COUNT(*) FROM purchase_line WHERE source_upload='dec'").fetchone()[0]
con.close()
P(still == 12, f"but its {still} lines are still there — nothing was destroyed")

print("\n=== and deleting is the second ===")
try:
    selection.destroy("aug", T)
    P(False, "should have refused to delete an unarchived file")
except ValueError as e:
    P("archive it first" in str(e), "a file that is not archived cannot be destroyed")

out = selection.destroy("dec", T)
P(out["lines_removed"] == 12, f"deleting from the archive took its 12 lines with it")
con = db.connect()
gone = con.execute("SELECT COUNT(*) FROM purchase_line WHERE source_upload='dec'").fetchone()[0]
con.close()
P(gone == 0, "and they are really gone")
P(db.stats(T)["lines"] == 160, "Augustus is untouched")

print("\n=== data older than all of this is adopted, not lost ===")
con = db.connect()
for m in (1, 2):
    con.execute("INSERT INTO purchase_line (tenant, year, month, klantnr, restaurant, "
                "city, artikelnr, aantal, omzet, kg, kg_known, quality, source_upload) "
                "VALUES (?,2023,?,'K1','OLD','X','194072',1,5.0,5.0,1,'complete',"
                "'seed:analysis.db')", (T, m))
con.commit()
con.close()
db.invalidate()
P(db.stats(T)["lines"] == 160, "orphan lines count for nothing while they have no file")
made = selection.adopt_orphans()
P(made == 1, f"one file was synthesised for them ({made})")
P(db.stats(T)["lines"] == 162, "and now they count again — no number moved for a user")
P(selection.adopt_orphans() == 0, "running it twice adopts nothing further")

print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED"))
sys.exit(1 if FAILED else 0)

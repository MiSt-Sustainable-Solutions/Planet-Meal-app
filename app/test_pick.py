"""
Analysing a chosen set of files, and what a client is shown.

Two changes that look unrelated and are not. A client's Files page now lists only the
files their numbers are made of -- held-but-not-counted files and the archive are MiSt's
housekeeping, and showing them raises questions about the client's data that are really
questions about ours. The dashboard, meanwhile, can now be asked "what do THESE files
say" instead of "what did we buy in FY2025".

They meet at the same rule: a person can only choose from the files they can see. Two
copies of that rule would let a client analyse a file we are deliberately not showing
them, so there is one function and this checks that both screens use it.

The thing most worth testing is the double count. Sligro's exports are cumulative
year-to-date, so "Augustus 2024" and "December 2024" both contain January. Choose both
and a naive answer counts January twice -- and a doubled footprint does not look wrong,
it looks like a bad year. selection.resolve() gives every month exactly one file and says
which ones it had to decide.

    python test_pick.py        (needs the catalogue API running)
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_TMP = tempfile.mkdtemp(prefix="pp_pick_")
os.environ["MIST_APP_DB"] = os.path.join(_TMP, "pick.db")
os.environ["MIST_TENANT"] = "acme"
os.environ["MIST_SECRET_KEY"] = "test-only-key"

from fastapi.testclient import TestClient   # noqa: E402

import analysis    # noqa: E402
import auth        # noqa: E402
import catalogue   # noqa: E402
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
auth.add_tenant("acme", "Acme Catering Co", "Acme Kitchens")

if catalogue.health() is None:
    print("the catalogue API is not running — start it and try again:")
    print("  python -m uvicorn api:app --port 8077   (in the catalogue repo, src/)")
    sys.exit(2)

T = "acme"

con = db.connect()
store.upsert(con, "product",
             ["tenant", "artikelnr", "description", "category", "ean", "ean_he",
              "first_seen", "last_seen"],
             [(T, "194072", "MEYERIJ VOLLE MELK", "ZUIVEL HOUDBAAR",
               "8710401996797", "", "2024-01", "2024-12")],
             conflict=["tenant", "artikelnr"])
con.commit()
con.close()


def add_file(uid, name, months, per_month, selected=0, archived=None):
    con = db.connect()
    per = [f"2024-{m:02d}" for m in months]
    store.upsert(con, "upload",
                 ["id", "tenant", "filename", "stored_path", "adapter", "year",
                  "uploaded_at", "periods", "lines", "products", "spend_eur", "verdict",
                  "report_json", "committed_at", "commit_mode", "commit_note",
                  "selected", "archived_at"],
                 [(uid, T, name, "", "sligro", 2024, "2026-01-01T00:00:00",
                   json.dumps(per), len(months) * per_month, 1, 100.0, "go",
                   json.dumps(dict(verdict="go", findings=[], summary={}, filename=name,
                                   adapter="sligro", lines=len(months) * per_month,
                                   products=1, spend_eur=100.0, periods=per)),
                   None, None, None, selected, archived)],
                 conflict=["id"])
    for m in months:
        for _ in range(per_month):
            con.execute(
                "INSERT INTO purchase_line (tenant, year, month, klantnr, restaurant, "
                "city, artikelnr, aantal, omzet, kg, kg_known, quality, source_upload) "
                "VALUES (?,2024,?,'K1','CANTEEN','Delft','194072',1,10.0,10.0,1,"
                "'complete',?)", (T, m, uid))
    con.commit()
    con.close()
    db.invalidate()


# The real shape: cumulative year-to-date exports that overlap on eight months.
add_file("aug", "Augustus 2024.xlsx", range(1, 9), 20, selected=1)
add_file("dec", "December 2024.xlsx", range(1, 13), 1)
add_file("old", "Old 2024.xlsx", range(1, 3), 5, archived="2026-01-02T00:00:00")

print("=== reading a files: window ===")
P(analysis.picked("FY2024") is None, "a period is not a pick")
P(analysis.picked("all") is None, "and neither is 'all'")
P(analysis.picked("files:aug") == ["aug"], "one file")
P(analysis.picked("files: dec , aug ,dec") == ["aug", "dec"],
  "several, deduplicated and ordered, so the same pick is the same cache key")

print()
print("=== a chosen file answers for ITSELF, counted or not ===")
# 'dec' is not selected. The whole point of choosing it is to ask about it anyway.
one = analysis.run(window="files:dec", tenant=T, save=False)["headline"]
P(one["lines"] == 12, f"December 2024 alone is its own 12 lines ({one['lines']})")
counted = analysis.run(window="all", tenant=T, save=False)["headline"]
P(counted["lines"] == 160, f"and the counted numbers are untouched ({counted['lines']})")

print()
print("=== the double count, which is the reason this exists ===")
owner, overlaps = selection.resolve(T, ["aug", "dec"])
P(len(owner) == 12, f"every month has exactly one file ({len(owner)})")
P(len(overlaps) == 8, f"eight months had to be decided ({len(overlaps)})")
P(all(o["chosen"]["filename"] == "Augustus 2024.xlsx" for o in overlaps),
  "and the fuller file won each of them")
P(all(not o["agree"] for o in overlaps),
  "which it did BECAUSE the two disagree — 20 lines a month against 1")
P(owner[(2024, 12)] == "dec", "a month only one file covers still comes from that file")

both = analysis.run(window="files:aug,dec", tenant=T, save=False)["headline"]
naive = 160 + 12
P(both["lines"] == 164,
  f"Jan-Aug from Augustus (160) + Sep-Dec from December (4) = 164, not {naive} ({both['lines']})")
P(both["lines"] < naive, "so choosing both files does not count January twice")

print()
print("=== the numbers say what they are, without the URL that made them ===")
r = analysis.run(window="files:aug,dec", tenant=T, save=False)
P(r["window"]["label"] == "2 files chosen", f"labelled a pick ({r['window']['label']})")
P(r["picked"] and r["picked"]["files"] == 2, "and the result carries the pick itself")
codes = [c["code"] for c in r["headline"]["caveats"]]
P("chosen_files" in codes, "a caveat says these are not the reported footprint")
P("chosen_disagree" in codes,
  "and another says the two files DISAGREE, which is the thing worth knowing")
msg = next(c["message"] for c in r["headline"]["caveats"] if c["code"] == "chosen_disagree")
P("Augustus 2024.xlsx" in msg and "December 2024.xlsx" in msg,
  "naming both files, so a person can go and look")
P("20 lines" in msg and "1 line," in msg,
  "the numbers they each hold, so the size of the difference is visible")
P("1 lines" not in msg, "and it says one line, not 1 lines")
P(next(c["severity"] for c in r["headline"]["caveats"]
       if c["code"] == "chosen_disagree") == "error",
  "at error, because one of those files is partial or restated")

solo = analysis.run(window="files:dec", tenant=T, save=False)
P(not [c for c in solo["headline"]["caveats"]
       if c["code"] in ("chosen_disagree", "chosen_repeat")],
  "one file needs no such warning")
P(solo["window"]["label"] == "File: December 2024.xlsx",
  f"and is named, not numbered ({solo['window']['label']})")

print()
print("=== the ordinary case: two exports repeating a month they agree about ===")
# Sligro's exports all run from January, so a later one repeats an earlier one exactly.
# This is nearly every overlap in real data, it changes no number, and it must NOT read
# like the disagreement above -- or the warning that matters gets lost among the ones
# that do not.
add_file("mar", "Maart 2025.xlsx", range(1, 4), 7)
add_file("mei", "Mei 2025.xlsx", range(1, 4), 7)      # identical months and volumes
_own, same = selection.resolve(T, ["mar", "mei"])
P(len(same) == 3, f"three months are in both files ({len(same)})")
P(all(o["agree"] for o in same), "and both files say the same thing about each")

rr = analysis.run(window="files:mar,mei", tenant=T, save=False)
rcodes = [c["code"] for c in rr["headline"]["caveats"]]
P("chosen_repeat" in rcodes, "reported as a repeat")
P("chosen_disagree" not in rcodes, "and NOT as a disagreement")
P(next(c["severity"] for c in rr["headline"]["caveats"]
       if c["code"] == "chosen_repeat") == "info",
  "at info, because nothing is wrong and no number moved")
P(rr["headline"]["lines"] == 21,
  f"21 lines, not 42 — counted once, not added together ({rr['headline']['lines']})")
alone = analysis.run(window="files:mar", tenant=T, save=False)["headline"]
P(alone["co2_kg"] == rr["headline"]["co2_kg"],
  "and choosing both gives exactly what choosing one gives")

print()
print("=== a gram of rounding is not a disagreement ===")
con = db.connect()
con.execute("UPDATE purchase_line SET kg = kg + 0.002 WHERE source_upload='mei' "
            "AND year=2024 AND month=1")
con.commit()
con.close()
db.invalidate()
_own, nudged = selection.resolve(T, ["mar", "mei"])
P(all(o["agree"] for o in nudged),
  "the same purchases arriving a gram apart still agree")

print()
print("=== two picks over the same months are not the same answer ===")
k1 = analysis.cache_key(2024, 1, 2024, 12, None, ["aug", "dec"])
k2 = analysis.cache_key(2024, 1, 2024, 12, None, ["dec"])
k3 = analysis.cache_key(2024, 1, 2024, 12, None, None)
P(k1 != k2 != k3 and k1 != k3,
  "the cache key separates them, so one pick cannot serve another's result")

print()
print("=== a client sees the files their numbers are made of, and only those ===")
import main    # noqa: E402

auth.create_user("boss", "boss-password-1", "admin", None, "Boss")
auth.create_user("acmeuser", "acme-password-1", "client", "acme", "Acme")


def signed_in(u, pw):
    c = TestClient(main.app, follow_redirects=False)
    assert c.post("/login", data={"username": u, "password": pw}).status_code == 303
    return c


client, admin = signed_in("acmeuser", "acme-password-1"), signed_in("boss", "boss-password-1")

cf = client.get("/files").text
P("Augustus 2024.xlsx" in cf, "the counted file is listed")
P("December 2024.xlsx" not in cf, "the held-but-not-counted file is not")
P("Old 2024.xlsx" not in cf and "Out of the way" not in cf, "and neither is the archive")
P("not counted" not in cf, "nothing is labelled 'not counted', because nothing here is")

af = admin.get("/files").text
P(all(n in af for n in ("Augustus 2024.xlsx", "December 2024.xlsx", "Old 2024.xlsx")),
  "an admin still sees all three, archive included")

print()
print("=== and can only choose from what they can see ===")
d = admin.get("/?file=dec").text
P("File: December 2024.xlsx" in d, "an admin may analyse a file that is not counted")

d = client.get("/?file=dec").text
P("File: December 2024.xlsx" not in d,
  "a client may NOT, by putting its id in the URL")
P("Acme Catering Co" in d, "they get their own default window instead of an error")

print()
print("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED")
sys.exit(1 if FAILED else 0)

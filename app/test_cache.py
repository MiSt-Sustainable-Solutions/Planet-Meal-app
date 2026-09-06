"""
Tests for the analysis cache — the thing that turns a thirty-second page into an instant one.

Runs against a DISPOSABLE database: the env overrides are set before anything imports
config, so this never touches the real one.

The catalogue API must be running (the first calculation is real). Everything after that
is about whether the saved answer is served instead of recalculated, and whether the
client is told when it is behind.

Two failures this is here to catch, both of which are silent:

  * the cache never hitting, which just looks like "the app is slow";
  * the cache hitting when it should NOT have, which shows somebody a number that a later
    decision already changed. That one is worse, and it is the reason the key contains the
    catalogue version and a fingerprint of the client's own data, not just the window.

    python test_cache.py
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_TMP = tempfile.mkdtemp(prefix="pp_cache_")
os.environ["MIST_APP_DB"] = os.path.join(_TMP, "cache_test.db")
os.environ["MIST_TENANT"] = "cachetest"

import analysis          # noqa: E402
import catalogue         # noqa: E402
import config            # noqa: E402
import db                # noqa: E402
import store             # noqa: E402

FAILED = []


def P(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAILED.append(msg)


def runs() -> int:
    con = db.connect()
    n = con.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0]
    con.close()
    return n


# --------------------------------------------------------------------------- fixture

def _fresh_database():
    """Start from nothing, on either backend.

    On SQLite each run gets its own temporary file, so this is a no-op. On Postgres the
    tests share one database, and leftovers from the previous run would collide -- a test
    that only passes on an empty database is a test that passes once.
    """
    import store
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

# A handful of real Sligro articles, enough to score in a second or two. Real article
# numbers and a real barcode, so the catalogue resolves them the way it would in anger.
LINES = [
    ("354048", "DR.OETKER QUICHE VEGETARISCH", "DIEPVRIES", "4001724639206", 120.0, 640.0),
    ("102030", "MELK HALFVOL 1L", "ZUIVEL", "", 400.0, 380.0),
    ("204060", "KIPFILET NATUREL 2KG", "VLEES", "", 210.0, 1900.0),
    ("308090", "TOMATEN BLIK 2500G", "CONSERVEN", "", 300.0, 260.0),
]

con = db.connect()

# Lines only count when the file they came from is counted, so the fixture makes one.
# Two of them, in fact: the second insert further down represents a later file arriving,
# which is what the fingerprint is being asked to notice.
def _fixture_file(con, uid, name):
    import json
    store.upsert(
        con, "upload",
        ["id", "tenant", "filename", "stored_path", "adapter", "year", "uploaded_at",
         "periods", "lines", "products", "spend_eur", "verdict", "report_json",
         "committed_at", "commit_mode", "commit_note", "selected", "archived_at"],
        [(uid, config.TENANT, name, "", "sligro", 2025, "2025-01-01T00:00:00",
          json.dumps(["2025-01"]), 0, 0, 0.0, "go",
          json.dumps(dict(verdict="go", findings=[], summary={}, filename=name,
                          adapter="sligro", lines=0, products=0, spend_eur=0.0,
                          periods=["2025-01"])),
          None, None, None, 1, None)],
        conflict=["id"])


_fixture_file(con, 'fixture', 'fixture.xlsx')
for art, desc, cat, ean, kg, eur in LINES:
    con.execute("INSERT INTO product (tenant, artikelnr, description, category, ean, "
                "ean_he, first_seen, last_seen) VALUES (?,?,?,?,?,'','2025-01','2025-06')",
                (config.TENANT, art, desc, cat, ean))
    for month in range(1, 7):
        con.execute("INSERT INTO purchase_line (tenant, year, month, klantnr, restaurant, "
                    "city, artikelnr, aantal, omzet, kg, kg_known, quality, source_upload) "
                    "VALUES (?,2025,?,'K1','Aula','Delft',?,10,?,?,1,'complete','fixture')",
                    (config.TENANT, month, art, eur / 6, kg / 6))
con.commit()
con.close()
db.invalidate()

if catalogue.health() is None:
    print("the catalogue API is not running — start it and try again:")
    print("  python -m uvicorn api:app --port 8077   (in the catalogue repo, src/)")
    sys.exit(2)


# --------------------------------------------------------------------------- tests
print("=== the first look calculates; the second does not ===")
before = runs()
first = analysis.run(window="FY2025")
P(first.get("cached") is False, "the first run really calculated")
P(runs() == before + 1, "and saved exactly one row")

second = analysis.run(window="FY2025")
P(second.get("cached") is True, "the second run came from the cache")
P(second.get("run_id") == first.get("run_id"), "it is the same saved run, not a new one")
P(runs() == before + 1, "and nothing new was written — history stays one row per answer")
P(second["headline"]["co2_kg"] == first["headline"]["co2_kg"],
  f"the numbers are identical ({second['headline']['co2_kg']:,} kg CO2e)")

print("\n=== the key is the window ASKED FOR, not the profile that came back ===")
# The bug this pins: the result carries the RESOLVED profile name, so keying the cache on
# it meant a caller asking for the default never matched their own saved row. The cache
# looked correct, wrote a row every time, and never hit once.
P("|default" in analysis.cache_key(2025, 1, 2025, 12, None),
  "asking for the default is keyed as 'default'")
P(analysis.cache_key(2025, 1, 2025, 12, None)
  != analysis.cache_key(2025, 1, 2025, 12, "eat_reference_full_precision"),
  "and a named profile is a different key")
P(first["headline"].get("eat_profile") not in (None, "default"),
  f"even though the answer names a real profile ({first['headline'].get('eat_profile')})")

print("\n=== a different window is a different answer ===")
n = runs()
half = analysis.run(frm="2025-01", to="2025-03")
P(half.get("cached") is False, "a window never scored before is calculated")
P(runs() == n + 1, "and saved separately")
P(analysis.run(frm="2025-01", to="2025-03").get("cached") is True,
  "then cached in its own right")

print("\n=== changing the client's own data invalidates it ===")
fp_before = db.window_fingerprint(2025, 1, 2025, 12)
con = db.connect()
_fixture_file(con, "fixture2", "fixture2.xlsx")
con.execute("INSERT INTO purchase_line (tenant, year, month, klantnr, restaurant, city, "
            "artikelnr, aantal, omzet, kg, kg_known, quality, source_upload) "
            "VALUES (?,2025,6,'K1','Aula','Delft','102030',5,90,55,1,'complete','fixture2')",
            (config.TENANT,))
con.commit()
con.close()
db.invalidate()
fp_after = db.window_fingerprint(2025, 1, 2025, 12)
P(fp_after != fp_before, "one more purchase line moves the data fingerprint")
after = analysis.run(window="FY2025")
P(after.get("cached") is False, "so the analysis is recalculated, not served stale")
P(after["headline"]["co2_kg"] != first["headline"]["co2_kg"],
  "and the number actually changed")

print("\n=== a decision landing does NOT silently recalculate ===")
# The rule the whole design rests on: a number never changes underneath a reader. The app
# says it is behind and waits to be asked.
real_version = catalogue.version
live = real_version()
bumped = dict(live, version=live["version"] + 2,
              etag=f"cat-{live['version'] + 2}.{live['fingerprint']}",
              note="curated 2 pins")
catalogue.version = lambda: bumped
try:
    n = runs()
    stale = analysis.run(window="FY2025")
    P(stale.get("cached") is True, "the saved numbers are still served — instantly")
    P(runs() == n, "nothing was recalculated behind the client's back")
    P(stale.get("stale") is not None, "but the result says it is behind")
    P(stale["stale"]["behind"] == 2,
      f"and by how much: {stale['stale']['behind']} decisions")
    P(stale["stale"]["reachable"] is True, "with the catalogue up, so it can be refreshed")
    P(stale["stale"]["was"] == live["etag"] and stale["stale"]["now"] == bumped["etag"],
      "naming both versions, so the claim is checkable")

    print("\n=== ...until you ask ===")
    forced = analysis.run(window="FY2025", force=True)
    P(forced.get("cached") is False, "force=True recalculates")
    P(runs() == n + 1, "and saves the new answer")
    P(forced.get("stale") is None, "which is no longer behind anything")
finally:
    catalogue.version = real_version

print("\n=== with the catalogue down, saved numbers are served and labelled ===")
catalogue.version = lambda: None
try:
    n = runs()
    offline = analysis.run(window="FY2025")
    P(offline.get("cached") is True, "the page still renders from the cache")
    P(runs() == n, "and writes nothing")
    P(offline.get("stale") is not None and offline["stale"]["reachable"] is False,
      "flagged as unreachable rather than as current")
    P(offline["stale"]["since"] is not None,
      f"saying when they were calculated ({offline['stale']['since']})")
finally:
    catalogue.version = real_version

print("\n=== the cache never crosses clients ===")
# Two clients can hold the same window, the same catalogue version and -- if they bought
# the same things -- the same data fingerprint. Serving one client's numbers to the other
# would be the worst failure this app could have, so it is tested rather than assumed.
current = analysis.run(window="FY2025")
key = analysis.cache_key(2025, 1, 2025, 12, None)
fp = db.window_fingerprint(2025, 1, 2025, 12)
etag = current["catalogue_version"]["etag"]
con = db.connect()
con.execute("""INSERT INTO analysis_run (id, tenant, label, period_from, period_to,
               eat_profile, ran_at, lines, food_kg, co2_kg, intensity, eat_score,
               specific_pct, result_json, window_key, catalogue_version, data_fingerprint)
               VALUES ('rival','someone_else','FY2025','2025-01','2025-12',NULL,
               '2099-01-01T00:00:00',1,1,999999,9.9,0.1,50,
               '{"headline":{"co2_kg":999999}}',?,?,?)""", (key, etag, fp))
con.commit()
con.close()
mine = analysis._lookup(config.TENANT, key, fp, etag)
theirs = analysis._lookup("someone_else", key, fp, etag)
P(mine is not None and mine["run_id"] != "rival",
  "an identical key under another tenant is not returned")
P(analysis.run(window="FY2025")["headline"]["co2_kg"] != 999999,
  "and the client still sees their own numbers")
P(theirs is not None and theirs["run_id"] == "rival",
  "while that tenant does get theirs -- the key works, it is scoped")

# --------------------------------------------------------------------- the change log
# The runs table is a cache log: a row lands whenever something had to be recalculated,
# which is mostly "somebody opened the dashboard after a deploy". TU Delft's held 67 rows,
# 59 of them FY2025, nearly all repeating the same number -- and the page printed all of
# them. There was one fact in there. analysis.changes() is the function that finds it, so
# what it must do is emit a row ONLY where an answer actually came out different, and say
# which of the two possible causes moved it.
print("\n=== the change log reports changes, not recalculations ===")


def seed_run(rid, label, frm, to, ran, co2, *, key=None, ver="cat-1", fp="fp1", lines=100,
             tenant=None):
    con = db.connect()
    con.execute("""INSERT INTO analysis_run (id, tenant, label, period_from, period_to,
                   eat_profile, ran_at, lines, food_kg, co2_kg, intensity, eat_score,
                   specific_pct, result_json, window_key, catalogue_version,
                   data_fingerprint)
                   VALUES (?,?,?,?,?,NULL,?,?,?,?,1.5,0.7,70,'{}',?,?,?)""",
                (rid, tenant or config.TENANT, label, frm, to, ran, lines, co2 / 2, co2,
                 key or f"{frm}:{to}|default", ver, fp))
    con.commit()
    con.close()


con = db.connect()
con.execute("DELETE FROM analysis_run")
con.commit()
con.close()

# four looks at the same year; only one of them produced a different answer
seed_run("c1", "FY2025", "2025-01", "2025-12", "2026-01-01T09:00:00", 329288)
seed_run("c2", "FY2025", "2025-01", "2025-12", "2026-01-02T09:00:00", 329288)
seed_run("c3", "FY2025", "2025-01", "2025-12", "2026-01-03T09:00:00", 329288)
seed_run("c4", "FY2025", "2025-01", "2025-12", "2026-01-04T09:00:00", 329620, ver="cat-2")

ch = analysis.changes()
P(len(ch) == 1, f"four runs, one change ({len(ch)})")
P(ch[0]["moves"][0]["what"] == "CO2", "and it says what moved")
P("329,288" in ch[0]["moves"][0]["before"] and "329,620" in ch[0]["moves"][0]["after"],
  "with the figure before and after, not just that it moved")
P(ch[0]["rules"] and not ch[0]["data"], "attributed to the catalogue, not to their files")

seed_run("c5", "FY2025", "2025-01", "2025-12", "2026-01-05T09:00:00", 400000,
         ver="cat-2", fp="fp2", lines=140)
ch = analysis.changes()
P(len(ch) == 2, f"a second change is picked up ({len(ch)})")
P(ch[0]["data"] and not ch[0]["rules"],
  "and a move with the same catalogue is attributed to their files")
P(ch[0]["lines_before"] == 100 and ch[0]["lines_after"] == 140,
  "which the purchase-line count backs up")
P(ch[0]["ran_at"] > ch[1]["ran_at"], "newest first")

print("\n=== two windows are never compared against each other ===")
# window_key arrived with the cache and is empty on every older row. Grouping on it alone
# put all of them in one bucket, and the first version of this reported FY2025 "changing"
# into All data -- a 57% drop presented as a real event.
con = db.connect()
con.execute("DELETE FROM analysis_run")
con.commit()
con.close()
seed_run("l1", "FY2025", "2025-01", "2025-12", "2026-02-01T09:00:00", 329288, key="")
seed_run("l2", "All data", "2024-01", "2026-06", "2026-02-01T10:00:00", 766854, key="")
seed_run("l3", "FY2025", "2025-01", "2025-12", "2026-02-01T11:00:00", 329288, key="")
P(analysis.changes() == [],
  "three legacy rows with no window key, two windows, and nothing changed")

print("\n=== a figure that barely twitched is not news ===")
con = db.connect()
con.execute("DELETE FROM analysis_run")
con.commit()
con.close()
seed_run("n1", "FY2025", "2025-01", "2025-12", "2026-03-01T09:00:00", 329288.0)
seed_run("n2", "FY2025", "2025-01", "2025-12", "2026-03-02T09:00:00", 329288.05)
P(analysis.changes() == [], "0.00002% is arithmetic, not a change")

print("\n=== a run we cannot attribute says so, rather than guessing ===")
con = db.connect()
con.execute("DELETE FROM analysis_run")
con.commit()
con.close()
seed_run("u1", "FY2025", "2025-01", "2025-12", "2026-04-01T09:00:00", 100000,
         ver=None, fp=None)
seed_run("u2", "FY2025", "2025-01", "2025-12", "2026-04-02T09:00:00", 200000,
         ver=None, fp=None)
ch = analysis.changes()
P(len(ch) == 1 and not ch[0]["known"], "flagged as unattributable")
P("not recorded" in ch[0]["why"],
  f"and says why rather than claiming nothing changed ({ch[0]['why']})")

print("\n=== the log is one client's, like everything else ===")
seed_run("x1", "FY2025", "2025-01", "2025-12", "2026-05-01T09:00:00", 1, tenant="rival")
seed_run("x2", "FY2025", "2025-01", "2025-12", "2026-05-02T09:00:00", 999999, tenant="rival")
P(all(c["run_id"] not in ("x1", "x2") for c in analysis.changes()),
  "another tenant's changes are not in this one's log")
P(len(analysis.changes("rival")) == 1, "and theirs is in theirs")

print(f"\n{'ALL PASS' if not FAILED else str(len(FAILED)) + ' FAILED'}")
sys.exit(1 if FAILED else 0)

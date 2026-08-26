"""
Does the database layer behave the same on SQLite and on Postgres?

Every test here runs TWICE -- once against a temporary SQLite file, once against a real
Postgres -- and asserts the same answer both times. A layer that merely "works on
Postgres" is not enough; it has to agree with the SQLite behaviour the whole codebase was
written against, or the numbers change when we deploy.

The one that matters most is the upsert. In SQLite, INSERT OR REPLACE replaces a row with
the same primary key. In Postgres the equivalent needs the conflict columns spelled out,
and if they are wrong the statement does not fail -- it inserts a second row. That would
double-count silently, so it is tested by counting rows, not by checking for an error.

    set DATABASE_URL=postgresql://mist:...@127.0.0.1:5432/mist_catalogue
    python test_store.py

Without DATABASE_URL only the SQLite half runs, and it says so.
"""
from __future__ import annotations

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

FAILED = []


def P(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAILED.append(msg)


def run_suite(label: str, url: str | None, sqlite_path: str | None):
    """The same tests, whichever backend is underneath."""
    import importlib
    if url:
        os.environ["DATABASE_URL"] = url
    else:
        os.environ.pop("DATABASE_URL", None)
    import store
    importlib.reload(store)

    print(f"\n=== {label} ({store.backend()}) ===")
    con = store.connect(sqlite_path)

    con.execute("DROP TABLE IF EXISTS t_store")
    con.execute("""CREATE TABLE t_store(
        a TEXT, b TEXT, n INTEGER, note TEXT, PRIMARY KEY (a, b))""")
    con.commit()

    # ---- placeholders ----
    con.execute("INSERT INTO t_store (a,b,n,note) VALUES (?,?,?,?)", ("x", "1", 10, "first"))
    con.commit()
    got = con.execute("SELECT n FROM t_store WHERE a=? AND b=?", ("x", "1")).fetchone()
    P(got[0] == 10, "? placeholders work, and round-trip a value")

    # ---- a question mark inside a string must NOT become a parameter ----
    con.execute("INSERT INTO t_store (a,b,n,note) VALUES (?,?,?,'really?')",
                ("q", "1", 1))
    con.commit()
    got = con.execute("SELECT note FROM t_store WHERE a='q'").fetchone()
    P(got[0] == "really?", "a ? inside a string literal survives translation")

    # ---- upsert: replace ----
    store.upsert(con, "t_store", ["a", "b", "n", "note"],
                 [("x", "1", 99, "replaced")], conflict=["a", "b"])
    con.commit()
    rows = con.execute("SELECT COUNT(*) FROM t_store WHERE a='x' AND b='1'").fetchone()[0]
    val = con.execute("SELECT n, note FROM t_store WHERE a='x' AND b='1'").fetchone()
    P(rows == 1, f"upsert replaced rather than duplicated ({rows} row)")
    P(val[0] == 99 and val[1] == "replaced", "and the new values are there")

    # ---- upsert: leave alone ----
    store.upsert(con, "t_store", ["a", "b", "n", "note"],
                 [("x", "1", 5, "should not appear")], conflict=["a", "b"], update=False)
    con.commit()
    rows = con.execute("SELECT COUNT(*) FROM t_store WHERE a='x' AND b='1'").fetchone()[0]
    val = con.execute("SELECT n FROM t_store WHERE a='x' AND b='1'").fetchone()[0]
    P(rows == 1 and val == 99, "update=False left the existing row untouched")

    # ---- upsert: many rows, some new some not ----
    store.upsert(con, "t_store", ["a", "b", "n", "note"],
                 [("x", "1", 1, "again"), ("y", "1", 2, "new"), ("y", "2", 3, "new")],
                 conflict=["a", "b"])
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM t_store").fetchone()[0]
    P(total == 4, f"a mixed batch inserted the new and replaced the old ({total} rows)")

    # ---- INSERT OR REPLACE is refused rather than mistranslated ----
    if store.IS_POSTGRES:
        try:
            con.execute("INSERT OR REPLACE INTO t_store VALUES (?,?,?,?)", ("z", "1", 1, ""))
            P(False, "INSERT OR REPLACE should have been refused")
        except ValueError as e:
            P("upsert" in str(e), "INSERT OR REPLACE is refused with a message naming upsert")

    # ---- PRAGMA is harmless ----
    try:
        con.execute("PRAGMA foreign_keys=ON")
        P(True, "PRAGMA does not blow up")
    except Exception as e:
        P(False, f"PRAGMA raised {type(e).__name__}")

    counts = con.execute("SELECT a, COUNT(*) FROM t_store GROUP BY a ORDER BY a").fetchall()
    con.execute("DROP TABLE t_store")
    con.commit()
    con.close()
    return [(r[0], r[1]) for r in counts]


tmp = os.path.join(tempfile.mkdtemp(prefix="store_test_"), "t.db")
sqlite_result = run_suite("SQLite", None, tmp)

url = os.environ.get("MIST_TEST_PG", "").strip()
if not url:
    print("\n(no MIST_TEST_PG set -- the Postgres half was skipped, so this run proves"
          "\n nothing about the deployed backend)")
else:
    pg_result = run_suite("Postgres", url, None)
    print("\n=== and the two agree ===")
    P(sqlite_result == pg_result,
      f"identical row counts from both backends: {sqlite_result}")

print(f"\n{'ALL PASS' if not FAILED else str(len(FAILED)) + ' FAILED'}")
sys.exit(1 if FAILED else 0)

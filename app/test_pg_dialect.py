"""
Every query the app runs must be one Postgres will accept, checked without a Postgres.

Railway runs this app on Postgres and every local run is SQLite, and the two disagree
about the percent sign. SQLite ignores it. psycopg reads EVERY `%` in a query as the start
of a placeholder -- inside a string literal, inside a comment, anywhere -- and refuses
anything that is not %s, %b or %t.

So a startup query written as

    UPDATE upload SET filename=? WHERE ... AND filename LIKE '%(adopted)'

passed every suite here and took production down: psycopg read `%(adopted)` as a named
parameter, the app failed during startup, the health check never answered, and the deploy
was refused. A comment reading "-- only 10% counted" would have done the same.

This does not need a database server. It wraps store.connect so each statement is also
handed to psycopg's own query parser -- the same code that raised in production -- and
then drives the startup path and every page. Anything Postgres would refuse is listed.

That alone was not enough, and the reason is the second lesson. The first fix escaped
every % in the converter. This test passed. The next deploy failed anyway -- and the
catalogue, which deployed, could no longer write -- because store.py's Postgres branches
(upsert, columns, table_exists) write %s by hand and the escaping turned each into
literal text. Those branches never run on SQLite, so a test that only watches SQLite
queries cannot see them. So this also drives those branches through the real
store.Connection and store.Cursor, with psycopg's parser standing in for the server.

    python test_pg_dialect.py        (needs the catalogue API running, for the pages)
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_TMP = tempfile.mkdtemp(prefix="pp_pgdialect_")
os.environ["MIST_APP_DB"] = os.path.join(_TMP, "pg.db")
os.environ["MIST_TENANT"] = "acme"
os.environ["MIST_SECRET_KEY"] = "test-only-key"

import store   # noqa: E402

if store.IS_POSTGRES:
    print("DATABASE_URL is set, so this is already running on Postgres; nothing to simulate.")
    sys.exit(0)

from psycopg.adapt import Transformer        # noqa: E402
from psycopg._queries import PostgresQuery   # noqa: E402

FAILED = []
REFUSED: dict[str, str] = {}
SEEN = 0


def P(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAILED.append(msg)


def postgres_would_refuse(sql: str, args) -> str | None:
    """The placeholder check psycopg runs before a query reaches the server."""
    try:
        PostgresQuery(Transformer()).convert(store.Placeholders.convert(sql), tuple(args))
    except Exception as e:                       # noqa: BLE001
        text = str(e)
        # Only the placeholder complaints are this test's business. Dumping a parameter
        # can fail here for want of a live connection, which says nothing about the SQL.
        if "placeholder" in text or "%" in text:
            return text
    return None


class Checked:
    """A SQLite connection that also asks psycopg whether it would accept each query."""

    def __init__(self, con):
        object.__setattr__(self, "_con", con)

    def _check(self, sql, args):
        global SEEN
        SEEN += 1
        why = postgres_would_refuse(sql, args if args is not None else ())
        if why:
            REFUSED.setdefault(" ".join(str(sql).split())[:160], why)

    def execute(self, sql, args=()):
        self._check(sql, args)
        return self._con.execute(sql, args)

    def executemany(self, sql, rows):
        rows = list(rows)
        self._check(sql, rows[0] if rows else ())
        return self._con.executemany(sql, rows)

    def __getattr__(self, name):
        return getattr(self._con, name)

    def __setattr__(self, name, value):
        setattr(self._con, name, value)


_real_connect = store.connect
store.connect = lambda *a, **k: Checked(_real_connect(*a, **k))

import auth        # noqa: E402
import catalogue   # noqa: E402
import db          # noqa: E402
import selection   # noqa: E402

print("=== the startup path: the part that took the deploy down ===")
db.init()
auth.init()
auth.add_tenant("acme", "Acme Catering Co", "")

# Something for adopt_orphans to adopt, and a file carrying the old "(adopted)" name for
# rename_adopted to rename -- the exact query that failed on Railway.
con = db.connect()
con.execute("INSERT INTO purchase_line (tenant, year, month, klantnr, restaurant, city, "
            "artikelnr, aantal, omzet, kg, kg_known, quality, source_upload) VALUES "
            "('acme',2025,1,'K1','CANTEEN','Delft','194072',1,10.0,10.0,1,'complete','orphan')")
store.upsert(con, "upload",
             ["id", "tenant", "filename", "stored_path", "adapter", "year", "uploaded_at",
              "periods", "lines", "products", "spend_eur", "verdict", "report_json",
              "committed_at", "commit_mode", "commit_note", "selected", "archived_at"],
             [("old", "acme", "seed:analysis.db (adopted)", "", "legacy", 2025,
               "2026-01-01T00:00:00", json.dumps(["2025-01"]), 1, 1, 10.0, "go", "{}",
               None, None, None, 1, None)],
             conflict=["id"])
con.commit()
con.close()

selection.adopt_orphans()
start_refused = dict(REFUSED)
P(not start_refused,
  f"every startup query is one Postgres accepts ({len(start_refused)} refused)")
for sql, why in start_refused.items():
    print(f"        {why}\n          in: {sql}")

con = db.connect()
names = {r[0] for r in con.execute("SELECT filename FROM upload WHERE tenant='acme'")}
con.close()
P("seed:analysis.db (adopted)" not in names,
  "and the old file name really was renamed, so the query that failed was exercised")

print()
print("=== every page an admin and a client can open ===")
if catalogue.health() is None:
    print("  (catalogue API not running -- pages skipped; startup path above still checked)")
else:
    from fastapi.testclient import TestClient   # noqa: E402
    from starlette.routing import Route          # noqa: E402
    import main                                  # noqa: E402

    import publish                               # noqa: E402

    auth.create_user("boss", "boss-password-1", "admin", None, "Boss")
    auth.create_user("acmeuser", "acme-password-1", "client", "acme", "Acme")
    # Publishing writes more than any page does -- the frozen figures, the downloads, and
    # retiring the copy it replaces -- and a client's pages then read only that. So it is
    # run twice before the pages are walked: once to create, once to replace.
    for _ in range(2):
        pub = publish.get(publish.start("acme", "boss", wait=True))
    P(pub["status"] == "live", f"a publish runs start to finish, twice ({pub.get('error')})")
    publish.recover(startup=True)
    for user, pw in (("boss", "boss-password-1"), ("acmeuser", "acme-password-1")):
        c = TestClient(main.app, follow_redirects=False)
        c.post("/login", data={"username": user, "password": pw})
        for r in main.app.router.routes:
            if isinstance(r, Route) and "GET" in (r.methods or set()) and "{" not in r.path \
                    and r.path not in ("/logout",):
                c.get(r.path)
    page_refused = {k: v for k, v in REFUSED.items() if k not in start_refused}
    P(not page_refused,
      f"every query behind every page is one Postgres accepts ({len(page_refused)} refused)")
    for sql, why in page_refused.items():
        print(f"        {why}\n          in: {sql}")

print()
print("=== the Postgres-only branches, which never run on SQLite ===")


class _FakeRawCursor:
    """Stands in for a psycopg cursor: parses as psycopg would, sends nothing anywhere."""
    rowcount = 0
    description = None

    def execute(self, sql, params=None):
        if params is not None:
            PostgresQuery(Transformer()).convert(sql, params)

    def executemany(self, sql, rows):
        for r in rows:
            PostgresQuery(Transformer()).convert(sql, r)

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class _FakeRaw:
    def cursor(self, row_factory=None):
        return _FakeRawCursor()


def postgres_branches() -> dict[str, str]:
    """Run store.py's Postgres-only code through the real Connection. -> {name: error}."""
    was = store.IS_POSTGRES
    store.IS_POSTGRES = True
    pg = store.Connection(_FakeRaw())
    probes = {
        "store.columns": lambda: store.columns(pg, "upload"),
        "store.table_exists": lambda: store.table_exists(pg, "upload"),
        "store.upsert, replacing": lambda: store.upsert(
            pg, "t", ["a", "b"], [(1, 2)], conflict=["a"]),
        "store.upsert, ignoring": lambda: store.upsert(
            pg, "t", ["a", "b"], [(1, 2)], conflict=["a"], update=False),
    }
    broken = {}
    try:
        for name, probe in probes.items():
            try:
                probe()
            except Exception as e:           # noqa: BLE001
                broken[name] = str(e)
    finally:
        store.IS_POSTGRES = was
    return broken


broken = postgres_branches()
P(not broken, f"upsert, columns and table_exists work on Postgres ({len(broken)} broken)")
for name, why in broken.items():
    print(f"        {name}: {why}")

print()
print("=== and both checks are real ===")
# Two misses in one day, so each guard is shown the mistake it exists for.
P(postgres_would_refuse(
    "UPDATE upload SET filename=? WHERE adapter='legacy' AND filename LIKE '%(adopted)'",
    ("x",)) is not None,
  "the query that stopped Railway, written inline, is refused")
P(postgres_would_refuse(
    "UPDATE upload SET filename=? WHERE adapter='legacy' AND filename LIKE ?",
    ("x", "%(adopted)")) is None,
  "and the same query with the pattern as a parameter is accepted")

# The first attempted fix escaped every % in the converter. Put it back for a moment and
# confirm the branch check notices the damage it did.
_real_convert = store.Placeholders.convert


def _escaping_convert(sql):
    return _real_convert(sql.replace("%", "%%"))


store.Placeholders.convert = staticmethod(_escaping_convert)
try:
    damaged = postgres_branches()
finally:
    store.Placeholders.convert = staticmethod(_real_convert)
P(len(damaged) == 4,
  f"escaping % in the converter would be caught ({len(damaged)} of 4 branches break)")
print(f"  ({SEEN:,} statements checked)")

print()
print("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED")
sys.exit(1 if FAILED else 0)

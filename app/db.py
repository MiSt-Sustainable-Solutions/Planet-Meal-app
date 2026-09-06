"""
The app's own database — everything that belongs to a CLIENT.

    purchase_line   what this client bought, per product per restaurant per month
    product         the products they buy, as their supplier describes them
    upload          every file received, its verdict, and whether it was committed
    upload_line     a staged file's lines, held until someone accepts them
    analysis_run    saved results, so history survives and can be downloaded

The shared catalogue API holds none of this. It knows about products in general; this
knows what one client actually purchased. That split is what makes a second client a
tenant rather than a rebuild.

SQLite locally. The schema is deliberately plain so it ports to Postgres: no SQLite-only
types, no AUTOINCREMENT, explicit primary keys.
"""
from __future__ import annotations

import hashlib

import config
import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS product(
  tenant TEXT NOT NULL,
  artikelnr TEXT NOT NULL,
  description TEXT, brand TEXT, category TEXT,
  ivp TEXT, vp TEXT, maat REAL, eenh TEXT,
  ean TEXT, ean_he TEXT, foodflag TEXT,
  first_seen TEXT, last_seen TEXT,
  PRIMARY KEY (tenant, artikelnr));

CREATE TABLE IF NOT EXISTS purchase_line(
  tenant TEXT NOT NULL,
  year INTEGER NOT NULL, month INTEGER NOT NULL,
  klantnr TEXT, restaurant TEXT, city TEXT,
  artikelnr TEXT NOT NULL,
  aantal REAL, omzet REAL, kg REAL, kg_known INTEGER,
  quality TEXT, source_upload TEXT);
CREATE INDEX IF NOT EXISTS ix_pl_period ON purchase_line(tenant, year, month);
CREATE INDEX IF NOT EXISTS ix_pl_art ON purchase_line(tenant, artikelnr);

CREATE TABLE IF NOT EXISTS upload(
  id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
  filename TEXT, stored_path TEXT, adapter TEXT, year INTEGER,
  uploaded_at TEXT, periods TEXT, lines INTEGER, products INTEGER, spend_eur REAL,
  verdict TEXT, report_json TEXT,
  committed_at TEXT, commit_mode TEXT, commit_note TEXT);

CREATE TABLE IF NOT EXISTS upload_line(
  upload_id TEXT NOT NULL,
  year INTEGER, month INTEGER, klantnr TEXT, restaurant TEXT, city TEXT,
  artikelnr TEXT, aantal REAL, omzet REAL, kg REAL, kg_known INTEGER, quality TEXT);
CREATE INDEX IF NOT EXISTS ix_ul ON upload_line(upload_id);

CREATE TABLE IF NOT EXISTS upload_product(
  upload_id TEXT NOT NULL, artikelnr TEXT, description TEXT, brand TEXT, category TEXT,
  ivp TEXT, vp TEXT, maat REAL, eenh TEXT, ean TEXT, ean_he TEXT, foodflag TEXT);
CREATE INDEX IF NOT EXISTS ix_up ON upload_product(upload_id);

-- Which file owns a month, when more than one selected file supplies it. Only
-- contested months need a row; everything else is decided by there being one candidate.
CREATE TABLE IF NOT EXISTS month_owner(
  tenant TEXT NOT NULL,
  year INTEGER NOT NULL,
  month INTEGER NOT NULL,
  upload_id TEXT NOT NULL,
  decided_at TEXT,
  decided_by TEXT,
  PRIMARY KEY (tenant, year, month));

CREATE TABLE IF NOT EXISTS analysis_run(
  id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
  label TEXT, period_from TEXT, period_to TEXT, eat_profile TEXT,
  ran_at TEXT, lines INTEGER,
  food_kg REAL, co2_kg REAL, intensity REAL, eat_score REAL,
  specific_pct REAL, result_json TEXT,
  window_key TEXT, catalogue_version TEXT, data_fingerprint TEXT);
CREATE INDEX IF NOT EXISTS ix_run ON analysis_run(tenant, ran_at);
"""

# Runs only after MIGRATIONS, because an index cannot name a column the table does not
# have yet -- and on an app that predates caching, it does not.
POST_MIGRATION = """
CREATE INDEX IF NOT EXISTS ix_run_cache
  ON analysis_run(tenant, window_key, catalogue_version, data_fingerprint);
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not add a column
# to a table that already exists, so an app that has been running since before caching
# would keep its old five-column analysis_run and every insert would fail.
MIGRATIONS = [
    ("analysis_run", "window_key", "TEXT"),
    ("analysis_run", "catalogue_version", "TEXT"),
    ("analysis_run", "data_fingerprint", "TEXT"),
    # A file is held whether or not it counts. `selected` is the switch; archiving is the
    # first of the two steps to being rid of it.
    ("upload", "selected", "INTEGER DEFAULT 0"),
    ("upload", "archived_at", "TEXT"),
]

# WHICH LINES COUNT.
#
# Every purchase line records the file it came from, so a line counts when that file is
# selected, is not archived, and OWNS the month the line falls in.
#
# Ownership is the part that is not obvious, and it exists because of a real case in TU
# Delft's own data: "Augustus 2024" covers January to August and "December 2024" covers
# January to December. Select both and eight months are counted twice; select either one
# alone and months are lost. Neither file is wrong -- they simply overlap, and no rule can
# decide which should win. So a month has an owner, recorded in a table, and a person can
# change it.
#
# A month with no owner row is owned by whichever selected file covers it, which is the
# ordinary case and needs no decision at all.
COUNTED_JOIN = """
        JOIN upload u ON u.id = l.source_upload AND u.tenant = l.tenant
        LEFT JOIN month_owner o
               ON o.tenant = l.tenant AND o.year = l.year AND o.month = l.month
"""
COUNTED_WHERE = """
        AND u.selected = 1 AND u.archived_at IS NULL
        AND (o.upload_id IS NULL OR o.upload_id = l.source_upload)
"""


def connect():
    """The one place the app opens a database.

    SQLite on a laptop, Postgres when DATABASE_URL is set. Because every read and write
    in the app comes through here, that is the whole of the switch -- nothing else in the
    app knows or cares which backend it got.
    """
    con = store.connect(config.APP_DB, rows=True)
    con.execute("PRAGMA foreign_keys=ON")   # a SQLite hint; ignored on Postgres
    return con


def init() -> None:
    con = connect()
    con.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        # store.columns() rather than PRAGMA: PRAGMA is SQLite-only, and on Postgres it
        # would answer "no columns", so every migration would try to add a column that is
        # already there.
        have = store.columns(con, table)
        if column not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    con.executescript(POST_MIGRATION)
    con.commit()
    con.close()


def window_fingerprint(y0: int, m0: int, y1: int, m1: int,
                       tenant: str | None = None) -> str:
    """A short hash of THIS CLIENT'S data for a window. Changes when the data changes.

    Half of the cache key. The catalogue's version answers "have the rules changed?"; this
    answers "have the purchases changed?". Both must be unchanged for a saved result to
    still be the right answer, and importing a month into the middle of a year moves this
    even though the catalogue has not moved at all.

    Counts and sums, not a row-by-row checksum: it is one indexed aggregate rather than a
    scan of 29,000 rows, and any insert, delete or re-import moves it.
    """
    snap = snapshot(tenant)
    lo, hi = y0 * 100 + m0, y1 * 100 + m1
    parts = [f"{m['period']}:{m['lines']}:{m['kg']}:{m['spend_eur']}:{m['quality']}"
             for m in snap["months"] if lo <= m["year"] * 100 + m["month"] <= hi]
    # The product table feeds matching — a barcode arriving on a later upload changes the
    # answer without touching a single purchase line.
    parts.append(f"products:{snap['products']}:{snap['last_seen']}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- reads
#
# One page asks "what months do we have?" four separate times: to pick the default window,
# to parse it, to fingerprint it for the cache, and to build the window picker. That is
# one GROUP BY over every purchase line, four times over.
#
# On a laptop with the database on a local disk that is a few milliseconds and nobody
# notices. This database lives in a synced Google Drive folder, where the same query costs
# about five seconds — so four of them were most of a twenty-second page.
#
# So it is read once and held. The guard is the database file's own modification time,
# not a timer and not a flag someone has to remember to clear: one stat() call, and any
# write from any process invalidates it. Getting cache invalidation wrong here would show
# a client last week's months as though they were this week's.
_SNAP: dict[str, tuple] = {}


def _stamp() -> tuple:
    """(size, mtime) of the database, including its write-ahead log if there is one."""
    import os
    out = []
    for path in (config.APP_DB, config.APP_DB + "-wal"):
        try:
            st = os.stat(path)
            out.append((st.st_size, st.st_mtime_ns))
        except OSError:
            out.append(None)
    return tuple(out)


def invalidate() -> None:
    """Drop the held snapshot. Belt and braces — the file stamp already catches writes."""
    _SNAP.clear()


def snapshot(tenant: str | None = None) -> dict:
    """Everything cheap-to-derive about this client's data, read once per change.

    {months: [...], products: n, last_seen: str}
    """
    tenant = tenant or config.TENANT
    stamp = _stamp()
    held = _SNAP.get(tenant)
    if held and held[0] == stamp:
        return held[1]

    con = connect()
    rows = con.execute("""
        -- Every column qualified with l. now that month_owner is joined: it carries a
        -- year and a month of its own, so a bare `year` is ambiguous.
        SELECT l.year AS year, l.month AS month,
               -- A month is partial if ANY of it is. SQLite would happily return a
               -- column that is not in the GROUP BY, picking an arbitrary row's value;
               -- Postgres refuses, which is the better behaviour and forces the rule to
               -- be stated. Written out rather than MIN(quality) so it does not depend
               -- on 'PARTIAL' happening to sort before 'complete'.
               CASE WHEN SUM(CASE WHEN l.quality = 'complete' THEN 0 ELSE 1 END) > 0
                    THEN 'PARTIAL' ELSE 'complete' END AS quality,
               COUNT(*) AS lines, SUM(l.omzet) AS spend,
               SUM(l.kg) AS kg,
               SUM(CASE WHEN l.kg_known=0 THEN 1 ELSE 0 END) AS piece_lines
        FROM purchase_line l""" + COUNTED_JOIN + """
        WHERE l.tenant=?""" + COUNTED_WHERE + """
        GROUP BY l.year, l.month ORDER BY l.year, l.month""", (tenant,)).fetchall()
    pr = con.execute("SELECT COUNT(*), MAX(last_seen) FROM product WHERE tenant=?",
                     (tenant,)).fetchone()
    con.close()

    out = dict(
        months=[dict(year=r["year"], month=r["month"],
                     period=f"{r['year']}-{r['month']:02d}",
                     quality=r["quality"], complete=(r["quality"] == "complete"),
                     lines=r["lines"], spend_eur=round(r["spend"] or 0),
                     kg=round(r["kg"] or 0), piece_lines=r["piece_lines"])
                for r in rows],
        products=pr[0] or 0, last_seen=pr[1])
    _SNAP[tenant] = (stamp, out)
    return out


def months(tenant: str | None = None) -> list[dict]:
    """Every month this client has data for, with the quality flag it arrived with.

    This is the app's own answer to "what do we already have?" — the question the
    overlap check asks, and the reason it does not need the catalogue API.
    """
    return snapshot(tenant)["months"]


def lines_for(y0: int, m0: int, y1: int, m1: int, tenant: str | None = None) -> list[dict]:
    """The purchase lines for a window, shaped exactly as the catalogue API expects."""
    tenant = tenant or config.TENANT
    con = connect()
    rows = con.execute("""
        SELECT l.artikelnr, p.description, p.category, p.ean, p.ean_he,
               l.restaurant, l.klantnr, l.year, l.month, l.aantal, l.omzet, l.kg, l.kg_known
        FROM purchase_line l
        LEFT JOIN product p ON p.tenant=l.tenant AND p.artikelnr=l.artikelnr""" +
        COUNTED_JOIN + """
        WHERE l.tenant=? AND (l.year*100+l.month) BETWEEN ? AND ?""" + COUNTED_WHERE,
        (tenant, y0 * 100 + m0, y1 * 100 + m1)).fetchall()
    con.close()
    # The barcodes travel with the line. An article number belongs to the supplier; a
    # barcode belongs to the product, so it is the only identifier that survives a change
    # of wholesaler — and the only one that lets a decision made for one client be reused
    # for another.
    return [dict(artikelnr=r["artikelnr"], description=r["description"] or "",
                 category=r["category"] or "", restaurant=r["restaurant"] or "",
                 klantnr=r["klantnr"] or "", year=r["year"], month=r["month"],
                 aantal=r["aantal"] or 0.0, omzet=r["omzet"] or 0.0,
                 kg=r["kg"] or 0.0, kg_known=r["kg_known"] or 0,
                 ean_ce=r["ean"] or "", ean_he=r["ean_he"] or "")
            for r in rows]


def lines_picked(owner: dict, tenant: str | None = None) -> list[dict]:
    """The lines a chosen set of files supplies, one file per month.

    `owner` maps (year, month) -> upload_id and comes from selection.resolve(), which is
    what stops two chosen files that both cover March from counting March twice.

    Ignores selected/archived entirely. This answers "what do these files say", and the
    answer must not depend on which of them somebody happens to be counting today.
    """
    if not owner:
        return []
    ids = sorted(set(owner.values()))
    marks = ",".join("?" for _ in ids)
    con = connect()
    rows = con.execute(f"""
        SELECT l.artikelnr, p.description, p.category, p.ean, p.ean_he,
               l.restaurant, l.klantnr, l.year, l.month, l.aantal, l.omzet, l.kg, l.kg_known,
               l.source_upload
        FROM purchase_line l
        LEFT JOIN product p ON p.tenant=l.tenant AND p.artikelnr=l.artikelnr
        WHERE l.tenant=? AND l.source_upload IN ({marks})""",
        (tenant or config.TENANT, *ids)).fetchall()
    con.close()
    return [dict(artikelnr=r["artikelnr"], description=r["description"] or "",
                 category=r["category"] or "", restaurant=r["restaurant"] or "",
                 klantnr=r["klantnr"] or "", year=r["year"], month=r["month"],
                 aantal=r["aantal"] or 0.0, omzet=r["omzet"] or 0.0,
                 kg=r["kg"] or 0.0, kg_known=r["kg_known"] or 0,
                 ean_ce=r["ean"] or "", ean_he=r["ean_he"] or "")
            for r in rows
            if owner.get((r["year"], r["month"])) == r["source_upload"]]


def picked_fingerprint(upload_ids: list[str], tenant: str | None = None) -> str:
    """Half the cache key for a chosen set of files.

    window_fingerprint() aggregates the COUNTED data for a period, so it cannot see a
    file that is not being counted -- and an ad-hoc pick is mostly interesting for
    exactly those. Re-import one and the months would look untouched while the answer
    changed, which is a stale number nobody asked for.
    """
    if not upload_ids:
        return "empty"
    marks = ",".join("?" for _ in upload_ids)
    con = connect()
    rows = con.execute(
        "SELECT source_upload, COUNT(*), SUM(kg), SUM(omzet) FROM purchase_line "
        f"WHERE tenant=? AND source_upload IN ({marks}) GROUP BY source_upload "
        "ORDER BY source_upload", (tenant or config.TENANT, *upload_ids)).fetchall()
    con.close()
    parts = [f"{r[0]}:{r[1]}:{r[2]}:{r[3]}" for r in rows]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def lines_from_upload(upload_id: str, tenant: str | None = None) -> list[dict]:
    """Every line ONE file supplied, shaped for the catalogue.

    Deliberately ignores selection and ownership. Those decide what makes up a client's
    numbers; this answers a different question -- what is in this file -- and the answer
    should not change because somebody ticked a box on another page.
    """
    tenant = tenant or config.TENANT
    con = connect()
    rows = con.execute("""
        SELECT l.artikelnr, p.description, p.category, p.ean, p.ean_he,
               l.restaurant, l.klantnr, l.year, l.month, l.aantal, l.omzet, l.kg, l.kg_known
        FROM purchase_line l
        LEFT JOIN product p ON p.tenant=l.tenant AND p.artikelnr=l.artikelnr
        WHERE l.tenant=? AND l.source_upload=?""", (tenant, upload_id)).fetchall()
    con.close()
    return [dict(artikelnr=r["artikelnr"], description=r["description"] or "",
                 category=r["category"] or "", restaurant=r["restaurant"] or "",
                 klantnr=r["klantnr"] or "", year=r["year"], month=r["month"],
                 aantal=r["aantal"] or 0.0, omzet=r["omzet"] or 0.0,
                 kg=r["kg"] or 0.0, kg_known=r["kg_known"] or 0,
                 ean_ce=r["ean"] or "", ean_he=r["ean_he"] or "")
            for r in rows]


def _owner_clause(owner: dict) -> tuple[str, list]:
    """SQL restricting lines to the ONE chosen file per month, and its parameters.

    The counted reads get the same restriction from month_owner through COUNTED_WHERE.
    An ad-hoc pick has no stored owner -- deliberately, it must not disturb the client's
    real selection -- so it carries the decision in the query instead.
    """
    if not owner:
        return " AND 1=0", []
    terms, params = [], []
    for (y, m), upload_id in sorted(owner.items()):
        terms.append("(l.year=? AND l.month=? AND l.source_upload=?)")
        params.extend([y, m, upload_id])
    return " AND (" + " OR ".join(terms) + ")", params


def piece_items(y0: int, m0: int, y1: int, m1: int, limit: int = 100,
                tenant: str | None = None, owner: dict | None = None) -> dict:
    """The lines that weigh zero. The app's own data, so no API call needed.

    `owner` switches it from the counted selection to a chosen set of files, so that a
    per-file analysis reports ITS per-piece gap rather than the client's official one.
    """
    tenant = tenant or config.TENANT
    if owner is None:
        join, cond, extra = COUNTED_JOIN, COUNTED_WHERE, []
    else:
        join, (cond, extra) = "", _owner_clause(owner)
    con = connect()
    rows = con.execute("""
        SELECT l.artikelnr, p.description, p.category, p.vp, p.eenh,
               SUM(l.aantal) AS pieces, SUM(l.omzet) AS spend
        FROM purchase_line l
        LEFT JOIN product p ON p.tenant=l.tenant AND p.artikelnr=l.artikelnr""" +
        join + """
        WHERE l.tenant=? AND l.kg_known=0 AND (l.year*100+l.month) BETWEEN ? AND ?""" +
        cond + """
        -- every product column here is determined by artikelnr (it is the product
        -- table's key), so naming them changes nothing except that Postgres will
        -- accept it. SQLite allowed the shorter form and picked a value at random.
        GROUP BY l.artikelnr, p.description, p.category, p.vp, p.eenh
        ORDER BY spend DESC""",
        (tenant, y0 * 100 + m0, y1 * 100 + m1, *extra)).fetchall()
    total = con.execute("""SELECT SUM(l.omzet) FROM purchase_line l""" + join +
        """ WHERE l.tenant=? AND (l.year*100+l.month) BETWEEN ? AND ?""" + cond,
        (tenant, y0 * 100 + m0, y1 * 100 + m1, *extra)).fetchone()[0] or 0
    con.close()
    spend = sum(r["spend"] or 0 for r in rows)
    return dict(products=len(rows), spend_eur=round(spend),
                pct_of_spend=round(100 * spend / total, 1) if total else 0.0,
                rows=[dict(artikelnr=r["artikelnr"], description=r["description"] or "",
                           category=r["category"] or "", vp=r["vp"], eenh=r["eenh"],
                           pieces=round(r["pieces"] or 0), spend_eur=round(r["spend"] or 0))
                      for r in rows[:limit]])


def known_articles(tenant: str | None = None) -> set[str]:
    tenant = tenant or config.TENANT
    con = connect()
    out = {r[0] for r in con.execute(
        "SELECT artikelnr FROM product WHERE tenant=?", (tenant,))}
    con.close()
    return out


def stats(tenant: str | None = None) -> dict:
    """Row counts and spend for ONE client. Never call this without saying which.

    The default exists for the single-tenant scripts that predate multi-tenancy. It is a
    trap on any route: it does not fail, it quietly answers about whichever client
    MIST_TENANT happens to name. That is how /api/health came to publish TU Delft's spend
    to the open internet.
    """
    tenant = tenant or config.TENANT
    con = connect()
    r = con.execute("""SELECT COUNT(*) AS lines, COUNT(DISTINCT l.artikelnr) AS products,
                              COUNT(DISTINCT l.restaurant) AS restaurants,
                              SUM(l.omzet) AS spend
                       FROM purchase_line l""" + COUNTED_JOIN + """
                       WHERE l.tenant=?""" + COUNTED_WHERE, (tenant,)).fetchone()
    uploads = con.execute("SELECT COUNT(*) FROM upload WHERE tenant=?", (tenant,)).fetchone()[0]
    runs = con.execute("SELECT COUNT(*) FROM analysis_run WHERE tenant=?", (tenant,)).fetchone()[0]
    con.close()
    return dict(lines=r["lines"] or 0, products=r["products"] or 0,
                restaurants=r["restaurants"] or 0, spend_eur=round(r["spend"] or 0),
                uploads=uploads, analyses=runs)

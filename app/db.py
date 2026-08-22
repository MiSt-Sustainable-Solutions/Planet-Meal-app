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

import sqlite3

import config

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

CREATE TABLE IF NOT EXISTS analysis_run(
  id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
  label TEXT, period_from TEXT, period_to TEXT, eat_profile TEXT,
  ran_at TEXT, lines INTEGER,
  food_kg REAL, co2_kg REAL, intensity REAL, eat_score REAL,
  specific_pct REAL, result_json TEXT);
CREATE INDEX IF NOT EXISTS ix_run ON analysis_run(tenant, ran_at);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.APP_DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init() -> None:
    con = connect()
    con.executescript(SCHEMA)
    con.commit()
    con.close()


# --------------------------------------------------------------------------- reads
def months(tenant: str | None = None) -> list[dict]:
    """Every month this client has data for, with the quality flag it arrived with.

    This is the app's own answer to "what do we already have?" — the question the
    overlap check asks, and the reason it does not need the catalogue API.
    """
    tenant = tenant or config.TENANT
    con = connect()
    rows = con.execute("""
        SELECT year, month, quality, COUNT(*) AS lines, SUM(omzet) AS spend,
               SUM(kg) AS kg, SUM(CASE WHEN kg_known=0 THEN 1 ELSE 0 END) AS piece_lines
        FROM purchase_line WHERE tenant=?
        GROUP BY year, month ORDER BY year, month""", (tenant,)).fetchall()
    con.close()
    return [dict(year=r["year"], month=r["month"], period=f"{r['year']}-{r['month']:02d}",
                 quality=r["quality"], complete=(r["quality"] == "complete"),
                 lines=r["lines"], spend_eur=round(r["spend"] or 0), kg=round(r["kg"] or 0),
                 piece_lines=r["piece_lines"])
            for r in rows]


def lines_for(y0: int, m0: int, y1: int, m1: int, tenant: str | None = None) -> list[dict]:
    """The purchase lines for a window, shaped exactly as the catalogue API expects."""
    tenant = tenant or config.TENANT
    con = connect()
    rows = con.execute("""
        SELECT l.artikelnr, p.description, p.category, l.restaurant, l.klantnr,
               l.year, l.month, l.aantal, l.omzet, l.kg, l.kg_known
        FROM purchase_line l
        LEFT JOIN product p ON p.tenant=l.tenant AND p.artikelnr=l.artikelnr
        WHERE l.tenant=? AND (l.year*100+l.month) BETWEEN ? AND ?""",
        (tenant, y0 * 100 + m0, y1 * 100 + m1)).fetchall()
    con.close()
    return [dict(artikelnr=r["artikelnr"], description=r["description"] or "",
                 category=r["category"] or "", restaurant=r["restaurant"] or "",
                 klantnr=r["klantnr"] or "", year=r["year"], month=r["month"],
                 aantal=r["aantal"] or 0.0, omzet=r["omzet"] or 0.0,
                 kg=r["kg"] or 0.0, kg_known=r["kg_known"] or 0)
            for r in rows]


def piece_items(y0: int, m0: int, y1: int, m1: int, limit: int = 100,
                tenant: str | None = None) -> dict:
    """The lines that weigh zero. The app's own data, so no API call needed."""
    tenant = tenant or config.TENANT
    con = connect()
    rows = con.execute("""
        SELECT l.artikelnr, p.description, p.category, p.vp, p.eenh,
               SUM(l.aantal) AS pieces, SUM(l.omzet) AS spend
        FROM purchase_line l
        LEFT JOIN product p ON p.tenant=l.tenant AND p.artikelnr=l.artikelnr
        WHERE l.tenant=? AND l.kg_known=0 AND (l.year*100+l.month) BETWEEN ? AND ?
        GROUP BY l.artikelnr ORDER BY spend DESC""",
        (tenant, y0 * 100 + m0, y1 * 100 + m1)).fetchall()
    total = con.execute("""SELECT SUM(omzet) FROM purchase_line
        WHERE tenant=? AND (year*100+month) BETWEEN ? AND ?""",
        (tenant, y0 * 100 + m0, y1 * 100 + m1)).fetchone()[0] or 0
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
    tenant = tenant or config.TENANT
    con = connect()
    r = con.execute("""SELECT COUNT(*) AS lines, COUNT(DISTINCT artikelnr) AS products,
                              COUNT(DISTINCT restaurant) AS restaurants, SUM(omzet) AS spend
                       FROM purchase_line WHERE tenant=?""", (tenant,)).fetchone()
    uploads = con.execute("SELECT COUNT(*) FROM upload WHERE tenant=?", (tenant,)).fetchone()[0]
    runs = con.execute("SELECT COUNT(*) FROM analysis_run WHERE tenant=?", (tenant,)).fetchone()[0]
    con.close()
    return dict(lines=r["lines"] or 0, products=r["products"] or 0,
                restaurants=r["restaurants"] or 0, spend_eur=round(r["spend"] or 0),
                uploads=uploads, analyses=runs)

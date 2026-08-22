"""
Seed this app's database with a client's existing purchase history.

TU Delft's 1.5 years of Sligro exports were parsed long before this app existed; the
result lives in the catalogue repo's `analysis.db`. That data is CLIENT data, so its home
is here, not there. This moves it across once.

    python -m app.seed --from "../MiSt Tool Mrigank/catalogue/db/analysis.db"

Idempotent: re-running replaces the seeded rows rather than doubling them. That matters,
because doubling purchase lines is the exact failure this whole product is built to
prevent.

Nothing is deleted from the catalogue repo — it still needs its copy to rebuild the
verified reference baseline.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import db  # noqa: E402

SEED_TAG = "seed:analysis.db"


def seed(source: str, tenant: str | None = None, quiet: bool = False) -> dict:
    tenant = tenant or config.TENANT
    if not os.path.exists(source):
        raise SystemExit(f"no such file: {source}")

    db.init()
    src = sqlite3.connect(source)
    src.row_factory = sqlite3.Row
    products = src.execute("SELECT * FROM product").fetchall()
    facts = src.execute("SELECT * FROM fact").fetchall()
    src.close()

    now = dt.datetime.now().isoformat(timespec="seconds")
    con = db.connect()
    before = con.execute("SELECT COUNT(*) FROM purchase_line WHERE tenant=?",
                         (tenant,)).fetchone()[0]
    # replace only what a previous seed inserted, so a real upload is never clobbered
    con.execute("DELETE FROM purchase_line WHERE tenant=? AND source_upload=?",
                (tenant, SEED_TAG))
    # upsert products, so a re-seed refreshes descriptions without losing a barcode
    con.executemany("""
        INSERT INTO product (tenant, artikelnr, description, brand, category, ivp, vp,
                             maat, eenh, ean, ean_he, foodflag, first_seen, last_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(tenant, artikelnr) DO UPDATE SET
            description=excluded.description, brand=excluded.brand,
            category=excluded.category, ivp=excluded.ivp, vp=excluded.vp,
            maat=excluded.maat, eenh=excluded.eenh,
            ean=CASE WHEN excluded.ean<>'' THEN excluded.ean ELSE product.ean END,
            last_seen=excluded.last_seen""",
        [(tenant, p["artikelnr"], p["description"], p["brand"], p["category"], p["ivp"],
          p["vp"], p["maat"], p["eenh"], p["gtin_ce"], p["gtin_he"], p["foodflag"], now, now)
         for p in products])

    con.executemany(
        "INSERT INTO purchase_line VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(tenant, f["year"], f["month"], f["klantnr"], f["restaurant"], f["city"],
          f["artikelnr"], f["aantal"], f["omzet"], f["kg"], f["kg_known"],
          f["quality"], SEED_TAG) for f in facts])
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM purchase_line WHERE tenant=?",
                        (tenant,)).fetchone()[0]
    con.close()

    out = dict(tenant=tenant, source=source, products=len(products), lines=len(facts),
               rows_before=before, rows_after=after)
    if not quiet:
        print(f"seeded {tenant}: {len(products):,} products, {len(facts):,} purchase lines "
              f"({before:,} -> {after:,} rows)")
        for m in db.months(tenant):
            flag = "" if m["complete"] else f"   <-- {m['quality']}"
            print(f"  {m['period']}  EUR {m['spend_eur']:>9,}  kg {m['kg']:>8,}  "
                  f"lines {m['lines']:>5}{flag}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    default = os.path.join(config.ROOT.parent, "MiSt Tool Mrigank", "catalogue", "db",
                           "analysis.db")
    ap.add_argument("--from", dest="source", default=default,
                    help="the catalogue repo's analysis.db")
    ap.add_argument("--tenant", default=config.TENANT)
    seed(**vars(ap.parse_args()))

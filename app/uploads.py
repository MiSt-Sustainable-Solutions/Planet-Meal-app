"""
Receive a file, judge it, and hold everything it contained.

There is no commit step any more, and no import modes. A file used to be poured into one
merged history by new_only / replace / all -- three modes that existed only to stop the
same month landing twice, where choosing wrongly doubled a year and nothing said so.

Now every line a file supplies is stored the moment the file is read, tagged with the
file it came from, and counts for nothing until somebody selects that file. Double
counting stops being a risk the app guards against: a month is supplied by exactly one
selected file, because one file owns it. See selection.py.

So this module only receives. What counts, what a month is worth, and what gets thrown
away are all decisions, and decisions live in selection.py.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import uuid

import adapters
import catalogue
import config
import db
import preflight


class CommitError(Exception):
    """The commit was refused. The message says what to do instead."""


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")



def _ingest(con, uid: str, tenant: str, reading, rep: dict) -> int:
    """Store every line and product the file supplied. Counts for nothing yet.

    Everything the file contains goes in, whole. There is no month filtering here,
    because whether a month should be counted is not a property of the file -- it is a
    decision, made later, per month, and recorded where it can be seen.
    """
    # An adapter cannot tell whether a month is complete -- only the pre-flight can, by
    # comparing it against what this client already holds. So the verdict is recorded
    # against the months it applies to, and every screen reads it from the data rather
    # than re-deriving it. A month judged partial is kept and labelled, never dropped.
    partial = set()
    for fnd in rep.get("findings", []):
        if fnd.get("code") in ("partial_months", "thin_months"):
            partial |= set(fnd.get("months") or [])

    rows = [(tenant, l["year"], l["month"], l["klantnr"], l["restaurant"], l["city"],
             l["artikelnr"], l["aantal"], l["omzet"], l["kg"], l["kg_known"],
             ("PARTIAL" if f"{l['year']}-{l['month']:02d}" in partial else "complete"),
             uid)
            for l in ({"year": x[0], "month": x[1], "klantnr": x[2], "restaurant": x[3],
                       "city": x[4], "artikelnr": x[5], "aantal": x[6], "omzet": x[7],
                       "kg": x[8], "kg_known": x[9]} for x in reading.lines)]
    con.executemany(
        "INSERT INTO purchase_line VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    now = _now()
    for p in reading.products.values():
        con.execute("""
            INSERT INTO product (tenant, artikelnr, description, brand, category, ivp, vp,
                                 maat, eenh, ean, ean_he, foodflag, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(tenant, artikelnr) DO UPDATE SET
                description=excluded.description, brand=excluded.brand,
                category=excluded.category, ivp=excluded.ivp, vp=excluded.vp,
                maat=excluded.maat, eenh=excluded.eenh,
                ean=CASE WHEN excluded.ean<>'' THEN excluded.ean ELSE product.ean END,
                last_seen=excluded.last_seen""",
            (tenant, p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[8], p[9], p[10],
             now, now))
    return len(rows)


def _teach(reading) -> dict | None:
    """Tell the shared catalogue about products it has never seen. Never fatal.

    A product resolved once is resolved for every future upload and every future client
    -- that is the whole reason the catalogue is shared. If it is unreachable the file is
    still stored; the products are simply resolved live the next time something asks.
    """
    try:
        return catalogue.learn([
            dict(artikelnr=p[0], description=p[1] or "", category=p[3] or "",
                 ean_ce=p[8] or "", ean_he=p[9] or "")
            for p in reading.products.values()])
    except Exception:
        return None


# --------------------------------------------------------------------------- receive
def stage(src_path: str, filename: str | None = None, year: int | None = None,
          tenant: str | None = None) -> dict:
    """Read, validate and park a file. -> the pre-flight report plus an upload id."""
    tenant = tenant or config.TENANT
    filename = filename or os.path.basename(src_path)
    reading = adapters.read(src_path, filename, year)
    rep = preflight.report(reading, tenant)

    uid = uuid.uuid4().hex[:12]
    stored = os.path.join(str(config.UPLOADS),
                          f"{uid}{os.path.splitext(filename)[1] or '.xlsx'}")
    try:
        shutil.copyfile(src_path, stored)
    except Exception:
        stored = ""                                   # audit copy is best-effort

    con = db.connect()
    con.execute(
        """INSERT INTO upload
               (id, tenant, filename, stored_path, adapter, year, uploaded_at, periods,
                lines, products, spend_eur, verdict, report_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uid, tenant, filename, stored, reading.adapter, reading.year, _now(),
         json.dumps(reading.periods), len(reading.lines), len(reading.products),
         rep["spend_eur"], rep["verdict"], json.dumps(rep)))
    con.executemany("INSERT INTO upload_line VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(uid, *l) for l in reading.lines])
    con.executemany("INSERT INTO upload_product VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(uid, *p) for p in reading.products.values()])
    con.commit()
    con.close()

    stored = _ingest(con2 := db.connect(), uid, tenant, reading, rep)
    con2.commit()
    con2.close()
    db.invalidate()
    learned = _teach(reading)

    rep.update(upload_id=uid, uploaded_at=_now(), stored_lines=stored,
               learned=learned, selected=False)
    return rep


def get(uid: str, tenant: str | None) -> dict | None:
    """A staged upload by id, restricted to one client. `tenant=None` means any.

    Required, like analysis.saved, and for the same reason: an upload id is a handle,
    not a permission. Without the filter one client could read another's file report --
    every product, every month, every euro of it.
    """
    con = db.connect()
    if tenant is None:
        r = con.execute("SELECT * FROM upload WHERE id=?", (uid,)).fetchone()
    else:
        r = con.execute("SELECT * FROM upload WHERE id=? AND tenant=?",
                        (uid, tenant)).fetchone()
    con.close()
    if not r:
        return None
    rep = json.loads(r["report_json"])
    rep.update(upload_id=r["id"], uploaded_at=r["uploaded_at"],
               selected=bool(r["selected"]), archived_at=r["archived_at"])
    return rep


def listing(limit: int = 50, tenant: str | None = None) -> list[dict]:
    tenant = tenant or config.TENANT
    con = db.connect()
    rows = con.execute(
        """SELECT id, filename, adapter, uploaded_at, periods, lines, products, spend_eur,
                  verdict, selected, archived_at
           FROM upload WHERE tenant=? ORDER BY uploaded_at DESC, id DESC LIMIT ?""",
        (tenant, limit)).fetchall()
    con.close()
    return [dict(upload_id=r["id"], filename=r["filename"], adapter=r["adapter"],
                 uploaded_at=r["uploaded_at"], periods=json.loads(r["periods"] or "[]"),
                 lines=r["lines"], products=r["products"], spend_eur=r["spend_eur"],
                 verdict=r["verdict"], selected=bool(r["selected"]),
                 archived_at=r["archived_at"], archived=bool(r["archived_at"]))
            for r in rows]


# Commit and uncommit used to live here.
#
# commit() moved a staged file into the merged history under one of three modes;
# uncommit() took it back out again. Both are gone: a file's lines are stored when it is
# read, and whether they count is selection.set_selected(). There is nothing to move, so
# there is nothing to move back.
#
# discard() is gone too. Getting rid of a file is selection.archive() and then
# selection.destroy() -- two steps, because deleting takes the purchase lines with it.



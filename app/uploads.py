"""
Staging: receive a file, judge it, park it, and only then commit.

Nothing enters the client's purchase history on arrival. A file is read by its adapter,
judged by preflight, and held. A human reads the verdict and decides.

Commit modes:
    new_only   import only months not already present.  The safe default.
    replace    delete the existing rows for every month in this file, then import.
    all        import everything as-is. Refused on overlap, because that is precisely how
               a cumulative export double-counts.

Committing is reversible in the sense that matters: purchase_line is derived from the
files, all of which are kept under data/uploads/. Nothing here is the only copy.
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
        "INSERT INTO upload VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
        (uid, tenant, filename, stored, reading.adapter, reading.year, _now(),
         json.dumps(reading.periods), len(reading.lines), len(reading.products),
         rep["spend_eur"], rep["verdict"], json.dumps(rep)))
    con.executemany("INSERT INTO upload_line VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(uid, *l) for l in reading.lines])
    con.executemany("INSERT INTO upload_product VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(uid, *p) for p in reading.products.values()])
    con.commit()
    con.close()

    rep.update(upload_id=uid, uploaded_at=_now(), committed=False)
    return rep


def get(uid: str) -> dict | None:
    con = db.connect()
    r = con.execute("SELECT * FROM upload WHERE id=?", (uid,)).fetchone()
    con.close()
    if not r:
        return None
    rep = json.loads(r["report_json"])
    rep.update(upload_id=r["id"], uploaded_at=r["uploaded_at"],
               committed=bool(r["committed_at"]), committed_at=r["committed_at"],
               commit_mode=r["commit_mode"], commit_note=r["commit_note"])
    return rep


def listing(limit: int = 50, tenant: str | None = None) -> list[dict]:
    tenant = tenant or config.TENANT
    con = db.connect()
    rows = con.execute(
        """SELECT id, filename, adapter, uploaded_at, periods, lines, products, spend_eur,
                  verdict, committed_at, commit_mode
           FROM upload WHERE tenant=? ORDER BY uploaded_at DESC, rowid DESC LIMIT ?""",
        (tenant, limit)).fetchall()
    con.close()
    return [dict(upload_id=r["id"], filename=r["filename"], adapter=r["adapter"],
                 uploaded_at=r["uploaded_at"], periods=json.loads(r["periods"] or "[]"),
                 lines=r["lines"], products=r["products"], spend_eur=r["spend_eur"],
                 verdict=r["verdict"], committed=bool(r["committed_at"]),
                 committed_at=r["committed_at"], commit_mode=r["commit_mode"])
            for r in rows]


def discard(uid: str) -> bool:
    con = db.connect()
    r = con.execute("SELECT committed_at FROM upload WHERE id=?", (uid,)).fetchone()
    if r is None:
        con.close()
        return False
    if r["committed_at"]:
        con.close()
        raise CommitError("this upload has already been committed and cannot be discarded")
    con.execute("DELETE FROM upload_line WHERE upload_id=?", (uid,))
    con.execute("DELETE FROM upload_product WHERE upload_id=?", (uid,))
    con.execute("DELETE FROM upload WHERE id=?", (uid,))
    con.commit()
    con.close()
    return True


# --------------------------------------------------------------------------- commit
def commit(uid: str, mode: str = "new_only", override: bool = False,
           tenant: str | None = None) -> dict:
    """Move a staged upload into the client's purchase history."""
    tenant = tenant or config.TENANT
    if mode not in ("new_only", "replace", "all"):
        raise CommitError(f"unknown mode {mode!r} — use new_only, replace or all")

    con = db.connect()
    row = con.execute("SELECT * FROM upload WHERE id=?", (uid,)).fetchone()
    if not row:
        con.close()
        raise CommitError(f"no upload {uid}")
    if row["committed_at"]:
        con.close()
        raise CommitError(f"upload {uid} was already committed at {row['committed_at']}")
    if row["verdict"] == "blocked" and not override:
        con.close()
        raise CommitError(
            "the pre-flight verdict is 'blocked'. Read the findings and fix the file, or "
            "commit again with override if you accept the consequences knowingly.")

    lines = con.execute(
        """SELECT year,month,klantnr,restaurant,city,artikelnr,aantal,omzet,kg,kg_known,quality
           FROM upload_line WHERE upload_id=?""", (uid,)).fetchall()
    prods = con.execute(
        """SELECT artikelnr,description,brand,category,ivp,vp,maat,eenh,ean,ean_he,foodflag
           FROM upload_product WHERE upload_id=?""", (uid,)).fetchall()

    file_periods = sorted({(l["year"], l["month"]) for l in lines})
    existing = {(r["year"], r["month"]) for r in con.execute(
        "SELECT DISTINCT year, month FROM purchase_line WHERE tenant=?", (tenant,))}
    overlap = [p for p in file_periods if p in existing]

    if mode == "all" and overlap and not override:
        con.close()
        raise CommitError(
            f"{len(overlap)} month(s) in this file are already loaded. Mode 'all' would "
            "count them twice. Use 'new_only' to import just the new months, or 'replace' "
            "to overwrite the existing ones.")

    take, replaced = file_periods, []
    if mode == "new_only":
        take = [p for p in file_periods if p not in existing]
        if not take:
            con.close()
            raise CommitError(
                "every month in this file is already loaded, so 'new_only' has nothing to "
                "import. Use 'replace' if you mean to overwrite them.")
    elif mode == "replace":
        replaced = overlap
        for y, m in overlap:
            con.execute("DELETE FROM purchase_line WHERE tenant=? AND year=? AND month=?",
                        (tenant, y, m))

    # An adapter cannot tell whether a month is complete — only the pre-flight can, by
    # comparing it against this client's history. So the verdict is recorded HERE, against
    # the months it applies to, and every screen reads it from the data rather than
    # re-deriving it. A month judged partial is kept and labelled, never dropped.
    partial = set()
    try:
        rep = json.loads(row["report_json"])
        for fnd in rep.get("findings", []):
            if fnd.get("code") in ("partial_months", "thin_months"):
                partial |= set(fnd.get("months") or [])
    except Exception:
        pass

    keep = set(take)
    rows = [(tenant, l["year"], l["month"], l["klantnr"], l["restaurant"], l["city"],
             l["artikelnr"], l["aantal"], l["omzet"], l["kg"], l["kg_known"],
             ("PARTIAL" if f"{l['year']}-{l['month']:02d}" in partial else "complete"), uid)
            for l in lines if (l["year"], l["month"]) in keep]
    con.executemany(
        "INSERT INTO purchase_line VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    now = _now()
    # products are identity, not measurement: refresh what a supplier may have improved
    # (descriptions get corrected), but never invent a first_seen that is later than reality
    for p in prods:
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
            (tenant, p["artikelnr"], p["description"], p["brand"], p["category"], p["ivp"],
             p["vp"], p["maat"], p["eenh"], p["ean"], p["ean_he"], p["foodflag"], now, now))

    # Tell the shared catalogue about anything it has not seen. A product resolved once is
    # resolved for every future upload and every future client — that is the whole reason
    # the catalogue is shared. Non-fatal: the import has already succeeded.
    learned = None
    try:
        learned = catalogue.learn([
            dict(artikelnr=p["artikelnr"], description=p["description"] or "",
                 category=p["category"] or "", ean_ce=p["ean"] or "",
                 ean_he=p["ean_he"] or "")
            for p in prods])
    except Exception:
        learned = None

    note = (f"imported {len(rows):,} lines for {len(take)} month(s)"
            + (f"; replaced {len(replaced)} existing month(s)" if replaced else "")
            + (f"; skipped {len(file_periods) - len(take)} already-loaded month(s)"
               if len(take) < len(file_periods) else ""))
    con.execute("UPDATE upload SET committed_at=?, commit_mode=?, commit_note=? WHERE id=?",
                (now, mode, note, uid))
    con.commit()
    con.close()

    return dict(upload_id=uid, mode=mode, note=note, imported_lines=len(rows),
                catalogue_learned=(learned or {}).get("learned"),
                catalogue_reachable=learned is not None,
                imported_months=[f"{y}-{m:02d}" for y, m in take],
                replaced_months=[f"{y}-{m:02d}" for y, m in replaced],
                skipped_months=[f"{y}-{m:02d}" for y, m in file_periods if (y, m) not in keep])

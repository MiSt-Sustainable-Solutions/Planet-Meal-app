"""
Which files count, and which file owns a month.

The old model poured every file into one merged history and then forgot it: three import
modes existed to stop the same month landing twice, and getting the mode wrong doubled a
year. The new model keeps every file as a thing you can see, and a file either counts or
it does not. Double counting stops being a risk the app guards against and becomes
something the data cannot express -- a month is supplied by one file, because one file
owns it.

OWNERSHIP, AND WHY IT HAS TO EXIST.

Selection alone is not enough, and TU Delft's own files show why. "Augustus 2024" covers
January to August. "December 2024" covers January to December. Select both and eight
months are counted twice. Select either alone and months are lost -- Augustus has no
autumn, December has a twentieth of Augustus's volume for the months they share.

Neither file is wrong. They overlap, and no rule can decide which should win, because the
answer depends on what the person knows about where the files came from. So a contested
month gets an OWNER, written into a table, defaulted to the file with the most lines for
that month, and changeable by a person who knows better.

An uncontested month has no row at all. That is the ordinary case, it needs no decision,
and it costs nothing.
"""
from __future__ import annotations

import datetime as dt
import json

import db
import store


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _periods(con, upload_id: str, tenant: str) -> list[tuple[int, int]]:
    """The (year, month) pairs a file actually supplies lines for.

    Read from the lines rather than from the file's declared periods: what it says it
    covers and what it delivered can differ, and only one of them affects a number.
    """
    return [(r[0], r[1]) for r in con.execute(
        "SELECT DISTINCT year, month FROM purchase_line "
        "WHERE tenant=? AND source_upload=? ORDER BY year, month", (tenant, upload_id))]


def claims(tenant: str) -> dict[tuple[int, int], list[str]]:
    """-> {(year, month): [upload_id, ...]} for every SELECTED, unarchived file."""
    con = db.connect()
    rows = con.execute("""
        SELECT DISTINCT l.year, l.month, l.source_upload
        FROM purchase_line l
        JOIN upload u ON u.id = l.source_upload AND u.tenant = l.tenant
        WHERE l.tenant=? AND u.selected=1 AND u.archived_at IS NULL
        ORDER BY l.year, l.month""", (tenant,)).fetchall()
    con.close()
    out: dict[tuple[int, int], list[str]] = {}
    for r in rows:
        out.setdefault((r[0], r[1]), []).append(r[2])
    return out


def owners(tenant: str) -> dict[tuple[int, int], str]:
    con = db.connect()
    rows = con.execute("SELECT year, month, upload_id FROM month_owner WHERE tenant=?",
                       (tenant,)).fetchall()
    con.close()
    return {(r[0], r[1]): r[2] for r in rows}


def contested(tenant: str) -> list[dict]:
    """Months more than one selected file supplies. The warning the dashboard must carry.

    Reported even once an owner has been chosen, because the overlap is still a fact
    about the selection and a person may want to revisit it -- `decided` says whether it
    has been settled.
    """
    have, own = claims(tenant), owners(tenant)
    con = db.connect()
    names = {r[0]: r[1] for r in con.execute(
        "SELECT id, filename FROM upload WHERE tenant=?", (tenant,))}
    counts = {(r[0], r[1], r[2]): r[3] for r in con.execute(
        "SELECT year, month, source_upload, COUNT(*) FROM purchase_line "
        "WHERE tenant=? GROUP BY year, month, source_upload", (tenant,))}
    con.close()

    out = []
    for (y, m), ids in sorted(have.items()):
        if len(ids) < 2:
            continue
        out.append(dict(
            year=y, month=m, period=f"{y}-{m:02d}",
            owner=own.get((y, m)), decided=(y, m) in own,
            files=[dict(upload_id=i, filename=names.get(i, i),
                        lines=counts.get((y, m, i), 0),
                        owns=(own.get((y, m)) == i)) for i in ids]))
    return out


def _settle(con, tenant: str, by: str = "") -> int:
    """Give every contested month an owner, and drop owners that are no longer contested.

    The default is the file with the most lines for that month -- a starting point, not a
    judgement, and written down explicitly so it can be seen and changed rather than
    silently applied at read time. A month that stops being contested loses its row, so
    an owner never outlives the reason for it.
    """
    rows = con.execute("""
        SELECT l.year, l.month, l.source_upload, COUNT(*) AS n
        FROM purchase_line l
        JOIN upload u ON u.id = l.source_upload AND u.tenant = l.tenant
        WHERE l.tenant=? AND u.selected=1 AND u.archived_at IS NULL
        GROUP BY l.year, l.month, l.source_upload""", (tenant,)).fetchall()
    by_month: dict[tuple[int, int], list] = {}
    for r in rows:
        by_month.setdefault((r[0], r[1]), []).append((r[3], r[2]))

    existing = {(r[0], r[1]): r[2] for r in con.execute(
        "SELECT year, month, upload_id FROM month_owner WHERE tenant=?", (tenant,))}

    written = 0
    for (y, m), cands in by_month.items():
        if len(cands) < 2:
            if (y, m) in existing:
                con.execute("DELETE FROM month_owner WHERE tenant=? AND year=? AND month=?",
                            (tenant, y, m))
            continue
        ids = {c[1] for c in cands}
        if existing.get((y, m)) in ids:
            continue                                   # a standing decision still applies
        winner = sorted(cands, reverse=True)[0][1]     # most lines for that month
        store.upsert(con, "month_owner",
                     ["tenant", "year", "month", "upload_id", "decided_at", "decided_by"],
                     [(tenant, y, m, winner, _now(), by or "automatic")],
                     conflict=["tenant", "year", "month"])
        written += 1

    # An owner for a month nobody supplies any more is just clutter.
    live = set(by_month)
    for key in existing:
        if key not in live:
            con.execute("DELETE FROM month_owner WHERE tenant=? AND year=? AND month=?",
                        (tenant, key[0], key[1]))
    return written


def set_selected(upload_id: str, tenant: str, on: bool, by: str = "") -> dict:
    """Count this file, or stop counting it. Scoped to one client, like every write here."""
    con = db.connect()
    row = con.execute("SELECT id FROM upload WHERE id=? AND tenant=?",
                      (upload_id, tenant)).fetchone()
    if not row:
        con.close()
        raise ValueError(f"no file {upload_id}")
    con.execute("UPDATE upload SET selected=? WHERE id=? AND tenant=?",
                (1 if on else 0, upload_id, tenant))
    settled = _settle(con, tenant, by)
    con.commit()
    con.close()
    db.invalidate()
    return dict(upload_id=upload_id, selected=bool(on), months_settled=settled)


def set_owner(tenant: str, year: int, month: int, upload_id: str, by: str = "") -> None:
    """A person deciding which file supplies a contested month."""
    con = db.connect()
    ok = con.execute("""SELECT 1 FROM purchase_line l
                        JOIN upload u ON u.id=l.source_upload AND u.tenant=l.tenant
                        WHERE l.tenant=? AND l.year=? AND l.month=? AND l.source_upload=?
                          AND u.selected=1 AND u.archived_at IS NULL LIMIT 1""",
                     (tenant, year, month, upload_id)).fetchone()
    if not ok:
        con.close()
        raise ValueError("that file does not supply that month, or is not selected")
    store.upsert(con, "month_owner",
                 ["tenant", "year", "month", "upload_id", "decided_at", "decided_by"],
                 [(tenant, year, month, upload_id, _now(), by or "a person")],
                 conflict=["tenant", "year", "month"])
    con.commit()
    con.close()
    db.invalidate()


# --------------------------------------------------------------------------- lifecycle
def archive(upload_id: str, tenant: str, by: str = "") -> None:
    """Step one of two. An archived file counts for nothing but is still there to read."""
    con = db.connect()
    n = con.execute("UPDATE upload SET archived_at=?, selected=0 WHERE id=? AND tenant=?",
                    (_now(), upload_id, tenant)).rowcount
    if n:
        _settle(con, tenant, by)
    con.commit()
    con.close()
    db.invalidate()
    if not n:
        raise ValueError(f"no file {upload_id}")


def restore(upload_id: str, tenant: str) -> None:
    con = db.connect()
    con.execute("UPDATE upload SET archived_at=NULL WHERE id=? AND tenant=?",
                (upload_id, tenant))
    con.commit()
    con.close()
    db.invalidate()


def destroy(upload_id: str, tenant: str) -> dict:
    """Step two, and there is no step three. Only from the archive, deliberately.

    Two stages because this cannot be undone: it takes the purchase lines with it, and a
    file that is merely unselected or archived can always be brought back.
    """
    con = db.connect()
    row = con.execute("SELECT archived_at, filename FROM upload WHERE id=? AND tenant=?",
                      (upload_id, tenant)).fetchone()
    if not row:
        con.close()
        raise ValueError(f"no file {upload_id}")
    if not row["archived_at"]:
        con.close()
        raise ValueError("archive it first — deleting takes its purchase lines with it")
    n = con.execute("DELETE FROM purchase_line WHERE tenant=? AND source_upload=?",
                    (tenant, upload_id)).rowcount
    con.execute("DELETE FROM upload_line WHERE upload_id=?", (upload_id,))
    con.execute("DELETE FROM upload_product WHERE upload_id=?", (upload_id,))
    con.execute("DELETE FROM month_owner WHERE tenant=? AND upload_id=?", (tenant, upload_id))
    con.execute("DELETE FROM upload WHERE id=? AND tenant=?", (upload_id, tenant))
    _settle(con, tenant)
    con.commit()
    con.close()
    db.invalidate()
    return dict(upload_id=upload_id, filename=row["filename"], lines_removed=n)


# --------------------------------------------------------------------------- migration
LEGACY_NOTE = ("Adopted from data that predates file tracking. Re-upload the original "
               "exports and archive this to replace it.")


def adopt_orphans() -> int:
    """Give a file to purchase lines that have none. Idempotent; safe to run at startup.

    Data loaded before any of this existed -- TU Delft's 66,145 lines came from the old
    pipeline, not through the app -- belongs to no file. Under a model where only a
    selected file counts, that data would silently become invisible. Adopting it keeps
    every number exactly where it was, and puts a row on the Files page saying what it is,
    so replacing it is a decision rather than an accident.
    """
    con = db.connect()
    orphans = con.execute("""
        SELECT l.tenant, l.source_upload, COUNT(*) AS n,
               MIN(l.year*100+l.month) AS lo, MAX(l.year*100+l.month) AS hi,
               SUM(l.omzet) AS spend
        FROM purchase_line l
        LEFT JOIN upload u ON u.id = l.source_upload AND u.tenant = l.tenant
        WHERE u.id IS NULL
        GROUP BY l.tenant, l.source_upload""").fetchall()
    made = 0
    for r in orphans:
        tenant, src = r["tenant"], r["source_upload"]
        periods = [f"{p // 100}-{p % 100:02d}" for p in
                   sorted({y * 100 + m for y, m in _periods(con, src, tenant)})]
        name = f"{src} (adopted)"
        report = json.dumps(dict(verdict="go", findings=[], summary={}, filename=name,
                                 adapter="legacy", lines=r["n"], products=0,
                                 spend_eur=r["spend"] or 0.0, periods=periods,
                                 note=LEGACY_NOTE))
        # Columns named, not positional: `selected` and `archived_at` arrive by migration
        # and therefore sit at the end, so a positional INSERT here would depend on the
        # order migrations happened to run in.
        store.upsert(
            con, "upload",
            ["id", "tenant", "filename", "stored_path", "adapter", "year", "uploaded_at",
             "periods", "lines", "products", "spend_eur", "verdict", "report_json",
             "committed_at", "commit_mode", "commit_note", "selected", "archived_at"],
            [(src, tenant, name, "", "legacy",
              int(periods[0][:4]) if periods else None, _now(), json.dumps(periods),
              r["n"], 0, r["spend"] or 0.0, "go", report,
              _now(), "adopted", LEGACY_NOTE, 1, None)],
            conflict=["id"], update=False)
        made += 1
    if made:
        con.commit()
    con.close()
    if made:
        db.invalidate()
    return made

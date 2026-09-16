"""
Product adjustments: how much of a product counts, for one client, over a stated period.

"Only 10% of the frying oil TU Delft buys is counted" used to be a rule inside the shared
catalogue: any product with FRITUUR in its name, for every client, for all time. That was
wrong three ways. It was one client's arrangement applied to everyone. It matched on a
name, so the same frying fat sold as "KERN FR.VET VLB NEUTRAAL 10L" was counted in full.
And it had no dates, so an arrangement that started in 2024 was applied to 2023 as well.

So an adjustment is now

    one client  x  one article number  x  a range of months  ->  the share that counts

stored here, in the client's app, and sent to the catalogue as each purchase line's
`share`. The catalogue applies the share and has no opinion about it.

Several articles are usually one adjustment in the client's eyes -- five frying oils are
"frying oil" -- so each carries a `label`, and the note on the dashboard is written per
label: "Only 10% of purchased frying oil is counted." The reason is kept for MiSt and is
not shown to the client: why a share is what it is varies by client and product, and a
reason stated on screen is a claim somebody will quote.

An adjustment is never edited or deleted. Removing one stamps it removed and keeps the
row, so what was applied to a published figure can always be read back.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import uuid

import db

PERIOD = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


class AdjustmentError(ValueError):
    pass


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _key(period: str | None) -> int | None:
    return int(period[:4]) * 100 + int(period[5:7]) if period else None


def _pct(share: float) -> str:
    """0.1 -> "10", 0.125 -> "12.5". People write shares as percentages."""
    return f"{share * 100:.1f}".rstrip("0").rstrip(".")


def active(tenant: str) -> list[dict]:
    """This client's adjustments in force, with each article's name, newest first."""
    con = db.connect()
    rows = con.execute("""
        SELECT a.*, p.description, p.category
        FROM adjustment a
        LEFT JOIN product p ON p.tenant = a.tenant AND p.artikelnr = a.artikelnr
        WHERE a.tenant=? AND a.removed_at IS NULL
        ORDER BY a.created_at DESC, a.label, a.artikelnr""", (tenant,)).fetchall()
    con.close()
    return [dict(r, pct=_pct(r["share"])) for r in rows]


def removed(tenant: str, limit: int = 50) -> list[dict]:
    con = db.connect()
    rows = con.execute("""
        SELECT a.*, p.description FROM adjustment a
        LEFT JOIN product p ON p.tenant = a.tenant AND p.artikelnr = a.artikelnr
        WHERE a.tenant=? AND a.removed_at IS NOT NULL
        ORDER BY a.removed_at DESC LIMIT ?""", (tenant, limit)).fetchall()
    con.close()
    return [dict(r, pct=_pct(r["share"])) for r in rows]


def add(tenant: str, artikelnrs: list[str], share_pct: float, label: str, reason: str,
        from_period: str, to_period: str | None, by: str) -> list[str]:
    """Adjust these articles for this client. -> the new ids. All or nothing."""
    arts = sorted({a.strip() for a in artikelnrs if a and a.strip()})
    if not arts:
        raise AdjustmentError("choose at least one product")
    try:
        pct = float(str(share_pct).replace(",", ".").rstrip("%").strip())
    except ValueError:
        raise AdjustmentError("the share counted must be a number from 0 to 100")
    if not 0 <= pct < 100:
        raise AdjustmentError("the share counted must be from 0 up to (not including) 100%"
                              " -- at 100% nothing is being adjusted")
    from_period = (from_period or "").strip()
    to_period = (to_period or "").strip() or None
    for p in (from_period, to_period):
        if p is not None and not PERIOD.match(p):
            raise AdjustmentError(f"{p or 'the start month'} is not a month written as YYYY-MM")
    if to_period and _key(to_period) < _key(from_period):
        raise AdjustmentError("the last month is before the first month")
    label = " ".join((label or "").split())
    if len(arts) > 1 and not label:
        # Without one, each product would be named on its own line on the client's dashboard:
        # nine frying oils became nine "Adjusted:" lines (16 Sep 2026).
        raise AdjustmentError(f"give the {len(arts)} ticked products one name the client "
                              "will read, for example 'frying oil'")
    reason = " ".join((reason or "").split())
    if not reason:
        raise AdjustmentError("say why, for the record -- it is not shown to the client")

    con = db.connect()
    names = {r["artikelnr"]: r["description"] for r in con.execute(
        f"SELECT artikelnr, description FROM product WHERE tenant=? AND artikelnr IN "
        f"({','.join('?' for _ in arts)})", (tenant, *arts))}
    unknown = [a for a in arts if a not in names]
    if unknown:
        con.close()
        raise AdjustmentError(f"this client has never bought article {', '.join(unknown)}")

    # One share per article per month. Two adjustments on the same month would leave the
    # figure depending on which one a query happened to read first.
    lo, hi = _key(from_period), _key(to_period) or 999912
    for r in con.execute(
            f"SELECT artikelnr, from_period, to_period, share, label FROM adjustment "
            f"WHERE tenant=? AND removed_at IS NULL AND artikelnr IN "
            f"({','.join('?' for _ in arts)})", (tenant, *arts)):
        if _key(r["from_period"]) <= hi and lo <= (_key(r["to_period"]) or 999912):
            con.close()
            raise AdjustmentError(
                f"article {r['artikelnr']} ({names[r['artikelnr']]}) already has "
                f"{_pct(r['share'])}% counted from {r['from_period']}"
                f"{' to ' + r['to_period'] if r['to_period'] else ' onwards'}. Remove that "
                "one first, or choose months that do not overlap.")

    ids, now = [], _now()
    for a in arts:
        aid = uuid.uuid4().hex[:12]
        con.execute("INSERT INTO adjustment (id, tenant, artikelnr, share, label, reason, "
                    "from_period, to_period, created_at, created_by) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (aid, tenant, a, round(pct / 100, 6), label or names[a], reason,
                     from_period, to_period, now, by))
        ids.append(aid)
    con.commit()
    con.close()
    db.invalidate()
    return ids


def remove(tenant: str, adjustment_id: str, by: str, why: str = "") -> dict:
    con = db.connect()
    row = con.execute("SELECT * FROM adjustment WHERE id=? AND tenant=? AND removed_at IS NULL",
                      (adjustment_id, tenant)).fetchone()
    if not row:
        con.close()
        raise AdjustmentError("no such adjustment for this client, or it was already removed")
    con.execute("UPDATE adjustment SET removed_at=?, removed_by=?, removed_why=? "
                "WHERE id=? AND tenant=?",
                (_now(), by, " ".join((why or "").split()), adjustment_id, tenant))
    con.commit()
    con.close()
    db.invalidate()
    return dict(row)


# --------------------------------------------------------------------------- applying
def _index(tenant: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    con = db.connect()
    for r in con.execute("SELECT id, artikelnr, share, label, from_period, to_period "
                         "FROM adjustment WHERE tenant=? AND removed_at IS NULL", (tenant,)):
        out.setdefault(r["artikelnr"], []).append(dict(
            r, lo=_key(r["from_period"]), hi=_key(r["to_period"]) or 999912))
    con.close()
    return out


def _find(index: dict, line: dict) -> dict | None:
    ym = int(line.get("year") or 0) * 100 + int(line.get("month") or 0)
    for a in index.get(str(line.get("artikelnr")), ()):
        if a["lo"] <= ym <= a["hi"]:
            return a
    return None


def apply(lines: list[dict], tenant: str) -> list[dict]:
    """Give every line its share, in place. -> the same list.

    Every line gets one, 1.0 where nothing applies, so that what is sent says in full what
    was counted rather than leaving the default to be assumed.
    """
    index = _index(tenant)
    for line in lines:
        a = _find(index, line) if index else None
        line["share"] = a["share"] if a else 1.0
        line["adjustment"] = a["label"] if a else ""
    return lines


def label_rows(rows: list[dict], tenant: str) -> list[dict]:
    """Name the adjustment on each scored line, for the Excel lines sheet."""
    index = _index(tenant)
    for r in rows:
        a = _find(index, r) if index else None
        r["adjustment"] = a["label"] if a else ""
    return rows


def notes(lines: list[dict], tenant: str) -> list[dict]:
    """What the dashboard says about the adjustments these lines were counted with.

    One note per label and share, and only for adjustments that touched a line here -- an
    adjustment for months outside this window changes nothing in it and says nothing.
    """
    seen: dict[tuple, None] = {}
    for line in lines:
        if line.get("adjustment") and line.get("share", 1.0) < 1.0:
            seen[(line["adjustment"], line["share"])] = None
    out = []
    for label, share in sorted(seen):
        out.append(dict(
            code="adjustment", severity="info", owner="MiSt",
            message=(f"Purchased {label} is not counted." if share == 0 else
                     f"Only {_pct(share)}% of purchased {label} is counted.")))
    return out


def digest(tenant: str) -> str:
    """Changes whenever this client's adjustments do. Part of every cache key."""
    con = db.connect()
    rows = con.execute("SELECT id, artikelnr, share, from_period, to_period FROM adjustment "
                       "WHERE tenant=? AND removed_at IS NULL ORDER BY id", (tenant,)).fetchall()
    con.close()
    if not rows:
        return NONE
    return hashlib.sha1("|".join(
        f"{r['id']}:{r['artikelnr']}:{r['share']}:{r['from_period']}:{r['to_period']}"
        for r in rows).encode()).hexdigest()[:12]


# What digest() says when nothing is adjusted -- and what a publication made before
# adjustments existed is taken to have had, since at the time nothing could be.
NONE = "none"


def products(tenant: str, q: str, limit: int = 60) -> list[dict]:
    """Products this client has bought matching a name, article, barcode or category.

    Heaviest first, with the months it was bought in and any adjustment it already has,
    so the page can show what an adjustment would touch before it is made.
    """
    q = " ".join((q or "").split())
    if not q:
        return []
    like = f"%{q.lower()}%"
    con = db.connect()
    rows = con.execute("""
        SELECT p.artikelnr, p.description, p.category, p.ean,
               COUNT(l.artikelnr) AS lines, COALESCE(SUM(l.kg), 0) AS kg,
               MIN(l.year*100+l.month) AS first, MAX(l.year*100+l.month) AS last
        FROM product p
        LEFT JOIN purchase_line l ON l.tenant = p.tenant AND l.artikelnr = p.artikelnr
        WHERE p.tenant=? AND (LOWER(p.description) LIKE ? OR LOWER(p.category) LIKE ?
                              OR p.artikelnr = ? OR p.ean = ?)
        GROUP BY p.artikelnr, p.description, p.category, p.ean
        ORDER BY kg DESC, p.description LIMIT ?""",
        (tenant, like, like, q, q, limit)).fetchall()
    con.close()
    index = _index(tenant)
    return [dict(r, kg=round(r["kg"] or 0),
                 first=f"{r['first'] // 100}-{r['first'] % 100:02d}" if r["first"] else None,
                 last=f"{r['last'] // 100}-{r['last'] % 100:02d}" if r["last"] else None,
                 adjusted=[dict(a, pct=_pct(a["share"])) for a in index.get(r["artikelnr"], [])])
            for r in rows]

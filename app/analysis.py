"""
Running an analysis: the app's lines, the catalogue's maths, the result saved here.

The app never computes a footprint. It gathers this client's purchase lines for a window,
posts them to the shared catalogue, and stores what comes back. Every number on every
screen came from that one response, so nothing on a page can disagree with anything else
on it.

Results are saved. A client can look at what was reported in March without re-running
anything, and the saved row records which EAT-Lancet profile produced the score — so if
the profile changes later, old numbers still explain themselves.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid

import catalogue
import config
import db

FY = re.compile(r"^FY(\d{4})$", re.I)


class WindowError(ValueError):
    pass


def parse_window(window: str | None = None, frm: str | None = None, to: str | None = None,
                 tenant: str | None = None):
    """-> (label, y0, m0, y1, m1). 'FY2025', 'all', or from/to as YYYY-MM."""
    months = db.months(tenant)
    if not months:
        raise WindowError("no purchase data yet — upload a file first")
    first, last = months[0], months[-1]

    if frm or to:
        def split(s, default):
            if not s:
                return default
            m = re.match(r"^(\d{4})-(\d{1,2})$", s.strip())
            if not m:
                raise WindowError(f"expected YYYY-MM, got {s!r}")
            y, mo = int(m.group(1)), int(m.group(2))
            if not 1 <= mo <= 12:
                raise WindowError(f"month out of range in {s!r}")
            return y, mo
        y0, m0 = split(frm, (first["year"], first["month"]))
        y1, m1 = split(to, (last["year"], last["month"]))
        label = f"{y0}-{m0:02d} to {y1}-{m1:02d}"
    elif window and window.lower() == "all":
        y0, m0, y1, m1 = first["year"], first["month"], last["year"], last["month"]
        label = "All data"
    else:
        w = window or default_window(tenant)
        m = FY.match(w)
        if not m:
            raise WindowError(f"unknown window {w!r} — use FY####, 'all', or from/to")
        y = int(m.group(1))
        y0, m0, y1, m1, label = y, 1, y, 12, f"FY{y}"

    if (y0 * 100 + m0) > (y1 * 100 + m1):
        raise WindowError("the window starts after it ends")
    return label, y0, m0, y1, m1


def default_window(tenant: str | None = None) -> str:
    """The most recent complete calendar year we hold, else the latest year."""
    months = db.months(tenant)
    if not months:
        return "FY2025"
    by_year = {}
    for m in months:
        by_year.setdefault(m["year"], []).append(m)
    for y in sorted(by_year, reverse=True):
        if len(by_year[y]) == 12 and all(x["complete"] for x in by_year[y]):
            return f"FY{y}"
    return f"FY{max(by_year)}"


def windows(tenant: str | None = None) -> list[dict]:
    """The windows worth offering in the UI, newest first."""
    months = db.months(tenant)
    by_year = {}
    for m in months:
        by_year.setdefault(m["year"], []).append(m)
    out = []
    for y in sorted(by_year, reverse=True):
        ms = by_year[y]
        partial = [x["period"] for x in ms if not x["complete"]]
        out.append(dict(key=f"FY{y}", label=f"FY{y}", months=len(ms),
                        complete=len(ms) == 12 and not partial, partial=partial))
    if out:
        out.append(dict(key="all", label="All data", months=len(months),
                        complete=False, partial=[]))
    return out


# --------------------------------------------------------------------------- run
def run(window: str | None = None, frm: str | None = None, to: str | None = None,
        profile: str | None = None, top: int = 20, save: bool = True,
        tenant: str | None = None) -> dict:
    """Score a window and (by default) save the result."""
    tenant = tenant or config.TENANT
    label, y0, m0, y1, m1 = parse_window(window, frm, to, tenant)
    lines = db.lines_for(y0, m0, y1, m1, tenant)
    if not lines:
        raise WindowError(f"no purchase lines in {label}")

    result = catalogue.score(lines, label=label, profile=profile, top=top)

    # the app knows things the catalogue cannot: which months arrived partial, and which
    # products weigh nothing. Attach them so the page has one object to render.
    own = {m["period"]: m for m in db.months(tenant)}
    for row in result.get("by_month", []):
        m = own.get(row["period"])
        if m:
            row["quality"] = m["quality"]
            row["complete"] = m["complete"]
    partial = [p for p, m in own.items()
               if not m["complete"] and y0 * 100 + m0 <= m["year"] * 100 + m["month"] <= y1 * 100 + m1]
    if partial:
        result["headline"]["caveats"].insert(0, dict(
            code="partial_export", severity="error", owner="Sligro",
            message=("This window includes months that arrived in a partial export "
                     f"({', '.join(sorted(partial))}). Their volumes are a fraction of a "
                     "normal month and the totals here understate reality.")))
    result["piece_items"] = db.piece_items(y0, m0, y1, m1, tenant=tenant)
    result["window"] = dict(key=window or default_window(tenant), label=label,
                            period_from=f"{y0}-{m0:02d}", period_to=f"{y1}-{m1:02d}")

    if save:
        result["run_id"] = _save(result, label, y0, m0, y1, m1, len(lines), tenant)
    return result


def _save(result: dict, label, y0, m0, y1, m1, nlines: int, tenant: str) -> str:
    h = result["headline"]
    rid = uuid.uuid4().hex[:12]
    con = db.connect()
    con.execute("INSERT INTO analysis_run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, tenant, label, f"{y0}-{m0:02d}", f"{y1}-{m1:02d}",
                 h.get("eat_profile"), dt.datetime.now().isoformat(timespec="seconds"),
                 nlines, h["food_kg"], h["co2_kg"], h["intensity_kg_co2_per_kg"],
                 h["eat_lancet_score"],
                 h["confidence"]["product_specific_pct_of_weight"], json.dumps(result)))
    con.commit()
    con.close()
    return rid


def history(limit: int = 25, tenant: str | None = None) -> list[dict]:
    tenant = tenant or config.TENANT
    con = db.connect()
    rows = con.execute(
        """SELECT id, label, period_from, period_to, eat_profile, ran_at, lines,
                  food_kg, co2_kg, intensity, eat_score, specific_pct
           FROM analysis_run WHERE tenant=? ORDER BY ran_at DESC, rowid DESC LIMIT ?""",
        (tenant, limit)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def saved(run_id: str) -> dict | None:
    con = db.connect()
    r = con.execute("SELECT result_json FROM analysis_run WHERE id=?", (run_id,)).fetchone()
    con.close()
    return json.loads(r["result_json"]) if r else None

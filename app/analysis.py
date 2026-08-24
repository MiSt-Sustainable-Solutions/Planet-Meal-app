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


# --------------------------------------------------------------------------- caching
#
# An analysis is expensive: ~29,000 lines posted to the catalogue and scored, about thirty
# seconds. It is also completely determined by three things:
#
#     which client and window   ->  which purchase lines
#     the client's own data     ->  db.window_fingerprint()
#     the catalogue's rules     ->  catalogue.version()["etag"]
#
# If none of the three has moved, last time's answer IS this time's answer. So it is saved
# against all three and served straight back. The first view of a period pays the thirty
# seconds; every view after that is one indexed SELECT.
#
# This used to recalculate on every page load, which left thirty-six identical runs of
# FY2025 in the history table, each taking half a minute to produce a number that had not
# changed.

CACHE_COLUMNS = ("id, tenant, label, period_from, period_to, eat_profile, ran_at, lines, "
                 "food_kg, co2_kg, intensity, eat_score, specific_pct, result_json, "
                 "window_key, catalogue_version, data_fingerprint")


def cache_key(y0: int, m0: int, y1: int, m1: int, profile: str | None) -> str:
    """The window and the profile that was ASKED FOR, as one string.

    Not the profile that came back. A caller who asks for the default gets
    'tudelft_reconstruction_v1' in the result, and matching a later request for the
    default against that stored name fails every time -- which is exactly the bug that
    made this cache never hit once. Key on the request, not on the answer.

    If the default itself changes, that is a change to the eat_profile table, which moves
    the catalogue version, which invalidates these rows anyway.
    """
    return f"{y0}-{m0:02d}:{y1}-{m1:02d}|{profile or 'default'}"


def _lookup(tenant: str, wkey: str, fingerprint: str,
            etag: str | None = None) -> dict | None:
    """The saved result for this client, window and data. `etag` pins the version.

    With an etag: the exact answer, still valid, serve it and say nothing.
    Without one: the newest saved answer whatever version made it — used both when the
    catalogue is unreachable and when it has moved on. The caller decides what to say
    about it; this only ever reports which version actually produced it.
    """
    sql = ["SELECT id, ran_at, catalogue_version, result_json FROM analysis_run",
           "WHERE tenant=? AND window_key=? AND data_fingerprint=?"]
    args = [tenant, wkey, fingerprint]
    if etag:
        sql.append("AND catalogue_version=?")
        args.append(etag)
    sql.append("ORDER BY ran_at DESC, rowid DESC LIMIT 1")
    con = db.connect()
    row = con.execute(" ".join(sql), args).fetchone()
    con.close()
    if not row or not row["result_json"]:
        return None
    try:
        out = json.loads(row["result_json"])
    except (ValueError, TypeError):
        return None
    out["run_id"] = row["id"]
    out["cached"] = True
    out["ran_at"] = row["ran_at"]
    out["scored_against"] = row["catalogue_version"]
    if not out.get("catalogue_version"):
        out["catalogue_version"] = {"etag": row["catalogue_version"]}
    return out


def version_number(etag: str | None):
    """'cat-42.9f3ac1' -> 42. The counter is what makes 'two decisions behind' sayable."""
    if not etag:
        return None
    m = re.match(r"^cat-(\d+)\.", str(etag))
    return int(m.group(1)) if m else None


def staleness(tenant: str | None = None, live: dict | None = None) -> dict | None:
    """Are the newest saved numbers behind the catalogue? -> None when they are current.

    This is the honest half of caching. Serving a saved result instantly is only
    acceptable if the client is told when a decision has landed that the result does not
    include. Nothing is recalculated on their behalf -- they are told, and they choose.
    """
    tenant = tenant or config.TENANT
    # `live` lets a caller that has already asked the catalogue for its version hand it
    # over instead of asking again. /health carries the version, so a page that checks the
    # catalogue is up has the answer in hand already.
    live = live or catalogue.version()
    if not live:
        return None
    con = db.connect()
    row = con.execute(
        """SELECT catalogue_version, ran_at, label FROM analysis_run
           WHERE tenant=? AND catalogue_version IS NOT NULL
           ORDER BY ran_at DESC, rowid DESC LIMIT 1""", (tenant,)).fetchone()
    con.close()
    if not row or row["catalogue_version"] == live["etag"]:
        return None
    was, now = version_number(row["catalogue_version"]), live["version"]
    behind = (now - was) if (was is not None and now > was) else None
    return dict(was=row["catalogue_version"], now=live["etag"], behind=behind,
                since=row["ran_at"], label=row["label"], note=live.get("note"))


# --------------------------------------------------------------------------- run
def run(window: str | None = None, frm: str | None = None, to: str | None = None,
        profile: str | None = None, top: int = 20, save: bool = True,
        tenant: str | None = None, force: bool = False) -> dict:
    """Score a window and (by default) save the result.

    Serves the saved result when the client's data and the catalogue are both unchanged.
    `force=True` recalculates regardless -- the button behind "these numbers are one
    decision old".
    """
    tenant = tenant or config.TENANT
    label, y0, m0, y1, m1 = parse_window(window, frm, to, tenant)
    wkey = cache_key(y0, m0, y1, m1, profile)
    fingerprint = db.window_fingerprint(y0, m0, y1, m1, tenant)
    live = catalogue.version()
    etag = live["etag"] if live else None

    if not force:
        # An exact hit is only possible when we KNOW the live version. With the catalogue
        # unreachable, etag is None and there is nothing to match against — so skip
        # straight to the held result, which the branch below labels honestly. Passing
        # None into the exact lookup would have matched any version and called it current.
        if etag:
            hit = _lookup(tenant, wkey, fingerprint, etag)
            if hit is not None:
                hit["stale"] = None
                return hit

        # Same purchase data, but the catalogue has moved — or cannot be reached at all.
        # Serve what we have and SAY so, rather than silently spending thirty seconds
        # recalculating for somebody who only wanted to look at a chart. A number that
        # changes underneath a reader without warning costs more trust than a number that
        # is a day old and admits it.
        held = _lookup(tenant, wkey, fingerprint)
        if held is not None:
            was = held.get("scored_against")
            if etag:
                a, b = version_number(was), live["version"]
                held["stale"] = dict(was=was, now=etag, since=held["ran_at"],
                                     behind=(b - a) if (a is not None and b > a) else None,
                                     note=live.get("note"), reachable=True)
            else:
                held["stale"] = dict(was=was, now=None, since=held["ran_at"],
                                     behind=None, note=None, reachable=False)
            return held

    lines = db.lines_for(y0, m0, y1, m1, tenant)
    if not lines:
        raise WindowError(f"no purchase lines in {label}")

    result = catalogue.score(lines, label=label, profile=profile, top=top)
    result["cached"] = False
    result["stale"] = None

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
        result["ran_at"] = dt.datetime.now().isoformat(timespec="seconds")
        scored_at = (result.get("catalogue_version") or {}).get("etag") or etag
        result["run_id"] = _save(result, label, y0, m0, y1, m1, len(lines), tenant,
                                 wkey, scored_at, fingerprint)
    return result


def _save(result: dict, label, y0, m0, y1, m1, nlines: int, tenant: str,
          wkey: str, etag, fingerprint: str) -> str:
    """Store the result AND the three things that make it valid.

    The version comes from the scoring response where possible: the catalogue reports the
    version it actually scored against, so a rebuild landing between the version check and
    the scoring call cannot file a result under a version that did not produce it.
    """
    h = result["headline"]
    rid = uuid.uuid4().hex[:12]
    con = db.connect()
    placeholders = ",".join(["?"] * 17)
    con.execute(f"INSERT INTO analysis_run ({CACHE_COLUMNS}) VALUES ({placeholders})",
                (rid, tenant, label, f"{y0}-{m0:02d}", f"{y1}-{m1:02d}",
                 h.get("eat_profile"),
                 result.get("ran_at") or dt.datetime.now().isoformat(timespec="seconds"),
                 nlines, h["food_kg"], h["co2_kg"], h["intensity_kg_co2_per_kg"],
                 h["eat_lancet_score"],
                 h["confidence"]["product_specific_pct_of_weight"], json.dumps(result),
                 wkey, etag, fingerprint))
    con.commit()
    con.close()
    return rid


def history(limit: int = 25, tenant: str | None = None) -> list[dict]:
    tenant = tenant or config.TENANT   # always scoped; there is no all-clients history
    con = db.connect()
    rows = con.execute(
        """SELECT id, label, period_from, period_to, eat_profile, ran_at, lines,
                  food_kg, co2_kg, intensity, eat_score, specific_pct, catalogue_version
           FROM analysis_run WHERE tenant=? ORDER BY ran_at DESC, rowid DESC LIMIT ?""",
        (tenant, limit)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def saved(run_id: str, tenant: str | None) -> dict | None:
    """A saved analysis by id, restricted to one client. `tenant=None` means any.

    `tenant` is REQUIRED and has no default on purpose. This is a lookup by an opaque
    id, and an id is not a permission -- without the filter, changing one character in
    a URL returns another client's entire analysis: their spend, their volumes, their
    restaurants. A default of None would make forgetting it silent, so every caller is
    forced to say which client it is asking for, and only an admin route may say "any".
    """
    con = db.connect()
    if tenant is None:
        r = con.execute("SELECT result_json FROM analysis_run WHERE id=?",
                        (run_id,)).fetchone()
    else:
        r = con.execute("SELECT result_json FROM analysis_run WHERE id=? AND tenant=?",
                        (run_id, tenant)).fetchone()
    con.close()
    return json.loads(r["result_json"]) if r else None

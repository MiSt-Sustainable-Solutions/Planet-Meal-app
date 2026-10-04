"""
Transport from the wholesaler's distribution centre to the client, the TU Delft way.

RIVM's footprints stop at the distribution centre ("cradle-to-DC"). TU Delft's own tool,
CAAT's FoodPrint, adds the last leg as a separate line: the Sligro DC in Berkel &
Rodenrijs to campus, 12 km each way plus 5 km driven on campus, one truck a day on 200
days, 0.363 kg CO2e per tonne-kilometre. Asked for by TU Delft (4 Oct 2026); the method
and the reference numbers are in docs/transport-plan.md.

Their formulas, from the workbook cells:

    annual_km      = (2 x km_dc + km_campus) x resupply_days x trucks_per_day
    load_per_truck = tonnes x 1000 / resupply_days / trucks_per_day
    transport_kg   = annual_km x EF x load_per_truck / 1000

Days and trucks cancel, so

    transport_kg = (2 x km_dc + km_campus) x EF x tonnes delivered

-- 29 km x 0.363 = 10.527 kg CO2e per tonne at TU Delft's settings. It depends on the
route and the tonnes and nothing else, so it works for any window: it is worked out month
by month, each month with the setting in force for it, and summed. A year with one
setting reproduces their annual figure exactly. Days and trucks are kept because they
give the average truck load, which is the sanity check on the emission factor: 0.363 is
for a truck under 10 t.

WHAT IT DOES NOT CHANGE. The footprint, the intensity (kg CO2e per kg of food), every
breakdown and the per-line export stay food-only, as at TU Delft: their KPI is food CO2 /
food tonnes, and transport is reported beside it as one extra line. A setting is a
property of the client, not of a restaurant, so no per-restaurant view carries it.

WHICH TONNES. The food the app weighs -- food and drinks, as the dashboard counts them.
TU Delft also put non-food on the truck, but their non-food figure (478 t against 162 t of
food in the 2025-26 workbook) is not credible, and the app has no non-food weight per
month to apply a setting to. Stated in the caveat and on the Transport page.

A setting is never edited or deleted, like an adjustment: removing one stamps it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import uuid

import db
from adjustments import PERIOD, _key

# TU Delft's settings, as CAAT's form gives them, as the starting values of the form.
DEFAULTS = dict(wholesaler="Sligro", dc_location="Berkel & Rodenrijs", km_dc="12",
                km_campus="5", resupply_days="200", trucks_per_day="1", ef_kg_per_tkm="0.363",
                ef_source="Regular truck (<10 t), CAAT FoodPrint 2024 report")
TRUCK_TONNES = 10.0       # the class the default factor is for; a heavier average load is flagged


class TransportError(ValueError):
    pass


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _num(v, what: str, positive: bool = True, allow_zero: bool = False) -> float:
    try:
        x = float(str(v).replace(",", ".").strip())
    except ValueError:
        raise TransportError(f"{what} must be a number")
    if x < 0 or (x == 0 and not allow_zero):
        raise TransportError(f"{what} must be more than zero" if not allow_zero
                             else f"{what} cannot be negative")
    return x


def per_tonne(s: dict) -> float:
    """kg CO2e for each tonne delivered: the route there and back, plus campus, times EF."""
    return (2 * float(s["km_dc"]) + float(s["km_campus"])) * float(s["ef_kg_per_tkm"])


def describe(s: dict) -> dict:
    """The derived figures the page shows beside a setting."""
    route = 2 * float(s["km_dc"]) + float(s["km_campus"])
    return dict(round_trip_km=round(2 * float(s["km_dc"]), 1), route_km=round(route, 1),
                annual_km=round(route * float(s["resupply_days"]) * float(s["trucks_per_day"])),
                kg_per_tonne=round(per_tonne(s), 3))


# --------------------------------------------------------------------------- settings
def active(tenant: str) -> list[dict]:
    con = db.connect()
    rows = con.execute("SELECT * FROM transport WHERE tenant=? AND removed_at IS NULL "
                       "ORDER BY from_period", (tenant,)).fetchall()
    con.close()
    return [dict(r, **describe(r)) for r in rows]


def removed(tenant: str, limit: int = 50) -> list[dict]:
    con = db.connect()
    rows = con.execute("SELECT * FROM transport WHERE tenant=? AND removed_at IS NOT NULL "
                       "ORDER BY removed_at DESC LIMIT ?", (tenant, limit)).fetchall()
    con.close()
    return [dict(r, **describe(r)) for r in rows]


def add(tenant: str, form: dict, by: str) -> str:
    """Record a route setting for a range of months. -> its id."""
    wholesaler = " ".join((form.get("wholesaler") or "").split())
    dc = " ".join((form.get("dc_location") or "").split())
    if not wholesaler or not dc:
        raise TransportError("name the wholesaler and the location of its distribution centre")
    km_dc = _num(form.get("km_dc"), "the distance from the DC to the client")
    km_campus = _num(form.get("km_campus"), "the distance driven on site", allow_zero=True)
    days = _num(form.get("resupply_days"), "the resupply days per year")
    trucks = _num(form.get("trucks_per_day"), "the number of trucks per day")
    ef = _num(form.get("ef_kg_per_tkm"), "the emission factor")
    if days > 366:
        raise TransportError("there are at most 366 resupply days in a year")
    ef_source = " ".join((form.get("ef_source") or "").split())
    from_period = (form.get("from_period") or "").strip()
    to_period = (form.get("to_period") or "").strip() or None
    for p in (from_period, to_period):
        if p is not None and not PERIOD.match(p):
            raise TransportError(f"{p or 'the start month'} is not a month written as YYYY-MM")
    if to_period and _key(to_period) < _key(from_period):
        raise TransportError("the last month is before the first month")
    reason = " ".join((form.get("reason") or "").split())
    if not reason:
        raise TransportError("say where the figures come from, for the record")

    con = db.connect()
    # One route per month. Two settings on the same month would leave the figure depending
    # on which one a query read first.
    lo, hi = _key(from_period), _key(to_period) or 999912
    for r in con.execute("SELECT from_period, to_period, wholesaler FROM transport "
                         "WHERE tenant=? AND removed_at IS NULL", (tenant,)):
        if _key(r["from_period"]) <= hi and lo <= (_key(r["to_period"]) or 999912):
            con.close()
            raise TransportError(
                f"a route ({r['wholesaler']}) is already set from {r['from_period']}"
                f"{' to ' + r['to_period'] if r['to_period'] else ' onwards'}. Remove that "
                "one first, or choose months that do not overlap.")
    tid = uuid.uuid4().hex[:12]
    con.execute("""INSERT INTO transport (id, tenant, wholesaler, dc_location, km_dc,
                       km_campus, resupply_days, trucks_per_day, ef_kg_per_tkm, ef_source,
                       from_period, to_period, reason, created_at, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tid, tenant, wholesaler, dc, km_dc, km_campus, int(days), trucks, ef,
                 ef_source, from_period, to_period, reason, _now(), by))
    con.commit()
    con.close()
    db.invalidate()
    return tid


def remove(tenant: str, transport_id: str, by: str, why: str = "") -> dict:
    con = db.connect()
    row = con.execute("SELECT * FROM transport WHERE id=? AND tenant=? AND removed_at IS NULL",
                      (transport_id, tenant)).fetchone()
    if not row:
        con.close()
        raise TransportError("no such route setting for this client, or it was already removed")
    con.execute("UPDATE transport SET removed_at=?, removed_by=?, removed_why=? "
                "WHERE id=? AND tenant=?",
                (_now(), by, " ".join((why or "").split()), transport_id, tenant))
    con.commit()
    con.close()
    db.invalidate()
    return dict(row)


def digest(tenant: str) -> str:
    """Changes whenever this client's route settings do. Part of the publish signature."""
    rows = active(tenant)
    if not rows:
        return NONE
    return hashlib.sha1("|".join(
        f"{r['id']}:{r['km_dc']}:{r['km_campus']}:{r['ef_kg_per_tkm']}:{r['from_period']}:"
        f"{r['to_period']}" for r in rows).encode()).hexdigest()[:12]


# What digest() says when no route is set -- and what a publication made before transport
# existed is taken to have had, since at the time none could be.
NONE = "none"


# --------------------------------------------------------------------------- applying
def for_months(tenant: str, by_month: list[dict]) -> dict | None:
    """Transport for the months of a result. -> None when the client has no route set.

    Each month with the setting in force for it. A month no setting covers contributes
    nothing and is named, so a total that is short says why.
    """
    settings = active(tenant)
    if not settings:
        return None
    for s in settings:
        s["lo"], s["hi"] = _key(s["from_period"]), _key(s["to_period"]) or 999912
    per_month, used, uncovered, total, tonnes = [], {}, [], 0.0, 0.0
    for m in by_month or []:
        period = m.get("period") or ""
        if not PERIOD.match(period):
            continue
        ym = _key(period)
        s = next((x for x in settings if x["lo"] <= ym <= x["hi"]), None)
        t = (m.get("food_kg") or 0) / 1000
        if s is None:
            uncovered.append(period)
            per_month.append(dict(period=period, tonnes=round(t, 3), kg=0.0, setting=None))
            continue
        kg = per_tonne(s) * t
        total += kg
        tonnes += t
        used[s["id"]] = s
        per_month.append(dict(period=period, tonnes=round(t, 3), kg=round(kg, 1),
                              setting=s["id"]))
    settings_used = []
    for s in used.values():
        months = [p for p in per_month if p["setting"] == s["id"]]
        t = sum(p["tonnes"] for p in months)
        # The average load is a yearly notion: tonnes a year over the trucks a year.
        per_year = t * 12 / len(months) if months else 0.0
        load = per_year / (float(s["resupply_days"]) * float(s["trucks_per_day"]))
        settings_used.append(dict(
            {k: s[k] for k in ("id", "wholesaler", "dc_location", "km_dc", "km_campus",
                               "resupply_days", "trucks_per_day", "ef_kg_per_tkm",
                               "ef_source", "from_period", "to_period")},
            **describe(s), months=len(months), tonnes=round(t, 3),
            kg=round(sum(p["kg"] for p in months), 1),
            avg_load_t=round(load, 2), over_truck_class=load > TRUCK_TONNES))
    return dict(kg=round(total, 1), tonnes=round(tonnes, 3), per_month=per_month,
                settings_used=settings_used, months_uncovered=uncovered,
                basis="food and drinks delivered (non-food not included)")


def attach(result: dict, tenant: str) -> dict:
    """Put this client's transport beside a result's footprint. In place; -> the result.

    Done every time a result is served, not when it is saved: the footprint does not depend
    on the route, so a change of route should not make every saved period recalculate. A
    saved result is brought up to date here; a published copy keeps what it was published
    with, and the admin bar says when the route has changed since (publish.signature).
    """
    h = result.get("headline") or {}
    h["caveats"] = [c for c in (h.get("caveats") or []) if c.get("code") != "transport"]
    t = for_months(tenant, result.get("by_month") or [])
    result["transport"] = t
    if t is None:
        h.pop("transport_co2_kg", None)
        h.pop("total_co2_kg_incl_transport", None)
        return result
    h["transport_co2_kg"] = round(t["kg"])
    h["total_co2_kg_incl_transport"] = round((h.get("co2_kg") or 0) + t["kg"])
    if t["months_uncovered"]:
        h["caveats"].append(dict(
            code="transport", severity="warning", owner="MiSt",
            message=(f"Transport from the distribution centre is not counted for "
                     f"{len(t['months_uncovered'])} month"
                     f"{'' if len(t['months_uncovered']) == 1 else 's'} with no route set: "
                     f"{', '.join(t['months_uncovered'][:6])}"
                     f"{' and more' if len(t['months_uncovered']) > 6 else ''}.")))
    return result

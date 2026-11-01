"""
What the catalogue needs to give a line with no weight one (1 Nov 2026).

Sligro describes the same article differently from one yearly file to the next: 400792,
40 pizza bases, is "DS, Maat 40, Eenh ST" in 2023 -- no weight -- and "DS, Maat 12.6,
Eenh KG" in 2025. The adapter reads each file on its own, so the 2023 lines arrive with
kg_known=0 although this client's 2025 file states the weight. The catalogue fills the
weight in (its weigh.py) and the line says how; this module hands it two facts only the
app holds:

    the case, as the line's OWN file describes it      ivp, vp, maat, eenh
    this client's kg per case for the article           pack_kg: the median of kg / Aantal
                                                        over its weighed lines, in any file

Nothing here is stored and nothing overwrites a line: `kg` and `kg_known` stay what the
file said. Lines whose Aantal is kilograms (VP 'KG') are left out of the median, because
kg / Aantal is 1 there whatever a case weighs.
"""
from __future__ import annotations

import hashlib
import statistics

import config
import db

_CACHE: dict[str, tuple] = {}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def facts(tenant: str | None = None) -> dict:
    """-> {(upload_id, artikelnr): (ivp, vp, maat, eenh)}, {artikelnr: (ivp, vp, maat, eenh)},
    {artikelnr: kg per case}. Cached until the client's data changes."""
    tenant = tenant or config.TENANT
    snap = db.snapshot(tenant)
    stamp = (len(snap["months"]), snap["products"], snap["last_seen"],
             tuple((m["period"], m["lines"], m["kg"]) for m in snap["months"]))
    held = _CACHE.get(tenant)
    if held and held[0] == stamp:
        return held[1]
    con = db.connect()
    per_file = {(r["upload_id"], r["artikelnr"]): (r["ivp"], r["vp"], r["maat"], r["eenh"])
                for r in con.execute(
                    "SELECT up.upload_id, up.artikelnr, up.ivp, up.vp, up.maat, up.eenh "
                    "FROM upload_product up JOIN upload u ON u.id=up.upload_id "
                    "WHERE u.tenant=?", (tenant,))}
    latest = {r["artikelnr"]: (r["ivp"], r["vp"], r["maat"], r["eenh"])
              for r in con.execute("SELECT artikelnr, ivp, vp, maat, eenh FROM product "
                                   "WHERE tenant=?", (tenant,))}
    # only the articles that need it: those with at least one line the file did not weigh
    rows = con.execute("""
        SELECT l.artikelnr, l.source_upload, l.aantal, l.kg FROM purchase_line l
        WHERE l.tenant=? AND l.kg_known=1 AND l.aantal>0 AND l.kg>0
          AND l.artikelnr IN (SELECT DISTINCT artikelnr FROM purchase_line
                              WHERE tenant=? AND kg_known=0)""", (tenant, tenant)).fetchall()
    con.close()
    ratios: dict[str, list[float]] = {}
    for r in rows:
        d = per_file.get((r["source_upload"], r["artikelnr"])) or latest.get(r["artikelnr"])
        if d and (d[1] or "").upper() == "KG":
            continue
        ratios.setdefault(r["artikelnr"], []).append(r["kg"] / r["aantal"])
    pack = {a: statistics.median(v) for a, v in ratios.items()}
    out = dict(per_file=per_file, latest=latest, pack=pack)
    _CACHE[tenant] = (stamp, out)
    return out


def attach(lines: list[dict], tenant: str | None = None) -> list[dict]:
    """Give every line its case and, where it has no weight, the client's kg per case.

    In place, -> the same list. A line that names no file (seed data) takes the product's
    latest description of the case.
    """
    f = facts(tenant)
    for line in lines:
        d = (f["per_file"].get((line.pop("source_upload", None), line["artikelnr"]))
             or f["latest"].get(line["artikelnr"]) or ("1", "", None, ""))
        line["ivp"], line["vp"], line["eenh"] = str(d[0] or "1"), d[1] or "", d[3] or ""
        line["maat"] = _num(d[2])
        line["pack_kg"] = (f["pack"].get(line["artikelnr"], 0.0)
                           if not line.get("kg_known") else 0.0)
    return lines


def digest(tenant: str | None = None) -> str:
    """Half a line of the cache key: the kg per case this client's files give each article.

    A new file can weigh an article an old period bought by the piece, which changes that
    period's answer without touching one of its lines.
    """
    pack = facts(tenant)["pack"]
    s = ",".join(f"{a}:{pack[a]:.4f}" for a in sorted(pack))
    return hashlib.sha1(s.encode()).hexdigest()[:10]


def history(tenant: str | None = None) -> dict:
    """Every article this client holds -> dict(kg_case, weighed, unweighed).

    For the pre-flight, which compares a new file against it: kg_case is the median kg per
    case over weighed lines whose Aantal counts cases; weighed / unweighed say whether any
    line had a weight, and whether any had none.
    """
    tenant = tenant or config.TENANT
    f = facts(tenant)
    con = db.connect()
    rows = con.execute("SELECT artikelnr, source_upload, aantal, kg, kg_known FROM purchase_line "
                       "WHERE tenant=?", (tenant,)).fetchall()
    con.close()
    out: dict[str, dict] = {}
    ratios: dict[str, list[float]] = {}
    for r in rows:
        h = out.setdefault(r["artikelnr"], dict(kg_case=None, weighed=False, unweighed=False))
        if not r["kg_known"]:
            h["unweighed"] = True
            continue
        h["weighed"] = True
        d = f["per_file"].get((r["source_upload"], r["artikelnr"])) or f["latest"].get(r["artikelnr"])
        if (r["aantal"] or 0) > 0 and (r["kg"] or 0) > 0 and not (d and (d[1] or "").upper() == "KG"):
            ratios.setdefault(r["artikelnr"], []).append(r["kg"] / r["aantal"])
    for a, v in ratios.items():
        out[a]["kg_case"] = statistics.median(v)
    return out


def invalidate() -> None:
    _CACHE.clear()

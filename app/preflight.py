"""
The pre-flight report: what this file looks like, BEFORE anyone trusts a number from it.

Every check returns a finding with a severity, and the worst one decides the verdict:

    blocked            something here would produce a wrong number. Do not analyse.
    go_with_warnings   analysable, but the reader must be told what is soft about it.
    go                 nothing found worth saying.

Two kinds of question are being asked, and they are answered in different places:

  * "Do we already have these months?" · "Is this a plausible volume for this client?"
      -> the APP's own database. This client's history lives here.
  * "Do we know this product?" · "Is this category mapped?"
      -> the shared CATALOGUE API, which knows about products in general.

The catalogue-dependent checks degrade gracefully: if the API is unreachable the report is
still produced, with those checks marked as unavailable rather than silently passing.

`overlap` is the check that matters most. Sligro exports are cumulative, so uploading
'December 2025' when 'Augustus 2025' is already loaded re-supplies January through August.
Double-counting is the worst bug available in this project, so an overlap blocks and the
user has to say explicitly what to do about it.

Nothing here is silent. A file can be poor and still be analysed — but never quietly.
"""
from __future__ import annotations

import catalogue
import config
import db

PARTIAL_RATIO = 0.25          # below this share of a normal month = not a real month
THIN_RATIO = 0.50
HOLIDAY_MONTHS = (7, 8)       # Dutch university summer: genuinely quiet, not broken

SEVERITY_RANK = {"ok": 0, "info": 0, "unavailable": 0, "warning": 1, "error": 2}


def _f(code, severity, message, **extra):
    return dict(code=code, severity=severity, message=message, **extra)


# --------------------------------------------------------------------------- structure
def check_structure(r) -> list[dict]:
    out = [_f("readable", "ok",
              f"Read {len(r.lines):,} purchase lines across {len(r.products):,} products "
              f"and {len(r.periods)} month(s), using the {r.adapter} adapter.",
              periods=r.periods, adapter=r.adapter)]
    out.extend(r.quirks)                       # whatever the adapter wants to disclose
    out.extend(_f("note", "info", n) for n in r.notes)
    if r.row_problems:
        out.append(_f("unreadable_rows", "warning",
                      f"{len(r.row_problems)} row(s) could not be read and were skipped.",
                      rows=r.row_problems[:20]))
    return out


def check_duplicate_rows(r) -> list[dict]:
    """The same product billed twice for the same month, inside one file.

    Usually means the file was concatenated from several exports — which is exactly how a
    cumulative file double-counts.
    """
    seen = {}
    for line in r.lines:
        key = (line[0], line[1], line[5])
        seen[key] = seen.get(key, 0) + 1
    dupes = [f"{art} in {y}-{m:02d}" for (y, m, art), n in seen.items() if n > 1]
    if not dupes:
        return []
    return [_f("duplicate_rows", "warning",
               f"{len(dupes)} product/period combinations appear on more than one row. They "
               "will be summed. If this file was assembled from several exports, check that "
               "is what you intended.", examples=dupes[:10])]


# --------------------------------------------------------------------------- the app's data
def check_overlap(r, tenant: str | None = None) -> list[dict]:
    """Does this file re-supply months we already hold? The double-count guard."""
    have = {m["period"] for m in db.months(tenant)}
    clash = [p for p in r.periods if p in have]
    fresh = [p for p in r.periods if p not in have]
    if not clash:
        return [_f("overlap", "ok",
                   "None of these months are already loaded. Nothing can double-count.",
                   new_months=fresh)]
    return [_f("overlap", "error",
               f"{len(clash)} of the {len(r.periods)} months in this file are already "
               f"loaded ({', '.join(clash[:6])}{'...' if len(clash) > 6 else ''}). "
               "Loading this as-is would count them twice. Choose whether to import only "
               "the new months, or to replace the existing ones"
               + (f" — new here: {', '.join(fresh)}." if fresh else ", of which this file "
                  "has none."),
               already_loaded=clash, new_months=fresh)]


def check_volume(r, tenant: str | None = None) -> list[dict]:
    """Catch a truncated export.

    Measured against THIS CLIENT'S own median complete month where we have one, because a
    partial file judged only against itself looks perfectly consistent. July and August are
    genuinely quiet in a university, so they are exempt from the softer band but not from
    the hard one.
    """
    by_period = {}
    for line in r.lines:
        by_period[(line[0], line[1])] = by_period.get((line[0], line[1]), 0.0) + (line[7] or 0)
    if not by_period:
        return []

    hist = [m["spend_eur"] for m in db.months(tenant)
            if m["complete"] and m["month"] not in HOLIDAY_MONTHS and m["spend_eur"]]
    if len(hist) >= 3:
        hist.sort()
        yardstick = hist[len(hist) // 2]
        basis = "this client's median complete month"
    else:
        vals = sorted(v for (y, m), v in by_period.items() if m not in HOLIDAY_MONTHS) \
               or sorted(by_period.values())
        yardstick = vals[len(vals) // 2]
        basis = "the median month inside this file"
    if not yardstick:
        return []

    partial, thin = [], []
    for (y, m), eur in sorted(by_period.items()):
        ratio = eur / yardstick
        label = f"{y}-{m:02d}"
        if ratio < PARTIAL_RATIO:
            partial.append((label, eur))
        elif ratio < THIN_RATIO and m not in HOLIDAY_MONTHS:
            thin.append((label, eur))

    out = []
    if partial:
        out.append(_f("partial_months", "error",
                      f"{len(partial)} month(s) hold a small fraction of a normal month's "
                      f"spend ({', '.join(f'{p} at EUR {e:,.0f}' for p, e in partial[:6])}"
                      f"{'...' if len(partial) > 6 else ''}). Measured against {basis} of "
                      f"EUR {yardstick:,.0f}. This is what a truncated export looks like; "
                      "analysing it would understate the footprint without saying so.",
                      months=[p for p, _ in partial]))
    if thin:
        out.append(_f("thin_months", "warning",
                      f"{len(thin)} month(s) are well below a normal month but not obviously "
                      f"broken ({', '.join(f'{p} at EUR {e:,.0f}' for p, e in thin)}). Worth "
                      "confirming the export covers the whole month.",
                      months=[p for p, _ in thin]))
    if not out:
        out.append(_f("volume", "ok", f"Every month is a plausible size against {basis}."))
    return out


def check_weights(r) -> list[dict]:
    """How much of this file can actually be turned into kilograms."""
    n = len(r.lines)
    unweighable = [l for l in r.lines if l[9] == 0]
    spend = sum((l[7] or 0) for l in r.lines)
    uw_spend = sum((l[7] or 0) for l in unweighable)
    pct_lines = 100 * len(unweighable) / n if n else 0
    pct_spend = 100 * uw_spend / spend if spend else 0

    worst = {}
    for l in unweighable:
        worst[l[5]] = worst.get(l[5], 0.0) + (l[7] or 0)
    top = sorted(worst.items(), key=lambda kv: -kv[1])[:10]

    sev = "ok" if pct_spend < 1 else ("warning" if pct_spend < 15 else "error")
    return [_f("weights", sev,
               f"{100 - pct_lines:.1f}% of lines convert to kilograms. {len(unweighable):,} "
               f"line(s) ({pct_lines:.1f}% of lines, {pct_spend:.1f}% of spend) are sold per "
               "piece with no weight in the file and will contribute zero kg and zero CO2e. "
               "They are never silently dropped — they are reported as a known gap.",
               unweighable_lines=len(unweighable), pct_of_lines=round(pct_lines, 1),
               pct_of_spend=round(pct_spend, 1), products=len(worst),
               examples=[dict(artikelnr=a, description=(r.products.get(a) or ("", ""))[1],
                              spend_eur=round(v)) for a, v in top])]


def check_barcodes(r) -> list[dict]:
    with_ean = sum(1 for p in r.products.values() if p[8])
    pct = 100 * with_ean / len(r.products) if r.products else 0
    sev = "ok" if pct >= 80 else ("warning" if pct >= 40 else "error")
    return [_f("barcodes", sev,
               f"{pct:.0f}% of products carry a barcode ({with_ean:,} of "
               f"{len(r.products):,}). Barcodes are how a product is recognised across "
               "suppliers and how it inherits a decision already made; without one we fall "
               "back to the supplier's article number.",
               with_ean=with_ean, products=len(r.products), pct=round(pct, 1))]


# --------------------------------------------------------------------------- the catalogue
def check_catalogue(r, tenant: str | None = None) -> list[dict]:
    """What the shared catalogue already knows about these products.

    Degrades to 'unavailable' rather than pretending everything is fine.
    """
    spend = {}
    for l in r.lines:
        spend[l[5]] = spend.get(l[5], 0.0) + (l[7] or 0)
    total = sum(spend.values()) or 1

    seen_before = db.known_articles(tenant)
    new_to_client = [a for a in r.products if a not in seen_before]
    new_spend = sum(spend.get(a, 0) for a in new_to_client)

    out = [_f("known_products", "ok" if not new_to_client else "info",
              f"{len(r.products) - len(new_to_client):,} of {len(r.products):,} products "
              f"have been purchased before "
              f"({100 * (total - new_spend) / total:.1f}% of spend). "
              f"{len(new_to_client):,} are new to this client.",
              known=len(r.products) - len(new_to_client), new=len(new_to_client),
              new_pct_of_spend=round(100 * new_spend / total, 1))]

    probe = [dict(artikelnr=a, ean=r.products[a][8], category=r.products[a][3])
             for a in new_to_client] or \
            [dict(artikelnr=a, ean=r.products[a][8], category=r.products[a][3])
             for a in list(r.products)[:200]]
    known = catalogue.recognise(probe)
    if known is None:
        out.append(_f("catalogue", "unavailable",
                      "The shared catalogue could not be reached, so we cannot yet say how "
                      "well these products will resolve. The file itself is fine; try again, "
                      "or continue and check the data-health page after analysing."))
        return out

    if new_to_client:
        resolvable = known.get("recognised", 0)
        needs = len(new_to_client) - resolvable
        top = sorted(((a, spend.get(a, 0)) for a in new_to_client), key=lambda kv: -kv[1])[:10]
        out.append(_f("new_products", "info",
                      f"Of the {len(new_to_client):,} new products, {resolvable:,} are already "
                      f"in the shared catalogue and will resolve on sight; the remaining "
                      f"{needs:,} will fall to a category average until someone reviews them. "
                      "They enter the review queue ranked by weight.",
                      resolvable=resolvable, needs_review=needs,
                      examples=[dict(artikelnr=a, description=r.products[a][1],
                                     spend_eur=round(v)) for a, v in top]))

    unknown_cats = known.get("unmapped_categories") or []
    if unknown_cats:
        out.append(_f("categories", "warning",
                      f"{len(unknown_cats)} supplier categor(ies) have never been mapped and "
                      "will fall back to the crude average until a rule is added.",
                      unknown=unknown_cats[:25]))
    else:
        out.append(_f("categories", "ok", "Every supplier category in this file is mapped."))
    return out


# --------------------------------------------------------------------------- report
CHECKS = [check_structure, check_duplicate_rows, check_overlap, check_volume,
          check_weights, check_barcodes, check_catalogue]


def report(r, tenant: str | None = None) -> dict:
    tenant = tenant or config.TENANT
    findings = []
    for fn in CHECKS:
        try:
            findings.extend(fn(r, tenant) if fn.__code__.co_argcount > 1 else fn(r))
        except Exception as e:                    # a broken check must not hide the file
            findings.append(_f(fn.__name__, "warning",
                               f"This check could not run: {type(e).__name__}: {e}"))

    worst = max((SEVERITY_RANK.get(f["severity"], 0) for f in findings), default=0)
    verdict = ["go", "go_with_warnings", "blocked"][worst]
    errors = [f for f in findings if f["severity"] == "error"]
    warnings = [f for f in findings if f["severity"] == "warning"]

    if verdict == "blocked":
        summary = (f"Do not analyse this file yet — {len(errors)} thing(s) here would "
                   "produce a wrong number.")
    elif verdict == "go_with_warnings":
        summary = (f"This file can be analysed, with {len(warnings)} caveat(s) the reader "
                   "must be told about.")
    else:
        summary = "This file is clean. Nothing found worth flagging."

    return dict(verdict=verdict, summary=summary,
                filename=r.filename, adapter=r.adapter, periods=r.periods,
                lines=len(r.lines), products=len(r.products),
                spend_eur=round(sum((l[7] or 0) for l in r.lines)),
                errors=len(errors), warnings=len(warnings), findings=findings)

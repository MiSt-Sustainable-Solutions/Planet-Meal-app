"""
The analysis workbook: everything on the dashboard as a spreadsheet, every line on the last tab.

Built in one place because two things build it. /export.xlsx builds it on demand for MiSt,
from the figures as they are right now. Publishing builds it once, from the figures being
published, and keeps the bytes -- so what a client downloads is the same answer as what
their dashboard shows, however far the working figures have moved since.
"""
from __future__ import annotations

import io
import re

import charts
import lines_export


def analysis_workbook(result: dict, scored: dict, client: str,
                      extra_notes: list[str] | None = None) -> bytes:
    """`result` is an analysis; `scored` is catalogue.score_lines() for the same rows."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    head = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="1A4A2A")

    def sheet(name, cols, rows):
        ws = wb.create_sheet(name[:31])
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = head, fill
            c.alignment = Alignment(horizontal="center")
        for r in rows:
            ws.append(r)
        for i, col in enumerate(cols, start=1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = \
                max(12, min(46, len(str(col)) + 6))
        ws.freeze_panes = "A2"
        return ws

    h = result["headline"]
    wb.remove(wb.active)
    sheet("summary", ["metric", "value"], [
        ["window", h["window"]], ["months", h["months"]],
        ["period from", h["period_from"]], ["period to", h["period_to"]],
        ["food purchased (kg)", h["food_kg"]], ["non-food (kg)", h["nonfood_kg"]],
        ["CO2e (kg)", h["co2_kg"]],
        ["intensity (kg CO2e per kg food)", h["intensity_kg_co2_per_kg"]],
        ["EAT-Lancet score", h["eat_lancet_score"]],
        ["EAT-Lancet profile", h.get("eat_profile")],
        ["spend (EUR)", h["spend_eur"]], ["restaurants", h["restaurants"]],
        ["products", h["products"]], ["lines", h["lines"]],
        ["matched to a specific product (% of weight)",
         h["confidence"]["product_specific_pct_of_weight"]],
        ["per-piece share of spend (%)", h["piece_spend_pct"]],
        # Adjusted and unadjusted side by side, from the same lines as the last tab.
        ["food bought, before adjustments (kg)", round(sum(
            r.get("kg") or 0 for r in scored["rows"] if r.get("is_food")))],
        ["food not counted because of adjustments (kg)", round(sum(
            (r.get("kg") or 0) - (r.get("kg_eff") or 0)
            for r in scored["rows"] if r.get("is_food")))],
    ] + ([
        # Beside the footprint, never in it: the intensity above is food CO2 / food, as in
        # TU Delft's own tool. Only when a route is set (4 Oct 2026).
        ["transport DC -> client (kg CO2e)", h.get("transport_co2_kg")],
        ["total incl. transport (kg CO2e)", h.get("total_co2_kg_incl_transport")],
    ] if h.get("transport_co2_kg") is not None else []))
    t = result.get("transport")
    if t:
        ws = sheet("transport", ["period", "tonnes delivered", "kg CO2e", "route"], [
            [m["period"], m["tonnes"], m["kg"],
             "no route set" if not m["setting"] else
             next((f"{s['wholesaler']} {s['dc_location']}" for s in t["settings_used"]
                   if s["id"] == m["setting"]), "")]
            for m in t["per_month"]])
        ws.append([])
        ws.append(["total", t["tonnes"], t["kg"]])
        ws.append([])
        ws.append(["how", "kg CO2e = (2 x km DC->client + km on site) x kg CO2e per tonne-km "
                          "x tonnes delivered. The tonnes are the food and drinks counted; "
                          "non-food is not included."])
        for s in t["settings_used"]:
            ws.append([f"{s['from_period']} -> {s['to_period'] or 'onwards'}",
                       f"{s['wholesaler']}, DC {s['dc_location']}: 2 x {s['km_dc']} + "
                       f"{s['km_campus']} = {s['route_km']} km x {s['ef_kg_per_tkm']} "
                       f"= {s['kg_per_tonne']} kg CO2e per tonne. Average truck load "
                       f"{s['avg_load_t']} t ({s['resupply_days']} days, "
                       f"{s['trucks_per_day']} truck(s) a day)."])
    sheet("caveats", ["severity", "owner", "what you must know"],
          [[c["severity"], c["owner"], c["message"]] for c in h["caveats"]])
    # The EAT-Lancet score sits beside the CO2 in every cut: the question asked of these
    # sheets is "which restaurant, which month, and how do they compare". A month or a
    # restaurant with no food from the diet's groups has no score and gets an empty cell --
    # zero is a real score, and a bad one.
    sheet("by month", ["period", "food kg", "kg CO2e", "intensity", "EAT-Lancet",
                       "spend EUR", "quality"],
          [[r["period"], r["food_kg"], r["co2_kg"], r["intensity_kg_co2_per_kg"],
            r.get("eat_lancet_score"), r["spend_eur"], r.get("quality", "")]
           for r in result["by_month"]])
    sheet("by restaurant", ["restaurant", "food kg", "kg CO2e", "intensity", "EAT-Lancet",
                            "spend EUR", "products"],
          [[r["restaurant"], r["food_kg"], r["co2_kg"], r["intensity_kg_co2_per_kg"],
            r.get("eat_lancet_score"), r["spend_eur"], r["products"]]
           for r in result["by_restaurant"]])
    # Figures saved before 16 Sep 2026 carry no per-restaurant month breakdown: the sheet is
    # left out rather than written empty.
    if result.get("by_restaurant_month"):
        sheet("by restaurant per month",
              ["restaurant", "period", "food kg", "kg CO2e", "intensity", "EAT-Lancet"],
              # This cut carries no intensity of its own; it is the two columns beside it.
              [[r["restaurant"], r["period"], r["food_kg"], r["co2_kg"],
                round(r["co2_kg"] / r["food_kg"], 3) if r["food_kg"] else None,
                r.get("eat_lancet_score")]
               for r in result["by_restaurant_month"]])
    # The workbook is a client's file and says what the screen says. It used to write the
    # engine's own keys, so a reader who saw "Refined grain" on the dashboard found
    # "refined_grain" in the sheet beside it (21 Sep 2026). Labelling is idempotent, so it
    # is safe whether or not the page relabelled this result first.
    def group(r):
        return charts.food_group_label(r["food_group"])

    sheet("by food group", ["food group", "food kg", "kg CO2e", "% of weight", "% of CO2",
                            "intensity", "products"],
          [[group(r), r["food_kg"], r["co2_kg"], r["pct_of_weight"], r["pct_of_co2"],
            r["intensity_kg_co2_per_kg"], r["products"]] for r in result["by_food_group"]])
    # "matched to" names what the footprint was taken from, as on the lines sheet (4 Oct
    # 2026). Empty for figures scored before the catalogue recorded it.
    # The diet's group, not ours, in both top sheets (4 Oct 2026): "Whole Grains" covers
    # refined grain too, and sweets are outside the diet. Rows scored before the catalogue
    # said fall back to the food group, as on screen.
    def diet(r):
        return charts.eat_group_label(r) or group(r)

    sheet("top contributors", ["rank", "artikelnr", "product", "EAT-Lancet group", "food kg",
                               "kg CO2e", "kg CO2e per kg", "% of CO2", "running % of CO2",
                               "precision", "matched to (type)", "matched to", "NEVO code",
                               "confidence"],
          [[i, r["artikelnr"], r["description"], diet(r), r["food_kg"], r["co2_kg"],
            r["co2_per_kg"], r["pct_of_co2"], r.get("cumulative_pct_of_co2"),
            charts.grade(r.get("source")), r.get("reference_kind"),
            lines_export.matched_name(r), lines_export.nevo_code(r), r["confidence"]]
           for i, r in enumerate(result["top_contributors"], start=1)])
    # The same ranking per kitchen. One sheet rather than one per restaurant, so it can be
    # filtered and pivoted; the percentages are of that restaurant's own CO2, as on screen.
    sheet("top per restaurant", ["restaurant", "rank", "artikelnr", "product", "EAT-Lancet group",
                                 "food kg", "kg CO2e", "% of this restaurant's CO2",
                                 "running %", "precision", "matched to (type)", "matched to",
                                 "NEVO code"],
          [[per["restaurant"], i, r["artikelnr"], r["description"], diet(r),
            r["food_kg"], r["co2_kg"], r["pct_of_co2"], r.get("cumulative_pct_of_co2"),
            charts.grade(r.get("source")), r.get("reference_kind"),
            lines_export.matched_name(r), lines_export.nevo_code(r)]
           for per in result.get("top_by_restaurant") or []
           for i, r in enumerate(per.get("rows") or [], start=1)])
    # "scored as" says whether a row is a target or a limit: under a limit costs nothing, so
    # a reader adding the gaps up by hand would otherwise get a different total (21 Sep 2026).
    sheet("eat lancet", ["food group", "reference %", "purchased %", "gap", "scored as"],
          [[r["food_group"], r["reference_pct"], r["purchased_pct"], r["gap_pct"],
            "limit" if r.get("is_limit") else "target"]
           for r in result["eat_lancet"]["rows"]]
          + [[], ["measured against", result["eat_lancet"].get("profile_label")],
             ["source", result["eat_lancet"].get("profile_source")]])
    dh = result["data_health"]
    sheet("data health", ["tier", "precision", "what it means", "% of weight", "% of CO2",
                          "% of food spend", "products", "specific?"],
          [[t["label"], charts.grade(t.get("source")), t["explain"], t["pct_of_weight"],
            t["pct_of_co2"], t.get("pct_of_spend"), t["products"],
            "yes" if t["product_level"] else "no"] for t in dh["by_tier"]]
          # Food sold by the piece: a price, no weight, so no tier's kilograms or CO2.
          + ([["no weight", "No weight", "food sold per piece, counted as 0 kg", None, None,
               dh["no_weight"]["pct_of_spend"], dh["no_weight"]["products"], "no"]]
             if dh.get("no_weight") else []))
    sheet("zero weight", ["artikelnr", "product", "category", "pack", "unit", "pieces", "spend EUR"],
          [[r["artikelnr"], r["description"], r["category"], r["vp"], r["eenh"],
            r["pieces"], r["spend_eur"]] for r in result["piece_items"]["rows"]])

    # Every line the sheets above are made of. This is the point of the whole download:
    # a reader who doubts a figure can open the rows underneath it here, in the same
    # file, rather than being sent somewhere else to fetch them.
    lines_export.add_sheet(wb, scored["rows"], client, h["window"],
                           version=scored.get("catalogue_version"),
                           extra_notes=extra_notes or [])
    # ...and what those lines were matched to, so a name on a line can be looked up here.
    lines_export.add_reference_sheets(wb, scored["rows"], lines_export._averages())
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_name(tenant: str, result: dict) -> str:
    # Only characters every operating system accepts in a file name. A single file's
    # window is labelled "File: ...", and Windows refuses the colon.
    # An academic year reads "2025/26"; stripped of its slash it would be "202526", which
    # names no year at all.
    label = re.sub(r"[^A-Za-z0-9()_-]", "", result["headline"]["window"].replace("/", "-"))
    return f"PLANETmeal_{tenant}_{label}.xlsx"

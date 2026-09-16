"""
Chart geometry, computed here so the templates only loop.

Hand-rolled inline SVG rather than a charting library, deliberately:
  * no external script, so the app works offline and on a locked-down network;
  * the palette is the design system's own tokens, so a chart cannot drift from the brand;
  * nothing to load before the page is readable.

If the dashboard ever needs real interactivity — cross-filtering, zoom — swap in ECharts.
The API already returns clean JSON, so that is a front-end change and nothing else.
"""
from __future__ import annotations


def _nice(v: float) -> float:
    """A round number at or above v, for the top of an axis."""
    if v <= 0:
        return 1.0
    import math
    exp = math.floor(math.log10(v))
    base = 10 ** exp
    for mult in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if v <= base * mult:
            return base * mult
    return base * 10


def _fmt(v: float) -> str:
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f}M".replace(".0M", "M")
    if v >= 1_000:
        return f"{v/1_000:.0f}k"
    if v >= 10:
        return f"{v:.0f}"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def line(rows, x_key="period", y_key="co2_kg", flag_key="complete",
         width=980, height=250, pad_l=54, pad_b=34, pad_t=14, pad_r=36) -> dict:
    """A trend line with the axis, the gridlines and a marker per point.

    Points whose `flag_key` is False are drawn in the danger colour — a month that came
    from a partial export must be visibly different, not just footnoted.
    """
    rows = list(rows)
    if not rows:
        return dict(empty=True, width=width, height=height)
    vals = [float(r.get(y_key) or 0) for r in rows]
    ymax = _nice(max(vals) or 1)
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(rows)
    step = plot_w / max(n - 1, 1)

    pts = []
    for i, r in enumerate(rows):
        v = float(r.get(y_key) or 0)
        x = pad_l + i * step
        y = pad_t + plot_h * (1 - v / ymax)
        pts.append(dict(x=round(x, 2), y=round(y, 2), value=v,
                        label=str(r.get(x_key, "")),
                        ok=bool(r.get(flag_key, True)),
                        display=_fmt(v)))

    path = "M" + " L".join(f"{p['x']},{p['y']}" for p in pts)
    area = (f"M{pts[0]['x']},{pad_t + plot_h} L"
            + " L".join(f"{p['x']},{p['y']}" for p in pts)
            + f" L{pts[-1]['x']},{pad_t + plot_h} Z")

    gridlines = []
    for i in range(5):
        v = ymax * i / 4
        gridlines.append(dict(y=round(pad_t + plot_h * (1 - i / 4), 2), label=_fmt(v)))

    # show at most ~12 x labels so they never collide
    every = max(1, n // 12)
    xlabels = [dict(x=p["x"], label=p["label"][-7:], ok=p["ok"])
               for i, p in enumerate(pts) if i % every == 0 or i == n - 1]

    return dict(empty=False, width=width, height=height, points=pts, path=path, area=area,
                gridlines=gridlines, xlabels=xlabels, baseline=round(pad_t + plot_h, 2),
                pad_l=pad_l, pad_r=pad_r, ymax=ymax)


def bars(rows, label_key, value_key, width=980, row_h=30, pad_l=270, pad_r=90,
         secondary_key=None, secondary_suffix="", limit=None) -> dict:
    """Horizontal bars. Labels on the left, value on the right, optional second figure.

    Horizontal because the labels are long ('APPEL TU DELFT AULA HOOFDLOCAT') and a
    rotated axis label is a design failure, not a design choice.
    """
    rows = list(rows)[:limit] if limit else list(rows)
    if not rows:
        return dict(empty=True, width=width, height=60)
    vmax = max(float(r.get(value_key) or 0) for r in rows) or 1
    plot_w = width - pad_l - pad_r
    out = []
    for i, r in enumerate(rows):
        v = float(r.get(value_key) or 0)
        out.append(dict(
            y=i * row_h, h=row_h - 9,
            w=max(1.5, round(plot_w * v / vmax, 2)),
            label=str(r.get(label_key, "")),
            value=v, display=_fmt(v),
            secondary=(f"{r.get(secondary_key)}{secondary_suffix}"
                       if secondary_key and r.get(secondary_key) is not None else None),
            row=r))
    return dict(empty=False, width=width, height=len(out) * row_h + 6, rows=out,
                pad_l=pad_l, pad_r=pad_r, vmax=vmax)


def paired(rows, label_key="food_group", a_key="purchased_pct", b_key="reference_pct",
           width=980, row_h=38, pad_l=180, pad_r=120) -> dict:
    """Purchased share against the reference share, one pair per food group.

    The gap is the point of the EAT-Lancet chart, so the gap is what gets the label.
    """
    rows = list(rows)
    if not rows:
        return dict(empty=True, width=width, height=60)
    vmax = max(max(float(r.get(a_key) or 0), float(r.get(b_key) or 0)) for r in rows) or 1
    vmax = _nice(vmax)
    plot_w = width - pad_l - pad_r
    out = []
    for i, r in enumerate(rows):
        a = float(r.get(a_key) or 0)
        b = float(r.get(b_key) or 0)
        gap = a - b
        out.append(dict(
            y=i * row_h, label=str(r.get(label_key, "")),
            a_w=max(1.5, round(plot_w * a / vmax, 2)),
            b_w=max(1.5, round(plot_w * b / vmax, 2)),
            a=a, b=b, gap=gap,
            gap_display=("+" if gap > 0 else "") + f"{gap:.1f}",
            over=gap > 0))
    return dict(empty=False, width=width, height=len(out) * row_h + 6, rows=out,
                pad_l=pad_l, pad_r=pad_r, vmax=vmax)


def confidence(tiers) -> list[dict]:
    """The tier ladder as one bar. Widths are shares of weight, so it always sums to 100."""
    out = []
    for i, t in enumerate(tiers, start=1):
        pct = float(t.get("pct_of_weight") or 0)
        out.append(dict(cls=f"t{min(i, 6)}", pct=pct, label=t.get("label", ""),
                        show=pct >= 6, explain=t.get("explain", ""),
                        product_level=t.get("product_level", False),
                        products=t.get("products", 0)))
    return out


# --------------------------------------------------------------------------- labels
# The engine's bucket keys are internal. Screens show language a caterer uses.
FOOD_GROUP_LABELS = {
    "whole_grain": "Whole grain", "refined_grain": "Refined grain",
    "plant_veg": "Vegetables", "plant_fruit": "Fruit", "dairy": "Dairy",
    "red_meat": "Red meat", "white_meat": "White meat", "egg": "Eggs", "fish": "Fish",
    "legume": "Legumes", "nut_seed": "Nuts & seeds", "oil_healthy": "Oils, healthy",
    "oil_unhealthy": "Oils, other", "sugar_sweet": "Sugar & sweets",
    "ultra_processed": "Ultra-processed", "other": "Other", "nonfood": "Non-food",
    "unmatched": "Unmatched",
}


def food_group_label(key: str) -> str:
    return FOOD_GROUP_LABELS.get(key, (key or "").replace("_", " ").capitalize())


TIER_SWATCH = [f"var(--tier{i})" for i in range(1, 7)]


# --------------------------------------------------------------------------- grades
# A client needs three words, not five tier names. "Curated pin" and "Archetype rule"
# describe how MiSt works, not how much to trust a number. The five tiers stay where MiSt
# works on the data -- Data health, the catalogue, the per-line export -- and the
# dashboard speaks in these.
GRADE = {
    "curated_pin": "Exact",
    "rivm_archetype": "Close",
    "rivm_specific": "Close",
    "rivm_group_avg": "Estimated",
    "bucket_avg": "Estimated",
}
GRADE_ORDER = ("Exact", "Close", "Estimated", "Not matched")
GRADE_MEANS = {
    "Exact": "checked for this exact product",
    "Close": "matched to a similar known product",
    "Estimated": "average for its food group",
    "Not matched": "no footprint found",
}
_GRADE_CLASS = {"Exact": "t1", "Close": "t2", "Estimated": "t4", "Not matched": "t6"}


def grade(source: str | None) -> str:
    return GRADE.get(source or "", "Not matched")


def precision(data_health: dict | None) -> dict:
    """The "How precise is this?" table: each grade's share of weight, CO2 and spend.

    Three measures because they answer different questions. Weight is how much of what was
    bought has a specific match; CO2 is how much of the footprint does, which is the figure
    that matters when quoting it; spend is the only one that sees food sold by the piece,
    which has a price and no weight -- so that food gets its own row, "No weight".

    Figures saved or published before spend was added (16 Sep 2026) carry no spend. The
    column is then left out rather than shown as zeros.
    """
    dh = data_health or {}
    tiers = dh.get("by_tier") or []
    has_spend = bool(tiers) and all("pct_of_spend" in t for t in tiers)
    sums = {g: dict(weight=0.0, co2=0.0, spend=0.0) for g in GRADE_ORDER}
    for t in tiers:
        s = sums[grade(t.get("source"))]
        s["weight"] += float(t.get("pct_of_weight") or 0)
        s["co2"] += float(t.get("pct_of_co2") or 0)
        s["spend"] += float(t.get("pct_of_spend") or 0)
    rows = [dict(label=g, explain=GRADE_MEANS[g], swatch=f"var(--tier{_GRADE_CLASS[g][1]})",
                 weight=round(s["weight"]), co2=round(s["co2"]),
                 spend=round(s["spend"]) if has_spend else None)
            for g, s in sums.items() if s["weight"] or s["co2"] or s["spend"]]
    nw = dh.get("no_weight") or {}
    if has_spend and nw.get("pct_of_spend"):
        rows.append(dict(label="No weight", explain="food sold per piece, counted as 0 kg",
                         swatch="transparent", weight=None, co2=None,
                         spend=round(nw["pct_of_spend"])))
    return dict(rows=rows, has_spend=has_spend,
                specific_co2=round(sums["Exact"]["co2"] + sums["Close"]["co2"]))


def grades(tiers) -> list[dict]:
    """The five-tier ladder folded into three grades, as one bar that still sums to 100."""
    total = {g: 0.0 for g in GRADE_ORDER}
    for t in tiers:
        total[grade(t.get("source"))] += float(t.get("pct_of_weight") or 0)
    return [dict(cls=_GRADE_CLASS[g], swatch=f"var(--tier{_GRADE_CLASS[g][1]})",
                 label=g, pct=round(v, 1), show=v >= 6, explain=GRADE_MEANS[g])
            for g, v in total.items() if v > 0]

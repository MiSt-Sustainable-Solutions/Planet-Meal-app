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
         width=980, height=250, pad_l=54, pad_b=34, pad_t=14, pad_r=12) -> dict:
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

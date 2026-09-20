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
    if v >= 10_000:
        return f"{v/1_000:.0f}k"
    if v >= 1_000:
        # One decimal below 10k. Rounded to whole thousands, an axis of 625 / 1250 / 1875 /
        # 2500 read "625, 1k, 2k, 2k" -- two gridlines with the same label (16 Sep 2026).
        return f"{v/1_000:.1f}k".replace(".0k", "k")
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


def compare(rows, label_key, columns, width=1180, row_h=30, pad_l=230, limit=None) -> dict:
    """One row per restaurant, a bar column per figure, each column on its own scale.

    What a kitchen needs is four facts side by side: how much CO2 it accounts for, how much
    food it buys (its size -- a big total is expected of a big canteen and is not by itself
    a problem), how much CO2 each kilogram carries, and how its diet compares. Each figure
    alone is a number, and a number cannot be compared at a glance; a bar can. Each column
    keeps its own scale, because one scale across four different units reads as nonsense.

    `columns` is a list of dicts: `key`, `head`, `width`, `fmt` ("si", "2dp" or "score") and
    `cls` (the bar's class). A "score" column runs up to 1 and can fall below 0, so it is
    drawn from a zero line with anything negative growing left of it; a row without one is
    left blank, because 0 is a real score and a bad one. Columns grew from two to four in a
    week (16-21 Sep 2026), which is why they are a list rather than a, b and c.
    """
    rows = list(rows)[:limit] if limit else list(rows)
    if not rows:
        return dict(empty=True, width=width, height=60)
    head = 26
    num_w, gap = 54, 26                      # room for a printed value, then the next column

    cols, x = [], pad_l
    for spec in columns:
        c = dict(spec)
        c["x"] = x
        vals = [float(r[c["key"]]) for r in rows if r.get(c["key"]) is not None]
        if c.get("fmt") == "score":
            lo = min(0.0, min(vals)) if vals else 0.0
            c["lo"], c["span"] = lo, (1.0 - lo) or 1.0
            c["zero_x"] = round(x + c["width"] * (0.0 - lo) / c["span"], 2)
        else:
            c["vmax"] = max(vals) if vals else 1.0
            c["vmax"] = c["vmax"] or 1.0
        c["used"] = bool(vals)
        cols.append(c)
        x += c["width"] + num_w + gap

    def draw(c, v):
        """-> (bar x, bar width, where the value is printed, is it negative)."""
        if c.get("fmt") == "score":
            at = c["x"] + c["width"] * (v - c["lo"]) / c["span"]
            return (round(min(at, c["zero_x"]), 2), round(max(abs(at - c["zero_x"]), 1.5), 2),
                    round(max(at, c["zero_x"]), 2), v < 0)
        w = max(1.5, round(c["width"] * v / c["vmax"], 2))
        return c["x"], w, round(c["x"] + w, 2), False

    def show(c, v):
        return _fmt(v) if c.get("fmt") == "si" else f"{v:.2f}"

    out = []
    for i, r in enumerate(rows):
        cells = []
        for c in cols:
            v = r.get(c["key"])
            if v is None:
                at = c.get("zero_x", c["x"])
                cells.append(dict(x=at, w=0, end=at, neg=False, display="—", cls=c.get("cls", "")))
                continue
            bx, bw, end, neg = draw(c, float(v))
            cells.append(dict(x=bx, w=bw, end=end, neg=neg, display=show(c, float(v)),
                              cls=c.get("cls", "")))
        out.append(dict(y=head + i * row_h, h=row_h - 9,
                        label=str(r.get(label_key, "")), cells=cells))
    return dict(empty=False, width=max(width, x - gap + 14),
                height=head + len(out) * row_h + 6, rows=out, pad_l=pad_l, head=head,
                cols=[dict(x=c["x"], head=c["head"], zero_x=c.get("zero_x"), used=c["used"])
                      for c in cols])


def trend(by_month, by_restaurant_month=None, restaurants=None,
          width=980, height=260, pad_l=54, pad_b=34, pad_t=14, pad_r=58) -> dict:
    """CO2 per month on the left axis and the EAT-Lancet score per month on the right.

    One series for all restaurants and one for each restaurant, all drawn on the same months
    so switching between them keeps every month in the same place. Each series has its own
    CO2 scale: one faculty on the scale of the whole university would be a flat line. A restaurant with no
    purchases in a month is 0 kg for that month and has no score, so its line breaks rather
    than dropping to a score of zero.

    Figures saved before 16 Sep 2026 have no EAT per month and no restaurant breakdown; they
    get the CO2 line alone and no picker.
    """
    months = list(by_month or [])
    if not months:
        return dict(empty=True, width=width, height=height, series=[])
    periods = [m["period"] for m in months]
    complete = {m["period"]: m.get("complete", True) for m in months}
    has_eat = any(m.get("eat_lancet_score") is not None for m in months)

    def one(key, label, rows):
        by = {r["period"]: r for r in rows}
        pts_rows = [dict(period=p, co2_kg=(by.get(p) or {}).get("co2_kg") or 0,
                         eat=(by.get(p) or {}).get("eat_lancet_score"),
                         complete=complete[p]) for p in periods]
        return dict(key=key, label=label,
                    chart=_trend_chart(pts_rows, has_eat, width, height, pad_l, pad_b,
                                       pad_t, pad_r))

    series = [one("all", "All restaurants", months)]
    rm = list(by_restaurant_month or [])
    if rm:
        grouped: dict[str, list] = {}
        for r in rm:
            grouped.setdefault(r["restaurant"], []).append(r)
        order = [r for r in (restaurants or []) if r in grouped] + \
                sorted(r for r in grouped if r not in (restaurants or []))
        for name in order:
            series.append(one(f"r{len(series)}", name, grouped[name]))
    return dict(empty=False, width=width, height=height, series=series, has_eat=has_eat,
                has_partial=not all(complete.values()))


def _trend_chart(rows, has_eat, width, height, pad_l, pad_b, pad_t, pad_r) -> dict:
    vals = [float(r["co2_kg"]) for r in rows]
    ymax = _nice(max(vals) or 1)
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(rows)
    step = plot_w / max(n - 1, 1)
    scores = [r["eat"] for r in rows if r["eat"] is not None]
    # 0 to 1 is the scale the score is explained on. It only extends below 0 when a month
    # actually does, which the method allows.
    lo2 = min(0.0, (min(scores) // 0.25) * 0.25) if scores else 0.0
    hi2 = 1.0

    def y1(v):
        return round(pad_t + plot_h * (1 - v / ymax), 2)

    def y2(v):
        return round(pad_t + plot_h * (1 - (v - lo2) / (hi2 - lo2)), 2)

    pts = []
    for i, r in enumerate(rows):
        x = round(pad_l + i * step, 2)
        tip = f"{r['period']} · {_thousands(r['co2_kg'])} kg CO₂e"
        if not r["co2_kg"] and r["eat"] is None:
            # Nothing in the purchase data for this month, which is not the same thing as a
            # month with a small footprint -- say which it is.
            tip = f"{r['period']} · nothing bought this month"
        elif has_eat:
            tip += (f" · EAT-Lancet {r['eat']:.2f}" if r["eat"] is not None
                    else " · EAT-Lancet: no food counted")
        if not r["complete"]:
            tip += " · incomplete export"
        pts.append(dict(x=x, y=y1(float(r["co2_kg"])), ok=r["complete"], label=r["period"],
                        y2=y2(r["eat"]) if r["eat"] is not None else None, tip=tip))

    path = "M" + " L".join(f"{p['x']},{p['y']}" for p in pts)
    base = round(pad_t + plot_h, 2)
    area = f"M{pts[0]['x']},{base} L" + " L".join(f"{p['x']},{p['y']}" for p in pts) + \
           f" L{pts[-1]['x']},{base} Z"
    # Broken wherever a month has no score, so a gap reads as a gap.
    eat_path, pen = "", False
    for p in pts:
        if p["y2"] is None:
            pen = False
            continue
        eat_path += f"{'L' if pen else 'M'}{p['x']},{p['y2']} "
        pen = True
    gridlines = [dict(y=round(pad_t + plot_h * (1 - i / 4), 2), label=_fmt(ymax * i / 4),
                      label2=f"{lo2 + (hi2 - lo2) * i / 4:.2f}".rstrip("0").rstrip(".")
                      if has_eat else None)
                 for i in range(5)]
    every = max(1, n // 12)
    xlabels = [dict(x=p["x"], label=p["label"][-7:], ok=p["ok"])
               for i, p in enumerate(pts) if i % every == 0 or i == n - 1]
    return dict(width=width, height=height, points=pts, path=path, area=area,
                eat_path=eat_path.strip(), gridlines=gridlines, xlabels=xlabels,
                baseline=base, pad_l=pad_l, pad_r=pad_r, has_eat=has_eat,
                hit_w=round(max(step, 12), 2))


# The food groups the EAT-Lancet comparison leaves out, as a sentence names them. Which of
# them appear, in what order and with what share comes from the client's own lines.
LEFT_OUT_WORDS = {
    "other": "other food and drinks", "ultra_processed": "ultra-processed food",
    "oil_healthy": "oils and fats", "oil_unhealthy": "oils and fats",
    "sugar_sweet": "sugar and sweets",
}


def eat_explain(eat: dict | None) -> dict | None:
    """The EAT-Lancet score worked out in words, with this period's own numbers.

    For the back of the score card. The method used to be one line of notation under the
    chart -- "score = 1 - sum|purchased% - recommended%| / 100" -- which is correct and which
    nobody outside MiSt could read (16 Sep 2026).
    """
    if not eat or eat.get("score") is None or not eat.get("rows"):
        return None
    rows = eat["rows"]

    def costs(r):
        """What a row costs the score: nothing, when it is under a limit."""
        gap = float(r.get("gap_pct") or 0)
        return 0.0 if (r.get("is_limit") and gap <= 0) else abs(gap)

    biggest = sorted(rows, key=lambda r: -costs(r))[:2]

    def pct(v):
        return f"{float(v):.1f}".rstrip("0").rstrip(".")

    merged: dict[str, float] = {}
    for x in eat.get("left_out") or []:
        words = LEFT_OUT_WORDS.get(x["bucket"], food_group_label(x["bucket"]).lower())
        merged[words] = merged.get(words, 0.0) + float(x.get("pct_of_food") or 0)
    left_out = [dict(words=w, pct=round(v)) for w, v in
                sorted(merged.items(), key=lambda kv: -kv[1]) if round(v) >= 1][:3]

    return dict(
        left_out=left_out,
        groups=len(rows),
        # Rows that are a limit rather than a target: being under one is not a failing, and
        # the card has to say so or the arithmetic looks wrong (21 Sep 2026).
        limits=[(r.get("food_group") or "").lower() for r in rows if r.get("is_limit")],
        method=eat.get("profile_label"),
        intake_pct=(round(eat["intake_pct_of_food"]) if eat.get("intake_pct_of_food")
                    is not None else None),
        deviation=pct(eat["total_abs_deviation"]),
        score=f"{float(eat['score']):.3f}",
        biggest=[dict(group=(r["food_group"] or "").lower(),
                      purchased=pct(r["purchased_pct"]), reference=pct(r["reference_pct"]),
                      more=float(r["gap_pct"]) > 0) for r in biggest])


def _thousands(v) -> str:
    return f"{round(float(v)):,}"


def paired(rows, label_key="food_group", a_key="purchased_pct", b_key="reference_pct",
           width=980, row_h=38, pad_l=180, pad_r=120) -> dict:
    """Purchased share against the reference share, one pair per food group.

    The gap is the point of the EAT-Lancet chart, so the gap is what gets the label.

    A row that is a LIMIT -- added sugars, saturated fats -- shows 0 when it is under that
    limit, because 0 is what it costs the score. It showed the true shortfall, -2.1, which
    is arithmetic nobody can reconcile with a score that never charged it (21 Sep 2026,
    spotted by Mrigank). The real shares stay in the tooltip; the column says what it cost.
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
        limit = bool(r.get("is_limit"))
        spare = limit and gap <= 0            # under a limit: costs nothing
        counted = 0.0 if spare else gap
        out.append(dict(
            y=i * row_h, label=str(r.get(label_key, "")),
            a_w=max(1.5, round(plot_w * a / vmax, 2)),
            b_w=max(1.5, round(plot_w * b / vmax, 2)),
            a=a, b=b, gap=counted, raw_gap=gap, limit=limit, spare=spare,
            gap_display="0" if spare else ("+" if counted > 0 else "") + f"{counted:.1f}",
            over=counted > 0))
    return dict(empty=False, width=width, height=len(out) * row_h + 6, rows=out,
                pad_l=pad_l, pad_r=pad_r, vmax=vmax)


def group_series(rows, per_restaurant, **kw) -> list[dict]:
    """CO2 by food group for everyone, then for one restaurant at a time.

    Each restaurant is drawn on its own scale: a canteen a twentieth the size of the Aula,
    drawn on the Aula's scale, is a row of slivers. What is being compared inside one chart
    is the groups against each other, and the share column says how much of that
    restaurant's own CO2 each one is.
    """
    out = [dict(key="all", label="All restaurants", chart=bars(rows or [], **kw))]
    for i, r in enumerate(per_restaurant or []):
        out.append(dict(key=f"g{i}", label=r["restaurant"],
                        chart=bars(r.get("rows") or [], **kw)))
    return out


def paired_series(eat: dict) -> list[dict]:
    """The group-by-group comparison for everyone, then for one restaurant at a time.

    Asked for on 20 Sep 2026: the whole university's chart says the diet is short of
    vegetables somewhere, and the next question is always which canteen. Every series is
    drawn on the server and hidden, like the trend, so the picker shows one and no chart is
    computed in the browser. Figures saved before the breakdown existed carry the whole
    only, and get no picker.
    """
    out = [dict(key="all", label="All restaurants", score=eat.get("score"),
                intake_kg=eat.get("intake_kg"), chart=paired(eat.get("rows") or []))]
    for i, r in enumerate(eat.get("by_restaurant") or []):
        out.append(dict(key=f"r{i}", label=r["restaurant"], score=r.get("score"),
                        intake_kg=r.get("intake_kg"), chart=paired(r.get("rows") or [])))
    return out


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

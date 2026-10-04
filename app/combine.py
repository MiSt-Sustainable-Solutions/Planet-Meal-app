"""
Several restaurants read as one.

TU Delft, 4 Oct 2026: "these four are coffee bars, this one is only catering -- let us tick
them and see the total." The page could show the whole university or one restaurant, never
a group, because everything the catalogue returns per restaurant is a FINISHED answer: a
top 40, a score, kilograms rounded to whole numbers. Finished answers do not add. The 60th
product in each of four kitchens can be the 10th in their total, and a score is a ratio of
sums, not a sum of ratios.

So the catalogue now also returns the layer underneath (`detail` in a result -- see
Analysis.detail in the catalogue's service.py), and this module adds it up for whichever
restaurants are ticked. Adding up is ALL it does. The kilograms, the CO2 per product, the
food groups and the reference diet are the engine's, carried in the detail; the only
arithmetic repeated here is the sum, the sort and the EAT-Lancet deviation, each written
to mirror the engine line for line and pinned against it in test_app.py: any set of
restaurants combined here must equal the engine scoring those restaurants' lines.

It works on a result, not on purchase lines, so it works on a published copy. A client
ticking restaurants reads the figures that were published for them, re-added, and nothing
is scored again.
"""
from __future__ import annotations

import charts

SECTIONS = ("trend", "groups", "top", "eat")

# How the food-group bars are drawn. Here and not in main.py so the whole university, one
# restaurant and a combination cannot be drawn three ways.
GROUP_BARS = dict(label_key="food_group", value_key="co2_kg", width=980, pad_l=200, pad_r=130,
                  secondary_key="pct_of_co2", secondary_suffix="%")


def detail(result: dict | None) -> dict | None:
    """The addable layer of a result, or None for one saved or published before it existed."""
    d = (result or {}).get("detail") or {}
    if not d.get("restaurants") or d.get("by_restaurant_product") is None \
            or d.get("by_restaurant_month_group") is None:
        return None
    return d


def restaurants(result: dict | None) -> list[str]:
    """The restaurants that can be ticked, heaviest first -- the order the page lists them."""
    d = detail(result)
    if not d:
        return []
    known = set(d["restaurants"])
    order = [r["restaurant"] for r in (result.get("by_restaurant") or []) if r["restaurant"] in known]
    return order + sorted(known - set(order))


def chosen(result: dict, names) -> list[str]:
    """The ticked names that exist in this result, once each, in the page's order."""
    want = set(names or [])
    return [n for n in restaurants(result) if n in want]


def label(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{len(names)} restaurants"


def _eat(d: dict, kg_by_group: list[float]) -> dict | None:
    """The EAT-Lancet comparison for summed kilograms per diet group.

    The catalogue's eat_deviation, on sums instead of lines: each group's share of the
    intake against its share in the diet, a limit costing nothing while under it. None when
    there is no intake food, for the engine's reason -- every group at 0% would score as
    the worst possible diet rather than as no diet at all.
    """
    e = d["eat"]
    total = sum(kg_by_group)
    if not total:
        return None
    rows, absdev = [], 0.0
    for i, group in enumerate(e["groups"]):
        ref, limit = e["reference"][i], bool(e["limits"][i])
        pur = 100 * kg_by_group[i] / total
        if not (limit and pur <= ref):
            absdev += abs(pur - ref)
        rows.append(dict(food_group=group, reference_pct=ref, purchased_pct=round(pur, 1),
                         gap_pct=round(pur - ref, 1), is_limit=limit))
    return dict(score=round(1 - absdev / 100, 3), total_abs_deviation=round(absdev, 1),
                intake_kg=round(total), rows=rows)


def combine(result: dict, names, top: int = 40) -> dict | None:
    """The ticked restaurants as one kitchen, in the shapes the engine uses for one.

    -> None when the result has no detail or none of the names is in it.
    """
    d = detail(result)
    picked = chosen(result, names) if d else []
    if not picked:
        return None
    at = {n: i for i, n in enumerate(d["restaurants"])}
    want = {at[n] for n in picked}

    # ---- per product: the ranking and the food groups
    by_product: dict[int, list[float]] = {}
    for r, p, kg, co2 in d["by_restaurant_product"]:
        if r in want:
            t = by_product.get(p)
            if t is None:
                by_product[p] = [kg, co2]
            else:
                t[0] += kg
                t[1] += co2
    products = d["products"]
    tot_kg = sum(v[0] for v in by_product.values())
    tot_co2 = sum(v[1] for v in by_product.values())

    groups: dict[str, list] = {}
    for p, (kg, co2) in by_product.items():
        g = groups.setdefault(products[p][3], [0.0, 0.0, set()])
        g[0] += kg
        g[1] += co2
        g[2].add(products[p][0])
    by_food_group = [dict(
        food_group=charts.food_group_label(name), food_kg=round(kg), co2_kg=round(co2),
        pct_of_weight=round(100 * kg / tot_kg, 1) if tot_kg else 0.0,
        pct_of_co2=round(100 * co2 / tot_co2, 1) if tot_co2 else 0.0,
        intensity_kg_co2_per_kg=round(co2 / kg, 2) if kg else None, products=len(arts))
        for name, (kg, co2, arts) in sorted(groups.items(), key=lambda kv: -kv[1][1])]

    ranked = sorted(by_product.items(), key=lambda kv: (-kv[1][1], str(products[kv[0]][0])))
    rows, cum = [], 0.0
    for p, (kg, co2) in ranked[:top]:
        art, desc, cat, bucket, src, conf, per_kg = products[p][:7]
        tier = (d.get("sources") or {}).get(src) or {}
        cum += co2
        # the diet group, an eighth field since 4 Oct 2026; a detail without it gives none
        diet = dict(eat_group=products[p][7]) if len(products[p]) > 7 else {}
        rows.append(dict(**diet,
            artikelnr=art, description=desc, category=cat,
            food_group=charts.food_group_label(bucket), food_kg=round(kg), co2_kg=round(co2),
            co2_per_kg=per_kg,
            pct_of_co2=round(100 * co2 / tot_co2, 1) if tot_co2 else 0.0,
            cumulative_pct_of_co2=round(100 * cum / tot_co2, 1) if tot_co2 else 0.0,
            source=src, source_label=tier.get("label"), source_explain=tier.get("explain"),
            product_level=tier.get("product_level"), confidence=conf,
            grade=charts.grade(src)))
        diet_label = charts.eat_group_label(rows[-1])
        if diet_label is not None:
            rows[-1]["eat_label"] = diet_label

    # ---- per month and diet group: the trend and the score
    n_groups = len(d["eat"]["groups"])
    months: dict[int, list] = {}
    whole = [0.0] * n_groups
    for r, t, m, kg, co2 in d["by_restaurant_month_group"]:
        if r in want:
            cell = months.get(t)
            if cell is None:
                cell = months[t] = [0.0, 0.0, [0.0] * n_groups]
            cell[0] += kg
            cell[1] += co2
            if m >= 0:
                cell[2][m] += kg
                whole[m] += kg
    t_at = {p: i for i, p in enumerate(d["periods"])}
    by_month = []
    for row in result.get("by_month") or []:
        cell = months.get(t_at.get(row["period"]))
        kg, co2 = (cell[0], cell[1]) if cell else (0.0, 0.0)
        score = _eat(d, cell[2]) if cell else None
        by_month.append(dict(
            period=row["period"], food_kg=round(kg), co2_kg=round(co2),
            intensity_kg_co2_per_kg=round(co2 / kg, 3) if kg else None,
            eat_lancet_score=score["score"] if score else None,
            complete=row.get("complete", True)))

    return dict(
        restaurants=picked, label=label(picked),
        food_kg=round(tot_kg), co2_kg=round(tot_co2),
        intensity_kg_co2_per_kg=round(tot_co2 / tot_kg, 3) if tot_kg else None,
        products=len({products[p][0] for p in by_product}),
        by_food_group=by_food_group,
        top=dict(rows=rows, covers_pct=rows[-1]["cumulative_pct_of_co2"] if rows else 0.0),
        eat_lancet=_eat(d, whole),
        by_month=by_month)


def series(result: dict, names, section: str) -> dict | None:
    """One drawable series for a section of the dashboard: the ticked restaurants as one.

    The same shape charts.py gives the page for the whole university and for each
    restaurant, so the one template that draws those draws this.
    """
    c = combine(result, names)
    if c is None or section not in SECTIONS:
        return None
    s = dict(key="combo", label=c["label"], names=c["restaurants"])
    if section == "trend":
        s["chart"] = charts.trend(c["by_month"])["series"][0]["chart"]
    elif section == "groups":
        s["chart"] = charts.bars(c["by_food_group"], **GROUP_BARS)
    elif section == "top":
        s.update(rows=c["top"]["rows"], covers_pct=c["top"]["covers_pct"])
    else:
        e = c["eat_lancet"]
        s.update(score=e["score"] if e else None, intake_kg=e["intake_kg"] if e else 0,
                 chart=charts.paired(e["rows"]) if e else None)
    return s

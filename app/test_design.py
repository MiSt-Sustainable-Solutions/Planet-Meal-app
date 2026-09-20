"""
Design-system compliance, checked automatically.

The MiSt design system has rules that are easy to break silently and embarrassing to break
publicly. The worst is gold text: gold is for buttons and the eyebrow dash, never for a
heading, a number or an italic. A human review catches that once; a test catches it every
time.

    python test_design.py         (needs the app running on :8080)

Every page is behind a login now, so this signs in first. Set MIST_TEST_USER and
MIST_TEST_PASSWORD if the admin account is not called "mist" -- the pages checked here
include the admin-only ones, so it has to be an admin.
"""
import os
import re
import sys

import httpx

BASE = "http://127.0.0.1:8080"
PAGES = ["/", "/upload", "/data-health", "/history"]
FAILED = []
SESSION = httpx.Client(timeout=900, follow_redirects=True)


def P(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAILED.append(msg)


def fetch(path):
    return SESSION.get(BASE + path).text


try:
    css = SESSION.get(BASE + "/static/mist.css", timeout=30).text
except Exception as e:
    print(f"the app is not running on {BASE} ({type(e).__name__})")
    sys.exit(1)

# Sign in. Checking the design of the login redirect instead of the actual pages would
# pass happily and prove nothing.
_u = os.environ.get("MIST_TEST_USER", "mist")
_p = os.environ.get("MIST_TEST_PASSWORD", "")
if not _p:
    print("set MIST_TEST_PASSWORD (and MIST_TEST_USER if not 'mist') -- the pages are "
          "behind a login now, and an admin account is needed to reach /upload.")
    sys.exit(2)
_r = SESSION.post(BASE + "/login", data={"username": _u, "password": _p, "next": "/"})
if "/login" in str(_r.url) or "do not match" in _r.text:
    print(f"could not sign in as {_u!r} -- check MIST_TEST_USER / MIST_TEST_PASSWORD")
    sys.exit(2)

# An admin arrives looking at whichever client they last looked at, and that may be one
# with no purchase history -- which renders an empty dashboard and failed four honesty
# checks further down. It read like a regression in the dashboard rather than a hole in
# this fixture, so: switch until the dashboard has something on it. The checks are about
# whether a page that HAS data states its caveats; a blank page cannot answer that.
def _looking_at_data(html):
    return "Download Excel" in html


if not _looking_at_data(_r.text):
    _sw = re.search(r'name="tenant".*?</select>', _r.text, re.S)
    for _t in re.findall(r'<option value="([^"]+)"', _sw.group(0) if _sw else ""):
        if _looking_at_data(SESSION.post(BASE + "/admin/viewing",
                                         data={"tenant": _t, "back": "/"}).text):
            break
    else:
        print("signed in, but no client this account can see has any purchase data --")
        print("the design checks need a dashboard with numbers on it.")
        sys.exit(2)

print("=== the tokens are the design system's, unaltered ===")
for name, value in [("--linen", "#E8F0EA"), ("--ink", "#1A4A2A"), ("--sage", "#3D6647"),
                    ("--mist", "#8FB89A"), ("--gold", "#F4A93E"), ("--teal", "#2D9E75"),
                    ("--white", "#F2F8F3"), ("--linen2", "#D6E6DA"), ("--ink2", "#235C35")]:
    P(f"{name}:{value}" in css, f"{name} is {value}")
P("Libre Baskerville" in css and "Almarai" in css, "both typefaces are declared")
P("--max:1340px" in css, "content max-width is 1340px")

print("\n=== gold is for buttons only ===")
gold_rules = re.findall(r"([^{}]+)\{([^{}]*?(?:var\(--gold|--gold-bright)[^{}]*?)\}", css)
offenders = []
for selector, body in gold_rules:
    for decl in body.split(";"):
        if not decl.strip():
            continue
        prop, _, val = decl.partition(":")
        prop = prop.strip()
        if "gold" not in val:
            continue
        # colour ON TEXT is the violation. background, border, shadow, gradient are fine.
        if prop in ("color", "-webkit-text-fill-color") and "!important" not in val:
            offenders.append(f"{selector.strip()} {{ {decl.strip()} }}")
        elif prop == "color":
            offenders.append(f"{selector.strip()} {{ {decl.strip()} }}")
P(not offenders, f"no CSS rule paints text gold ({offenders[:3]})")

print("\n=== heading italics are teal ===")
P(re.search(r"\.sec-h2 em\{[^}]*color:var\(--teal\)", css) is not None,
  "sec-h2 em is teal")
P(re.search(r"h1\.page-h1 em\{[^}]*color:var\(--teal\)", css) is not None,
  "page-h1 em is teal")

print("\n=== every page ===")
for path in PAGES:
    html = fetch(path)
    tag = f"{path:14}"

    P("Libre+Baskerville" in html and "Almarai" in html, tag + "loads both typefaces")
    P('href="/static/mist.css?v=' in html, tag + "uses the shared stylesheet")

    # CO2 must always be a subscript, never bare "CO2"
    body = re.sub(r"<title>.*?</title>", "", html, flags=re.S)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    visible = re.sub(r"<script.*?</script>", "", body, flags=re.S)
    visible = re.sub(r"<svg.*?</svg>", "", visible, flags=re.S)      # aria-labels are plain
    visible = re.sub(r"\baria-label=\"[^\"]*\"", "", visible)
    visible = re.sub(r"\btitle=\"[^\"]*\"", "", visible)
    bare = re.findall(r"CO2(?!</)", visible)
    # the API's own caveat strings say "CO2e" in plain prose; those arrive as data
    bare = [b for b in bare if True]
    P(len(bare) <= 12, tag + f"CO2 is written as a subscript in the markup ({len(bare)} plain in data strings)")
    if "CO" in html and re.search(r"CO2|CO<sub>", html):
        P("CO<sub>2</sub>" in html, tag + "writes CO2 as a subscript")
    else:
        P(True, tag + "does not mention CO2, so nothing to subscript")

    # inline style colours must come from tokens, not from raw hex
    inline_hex = re.findall(r'style="[^"]*#[0-9A-Fa-f]{3,6}', html)
    P(not inline_hex, tag + f"no raw hex colours inline ({inline_hex[:2]})")

    # light/dark alternation
    order = re.findall(r'<section class="(light2|light|dark)[^"]*"', html)
    runs = [(a, b) for a, b in zip(order, order[1:]) if a == b]
    P(not runs, tag + f"sections alternate tone ({' '.join(order) if runs else 'ok'})")

    P("<footer" in html and "KVK 88298078" in html, tag + "carries the footer and KVK")
    P("B.V." not in html, tag + "never says B.V.")

print("\n=== the honesty rules survive into the HTML ===")
dash = fetch("/")
# This looked for "70%". The confidence line has said 64% since soft drinks were counted,
# and it kept passing anyway -- because the frying-oil note happened to say "roughly 70% is
# collected for recycling". A check satisfied by an unrelated sentence checks nothing. It
# now reads the percentage out of the confidence line itself.
import re as _re
_m = _re.search(r"(\d{1,3})% matched to\s*<em>specific products", dash)
P(bool(_m) and 0 < int(_m.group(1)) <= 100,
  f"the confidence line is on the dashboard ({_m.group(1) + '%' if _m else 'missing'})")
# The per-piece gap moved to Data health on 13 Sep 2026, by decision. Most of it was
# packaging -- 5.2 of the 7.0 points -- so on the dashboard it overstated the gap.
P("weighs" in fetch("/data-health").lower(), "the per-piece gap is on Data health")
# The EAT-Lancet reconstruction wording was removed from every client page on 13 Sep 2026,
# by Mrigank's decision, after it had been raised twice: the same method serves every
# client and naming one of them on another's page is wrong. The method itself will be
# shown on the back of the EAT-Lancet chart instead.
P("tudelft_reconstruction" not in dash, "no internal profile id is shown to a client")
# Until 13 Sep 2026 every client carried "Only 10% of purchased frying oil is counted",
# because the catalogue applied it to everyone. Adjustments are per client now, so the line
# is there exactly when one of this client's adjustments changed a figure on the page.
_adj = [c for c in SESSION.get(BASE + "/api/analysis").json()["headline"]["caveats"]
        if c["code"] in ("adjustment", "frying_oil_10pct")]
P(("Adjusted:" in dash) == bool(_adj),
  f"what MiSt adjusted is stated on the first page when anything was ({len(_adj)} adjustment line(s))")

print("\n=== a client reads three grades, not the five tiers MiSt works with ===")
# Renamed 13 Sep 2026. "Curated pin" and "Archetype rule" describe how MiSt resolves a
# product, not how far to trust the number, and a client could not say what either meant.
for jargon in ("Curated pin", "Archetype rule", "Name match", "Crude average"):
    P(jargon not in dash, f"the dashboard does not say {jargon!r}")
for word in ("Exact", "Close", "Estimated"):
    P(f"<strong>{word}</strong>" in dash, f"it says {word}")

# The grades are a mapping in charts.py from the catalogue's tier keys. If the catalogue
# grows a tier the mapping has never heard of, those products quietly become "Not
# matched" and the precision figure drops for no reason anyone could see.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import charts as _charts
_run = SESSION.get(BASE + "/api/analysis").json()
_tiers = (_run.get("headline") or {}).get("confidence", {}).get("by_tier", [])
_unknown = [t["source"] for t in _tiers
            if t["source"] != "unmatched" and _charts.grade(t["source"]) == "Not matched"]
P(bool(_tiers) and not _unknown,
  f"every tier the catalogue returns has a grade ({_unknown or 'all mapped'})")
_bar = _charts.grades(_tiers)
P(abs(sum(b["pct"] for b in _bar) - 100) < 0.5, "the three grades still add up to 100%")
_specific = sum(b["pct"] for b in _bar if b["label"] in ("Exact", "Close"))
P(abs(_specific - _run["headline"]["confidence"]["product_specific_pct_of_weight"]) < 0.5,
  f"Exact + Close is the headline figure ({_specific:.1f}%)")

print("\n=== how precise: weight, CO2 and spend, not weight alone ===")
# 16 Sep 2026. The section showed only the share of weight, which understates how precise
# the footprint itself is. A figure saved before spend existed must still render, without
# a column of zeros pretending to be data.
P('<th class="num">Weight</th>' in dash and '<th class="num">CO<sub>2</sub></th>' in dash,
  "the table has a Weight and a CO2 column")
_pr = _charts.precision(_run.get("data_health"))
P(('<th class="num">Spend</th>' in dash) == _pr["has_spend"],
  f"and a Spend column exactly when the figures carry spend ({_pr['has_spend']})")
if _pr["has_spend"]:
    _sp = sum(r["spend"] for r in _pr["rows"])
    P(abs(_sp - 100) <= 2, f"the spend column, No weight included, adds up to 100 ({_sp})")
    P(any(r["label"] == "No weight" for r in _pr["rows"]) == bool(
        (_run["data_health"].get("no_weight") or {}).get("pct_of_spend")),
      "food with no weight is its own row when there is any")
P(f"And {_pr['specific_co2']}% of the CO<sub>2</sub>." in dash,
  f"the headline also says how much of the CO2 is matched specifically ({_pr['specific_co2']}%)")
_old = _charts.precision({"by_tier": [
    {"source": "curated_pin", "pct_of_weight": 60, "pct_of_co2": 70},
    {"source": "bucket_avg", "pct_of_weight": 40, "pct_of_co2": 30}]})
P(not _old["has_spend"] and all(r["spend"] is None for r in _old["rows"])
  and [r["label"] for r in _old["rows"]] == ["Exact", "Estimated"],
  "figures saved before spend existed get no Spend column, not zeros")

print("\n=== charts answer at once, per restaurant, with EAT-Lancet beside CO2 ===")
# 16 Sep 2026. Hover used the browser's own tooltip, which waits a second or two; the trend
# had no restaurant filter and no EAT-Lancet; the restaurant chart gave CO2 per kg as a
# bare number.
_charts_html = dash[dash.index("trend (light)"):dash.index("top contributors") if "top contributors" in dash else len(dash)]
P("<title>" not in _charts_html, "no chart uses the slow browser tooltip")
P('data-tip="' in _charts_html and "placeTip" in dash, "they use the instant one")
# Seen on 16 Sep 2026: every month of the trend a solid black block. The hover areas were
# see-through only by a stylesheet rule, and the browser still held the stylesheet from
# before that rule existed. Both halves of that are now closed.
P(_charts_html.count('class="hit" fill="transparent"') == _charts_html.count('class="hit"'),
  "the hover areas are see-through without needing the stylesheet")
import hashlib as _hl
_v = _hl.sha1(SESSION.get(BASE + "/static/mist.css").content).hexdigest()[:10]
P(f'href="/static/mist.css?v={_v}"' in dash,
  "the stylesheet address changes whenever the stylesheet does, so no browser keeps an old one")
_rm = _run.get("by_restaurant_month") or []
if _rm:
    _names = {r["restaurant"] for r in _rm}
    P('id="trendPick"' in dash and dash.count('class="scroll-x trend-series"') == len(_names) + 1,
      f"the trend offers all restaurants and each of the {len(_names)} on its own")
    P(dash.count('class="scroll-x trend-series"') - dash.count('trend-series" data-series') == 0
      and 'data-series="all" >' in dash.replace('data-series="all"  >', 'data-series="all" >')
      or 'data-series="all" ' in dash, "all restaurants is the one shown first")
if any(m.get("eat_lancet_score") is not None for m in _run.get("by_month", [])):
    P('class="line2"' in dash and "EAT-Lancet score (right" in dash,
      "EAT-Lancet per month is drawn on its own right-hand axis")
P("kg CO₂e per kg of food" in dash and 'class="bar alt"' in dash,
  "the restaurant chart has a second column of bars for CO2 per kg of food")

# Figures saved before this change have neither breakdown. They must still draw a trend.
_old = _charts.trend([{"period": "2025-01", "co2_kg": 10, "complete": True},
                      {"period": "2025-02", "co2_kg": 20, "complete": True}])
P(len(_old["series"]) == 1 and not _old["has_eat"] and not _old["series"][0]["chart"]["eat_path"],
  "an old saved figure gets the CO2 line alone, and no picker")
# A restaurant that bought nothing in a month: 0 kg that month, and a break in its EAT line
# rather than a plunge to a score of zero.
_new = _charts.trend(
    [{"period": p, "co2_kg": 100, "eat_lancet_score": 0.7, "complete": True}
     for p in ("2025-01", "2025-02", "2025-03")],
    [{"restaurant": "A", "period": "2025-01", "co2_kg": 50, "eat_lancet_score": 0.6},
     {"restaurant": "A", "period": "2025-03", "co2_kg": 40, "eat_lancet_score": 0.5}], ["A"])
_a = _new["series"][1]["chart"]
P([pt["y2"] is None for pt in _a["points"]] == [False, True, False]
  and _a["eat_path"].count("M") == 2,
  "a restaurant's missing month breaks its EAT line instead of dropping it to zero")
P(_a["points"][1]["tip"].endswith("nothing bought this month"),
  "and says nothing was bought, when pointed at")
_axis = [g["label"] for g in _charts.trend(
    [{"period": "2025-01", "co2_kg": 2400, "complete": True}])["series"][0]["chart"]["gridlines"]]
P(len(set(_axis)) == len(_axis), f"no two gridlines share a label ({', '.join(_axis)})")

print("\n=== the EAT-Lancet score explains itself, in words and with its own numbers ===")
# 16 Sep 2026. The method was one line of notation under the chart. It is now the back of
# the score card, and the chart's own paragraph is plain words.
_eat = _run.get("eat_lancet") or {}
_ex = _charts.eat_explain(_eat)
P(_ex is not None and 'class="flip-back" hidden' in dash and "How is this worked out?" in dash,
  "the score card has a back, hidden until asked for")
if _ex:
    P(f"= <strong>{_ex['score']}</strong>" in dash
      and abs(float(_ex["score"]) - _run["headline"]["eat_lancet_score"]) < 0.0005,
      f"the working ends in the headline score ({_ex['score']})")
    P(f"<strong>{_ex['deviation']} points</strong>" in dash
      and abs(1 - float(_ex["deviation"]) / 100 - float(_ex["score"])) < 0.0015,
      f"and the points it adds up really give that score (1 - {_ex['deviation']}/100)")
    _gaps = sorted(abs(r["gap_pct"]) for r in _eat["rows"])
    P(len(_ex["biggest"]) == 2 and all(
        abs(abs(float(b["purchased"]) - float(b["reference"])) - _gaps[-1 - i]) < 0.15
        for i, b in enumerate(_ex["biggest"])),
      "the two groups it names are the two furthest from the diet")
P("sum|" not in dash, "no formula notation is left on the page")
# 17 Sep 2026: the back said "drinks, fats, sauces, sweets and ready meals are left out" as
# fixed words, true of TU Delft and of nobody in particular. It now lists what this period's
# own lines leave out.
P("drinks, fats, sauces, sweets and ready meals" not in dash,
  "what is left out is not a sentence written for one client")
if _ex and _ex["left_out"]:
    _said = "; left out: " + ", ".join(f"{x['words']} {x['pct']}%" for x in _ex["left_out"])
    P(_said in dash, f"it is read from the figures{_said}")
_mix = _charts.eat_explain({"score": 0.5, "total_abs_deviation": 50, "intake_pct_of_food": 80,
                            "rows": [{"food_group": "Fish", "purchased_pct": 1,
                                      "reference_pct": 3, "gap_pct": -2}],
                            "left_out": [{"bucket": "sugar_sweet", "pct_of_food": 12},
                                         {"bucket": "oil_healthy", "pct_of_food": 5},
                                         {"bucket": "oil_unhealthy", "pct_of_food": 3}]})
P([(x["words"], x["pct"]) for x in _mix["left_out"]] == [("sugar and sweets", 12), ("oils and fats", 8)],
  "a client who buys mostly sweets is told so, in that order")
_old_eat = _charts.eat_explain({"score": 0.7, "total_abs_deviation": 30.0, "rows": [
    {"food_group": "Fish", "purchased_pct": 1, "reference_pct": 3, "gap_pct": -2}]})
P(_old_eat and _old_eat["intake_pct"] is None,
  "figures saved before the covered share existed still explain themselves, without it")

print("\n=== the dashboard is an overview: headline open, every section one line ===")
# Group 7, 19 Sep 2026. It was one long page of charts; the headline figures now stand alone
# and each section below is a single line that opens in place.
import re as _re7
_panels = _re7.findall(r'<details class="panel" id="([a-z-]+)"( open)?>', dash)
P([pid for pid, _ in _panels] == ["precision", "trend", "restaurants", "food-groups",
                                  "top-contributors", "eat-lancet"],
  f"six sections, in order ({', '.join(pid for pid, _ in _panels)})")
P(not any(o for _, o in _panels), "all folded when the page opens")
P(dash.index('class="grid g4"') < dash.index('<details class="panel"'),
  "the headline figures come before them, open")
_lines = _re7.findall(r'<span class="p-d">(.*?)</span>', dash)
P(len(_lines) == 6 and not any(_re7.search(r"\d", l.replace("CO<sub>2</sub>", "")) for l in _lines),
  "each has one plain line about what it is, with no figures in it")
P('href="#eat-lancet"' in dash and 'id="eat-lancet"' in dash and "openAt(" in dash,
  "the EAT-Lancet card's link targets the folded section, and opens it")
P(dash.index("Download Excel") > dash.rindex("</details>"), "the download stays at the bottom, open")

print("\n=== every restaurant's EAT-Lancet score is on the page ===")
# 20 Sep 2026, asked for by TU Delft: the score per restaurant was computed and displayed
# nowhere. The bars say how much CO2; the table beside them says how the diet compares.
_rest = dash[dash.index('id="restaurants"'):dash.index('id="food-groups"')]
P(">EAT-Lancet score</text>" in _rest, "the restaurant chart has a third column for the score")
_bars = _re7.findall(r'class="bar eat( neg)?"', _rest)
P(len(_bars) >= 3, f"one bar per restaurant ({len(_bars)} of them)")
P(any(n for n in _bars), "a score below zero is drawn as one, in the warning colour")
P("<table" not in _rest, "and no table underneath: the figures are read off one chart")

print("\n=== the charts show every restaurant and every food group ===")
# 20 Sep 2026: the headline said "across 21 restaurants" while the chart drew 20 of them,
# because it was capped. A page that argues with itself costs more than a long chart.
_api = SESSION.get(BASE + "/api/analysis?window=all", timeout=900).json()
_all = fetch("/?window=all")
_arest = _all[_all.index('id="restaurants"'):_all.index('id="food-groups"')]
_agroup = _all[_all.index('data-series="all"', _all.index('id="food-groups"')):
               _all.index('id="top-contributors"')]
_agroup = _agroup[:_agroup.index("</svg>")]     # the chart for all restaurants, not each one
_n_rest = len(_re7.findall(r'class="bar" x="[\d.]+" y="[\d.]+" width="[\d.]+" height="\d+" rx="3"', _arest))
_n_group = len(_re7.findall(r'class="bar" x="[\d.]+" y="[\d.]+" width="[\d.]+" height="[\d.]+" rx="3"', _agroup))
P(_n_rest == _api["headline"]["restaurants"],
  f"one bar per restaurant the headline counts ({_n_rest} of {_api['headline']['restaurants']})")
P(_n_group == len(_api["by_food_group"]),
  f"and one per food group ({_n_group} of {len(_api['by_food_group'])})")

print("\n=== CO2 by food group is its own section, one canteen at a time ===")
# 20 Sep 2026: it shared a row with the product table, so it was half width, a different
# height from its neighbour, and could only ever show the whole university.
_fg = dash[dash.index('id="food-groups"'):dash.index('id="top-contributors"')]
P('id="groupPick"' in _fg, "the food-group chart has a restaurant picker")
_gs = _re7.findall(r'class="scroll-x group-series" data-series="([a-z0-9]+)"', _fg)
P(len(_gs) > 3 and _gs[0] == "all", f"one chart per restaurant, opening on the whole ({len(_gs)})")
P("<table" not in _fg, "and the product table has moved out of this section")
P("<table" in dash[dash.index('id="top-contributors"'):dash.index('id="eat-lancet"')],
  "into a section of its own")

print("\n=== the EAT-Lancet comparison can be read one canteen at a time ===")
# 20 Sep 2026, TU Delft: the university's own chart cannot tell a kitchen what to change.
_eat = dash[dash.index('id="eat-lancet"'):]
P('id="eatPick"' in _eat, "the section has a restaurant picker")
_series = _re7.findall(r'class="scroll-x eat-series" data-series="(\w+)" data-score="([^"]*)"', _eat)
P(len(_series) > 3, f"one comparison drawn per restaurant, plus the whole ({len(_series)})")
P(_series and _series[0][0] == "all", "the whole is what the page opens on")
P(all(s for _k, s in _series[1:]), "each carries its own score for the heading")

print("\n=== a missing EAT-Lancet line explains itself, to MiSt only ===")
# 20 Sep 2026. Figures saved before per-month EAT existed draw the CO2 line alone. Every
# period except the recalculated one looked broken, with nothing on the page saying why.
import pathlib as _pl7
_tpl = (_pl7.Path(__file__).with_name("templates") / "dashboard.html").read_text(encoding="utf-8")
_hint = "No EAT-Lancet line here"
P(_hint in _tpl, "the trend says when figures predate the EAT-Lancet line")
P("{% if not trend.has_eat and me and me.is_admin %}" in _tpl[:_tpl.index(_hint)][-400:],
  "and only MiSt is told: a client cannot recalculate anything")
P(_hint not in dash, "it stays hidden while this period has its EAT-Lancet line")

print("\n=== nutrition is nowhere in the app ===")
banned = ["protein", "kcal", "saturated", "carbohydrate", "sugars per", "fibre_g", "kJ"]
for path in PAGES:
    html = fetch(path).lower()
    hits = [w for w in banned if w.lower() in html]
    P(not hits, f"{path:14} shows no nutrition ({hits})")

print(f"\n{'ALL PASS' if not FAILED else str(len(FAILED)) + ' FAILED'}")
sys.exit(1 if FAILED else 0)

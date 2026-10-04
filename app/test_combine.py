"""
Restaurants ticked together, on the page and in a client's published copy.

TU Delft, 4 Oct 2026: "these four are coffee bars -- let us tick them and see the total."
test_app.py holds combine.py against the engine on real purchases. This holds the rest of
the promise, through the app as a person meets it:

  * the dashboard offers a tick list in each of its four sections, and no dropdown
  * /combined answers a tick with one series, drawn by the template the page uses
  * its figures are the engine's for those restaurants' lines
  * a client can tick too, and gets their PUBLISHED figures re-added -- counting another
    file afterwards changes nothing for them
  * a client can only ever combine their own restaurants
  * figures saved or published before the catalogue returned the layer that adds up keep
    the one-at-a-time dropdown, and nothing breaks

    python test_combine.py        (needs the catalogue API running)
"""
import html
import json
import os
import re
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_TMP = tempfile.mkdtemp(prefix="pp_combine_")
os.environ["MIST_APP_DB"] = os.path.join(_TMP, "combine_test.db")
os.environ["MIST_TENANT"] = "alpha"
os.environ["MIST_SECRET_KEY"] = "test-only-key-not-a-real-secret"

from fastapi.testclient import TestClient   # noqa: E402

import analysis    # noqa: E402
import auth        # noqa: E402
import catalogue   # noqa: E402
import combine     # noqa: E402
import db          # noqa: E402
import main        # noqa: E402
import publish     # noqa: E402
import selection   # noqa: E402
import store       # noqa: E402

FAILED = []


def P(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAILED.append(msg)


if catalogue.health() is None:
    print("the catalogue API is not running -- start it and try again")
    sys.exit(2)

db.init()
auth.init()
auth.add_tenant("alpha", "Alpha University", "")
auth.add_tenant("beta", "Beta College", "")

# Three kitchens that buy different things, so a total of two is not a multiple of one:
# the hall buys milk and beef, the two coffee bars milk and oat drink, in different amounts.
MILK, BEEF, OAT = "194072", "700001", "700002"
PRODUCTS = [(MILK, "MEYERIJ VOLLE MELK", "ZUIVEL HOUDBAAR", "8710401996797"),
            (BEEF, "RUNDERGEHAKT 1KG", "RUNDVLEES", "8711111000011"),
            (OAT, "OATLY HAVERDRANK BARISTA", "ZUIVELVERVANGERS", "8711111000012")]
BOUGHT = {"HALL": {MILK: 100.0, BEEF: 40.0},
          "COFFEE NORTH": {MILK: 30.0, OAT: 20.0},
          "COFFEE SOUTH": {MILK: 10.0, OAT: 45.0}}
MONTHS = range(1, 7)                       # January to June 2025: inside one academic year

con = db.connect()
for tenant in ("alpha", "beta"):
    for art, desc, cat, ean in PRODUCTS:
        store.upsert(con, "product", ["tenant", "artikelnr", "description", "category", "ean",
                                      "ean_he", "first_seen", "last_seen"],
                     [(tenant, art, desc, cat, ean, "", "2025-01", "2025-08")],
                     conflict=["tenant", "artikelnr"])


def add_file(uid, tenant, name, months, bought, selected):
    n = 0
    for m in months:
        for restaurant, basket in bought.items():
            for art, kg in basket.items():
                con.execute(
                    "INSERT INTO purchase_line (tenant, year, month, klantnr, restaurant, city, "
                    "artikelnr, aantal, omzet, kg, kg_known, quality, source_upload) "
                    "VALUES (?,2025,?,?,?,'X',?,10,?,?,1,'complete',?)",
                    (tenant, m, "K-" + restaurant, restaurant, art, kg * 5, kg, uid))
                n += 1
    periods = [f"2025-{m:02d}" for m in months]
    store.upsert(con, "upload",
                 ["id", "tenant", "filename", "stored_path", "adapter", "year", "uploaded_at",
                  "periods", "lines", "products", "spend_eur", "verdict", "report_json",
                  "committed_at", "commit_mode", "commit_note", "selected", "archived_at"],
                 [(uid, tenant, name, "", "sligro", 2025, "2025-10-01T00:00:00",
                   json.dumps(periods), n, 3, 0.0, "go",
                   json.dumps(dict(verdict="go", findings=[], summary={}, filename=name)),
                   None, None, None, selected, None)],
                 conflict=["id"])


add_file("upl-a1", "alpha", "Alpha spring.xlsx", MONTHS, BOUGHT, 1)
add_file("upl-a2", "alpha", "Alpha summer.xlsx", range(7, 9),
         {"COFFEE NORTH": {BEEF: 500.0}}, 0)            # held back: nobody's figures yet
add_file("upl-b1", "beta", "Beta year.xlsx", MONTHS, {"BETA BAR": {MILK: 77.0}}, 1)
con.commit()
con.close()
db.invalidate()

auth.create_user("mist", "admin-password-1", "admin", None, "MiSt")
auth.create_user("alphauser", "alpha-password-1", "client", "alpha", "Alpha University")
auth.create_user("betauser", "beta-password-1", "client", "beta", "Beta College")


def signed_in(username, password):
    c = TestClient(main.app, follow_redirects=False)
    assert c.post("/login", data={"username": username, "password": password}).status_code == 303
    return c


def ask(client, section, names, window="AY2024"):
    return client.get("/combined", params=[("section", section), ("window", window)]
                      + [("r", n) for n in names])


def cells(fragment):
    """The top-contributor table of a fragment as [(product, kg food, kg CO2), ...]."""
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", fragment, re.S):
        td = [" ".join(re.sub(r"<[^>]+>", "", c).split()) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(td) >= 5:
            out.append((html.unescape(td[1]), int(td[3].replace(",", "")), int(td[4].replace(",", ""))))
    return out


M = signed_in("mist", "admin-password-1")
A = signed_in("alphauser", "alpha-password-1")
B = signed_in("betauser", "beta-password-1")
M.post("/admin/viewing", data={"tenant": "alpha", "back": "/"})

print("=== the page offers tick lists, one per section ===")
dash = M.get("/").text
P(dash.count('class="multi-pick"') == 4, "four tick lists: trend, food groups, top contributors, EAT-Lancet")
P({m for m in re.findall(r'class="multi-pick" data-section="(\w+)"', dash)} == set(combine.SECTIONS),
  "one for each section that could be read per restaurant")
P('class="pick-select"' not in dash, "and the one-at-a-time dropdown is gone")
_names = [html.unescape(n) for n in re.findall(
    r'<label class="mp-row"><input type="checkbox" value="([^"]*)"', dash)]
P(_names[:3] == ["HALL", "COFFEE SOUTH", "COFFEE NORTH"] or sorted(_names[:3]) == sorted(BOUGHT),
  f"each lists every restaurant ({', '.join(_names[:3])})")
P(dash.count("Clear all") == 4 and "Tick one, or several to see their total." in dash,
  "with a way back to all of them, and a line saying what ticking does")
P(all(f'data-name="{n}"' in dash for n in BOUGHT) and 'data-name="All restaurants"' in dash,
  "every series already on the page is named, so one tick shows it without asking the server")
P('data-window="AY2024"' in dash, "and each list knows which period it is ticking in")

print("\n=== two coffee bars, added up ===")
COFFEE = ["COFFEE NORTH", "COFFEE SOUTH"]
whole = analysis.run(window="AY2024", tenant="alpha")
_lab, y0, m0, y1, m1 = analysis.parse_window("AY2024", tenant="alpha")
engine = catalogue.score([l for l in db.lines_for(y0, m0, y1, m1, "alpha")
                          if l["restaurant"] in COFFEE], label="coffee", top=40)
mine = combine.combine(whole, COFFEE)
P(mine["food_kg"] == engine["headline"]["food_kg"] == 6 * (30 + 20 + 10 + 45),
  f"the two bars bought {mine['food_kg']} kg, which is the sum of their lines")
P(mine["co2_kg"] == engine["headline"]["co2_kg"]
  and mine["intensity_kg_co2_per_kg"] == engine["headline"]["intensity_kg_co2_per_kg"],
  f"and the CO2 and intensity the engine gives those lines ({mine['co2_kg']} kg, "
  f"{mine['intensity_kg_co2_per_kg']})")
P((mine["eat_lancet"] or {}).get("score") == engine["eat_lancet"]["score"],
  f"the EAT-Lancet score is the engine's for the two together ({engine['eat_lancet']['score']})")

r = ask(M, "top", COFFEE)
P(r.status_code == 200 and 'data-series="combo"' in r.text and 'data-name="2 restaurants"' in r.text,
  "asking for their top contributors returns one series")
P("Total of 2 restaurants: COFFEE NORTH · COFFEE SOUTH" in html.unescape(r.text),
  "which says on its face what it is the total of")
P(sorted(cells(r.text)) == sorted((x["description"], x["food_kg"], x["co2_kg"])
                                  for x in engine["top_contributors"]),
  f"and lists the engine's products with the engine's kilograms ({len(cells(r.text))} products)")
P(dict((d, kg) for d, kg, _ in cells(r.text))["MEYERIJ VOLLE MELK"] == 6 * (30 + 10),
  "milk is the two bars' milk added together, not either bar's")
P("RUNDERGEHAKT" not in r.text, "and the hall's beef is not in it")
_cov = re.search(r'data-covers="([^"]+)" data-rows="(\d+)"', " ".join(r.text.split()))
P(_cov and float(_cov.group(1)) == 100.0 and _cov.group(2) == "2",
  "the count and the share the sentence above the table reads from are the combination's")

r = ask(M, "eat", COFFEE)
P(r.status_code == 200 and f'data-score="{engine["eat_lancet"]["score"]}"' in r.text,
  "the EAT-Lancet section gets the combined score to show beside the heading")
r = ask(M, "groups", COFFEE)
P(r.status_code == 200 and r.text.count('class="bar"') == len(engine["by_food_group"]),
  f"the food groups are the combination's ({len(engine['by_food_group'])} of them)")
r = ask(M, "trend", COFFEE)
P(r.status_code == 200 and r.text.count("per kg of food · EAT-Lancet") == 6
  and all(f"{m['period']} · {m['intensity_kg_co2_per_kg']:.2f} kg" in r.text
          for m in engine["by_month"]),
  "and the trend is their intensity month by month")

print("\n=== what cannot be combined is refused, not guessed ===")
P(ask(M, "top", ["NO SUCH PLACE"]).status_code == 404, "a restaurant that is not in the figures")
P(ask(M, "nonsense", COFFEE).status_code == 404, "a section that does not exist")
P(ask(M, "top", COFFEE, window="AY1990").status_code == 404, "a period with no figures")
P(ask(M, "top", ["BETA BAR"]).status_code == 404,
  "another client's restaurant, asked for while looking at Alpha")
P("detail" not in M.get("/api/analysis?window=AY2024").json(),
  "the layer that adds up stays out of the JSON a person can download")

print("\n=== a client ticks their published copy ===")
P(ask(A, "top", COFFEE).status_code == 404, "before anything is published there is nothing to combine")
publish.wait(publish.start("alpha", "mist"), timeout=600)
publish.wait(publish.start("beta", "mist"), timeout=600)
page = A.get("/").text
P(page.count('class="multi-pick"') == 4 and 'class="pick-select"' not in page,
  "after publishing, the client's page has the same four tick lists")
r = ask(A, "top", COFFEE)
P(r.status_code == 200 and sorted(cells(r.text)) == sorted(
    (x["description"], x["food_kg"], x["co2_kg"]) for x in engine["top_contributors"]),
  "and their combination is the same total MiSt saw")
selection.set_selected("upl-a2", "alpha", True)        # 500 kg of beef for COFFEE NORTH
db.invalidate()
P("RUNDERGEHAKT" in ask(M, "top", COFFEE, window="all").text
  or analysis.run(window="AY2024", tenant="alpha") and "RUNDERGEHAKT" in ask(M, "top", COFFEE).text,
  "(MiSt counts the summer file: the bars' total now has beef in it)")
r = ask(A, "top", COFFEE)
P(r.status_code == 200 and "RUNDERGEHAKT" not in r.text and sorted(cells(r.text)) == sorted(
    (x["description"], x["food_kg"], x["co2_kg"]) for x in engine["top_contributors"]),
  "the client's total does not move: it is their published figures, re-added")
P(ask(A, "top", ["BETA BAR"]).status_code == 404 and ask(B, "top", COFFEE).status_code == 404,
  "one client cannot combine another's restaurants, by name or by guessing")
P(ask(B, "top", ["BETA BAR"]).status_code == 200, "(each can ask for their own)")
M.post("/admin/as-client", data={"on": "1", "back": "/"})
r = ask(M, "top", COFFEE)
P(r.status_code == 200 and "RUNDERGEHAKT" not in r.text,
  "'See what the client sees' combines the published copy too, not the working figures")
M.post("/admin/as-client", data={"on": "0", "back": "/"})

print("\n=== figures from before the layer existed keep the dropdown ===")
con = db.connect()
for row in con.execute("SELECT publication_id, window_key, result_json FROM publication_view").fetchall():
    old = json.loads(row["result_json"])
    old.pop("detail", None)
    con.execute("UPDATE publication_view SET result_json=? WHERE publication_id=? AND window_key=?",
                (json.dumps(old), row["publication_id"], row["window_key"]))
con.commit()
con.close()
page = A.get("/").text
P('class="multi-pick"' not in page and page.count('class="pick-select"') == 4,
  "a copy published without it shows the one-at-a-time dropdown in all four sections")
P('id="trendPick"' in page and 'id="topPick"' in page, "which still works as it did")
P(ask(A, "top", COFFEE).status_code == 404, "and asking for a combination is refused, not answered wrongly")

import shutil   # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n{'ALL PASS' if not FAILED else str(len(FAILED)) + ' FAILED'}")
sys.exit(1 if FAILED else 0)

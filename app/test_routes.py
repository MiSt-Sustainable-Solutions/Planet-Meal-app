"""
Does every page actually render?

This test exists because of a night when it did not. A dependency shipped a major
version, the TemplateResponse signature it removed was the one the app used, and every
single human-facing page returned a 500. The deploy went green. Railway's health check
passed on the first try, because /api/health returns JSON and never touches a template --
it shared no code path with the thing it was vouching for.

The existing suites did not catch it either, and they were not wrong to miss it:
test_tenant asks "can this client see that client's data", test_app asks "are the
kilograms right". Neither one asks the dumber and more important question first. So this
does, and only this:

    for every route the app has registered, does it come back without a 500?

It walks the ROUTER rather than a hand-written list of paths, deliberately. A list would
have to be remembered; the router cannot be forgotten, because adding a route is what
puts it there. And it PRINTS what it could not reach -- a route needing a path parameter
this fixture has no id for is reported as uncovered rather than quietly skipped, because
a coverage test that hides its gaps is worse than none.

Needs the catalogue API running, like the other suites:

    python test_routes.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_TMP = tempfile.mkdtemp(prefix="pp_routes_")
os.environ["MIST_APP_DB"] = os.path.join(_TMP, "routes_test.db")
os.environ["MIST_TENANT"] = "acme"
os.environ["MIST_SECRET_KEY"] = "test-only-key-not-a-real-secret"

from fastapi.testclient import TestClient   # noqa: E402
from starlette.routing import Route         # noqa: E402

import auth        # noqa: E402
import catalogue   # noqa: E402
import db          # noqa: E402
import main        # noqa: E402
import store       # noqa: E402

FAILED = []


def P(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAILED.append(msg)


# --------------------------------------------------------------------------- fixture
def _fresh_database():
    if not store.IS_POSTGRES:
        return
    con = store.connect()
    for t in ("analysis_run", "upload_line", "upload_product", "upload",
              "purchase_line", "product", "app_user", "tenant"):
        con.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    con.commit()
    con.close()


_fresh_database()
db.init()
auth.init()
auth.add_tenant("acme", "Acme Catering Co", "Acme Kitchens")

# Enough purchase history that the pages have something to draw. The figures do not
# matter here -- test_app and test_mass own whether they are right. This test only cares
# that a page which has data to render renders it.
con = db.connect()
store.upsert(con, "product",
             ["tenant", "artikelnr", "description", "category", "ean", "ean_he",
              "first_seen", "last_seen"],
             [("acme", "194072", "MEYERIJ VOLLE MELK", "ZUIVEL HOUDBAAR",
               "8710401996797", "", "2025-01", "2025-06")],
             conflict=["tenant", "artikelnr"])
for month in range(1, 7):
    con.execute("INSERT INTO purchase_line (tenant, year, month, klantnr, restaurant, "
                "city, artikelnr, aantal, omzet, kg, kg_known, quality, source_upload) "
                "VALUES ('acme',2025,?,'K1','ACME CANTEEN','Delft','194072',10,500.0,"
                "100.0,1,'complete','fixture')", (month,))
con.commit()
con.close()
db.invalidate()

# A staged upload, so the routes that take an upload id are actually walked instead of
# reported as unreachable. Inserted directly rather than by uploading a file: this test is
# about whether pages render, and test_app already owns whether staging works.
con = db.connect()
con.execute("INSERT INTO upload VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
            ("upl-acme", "acme", "acme.xlsx", "", "sligro", 2025,
             "2025-01-01T00:00:00", '["2025-01"]', 1, 1, 500.0, "ok",
             json.dumps(dict(verdict="ok", findings=[], summary={},
                             filename="acme.xlsx", adapter="sligro", lines=1,
                             products=1, spend_eur=500.0, periods=["2025-01"]))))
con.commit()
con.close()

auth.create_user("boss", "boss-password-1", "admin", None, "Boss")
auth.create_user("acmeuser", "acme-password-1", "client", "acme", "Acme")

if catalogue.health() is None:
    print("the catalogue API is not running — start it and try again:")
    print("  python -m uvicorn api:app --port 8077   (in the catalogue repo, src/)")
    sys.exit(2)


def client_for(username, password):
    c = TestClient(main.app, follow_redirects=False)
    r = c.post("/login", data={"username": username, "password": password, "next": "/"})
    assert r.status_code == 303, f"could not sign in as {username}"
    return c


admin = client_for("boss", "boss-password-1")

# Path parameters this fixture can fill. Anything else is reported as uncovered rather
# than guessed at -- a made-up id would test the 404 path, not the page.
KNOWN_PARAMS = {}
_run = admin.get("/api/analysis")
if _run.status_code == 200:
    rid = (_run.json() or {}).get("run_id")
    if rid:
        KNOWN_PARAMS["run_id"] = rid
        KNOWN_PARAMS["id"] = rid
KNOWN_PARAMS["upload_id"] = "upl-acme"


# --------------------------------------------------------------------------- the walk
def routes():
    """Every GET route the app has registered, from the router itself."""
    out = []
    for r in main.app.router.routes:
        if not isinstance(r, Route):
            continue                       # mounts (/static) and websockets
        if "GET" not in (r.methods or set()):
            continue
        out.append(r)
    return sorted(out, key=lambda r: r.path)


# /logout is walked last, on its own client. Reached in the middle of the sweep it
# signs the admin out, and every route after it alphabetically redirects to the login
# page -- which looks exactly like a pass, since 303 is under 500. The first run of this
# test did precisely that and reported five green 303s for pages it never saw.
WALK_LAST = {"/logout"}

print("=== every registered GET route answers without a 500 ===")
walked, uncovered, posts = 0, [], []

for r in routes():
    path = r.path
    if path in WALK_LAST:
        continue
    if "{" in path:
        filled, missing = path, False
        for part in path.split("{")[1:]:
            name = part.split("}")[0].split(":")[0]
            if name in KNOWN_PARAMS:
                filled = filled.replace("{" + name + "}", str(KNOWN_PARAMS[name]))
            else:
                missing = True
        if missing:
            uncovered.append(path)
            continue
        path = filled

    resp = admin.get(path)
    walked += 1
    # 303 is a legitimate answer (a redirect to login, or after an action). 4xx is a
    # decision the app made on purpose. 5xx is the app breaking, which is the only
    # thing this test is looking for.
    P(resp.status_code < 500, f"{path:26} -> {resp.status_code}")

# A page that renders is not the same as a page that renders SOMETHING. An empty 200
# would pass the check above and still be a broken screen.
print("\n=== the pages a person actually looks at carry content ===")
for path, must_contain in (("/", "Acme Catering Co"),
                           ("/data-health", "Acme Catering Co"),
                           ("/history", "Acme Catering Co"),
                           ("/admin", "Acme Catering Co"),
                           ("/upload", "Acme Catering Co"),
                           ("/login", "Sign")):
    body = TestClient(main.app).get(path).text if path == "/login" else admin.get(path).text
    P(must_contain in body and len(body) > 500,
      f"{path:14} renders real HTML ({len(body):,} bytes)")

# And a client account, which takes different branches through the templates: no admin
# bar, a shorter nav, no upload link.
print("\n=== and again as a client account, which renders different branches ===")
user = client_for("acmeuser", "acme-password-1")
for path in ("/", "/data-health", "/history"):
    resp = user.get(path)
    P(resp.status_code < 500, f"{path:14} -> {resp.status_code}")

print("\n=== signing out, last, on a client of its own ===")
_out = client_for("boss", "boss-password-1")
P(_out.get("/logout").status_code < 500, "/logout answers")
P(_out.get("/").status_code == 303, "and the session really is gone afterwards")

print("\n=== what this run did NOT reach ===")
for r in main.app.router.routes:
    if isinstance(r, Route) and "GET" not in (r.methods or set()):
        posts.append(f"{'/'.join(sorted(r.methods or []))} {r.path}")
if uncovered:
    print("  routes needing a path parameter no fixture supplies:")
    for p_ in uncovered:
        print(f"    {p_}")
else:
    print("  every GET route was reached.")
print(f"  {len(posts)} non-GET route(s) not exercised here (test_app and test_tenant "
      f"cover the upload and login flows):")
for p_ in sorted(posts):
    print(f"    {p_}")

print(f"\nwalked {walked} GET route(s).")
print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED"))
sys.exit(1 if FAILED else 0)

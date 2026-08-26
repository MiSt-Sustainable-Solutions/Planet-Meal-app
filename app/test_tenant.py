"""
Two clients in one database. Can either one reach the other's data?

This is the test the whole multi-tenant design rests on. Every client table carries a
tenant column and every query is supposed to filter by it -- but "supposed to" is not a
guarantee, and a missing WHERE clause is invisible: the page renders, the numbers look
plausible, and nobody finds out until a client sees a figure they do not recognise.

So this creates two clients with DIFFERENT, RECOGNISABLE numbers, signs in as each, and
walks every route asserting that neither ever sees the other's. It also tries the attacks
worth trying:

  * guessing another client's analysis id and asking for it by URL
  * guessing another client's upload id and reading, committing or discarding it
  * a client passing ?tenant= to see if a route will honour it
  * a client reaching an admin-only route directly, without the link being on screen

An id is a handle, not a permission. That distinction is the entire point.

    python test_tenant.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_TMP = tempfile.mkdtemp(prefix="pp_tenant_")
os.environ["MIST_APP_DB"] = os.path.join(_TMP, "tenant_test.db")
os.environ["MIST_TENANT"] = "alpha"
os.environ["MIST_SECRET_KEY"] = "test-only-key-not-a-real-secret"

from fastapi.testclient import TestClient   # noqa: E402

import analysis    # noqa: E402
import auth        # noqa: E402
import catalogue   # noqa: E402
import db          # noqa: E402
import store       # noqa: E402
import main        # noqa: E402

FAILED = []


def P(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAILED.append(msg)


# --------------------------------------------------------------------------- fixture

def _fresh_database():
    """Start from nothing, on either backend.

    On SQLite each run gets its own temporary file, so this is a no-op. On Postgres the
    tests share one database, and leftovers from the previous run would collide -- a test
    that only passes on an empty database is a test that passes once.
    """
    import store
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

# Deliberately different volumes, so one client's number could never be mistaken for the
# other's if it leaked.
CLIENTS = {
    "alpha": dict(name="Alpha University", kg=100.0, eur=500.0),
    "beta":  dict(name="Beta College",     kg=900.0, eur=4500.0),
}
PRODUCTS = [("194072", "MEYERIJ VOLLE MELK", "ZUIVEL HOUDBAAR", "8710401996797"),
            ("186099", "MEYERIJ BIOLOGISCHE VOLLE MELK 1L", "ZUIVEL HOUDBAAR", "")]

# Register the clients first, on their own connection. SQLite will not let a second
# writer in while another connection holds an open write transaction, and add_tenant
# opens its own.
for tenant, spec in CLIENTS.items():
    auth.add_tenant(tenant, spec["name"])

con = db.connect()
for tenant, spec in CLIENTS.items():
    for art, desc, cat, ean in PRODUCTS:
        store.upsert(con, "product",
                     ["tenant", "artikelnr", "description", "category", "ean",
                      "ean_he", "first_seen", "last_seen"],
                     [(tenant, art, desc, cat, ean, "", "2025-01", "2025-06")],
                     conflict=["tenant", "artikelnr"])
        for month in range(1, 7):
            con.execute("INSERT INTO purchase_line (tenant, year, month, klantnr, "
                        "restaurant, city, artikelnr, aantal, omzet, kg, kg_known, "
                        "quality, source_upload) VALUES (?,2025,?,'K1',?,'X',?,10,?,?,1,"
                        "'complete','fixture')",
                        (tenant, month, f"{tenant.upper()} CANTEEN", art,
                         spec["eur"], spec["kg"]))
    # a staged upload each, so the by-id attacks have something real to aim at
    con.execute("INSERT INTO upload VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
                (f"upl-{tenant}", tenant, f"{tenant}.xlsx", "", "sligro", 2025,
                 "2025-01-01T00:00:00", '["2025-01"]', 1, 1, spec["eur"], "ok",
                 json.dumps(dict(verdict="ok", findings=[], summary={},
                                 filename=f"{tenant}.xlsx", adapter="sligro",
                                 lines=1, products=1, spend_eur=spec["eur"],
                                 periods=["2025-01"]))))
con.commit()
con.close()
db.invalidate()

auth.create_user("mist", "admin-password-1", "admin", None, "MiSt")
auth.create_user("alphauser", "alpha-password-1", "client", "alpha", "Alpha University")
auth.create_user("betauser", "beta-password-1", "client", "beta", "Beta College")

if catalogue.health() is None:
    print("the catalogue API is not running — start it and try again:")
    print("  python -m uvicorn api:app --port 8077   (in the catalogue repo, src/)")
    sys.exit(2)


def client_for(username: str, password: str) -> TestClient:
    c = TestClient(main.app, follow_redirects=False)
    r = c.post("/login", data={"username": username, "password": password, "next": "/"})
    assert r.status_code == 303, f"could not sign in as {username}: {r.status_code}"
    return c


# --------------------------------------------------------------------------- tests
print("=== nothing is reachable without signing in ===")
anon = TestClient(main.app, follow_redirects=False)
for path in ("/", "/data-health", "/history", "/upload", "/export.xlsx", "/admin"):
    r = anon.get(path)
    P(r.status_code == 303 and "/login" in r.headers.get("location", ""),
      f"{path:16} -> redirected to the sign-in page")
for path in ("/api/analysis", "/api/months", "/api/catalogue-version"):
    P(anon.get(path).status_code == 401, f"{path:24} -> 401, not data")
P(anon.get("/api/health").status_code == 200,
  "/api/health stays open, so a monitor can check the app is alive")

print("\n=== a wrong password says nothing useful ===")
bad = TestClient(main.app, follow_redirects=False)
r1 = bad.post("/login", data={"username": "alphauser", "password": "wrong", "next": "/"})
r2 = bad.post("/login", data={"username": "nosuchperson", "password": "wrong", "next": "/"})
P(r1.status_code == 401 and r2.status_code == 401, "both refused")
P("do not match" in r1.text and r1.text.count("do not match") == r2.text.count("do not match"),
  "with the SAME message, so it never reveals which usernames exist")

print("\n=== each client sees their own numbers ===")
A = client_for("alphauser", "alpha-password-1")
B = client_for("betauser", "beta-password-1")
a = A.get("/api/analysis").json()
b = B.get("/api/analysis").json()
P(a["headline"]["food_kg"] != b["headline"]["food_kg"],
  f"Alpha {a['headline']['food_kg']:,} kg vs Beta {b['headline']['food_kg']:,} kg")
P(a["headline"]["food_kg"] == 1200 and b["headline"]["food_kg"] == 10800,
  "and each is its own figure, not a total of both")

print("\n=== and cannot ask for the other's ===")
# The attack a URL invites: name the other tenant and see if anything honours it.
for param in ("tenant", "client", "t"):
    got = A.get(f"/api/analysis?{param}=beta").json()
    P(got["headline"]["food_kg"] == a["headline"]["food_kg"],
      f"?{param}=beta is ignored — a client's tenant comes from their account")

print("\n=== a saved analysis id is a handle, not a permission ===")
b_run = b.get("run_id")
P(b_run is not None, f"Beta has a saved run ({b_run})")
P(A.get(f"/api/run/{b_run}").status_code == 404,
  "Alpha asking for Beta's run id by URL gets nothing")
P(B.get(f"/api/run/{b_run}").status_code == 200, "while Beta gets their own")

print("\n=== so is an upload id ===")
P(A.get("/upload/upl-beta").status_code in (303, 403),
  "Alpha cannot even open Beta's upload page (upload is admin-only anyway)")
M = client_for("mist", "admin-password-1")
# An admin sees ONE client at a time -- whichever the bar says they are looking at. So
# Beta's upload is not visible while looking at Alpha, and that is the intended answer:
# showing another client's file while the header says "Alpha" would be worse than a 404.
P(M.get("/upload/upl-beta").status_code == 404,
  "an admin looking at Alpha does not see Beta's upload either")
M.post("/admin/viewing", data={"tenant": "beta", "back": "/"})
P(M.get("/upload/upl-beta").status_code == 200, "but does once they switch to Beta")
M.post("/admin/viewing", data={"tenant": "alpha", "back": "/"})
before = db.connect().execute(
    "SELECT COUNT(*) FROM upload WHERE id='upl-beta'").fetchone()[0]
A.get("/upload/upl-beta/discard")
after = db.connect().execute(
    "SELECT COUNT(*) FROM upload WHERE id='upl-beta'").fetchone()[0]
P(before == after == 1, "and a client cannot delete another client's staged upload")

print("\n=== the admin-only doors are shut, whether or not the link is on screen ===")
for path in ("/upload", "/review-sheet.xlsx", "/admin"):
    P(A.get(path).status_code == 403, f"{path:22} -> 403 for a client account")
P(A.post("/curate", files={"file": ("x.xlsx", b"not a workbook")}).status_code == 403,
  "/curate                -> 403, so decisions cannot be filed from a client account")
P("/upload" not in A.get("/").text, "and the Upload link is not shown to them either")

print("\n=== an admin sees every client, one at a time ===")
P(M.get("/admin").status_code == 200, "the accounts page opens")
first = M.get("/api/analysis").json()["headline"]["food_kg"]
M.post("/admin/viewing", data={"tenant": "beta", "back": "/"})
now = M.get("/api/analysis").json()["headline"]["food_kg"]
P(now == 10800, f"after switching to Beta the admin sees Beta's numbers ({now:,} kg)")
M.post("/admin/viewing", data={"tenant": "alpha", "back": "/"})
P(M.get("/api/analysis").json()["headline"]["food_kg"] == 1200,
  "and switching back shows Alpha's")
P(first in (1200, 10800), "an admin always starts on a real client, never on nothing")

print("\n=== a client cannot use the admin's switch ===")
r = A.post("/admin/viewing", data={"tenant": "beta", "back": "/"})
P(r.status_code == 403, "posting to the picker directly is refused")
P(A.get("/api/analysis").json()["headline"]["food_kg"] == 1200,
  "and their view is unchanged — still their own data")

print("\n=== signing out actually signs out ===")
A.get("/logout")
P(A.get("/api/analysis").status_code == 401, "the session is gone")

print(f"\n{'ALL PASS' if not FAILED else str(len(FAILED)) + ' FAILED'}")
sys.exit(1 if FAILED else 0)

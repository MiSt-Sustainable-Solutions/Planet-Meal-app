"""
Onboarding a client without anyone handling somebody else's password.

There is no mail server here and there does not need to be one: MiSt onboards every
client personally, so the app makes a one-time link and a person hands it over. That
makes the LINK the credential, which is fine — but only if it behaves like one.

So this checks the properties that make it safe, not the happy path:

  * an invited account cannot be signed into at all until the link is used
  * the raw token is never stored — a copy of the database is not a set of keys
  * a link works exactly once, and dies on use
  * an expired link is refused
  * using one link kills the others outstanding for that account
  * a client cannot reach the pages that make links
  * deleting an account takes its outstanding links with it

And the ordinary ones: a second login for the same client sees the same data, and
changing your own password needs the old one.

    python test_accounts.py
"""
import datetime as dt
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_TMP = tempfile.mkdtemp(prefix="pp_acct_")
os.environ["MIST_APP_DB"] = os.path.join(_TMP, "acct.db")
os.environ["MIST_TENANT"] = "acme"
os.environ["MIST_SECRET_KEY"] = "test-only-key-not-a-real-secret"

from fastapi.testclient import TestClient   # noqa: E402

import auth        # noqa: E402
import db          # noqa: E402
import main        # noqa: E402
import store       # noqa: E402

FAILED = []


def P(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAILED.append(msg)


def _fresh():
    if not store.IS_POSTGRES:
        return
    con = store.connect()
    for t in ("account_token", "analysis_run", "upload_line", "upload_product",
              "upload", "purchase_line", "product", "app_user", "tenant"):
        con.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    con.commit()
    con.close()


_fresh()
db.init()
auth.init()
auth.create_user("boss", "boss-password-1", "admin", None, "Boss")


def signed_in(u, p):
    c = TestClient(main.app, follow_redirects=False)
    r = c.post("/login", data={"username": u, "password": p, "next": "/"})
    assert r.status_code == 303, f"could not sign in as {u}"
    return c


admin = signed_in("boss", "boss-password-1")

print("=== creating a client makes a login nobody can use yet ===")
r = admin.post("/admin/accounts", data={"client_name": "Acme University", "note": "pilot"})
P(r.status_code == 200, f"the form is accepted ({r.status_code})")
P("acmeuniversity1" in r.text,
  "the first login is derived from the client name (acmeuniversity1)")
P(any(t["tenant"] == "acmeuniversity" for t in auth.tenants())
  or any(t["display_name"] == "Acme University" for t in auth.tenants()),
  "the client exists")
uname = next(u["username"] for u in auth.users() if u["role"] == "client")
P(not auth.has_password(uname), f"{uname} has no password at all")
P(auth.authenticate(uname, "") is None and auth.authenticate(uname, "!") is None,
  "and cannot be signed into, by an empty password or by the sentinel")

# pull the raw token out of the rendered page, the way a person would copy it
import re                                                        # noqa: E402
m = re.search(r"/set-password/([A-Za-z0-9_\-]+)", r.text)
P(bool(m), "a one-time link is shown once, on the page")
raw = m.group(1)

print("\n=== the raw token is nowhere in the database ===")
con = db.connect()
stored = [dict(x) for x in con.execute("SELECT * FROM account_token")]
con.close()
blob = repr(stored)
P(raw not in blob, "only a hash is kept — a copy of this database is not a set of keys")
P(len(stored) == 1 and stored[0]["used_at"] is None, "one outstanding link")

print("\n=== the link sets a password, once ===")
anon = TestClient(main.app, follow_redirects=False)
P(anon.get(f"/set-password/{raw}").status_code == 200,
  "the page opens without signing in")
r = anon.post(f"/set-password/{raw}",
              data={"password": "client-password-1", "again": "client-password-1"})
P(r.status_code == 303, "setting a password redirects to sign in")
P(auth.authenticate(uname, "client-password-1") is not None, "the password works")
P(auth.check_link(raw) is None, "and the link is spent")
r2 = anon.post(f"/set-password/{raw}",
               data={"password": "second-attempt-1", "again": "second-attempt-1"})
P(auth.authenticate(uname, "second-attempt-1") is None,
  "a second use changes nothing")

print("\n=== an expired link is refused ===")
raw2 = auth.make_link(uname, "reset", by="boss")
con = db.connect()
con.execute("UPDATE account_token SET expires_at=? WHERE used_at IS NULL",
            ((dt.datetime.now() - dt.timedelta(hours=1)).isoformat(timespec="seconds"),))
con.commit()
con.close()
P(auth.check_link(raw2) is None, "check_link says no")
P("no longer" in anon.get(f"/set-password/{raw2}").text.lower(),
  "and the page says so instead of offering a form")

print("\n=== using one link kills the spares ===")
a = auth.make_link(uname, "reset", by="boss")
b = auth.make_link(uname, "reset", by="boss")
P(auth.check_link(a) and auth.check_link(b), "two links outstanding")
auth.use_link(b, "third-password-1")
P(auth.check_link(a) is None, "using one voids the other — no spare key left behind")

print("\n=== a second login for the same client ===")
tid = next(u["tenant"] for u in auth.users() if u["username"] == uname)
r = admin.post("/admin/accounts", data={"client_name": "-", "tenant": tid, "note": "2nd"})
names = sorted(u["username"] for u in auth.users() if u["role"] == "client")
P(len(names) == 2, f"two logins for one client ({', '.join(names)})")
P(all(u["tenant"] == tid for u in auth.users() if u["role"] == "client"),
  "both point at the same client, so both see the same data")

print("\n=== a client cannot make links ===")
client = signed_in(uname, "third-password-1")
P(client.get("/admin").status_code == 403, "/admin is refused")
P(client.post("/admin/accounts",
              data={"client_name": "Sneaky"}).status_code == 403,
  "and so is posting to it directly")
P(client.post("/admin/accounts/link",
              data={"username": "boss"}).status_code == 403,
  "a client cannot issue a link for the admin account")

print("\n=== changing your own password needs the old one ===")
r = client.post("/account", data={"current": "wrong", "password": "new-password-1",
                                  "again": "new-password-1"})
P(auth.authenticate(uname, "new-password-1") is None, "a wrong current password changes nothing")
r = client.post("/account", data={"current": "third-password-1",
                                  "password": "new-password-1", "again": "new-password-1"})
P(r.status_code == 303, "the right one is accepted")
P(auth.authenticate(uname, "new-password-1") is not None, "and the new password works")
P(auth.authenticate(uname, "third-password-1") is None, "the old one stops immediately")

print("\n=== a person is told when they last signed in ===")
# The only safety feature here, and it is shown to the account owner rather than kept
# for MiSt -- they are the one who knows whether that sign-in was them.
#
# The trap it has to avoid: last_seen is stamped DURING sign-in, so reading it from the
# table afterwards always says "just now", which tells nobody anything. The value shown
# has to be the one from before this visit.
auth.create_user("watcher", "watcher-password-1", "client", tid, "Watcher")
w1 = signed_in("watcher", "watcher-password-1")
body = w1.get("/history").text
P("first time this account has signed in" in body,
  "the first ever sign-in says so instead of inventing a date")
P("You last signed in on" not in body, "and shows no date at all")

# backdate it, so 'the previous sign-in' and 'now' cannot be confused
con = db.connect()
con.execute("UPDATE app_user SET last_seen='2020-01-02T03:04:05' WHERE username='watcher'")
con.commit()
con.close()

w2 = signed_in("watcher", "watcher-password-1")
body = w2.get("/history").text
P("2020-01-02" in body and "03:04" in body,
  "the second visit shows the PREVIOUS sign-in, not this one")
P("mistsustainablesolutions.com" in body,
  "and says who to ask if it was not them")

con = db.connect()
now = con.execute("SELECT last_seen FROM app_user WHERE username='watcher'").fetchone()[0]
con.close()
P(not now.startswith("2020"), f"while the table has moved on to {now[:10]}")

print("\n=== deleting an account takes its links with it ===")
spare = auth.make_link(uname, "reset", by="boss")
P(auth.check_link(spare) is not None, "a link is outstanding")
auth.delete_user(uname)
P(auth.check_link(spare) is None,
  "deleting the account voids it — not a live key to a recreated name")

print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED"))
sys.exit(1 if FAILED else 0)

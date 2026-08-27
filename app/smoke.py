"""
Is the DEPLOYED app actually working? Not "is it up" -- working.

Railway's health check answers a different question than the one you care about. It asks
/api/health, which returns JSON and never renders a template, so it passed with a green
tick on a night when every human-facing page was returning 500. A check that shares no
code path with the thing it is vouching for is not evidence.

So this asks the pages. It signs in if you give it an account, walks the real screens,
and reads what comes back. Point it at anything:

    python smoke.py https://web-production-xxxx.up.railway.app
    python smoke.py https://planetmeal.mistsustainablesolutions.com --user mist

With --user it prompts for the password and checks the signed-in pages too, which is the
only way to reach the templates that broke. Without it, it checks what an anonymous
visitor can see -- which is still worth something, because the login page is a rendered
template and would have failed that night.

Read-only. It performs no upload, commits nothing and changes no data. The one write it
makes is a login, and it signs out again afterwards.

Exit code 0 if everything passed, 1 if anything did not, so it can gate a deploy.
"""
from __future__ import annotations

import getpass
import sys

import httpx

TIMEOUT = 60.0

FAILED: list[str] = []


def P(ok: bool, msg: str) -> None:
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAILED.append(msg)


def check_health(c: httpx.Client) -> dict | None:
    """/api/health, read properly rather than trusted for its status code."""
    print("=== health ===")
    try:
        r = c.get("/api/health")
    except httpx.HTTPError as e:
        P(False, f"could not reach the app at all: {type(e).__name__}")
        return None

    P(r.status_code == 200, f"/api/health -> {r.status_code}")
    if r.status_code != 200:
        return None

    body = r.json()
    P(body.get("app") == "ok", f"the app reports itself ok ({body.get('app')})")

    # The one that a green tick hides. health is 200 whether or not the catalogue is
    # reachable, because the app being alive and the app being useful are different
    # facts. Without the catalogue nothing can be scored.
    cat = body.get("catalogue")
    P(cat is not None,
      "the catalogue is reachable" if cat is not None else
      "the catalogue is NOT reachable -- check MIST_CATALOGUE_API (health still says 200)")
    if isinstance(cat, dict):
        print(f"       catalogue version {cat.get('catalogue')}, "
              f"writes_protected={cat.get('writes_protected')}")
    return body


def check_anonymous(c: httpx.Client) -> None:
    """What a visitor with no account gets. The login page is a rendered template."""
    print("\n=== signed out ===")
    r = c.get("/login")
    P(r.status_code == 200, f"/login -> {r.status_code}")
    P("<form" in r.text and len(r.text) > 500,
      f"/login renders real HTML ({len(r.text):,} bytes)")
    if "No accounts exist yet" in r.text:
        print("       note: no accounts exist on this deployment yet")

    r = c.get("/", follow_redirects=False)
    P(r.status_code in (302, 303) and "/login" in r.headers.get("location", ""),
      f"/ redirects a stranger to the sign-in page ({r.status_code})")


def check_signed_in(c: httpx.Client, username: str, password: str) -> None:
    """The screens that actually broke. Nothing else reaches these templates."""
    print("\n=== signed in ===")
    r = c.post("/login", data={"username": username, "password": password, "next": "/"},
               follow_redirects=False)
    if r.status_code not in (302, 303):
        P(False, f"could not sign in as {username} ({r.status_code})")
        return
    P(True, f"signed in as {username}")

    for path in ("/", "/data-health", "/history"):
        r = c.get(path)
        ok = r.status_code == 200 and len(r.text) > 500 and "<html" in r.text.lower()
        P(ok, f"{path:14} -> {r.status_code}, {len(r.text):,} bytes"
              + ("" if ok else "   <-- this is the failure mode health cannot see"))

    # Admin-only. A client account gets 403 here, which is correct, not a failure.
    r = c.get("/upload")
    P(r.status_code in (200, 403), f"/upload         -> {r.status_code}")
    if r.status_code == 403:
        print("       (403 is right for a client account -- upload is admin-only)")

    c.get("/logout", follow_redirects=False)
    print("       signed out again")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1].startswith("-"):
        print(__doc__)
        return 2

    base = argv[1].rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base
    user = None
    if "--user" in argv:
        user = argv[argv.index("--user") + 1]

    print(f"checking {base}\n")
    with httpx.Client(base_url=base, timeout=TIMEOUT, follow_redirects=True) as c:
        if check_health(c) is None:
            print("\nthe app did not answer. nothing else can be checked.")
            return 1
        check_anonymous(c)
        if user:
            # Prompted, never an argument: a password passed on the command line ends up
            # in shell history and in the process list.
            pw = getpass.getpass(f"password for {user}: ")
            check_signed_in(c, user, pw)
        else:
            print("\n  no --user given, so the signed-in pages were NOT checked.")
            print("  those are the ones that broke last time. run again with --user "
                  "<name> to cover them.")

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

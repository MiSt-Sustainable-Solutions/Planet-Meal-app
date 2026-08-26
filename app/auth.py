"""
Who is looking, and what they are allowed to see.

Two roles, and the difference between them is the whole security model:

    admin    MiSt. Sees every client. Uploads on their behalf, files curation
             decisions, and switches between clients with a picker. Has no tenant
             of its own -- a null tenant means "all of them".

    client   One account per organisation. APPeL is one account, not one per
             person. Sees its own data and nothing else, ever.

THE RULE THAT MATTERS: a client's tenant comes from their session and from nowhere
else. Never from a query parameter, never from a form field, never from a path. If
a tenant could be supplied by the caller then changing one character in a URL would
be all it takes to read another client's purchasing, and nothing would look wrong
in the logs. Only an admin may name a tenant, and only because an admin is allowed
to see all of them anyway.

That rule is why `Principal.tenant` is a property rather than something a route
passes around: there is one place the answer comes from, and it cannot be
overridden from outside.

Passwords are stored as scrypt hashes with a per-password salt (stdlib, no extra
dependency). The plain password is never written anywhere -- not to the database,
not to a log, not to a session.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import secrets
import sqlite3
from dataclasses import dataclass

import config
import db
import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_user(
  username TEXT PRIMARY KEY,
  role TEXT NOT NULL,
  tenant TEXT,
  display_name TEXT,
  pwd_hash TEXT NOT NULL,
  created_at TEXT,
  last_seen TEXT,
  must_change INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS tenant(
  tenant TEXT PRIMARY KEY,
  display_name TEXT,
  caterer TEXT,
  created_at TEXT);
"""

ROLES = ("admin", "client")

# scrypt cost. n=2^14 is a few tens of milliseconds per hash on a laptop: slow
# enough to make guessing expensive, fast enough that a login does not stall.
_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32


# --------------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    """-> 'scrypt$<salt hex>$<hash hex>'. The password itself is never stored."""
    if not password or len(password) < 8:
        raise ValueError("a password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time compare, so a wrong password cannot be found by timing it."""
    try:
        scheme, salt_hex, hash_hex = (stored or "").split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=_N, r=_R, p=_P, dklen=_DKLEN)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# --------------------------------------------------------------------------- schema
def init() -> None:
    con = db.connect()
    con.executescript(SCHEMA)
    # The tenant this deployment started with, so an existing install keeps working.
    # Conflict column named explicitly -- see store.upsert for why it is never inferred.
    store.upsert(con, "tenant", ["tenant", "display_name", "caterer", "created_at"],
                 [(config.TENANT, config.CLIENT_NAME, config.CATERER_NAME,
                   dt.datetime.now().isoformat(timespec="seconds"))],
                 conflict=["tenant"], update=False)
    con.commit()
    con.close()


def tenants() -> list[dict]:
    con = db.connect()
    rows = [dict(r) for r in con.execute(
        "SELECT tenant, display_name, caterer FROM tenant ORDER BY display_name")]
    con.close()
    return rows


def add_tenant(tenant: str, display_name: str, caterer: str = "") -> None:
    con = db.connect()
    store.upsert(con, "tenant", ["tenant", "display_name", "caterer", "created_at"],
                 [(tenant, display_name, caterer,
                   dt.datetime.now().isoformat(timespec="seconds"))],
                 conflict=["tenant"])
    con.commit()
    con.close()


# --------------------------------------------------------------------------- users
def create_user(username: str, password: str, role: str,
                tenant: str | None = None, display_name: str = "") -> dict:
    """Create a login. An admin has no tenant; a client must have one."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if role == "client" and not tenant:
        raise ValueError("a client account must belong to a tenant")
    if role == "admin" and tenant:
        raise ValueError("an admin has no tenant of its own -- it sees all of them")
    con = db.connect()
    try:
        con.execute("INSERT INTO app_user VALUES (?,?,?,?,?,?,NULL,0)",
                    (username.strip().lower(), role, tenant,
                     display_name or username, hash_password(password),
                     dt.datetime.now().isoformat(timespec="seconds")))
        con.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"a user called {username!r} already exists") from None
    finally:
        con.close()
    return dict(username=username.strip().lower(), role=role, tenant=tenant)


def set_password(username: str, password: str) -> None:
    con = db.connect()
    n = con.execute("UPDATE app_user SET pwd_hash=?, must_change=0 WHERE username=?",
                    (hash_password(password), username.strip().lower())).rowcount
    con.commit()
    con.close()
    if not n:
        raise ValueError(f"no user called {username!r}")


def delete_user(username: str) -> None:
    con = db.connect()
    con.execute("DELETE FROM app_user WHERE username=?", (username.strip().lower(),))
    con.commit()
    con.close()


def users() -> list[dict]:
    con = db.connect()
    rows = [dict(r) for r in con.execute(
        "SELECT username, role, tenant, display_name, created_at, last_seen "
        "FROM app_user ORDER BY role, username")]
    con.close()
    return rows


def any_users() -> bool:
    con = db.connect()
    n = con.execute("SELECT COUNT(*) FROM app_user").fetchone()[0]
    con.close()
    return bool(n)


def authenticate(username: str, password: str) -> dict | None:
    """-> the user, or None. Deliberately says nothing about WHICH half was wrong."""
    con = db.connect()
    r = con.execute("SELECT * FROM app_user WHERE username=?",
                    ((username or "").strip().lower(),)).fetchone()
    if not r or not verify_password(password or "", r["pwd_hash"]):
        con.close()
        # Hash anyway when the user does not exist, so a missing username and a wrong
        # password take the same time and cannot be told apart by watching the clock.
        if not r:
            hash_password("x" * 12)
        return None
    con.execute("UPDATE app_user SET last_seen=? WHERE username=?",
                (dt.datetime.now().isoformat(timespec="seconds"), r["username"]))
    con.commit()
    con.close()
    return dict(username=r["username"], role=r["role"], tenant=r["tenant"],
                display_name=r["display_name"] or r["username"],
                must_change=bool(r["must_change"]))


# --------------------------------------------------------------------------- session
@dataclass(frozen=True)
class Principal:
    """Who is making this request, and which client's data they are looking at."""
    username: str
    role: str
    display_name: str
    _own_tenant: str | None      # a client's own tenant; None for an admin
    _viewing: str | None         # the tenant an admin has selected

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def tenant(self) -> str | None:
        """The ONE place a route learns whose data it may touch.

        A client always gets their own, whatever the request said. An admin gets
        whichever client they picked. Nothing here reads a query parameter -- that is
        the point, and it is why routes take the principal rather than a tenant string.
        """
        if self.role == "client":
            return self._own_tenant
        return self._viewing

    def may_see(self, tenant: str | None) -> bool:
        """Can this person look at a row belonging to `tenant`?"""
        if self.is_admin:
            return True
        return bool(tenant) and tenant == self._own_tenant


SESSION_USER = "u"
SESSION_VIEWING = "v"


def sign_in(request, user: dict) -> None:
    request.session[SESSION_USER] = user["username"]
    if user["role"] == "admin":
        # Start an admin on the first client rather than on nothing, so the dashboard
        # has something to show the moment they log in.
        ts = tenants()
        request.session[SESSION_VIEWING] = ts[0]["tenant"] if ts else None
    else:
        request.session.pop(SESSION_VIEWING, None)


def sign_out(request) -> None:
    request.session.clear()


def current(request) -> Principal | None:
    """The signed-in principal, or None. Reads the session cookie, nothing else."""
    username = request.session.get(SESSION_USER)
    if not username:
        return None
    con = db.connect()
    r = con.execute("SELECT username, role, tenant, display_name FROM app_user "
                    "WHERE username=?", (username,)).fetchone()
    con.close()
    if not r:
        # The account was deleted while the cookie was still valid.
        return None
    viewing = request.session.get(SESSION_VIEWING)
    if r["role"] == "admin" and viewing is None:
        ts = tenants()
        viewing = ts[0]["tenant"] if ts else None
    return Principal(username=r["username"], role=r["role"],
                     display_name=r["display_name"] or r["username"],
                     _own_tenant=r["tenant"], _viewing=viewing)


def view_tenant(request, tenant: str | None) -> bool:
    """Admin only: look at a different client. -> False if not permitted."""
    p = current(request)
    if not p or not p.is_admin:
        return False
    if tenant and tenant not in {t["tenant"] for t in tenants()}:
        return False
    request.session[SESSION_VIEWING] = tenant
    return True


# --------------------------------------------------------------------------- secret
def session_secret() -> str:
    """The key that signs session cookies.

    In production this MUST be set and MUST be stable: a changing key logs everybody
    out on every restart, and a guessable one lets somebody forge a session cookie
    and walk in as an admin.
    """
    key = os.environ.get("MIST_SECRET_KEY", "").strip()
    if key:
        return key
    if os.environ.get("MIST_ENV", "").lower() in ("production", "prod"):
        raise RuntimeError(
            "MIST_SECRET_KEY is not set. Session cookies would be signed with a key "
            "that changes on every restart, so nobody would stay logged in -- and a "
            "predictable key would let anyone forge an admin session. Generate one "
            "with: python -c \"import secrets; print(secrets.token_urlsafe(48))\"")
    return "dev-only-" + secrets.token_urlsafe(24)


# --------------------------------------------------------------------------- CLI
def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="manage PLANETprocure logins")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add-user", help="create a login")
    a.add_argument("username")
    a.add_argument("--role", choices=ROLES, required=True)
    a.add_argument("--tenant", default=None, help="required for a client, forbidden for an admin")
    a.add_argument("--name", default="", help="display name")

    p = sub.add_parser("set-password", help="change a password")
    p.add_argument("username")

    sub.add_parser("list", help="show users and tenants")

    d = sub.add_parser("delete-user")
    d.add_argument("username")

    t = sub.add_parser("add-tenant", help="register a client")
    t.add_argument("tenant", help="short id, e.g. tudelft")
    t.add_argument("--name", required=True, help="display name, e.g. TU Delft")
    t.add_argument("--caterer", default="")

    args = ap.parse_args()
    db.init()
    init()

    if args.cmd == "list":
        print("tenants:")
        for t in tenants():
            print(f"   {t['tenant']:16} {t['display_name']}"
                  + (f"  ({t['caterer']})" if t["caterer"] else ""))
        print("\nusers:")
        for u in users():
            scope = "ALL CLIENTS" if u["role"] == "admin" else (u["tenant"] or "-")
            print(f"   {u['username']:20} {u['role']:7} {scope:16} "
                  f"last seen {u['last_seen'] or 'never'}")
        if not any_users():
            print("   (none yet -- create one with: python auth.py add-user <name> --role admin)")
        return 0

    if args.cmd == "add-tenant":
        add_tenant(args.tenant, args.name, args.caterer)
        print(f"tenant {args.tenant} ({args.name}) registered")
        return 0

    if args.cmd == "delete-user":
        delete_user(args.username)
        print(f"deleted {args.username}")
        return 0

    # Read the password from a prompt, never from the command line: an argument ends up
    # in shell history and in the process list where anyone on the machine can read it.
    import getpass
    pw = getpass.getpass("password: ")
    if pw != getpass.getpass("again: "):
        print("they do not match")
        return 1

    if args.cmd == "add-user":
        u = create_user(args.username, pw, args.role, args.tenant, args.name)
        scope = "every client" if u["role"] == "admin" else u["tenant"]
        print(f"created {u['username']} ({u['role']}) -- can see {scope}")
    else:
        set_password(args.username, pw)
        print(f"password changed for {args.username}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())

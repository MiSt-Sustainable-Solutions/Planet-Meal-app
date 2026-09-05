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

-- One-time links, for setting a password without anyone else ever handling it.
--
-- The token is stored HASHED, for the same reason a password is: a copy of this
-- database should not hand somebody live access to an account. What is written here
-- cannot be turned back into a working link.
CREATE TABLE IF NOT EXISTS account_token(
  token_hash TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  kind TEXT NOT NULL,            -- 'invite' on a new account, 'reset' on an existing one
  created_at TEXT,
  created_by TEXT,
  expires_at TEXT,
  used_at TEXT,
  note TEXT);
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


# An account that exists but has never had a password set. Deliberately not an empty
# string: verify_password splits on "$" and needs three parts, so this can never match
# anything a person could type. An invited account holds this until the link is used.
NO_PASSWORD = "!"


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


def tenant_names(tenant: str | None, rows: list[dict] | None = None) -> tuple[str, str]:
    """-> (client name, caterer) for a tenant, from the tenant row.

    These belong to the CLIENT, not to the deployment. One deployment serves several
    clients, so reading them from an environment variable would show every client the
    first client's name. The config values are only the seed for the tenant created on a
    fresh install, and are the fallback for an admin who is viewing no tenant at all.
    """
    for t in (tenants() if rows is None else rows):
        if t["tenant"] == tenant:
            return (t["display_name"] or config.CLIENT_NAME,
                    t["caterer"] or config.CATERER_NAME)
    return config.CLIENT_NAME, config.CATERER_NAME


def add_tenant(tenant: str, display_name: str, caterer: str = "") -> None:
    con = db.connect()
    store.upsert(con, "tenant", ["tenant", "display_name", "caterer", "created_at"],
                 [(tenant, display_name, caterer,
                   dt.datetime.now().isoformat(timespec="seconds"))],
                 conflict=["tenant"])
    con.commit()
    con.close()


# --------------------------------------------------------------------------- users
def create_user(username: str, password: str | None, role: str,
                tenant: str | None = None, display_name: str = "") -> dict:
    """Create a login. An admin has no tenant; a client must have one.

    `password=None` creates an account that CANNOT be signed into at all, waiting for
    its owner to set one through an invite link. That is the normal path now: nobody
    should ever type a password that is not their own, so an admin never chooses one.
    """
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
                     display_name or username,
                     NO_PASSWORD if password is None else hash_password(password),
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


# --------------------------------------------------------------------------- links
#
# There is no mail server, and there does not need to be one. Every client here is
# onboarded by a person at MiSt, so the app makes a one-time link and MiSt hands it over
# the way they already talk to that client. That removes SMTP, a sending domain,
# deliverability and spam handling from the picture entirely -- and at this scale it is
# safer than email, not weaker: no mailbox to compromise, and no link sitting in an inbox
# for a year.
#
# The consequence to keep in mind: a link is a bearer credential. Whoever holds it can set
# that account's password. So it is single-use, it expires, and it is stored hashed.

INVITE_DAYS = 7          # a new client may not get to it the same day
RESET_HOURS = 24         # a reset is answering a request someone just made


def _hash_token(raw: str) -> str:
    """SHA-256, not scrypt. A 32-byte random token has nothing to brute force -- the
    slow hashing that protects a human-chosen password buys nothing here, and would make
    every link check needlessly expensive."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> dt.datetime:
    return dt.datetime.now()


def make_link(username: str, kind: str = "invite", by: str = "", note: str = "") -> str:
    """Create a one-time link for `username` and return the RAW token.

    Returned once and never recoverable: only its hash is stored. If it is lost, make
    another one -- which also invalidates nothing, because a link is only spent when it
    is used.
    """
    if kind not in ("invite", "reset"):
        raise ValueError("kind must be 'invite' or 'reset'")
    username = (username or "").strip().lower()
    con = db.connect()
    if not con.execute("SELECT 1 FROM app_user WHERE username=?", (username,)).fetchone():
        con.close()
        raise ValueError(f"no user called {username!r}")
    raw = secrets.token_urlsafe(32)
    life = dt.timedelta(days=INVITE_DAYS) if kind == "invite" else dt.timedelta(hours=RESET_HOURS)
    con.execute("INSERT INTO account_token VALUES (?,?,?,?,?,?,NULL,?)",
                (_hash_token(raw), username, kind,
                 _now().isoformat(timespec="seconds"), by,
                 (_now() + life).isoformat(timespec="seconds"), note))
    con.commit()
    con.close()
    return raw


def check_link(raw: str) -> dict | None:
    """-> {username, kind, expires_at} if this link can still be used, else None."""
    if not raw:
        return None
    con = db.connect()
    r = con.execute("SELECT * FROM account_token WHERE token_hash=?",
                    (_hash_token(raw),)).fetchone()
    con.close()
    if not r or r["used_at"]:
        return None
    try:
        if dt.datetime.fromisoformat(r["expires_at"]) < _now():
            return None
    except (TypeError, ValueError):
        return None
    return dict(username=r["username"], kind=r["kind"], expires_at=r["expires_at"])


def use_link(raw: str, password: str) -> dict:
    """Set the password this link is for, and spend the link. Raises if it is not valid.

    The spend and the password change happen in one transaction. Half of this succeeding
    would either leave a live link on a changed account or a spent link on an unchanged
    one, and both are worse than failing.
    """
    info = check_link(raw)
    if not info:
        raise ValueError("this link has already been used, or it has expired")
    hashed = hash_password(password)          # raises on a password that is too short
    con = db.connect()
    con.execute("UPDATE app_user SET pwd_hash=? WHERE username=?",
                (hashed, info["username"]))
    con.execute("UPDATE account_token SET used_at=? WHERE token_hash=?",
                (_now().isoformat(timespec="seconds"), _hash_token(raw)))
    # Any other outstanding link for this account is now void. Someone who asked twice
    # should not leave a spare key lying around.
    con.execute("UPDATE account_token SET used_at=? WHERE username=? AND used_at IS NULL",
                (_now().isoformat(timespec="seconds"), info["username"]))
    con.commit()
    con.close()
    return info


def links(username: str | None = None) -> list[dict]:
    """Outstanding links, newest first. Never returns a token -- there is none to return."""
    con = db.connect()
    sql = ("SELECT username, kind, created_at, created_by, expires_at, note, token_hash "
           "FROM account_token WHERE used_at IS NULL")
    args = ()
    if username:
        sql += " AND username=?"
        args = (username.strip().lower(),)
    rows = [dict(r) for r in con.execute(sql + " ORDER BY created_at DESC", args)]
    con.close()
    now = _now()
    out = []
    for r in rows:
        try:
            r["expired"] = dt.datetime.fromisoformat(r["expires_at"]) < now
        except (TypeError, ValueError):
            r["expired"] = True
        r["ref"] = r.pop("token_hash")[:12]     # enough to revoke by, useless as a key
        out.append(r)
    return out


def revoke_link(ref: str) -> bool:
    """Kill an outstanding link by the short reference shown in the admin page."""
    con = db.connect()
    n = con.execute(
        "UPDATE account_token SET used_at=? WHERE used_at IS NULL AND token_hash LIKE ?",
        (_now().isoformat(timespec="seconds"), (ref or "") + "%")).rowcount
    con.commit()
    con.close()
    return bool(n)


def has_password(username: str) -> bool:
    con = db.connect()
    r = con.execute("SELECT pwd_hash FROM app_user WHERE username=?",
                    ((username or "").strip().lower(),)).fetchone()
    con.close()
    return bool(r) and r["pwd_hash"] != NO_PASSWORD


def change_password(username: str, old: str, new: str) -> None:
    """A person changing their OWN password, signed in. Requires the old one."""
    if not authenticate(username, old):
        raise ValueError("that is not your current password")
    set_password(username, new)


def delete_user(username: str) -> None:
    con = db.connect()
    u = username.strip().lower()
    con.execute("DELETE FROM app_user WHERE username=?", (u,))
    # and any outstanding link, which would otherwise be a live key to a recreated
    # account with the same name.
    con.execute("DELETE FROM account_token WHERE username=?", (u,))
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

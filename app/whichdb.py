"""
Which database is DATABASE_URL actually pointing at?

There are three of them in this project -- the app's, the catalogue's, and whatever is on
your laptop -- and they are told apart by a connection string in an environment variable.
Paste the wrong one and nothing errors: auth.py would cheerfully create the app's eight
tables inside the catalogue's database and put the account there, and the only symptom
would be an app that still says no accounts exist.

So this looks, and changes nothing. It creates no tables, writes no rows and runs one
read-only query. Run it before anything that writes.

    python whichdb.py
"""
from __future__ import annotations

import os
import sys

import store

# Tables that only ever exist in one of them.
APP = {"app_user", "tenant", "purchase_line", "upload", "upload_line", "analysis_run"}
CATALOGUE = {"golden_product", "curated_pin", "supplier_product", "nevo_food",
             "rivm_footprint", "eat_profile"}


def main() -> int:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("DATABASE_URL is not set, so this would use SQLite on this laptop:")
        print(f"  {os.environ.get('MIST_APP_DB', '(the default under data/)')}")
        print("\nSet it first if you meant to look at a server:")
        print('  $env:DATABASE_URL = "<the public Postgres URL>"')
        return 0

    # Everything after the @ is host/port/database. Never print what comes before it.
    where = url.split("@")[-1]
    print(f"connecting to  {where}\n")

    try:
        con = store.connect()
    except Exception as e:
        print(f"could not connect: {type(e).__name__}: {str(e)[:160]}")
        if "proxy.rlwy.net" not in url and "railway.internal" in url:
            print("\nThat is the PRIVATE url. It only resolves inside Railway.")
            print("Use DATABASE_PUBLIC_URL instead -- it has proxy.rlwy.net in it.")
        return 1

    tables = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
    con.close()

    if not tables:
        print("This database is EMPTY. Nothing has created a schema in it yet.")
        print("That is normal for a brand new Postgres, and it means this check")
        print("cannot tell you which one it is. Be sure before you write to it.")
        return 0

    app_hits, cat_hits = tables & APP, tables & CATALOGUE
    print(f"{len(tables)} table(s): {', '.join(sorted(tables))}\n")

    if app_hits and not cat_hits:
        print("=> This is the APP database. Right one for auth.py add-user.")
        return 0
    if cat_hits and not app_hits:
        print("=> This is the CATALOGUE database. WRONG one for auth.py add-user --")
        print("   creating an account here would build the app's tables inside the")
        print("   catalogue. Get DATABASE_PUBLIC_URL from the app project instead.")
        return 1
    if app_hits and cat_hits:
        print("=> BOTH schemas are in here, which was not the plan. The app and the")
        print("   catalogue are meant to be separate databases. Worth untangling")
        print("   before adding anything else to it.")
        return 1

    print("=> Unrecognised. Neither schema is in this database.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

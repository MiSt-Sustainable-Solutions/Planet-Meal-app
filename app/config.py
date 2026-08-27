"""
Where things are. Nothing else in the app may hardcode a path or a URL.

Overridable by environment variable, so a Railway deployment differs from local by
configuration only:

    MIST_CATALOGUE_API   the shared catalogue API   (default: http://127.0.0.1:8077)
    MIST_APP_DB          this app's own database    (default: data/planetprocure.db)
    MIST_TENANT          the FIRST client, at setup   (default: tudelft)
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
for _d in (DATA, UPLOADS):
    _d.mkdir(parents=True, exist_ok=True)

# The shared catalogue API. It owns the product catalogue, the rules and the maths.
# It holds NO client data — everything below belongs to this app.
CATALOGUE_API = os.environ.get("MIST_CATALOGUE_API", "http://127.0.0.1:8077").rstrip("/")

# This app's database: purchase history, uploads, saved analyses.
APP_DB = os.environ.get("MIST_APP_DB", str(DATA / "planetprocure.db"))

# NOT "which client this deployment serves" -- one deployment serves all of them, and a
# signed-in account carries its own tenant. This is the id of the tenant row created on
# first boot, and the fallback for the handful of functions that still accept no tenant.
# That fallback is the dangerous half: it does not raise, it silently answers about
# whichever client is named here. Routes must always pass a tenant explicitly.
TENANT = os.environ.get("MIST_TENANT", "tudelft")

# SEED VALUES ONLY. The name a client is shown lives on its row in the `tenant` table,
# because one deployment serves several clients and an environment variable cannot differ
# between them. These two are used exactly twice: to create the first tenant on a fresh
# install, and as the fallback for an admin who is viewing no tenant at all. Everything
# that renders a name calls auth.tenant_names().
CLIENT_NAME = os.environ.get("MIST_CLIENT_NAME", "TU Delft")
CATERER_NAME = os.environ.get("MIST_CATERER_NAME", "APPèL")

# how long to wait on the catalogue API; scoring a full year is ~29k lines
API_TIMEOUT = float(os.environ.get("MIST_API_TIMEOUT", "300"))

# Keys for the catalogue's write endpoints. Reading needs neither.
#
#   CATALOGUE_KEY        lets this app teach the catalogue a product it has never seen.
#                        Routine, happens on every import.
#   CATALOGUE_ADMIN_KEY  lets it file curated decisions, which outrank every rule for
#                        every client. Only a MiSt deployment should hold this, and only
#                        an admin can reach the route that uses it.
#
# Empty locally, where the catalogue runs unprotected on 127.0.0.1.
CATALOGUE_KEY = os.environ.get("MIST_CATALOGUE_KEY", "").strip()
CATALOGUE_ADMIN_KEY = os.environ.get("MIST_CATALOGUE_ADMIN_KEY", "").strip()

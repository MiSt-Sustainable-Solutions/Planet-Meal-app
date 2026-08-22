"""
Where things are. Nothing else in the app may hardcode a path or a URL.

Overridable by environment variable, so a Railway deployment differs from local by
configuration only:

    MIST_CATALOGUE_API   the shared catalogue API   (default: http://127.0.0.1:8077)
    MIST_APP_DB          this app's own database    (default: data/planetprocure.db)
    MIST_TENANT          which client this serves   (default: tudelft)
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

TENANT = os.environ.get("MIST_TENANT", "tudelft")

CLIENT_NAME = os.environ.get("MIST_CLIENT_NAME", "TU Delft")
CATERER_NAME = os.environ.get("MIST_CATERER_NAME", "APPèL")

# how long to wait on the catalogue API; scoring a full year is ~29k lines
API_TIMEOUT = float(os.environ.get("MIST_API_TIMEOUT", "300"))

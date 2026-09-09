"""
The client for the shared catalogue API.

The only place this app talks to the catalogue. Everything goes over HTTP, even in local
development, because that boundary is the product: the catalogue is a shared asset that
gets better with every client, and this app is one client's packaging of it.

We send purchase lines and get scored results back. We never send who the client is, and
the catalogue never stores what we sent.

Every call degrades rather than explodes. If the catalogue is down the upload page still
works, the pre-flight still runs, and the parts that need it say so.
"""
from __future__ import annotations

import httpx

import config


class CatalogueDown(Exception):
    """The shared catalogue could not be reached or refused the request."""


def _client(admin: bool = False) -> httpx.Client:
    """A client for the catalogue. `admin=True` presents the stronger key.

    The admin key is sent ONLY by the call that files curated decisions. Attaching it to
    every request would make it the app's everyday credential, and then a leak of the
    routine key would be a leak of the one that can rewrite the catalogue for everybody.
    """
    key = (config.CATALOGUE_ADMIN_KEY if admin else config.CATALOGUE_KEY)
    headers = {"X-MiSt-Key": key} if key else {}
    return httpx.Client(base_url=config.CATALOGUE_API, timeout=config.API_TIMEOUT,
                        headers=headers)


def health() -> dict | None:
    try:
        with _client() as c:
            r = c.get("/health", timeout=5)
            return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def version() -> dict | None:
    """Which version of the catalogue is live right now.

    One cheap call, made before every analysis. If it matches the version a saved result
    was calculated against, that result is still the right answer and nothing needs to be
    recalculated — which is the difference between an instant page and a thirty-second
    one.

    Returns None if the catalogue cannot be reached. The caller must then serve a saved
    result and SAY it is a saved result, never assume the version is unchanged.
    """
    try:
        with _client() as c:
            r = c.get("/catalogue/version", timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def score(lines: list[dict], label: str = "analysis", profile: str | None = None,
          top: int = 20, queue: int = 60) -> dict:
    """Score purchase lines. -> the whole dashboard in one response.

    Raises CatalogueDown so the caller can show something honest instead of a stack trace.
    """
    if not lines:
        raise CatalogueDown("there are no purchase lines to score")
    payload = {"lines": lines, "label": label, "top": top, "queue": queue}
    if profile:
        payload["profile"] = profile
    try:
        with _client() as c:
            r = c.post("/analysis/run", json=payload)
    except Exception as e:
        raise CatalogueDown(
            f"could not reach the catalogue API at {config.CATALOGUE_API} "
            f"({type(e).__name__}). Is it running?") from e
    if r.status_code != 200:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith(
            "application/json") else r.text
        raise CatalogueDown(f"the catalogue refused the request ({r.status_code}): {detail}")
    return r.json()


def score_lines(lines: list[dict], label: str = "analysis",
                profile: str | None = None) -> dict:
    """Every line back with what the catalogue made of it. -> {window, lines, rows}.

    The aggregates say the footprint is a number. This says which rows it is made of and
    where each one came from -- the group, the kg CO2e, and which rung of which ladder
    produced them. It is what lets a client check any figure back to its source.
    """
    if not lines:
        raise CatalogueDown("there are no purchase lines to score")
    payload = {"lines": lines, "label": label}
    if profile:
        payload["profile"] = profile
    try:
        with _client() as c:
            r = c.post("/analysis/run/lines", json=payload)
    except Exception as e:
        raise CatalogueDown(
            f"could not reach the catalogue API at {config.CATALOGUE_API} "
            f"({type(e).__name__}). Is it running?") from e
    if r.status_code != 200:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith(
            "application/json") else r.text
        raise CatalogueDown(f"the catalogue refused the request ({r.status_code}): {detail}")
    return r.json()


def work_queue(lines: list[dict], label: str = "analysis", limit: int = 50) -> dict:
    try:
        with _client() as c:
            r = c.post("/analysis/run/work-queue",
                       json={"lines": lines, "label": label, "top": limit})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise CatalogueDown(f"could not fetch the work queue: {type(e).__name__}") from e


def explain(artikelnr: str, lines: list[dict], label: str = "analysis") -> dict | None:
    try:
        with _client() as c:
            r = c.post(f"/analysis/run/product/{artikelnr}",
                       json={"lines": lines, "label": label})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise CatalogueDown(f"could not explain {artikelnr}: {type(e).__name__}") from e


def learn(products: list[dict], dry_run: bool = False) -> dict | None:
    """Tell the catalogue about products it has never seen.

    Called when an upload is committed. This is the only call that changes the shared
    catalogue, and what it stores is knowledge about a PRODUCT — food group, footprint,
    and the tier that produced them. No quantities, no prices, no client identity.

    Returns None if the catalogue is unreachable. That is deliberately non-fatal: the
    client's own import has already succeeded, and the products will be learned the next
    time something is scored. Never fail a commit because a shared service is down.
    """
    if not products:
        return {"learned": 0, "already_known": 0, "rows": []}
    try:
        with _client() as c:
            r = c.post("/catalogue/learn",
                       json={"products": products, "dry_run": dry_run}, timeout=180)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def review_sheet(products: list[dict], filename: str = "MiSt_review.xlsx",
                 limit: int = 300) -> tuple[bytes, int]:
    """Ask the catalogue for the review sheet. -> (xlsx bytes, how many rows).

    The sheet is built by the catalogue, not here. It is full of reference codes, food
    groups and footprint figures — the catalogue's knowledge, none of it this client's
    business — and the endpoint that reads the answers back lives beside the code that
    writes them, so the two halves can never drift out of step.
    """
    try:
        with _client() as c:
            r = c.post("/catalogue/review-sheet",
                       json={"products": products, "limit": limit, "filename": filename},
                       timeout=180)
    except Exception as e:
        raise CatalogueDown(
            f"could not reach the catalogue to build the review sheet ({type(e).__name__})") from e
    if r.status_code != 200:
        raise CatalogueDown(f"the catalogue refused to build the sheet ({r.status_code})")
    return r.content, int(r.headers.get("X-Review-Rows", 0))


def curate(data: bytes, filename: str = "review.xlsx", dry_run: bool = False,
           by: str = "") -> dict:
    """Send an answered review sheet back. THIS CHANGES THE SHARED CATALOGUE.

    Every client sees the result, which is the point: a decision made once is made for
    everyone. It is also why this is a deliberate action behind a button and not something
    that happens as a side effect of anything else.

    `by` names the person, which the catalogue records against every decision. The admin
    key says the request is allowed; it does not say who made it, and a history that
    cannot answer "who decided this" answers half the question.
    """
    try:
        with _client(admin=True) as c:
            r = c.post("/catalogue/curate",
                       files={"file": (filename, data)},
                       headers={"x-mist-actor": by} if by else {},
                       params={"dry_run": str(bool(dry_run)).lower()}, timeout=300)
    except Exception as e:
        raise CatalogueDown(
            f"could not reach the catalogue to file the decisions ({type(e).__name__})") from e
    if r.status_code != 200:
        detail = r.json().get("detail") if r.headers.get(
            "content-type", "").startswith("application/json") else r.text
        raise CatalogueDown(f"the catalogue refused the sheet ({r.status_code}): {detail}")
    return r.json()


def decisions(limit: int = 20) -> dict | None:
    """What the catalogue has been taught so far. None if it cannot be reached."""
    try:
        with _client() as c:
            r = c.get("/catalogue/decisions", params={"limit": limit}, timeout=30)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def eat_profiles() -> dict | None:
    try:
        with _client() as c:
            r = c.get("/eat/profiles", timeout=15)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def recognise(products: list[dict]) -> dict | None:
    """How many of these products does the catalogue already know, and which categories
    are unmapped? Returns None if the catalogue is unreachable — the caller must then say
    'unavailable' rather than assume everything is fine.
    """
    if not products:
        return {"recognised": 0, "unmapped_categories": []}
    try:
        with _client() as c:
            r = c.post("/catalogue/recognise", json={"products": products}, timeout=60)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


# --------------------------------------------------------------------------- browsing
#
# Looking things up rather than computing anything. The only lookup used to be by
# barcode -- the identifier you are least likely to be holding -- so "what did we decide
# about this" had no answer short of opening the database, in a product whose whole claim
# is that any number can be followed back to its source.
def search(q: str, limit: int = 40) -> dict | None:
    """Products, references, groups and decisions matching one query. None if unreachable."""
    try:
        with _client() as c:
            r = c.get("/catalogue/search", params={"q": q, "limit": limit}, timeout=60)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def explain(supplier: str, sku: str) -> dict | None:
    """One product and every reason it has the numbers it has. None if unreachable or gone."""
    try:
        with _client() as c:
            r = c.get(f"/catalogue/explain/{supplier}/{sku}", timeout=60)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def reference(kind: str = "groups", q: str = "", limit: int = 400) -> dict | None:
    """The shelf every answer is drawn from: groups, RIVM products, or bucket averages."""
    try:
        with _client() as c:
            r = c.get("/catalogue/reference",
                      params={"kind": kind, "q": q, "limit": limit}, timeout=60)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def retract(supplier: str, sku: str, why: str = "", by: str = "") -> dict:
    """Remove one curated decision. THE PRODUCT FALLS BACK TO THE AUTOMATIC LADDER.

    Raises rather than returning None: unlike the reads above, a caller must never treat
    "we could not reach the catalogue" as "it is done".
    """
    try:
        with _client(admin=True) as c:
            r = c.post("/catalogue/decisions/retract",
                       json={"supplier": supplier, "supplier_sku": sku, "why": why},
                       headers={"x-mist-actor": by} if by else {}, timeout=60)
    except Exception as e:
        raise CatalogueDown(
            f"could not reach the catalogue to retract the decision "
            f"({type(e).__name__})") from e
    if r.status_code != 200:
        detail = r.json().get("detail") if r.headers.get(
            "content-type", "").startswith("application/json") else r.text
        raise CatalogueDown(f"the catalogue refused the retraction "
                            f"({r.status_code}): {detail}")
    return r.json()

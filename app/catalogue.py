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


def _client() -> httpx.Client:
    return httpx.Client(base_url=config.CATALOGUE_API, timeout=config.API_TIMEOUT)


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


def curate(data: bytes, filename: str = "review.xlsx", dry_run: bool = False) -> dict:
    """Send an answered review sheet back. THIS CHANGES THE SHARED CATALOGUE.

    Every client sees the result, which is the point: a decision made once is made for
    everyone. It is also why this is a deliberate action behind a button and not something
    that happens as a side effect of anything else.
    """
    try:
        with _client() as c:
            r = c.post("/catalogue/curate",
                       files={"file": (filename, data)},
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

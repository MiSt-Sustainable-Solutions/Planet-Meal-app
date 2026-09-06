"""
PLANETmeal — the web app.

Server-rendered pages, no build step, no external JavaScript. The design system is plain
HTML and CSS and is copied rather than re-implemented, which is the only way its rules
("gold is buttons only", "heading italics are teal") survive contact with a component tree.

The app owns the client's data and the screens. Everything that needs the catalogue —
scoring, provenance, the work queue — goes over HTTP to the shared API. When that API is
down the app still runs: you can upload, validate and browse, and the pages that need
scoring say so plainly instead of failing.

    uvicorn app.main:app --port 8080 --reload
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import (FastAPI, File, Form, HTTPException, Query, Request,
                     UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from fastapi.templating import Jinja2Templates

import adapters
import analysis
import auth
import catalogue
import charts
import config
import db
import lines_export
import selection
import uploads
from adapters import mist_template


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once before the first request is served.

    This was @app.on_event("startup"), which FastAPI deprecated and will remove -- the
    same class of defect as the TemplateResponse signature that took every page down,
    caught the same way: by running the suite with deprecation warnings promoted to
    errors rather than waiting for a major version to arrive.

    Worth doing early because of what it does. It creates the schema and seeds the first
    tenant. A startup hook that silently stopped running would not fail at boot -- the
    app would come up, pass its health check, and fall over on the first query against a
    database with no tables in it.
    """
    db.init()
    auth.init()
    # Purchase lines that predate file tracking would otherwise become invisible the
    # moment "only a selected file counts" took effect. Adopting them keeps every number
    # where it is and puts a row on the Files page saying what it is.
    selection.adopt_orphans()
    yield


HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="PLANETmeal", docs_url="/api/docs", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))

# Everything needs a login EXCEPT these. The list is short and explicit so that a route
# added later is protected by default -- the failure mode of forgetting is "nobody can
# reach it", not "everybody can".
PUBLIC_PATHS = {"/login", "/logout", "/api/health", "/favicon.ico"}

# Reached by someone who has no account yet, or who has forgotten their password, so it
# cannot sit behind the login. The link itself is the credential: 32 random bytes, single
# use, expiring, and stored only as a hash.
PUBLIC_PREFIXES = ("/static", "/set-password")

# Only an admin may reach these. Uploading and curating both change data that outlives
# the person doing it -- an upload becomes a client's history, and a curated pin
# outranks every rule for every client, forever. Neither belongs to a client account.
#
# /files/owner is named one by one rather than gating /files, because the Files PAGE is
# for the client too: what their numbers are built from is not a secret from them. But
# naming the file that supplies a contested month decides what those numbers ARE, which
# is not theirs to change. Everything else under /files is a read.
ADMIN_PATHS = ("/upload", "/curate", "/review-sheet.xlsx", "/admin", "/files/owner")


@app.middleware("http")
async def gate(request: Request, call_next):
    path = request.url.path
    if path.startswith(PUBLIC_PREFIXES) or path in PUBLIC_PATHS:
        return await call_next(request)

    me = auth.current(request)
    if me is None:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "not signed in"}, status_code=401)
        nxt = quote(path + ("?" + request.url.query if request.url.query else ""))
        return RedirectResponse(f"/login?next={nxt}", status_code=303)

    if any(path == a or path.startswith(a + "/") for a in ADMIN_PATHS) and not me.is_admin:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "admin only"}, status_code=403)
        denied_client, denied_caterer = auth.tenant_names(me.tenant)
        return templates.TemplateResponse(
            request, "denied.html", dict(request=request, me=me, page="", what=path,
                                         client_name=denied_client,
                                         caterer_name=denied_caterer,
                                         support_emails=config.support_emails(),
                                catalogue_api=config.CATALOGUE_API, api_up=True,
                                         tier_swatch=charts.TIER_SWATCH, fg=charts.food_group_label,
                                         stale=None, tenants=[], viewing=None),
            status_code=403)
    return await call_next(request)


# Added AFTER the gate, deliberately. Starlette runs the most recently added middleware
# OUTERMOST, so this puts the session layer around the gate -- and the gate can only read
# request.session because the session middleware has already run by then. Added before it,
# the gate fires first, there is no session on the request, and every single page 500s.
app.add_middleware(SessionMiddleware, secret_key=auth.session_secret(),
                   session_cookie="planetprocure", same_site="lax",
                   https_only=os.environ.get("MIST_ENV", "").lower().startswith("prod"),
                   max_age=60 * 60 * 12)


# COMMIT_MODES used to live here: new_only, replace, all.
#
# Three modes existed so that a cumulative export could be imported without counting a
# month twice, and choosing the wrong one doubled a year with nothing on screen to say
# so. There is nothing to choose now -- every line a file supplies is stored when the
# file is read, and a month is counted from exactly one selected file. See selection.py.


def relabel(result: dict) -> dict:
    """Swap the engine's internal bucket keys for language a caterer uses.

    Done here rather than in the engine: the keys are the catalogue's business, the words
    are the app's. A different client could label the same bucket differently.
    """
    for row in result.get("by_food_group", []):
        row["food_group"] = charts.food_group_label(row["food_group"])
    for row in result.get("top_contributors", []):
        row["food_group"] = charts.food_group_label(row["food_group"])
    return result


def me(request: Request) -> auth.Principal:
    """Who is asking. Guaranteed present: the gate middleware runs before every route."""
    p = auth.current(request)
    if p is None:                      # only reachable if the gate is ever bypassed
        raise HTTPException(401, "not signed in")
    return p


def ctx(request: Request, page: str, **kw) -> dict:
    """Everything every template needs."""
    # ONE call to the catalogue per page. /health answers both questions a page asks —
    # is it up, and what version is it on — so asking twice was a wasted round trip on
    # every single render.
    health = catalogue.health()
    who = auth.current(request)
    tenant = who.tenant if who else None
    all_tenants = auth.tenants()
    client_name, caterer_name = auth.tenant_names(tenant, all_tenants)
    base = dict(request=request, page=page, me=who,
                tenants=all_tenants if (who and who.is_admin) else [],
                viewing=tenant,
                client_name=client_name,
                caterer_name=caterer_name,
                support_emails=config.support_emails(),
                catalogue_api=config.CATALOGUE_API, api_up=health is not None,
                tier_swatch=charts.TIER_SWATCH, fg=charts.food_group_label,
                stale=analysis.staleness(tenant=tenant,
                                         live=(health or {}).get("catalogue")))
    base.update(kw)
    return base


# --------------------------------------------------------------------------- sign in
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str | None = None,
               set: int = 0):
    if auth.current(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", dict(
        request=request, next=next, error=error, setup=not auth.any_users(),
        set=bool(set), support_emails=config.support_emails()))


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          next: str = Form("/")):
    user = auth.authenticate(username, password)
    if not user:
        # One message for both failures. Saying "no such user" tells an attacker which
        # usernames exist, which is half of a password guess already done for them.
        return templates.TemplateResponse(request, "login.html", dict(
            request=request, next=next, setup=not auth.any_users(), set=False,
            support_emails=config.support_emails(),
            error="That username and password do not match."), status_code=401)
    auth.sign_in(request, user)
    dest = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(dest, status_code=303)


@app.get("/logout")
def logout(request: Request):
    auth.sign_out(request)
    return RedirectResponse("/login", status_code=303)


@app.post("/admin/viewing")
def set_viewing(request: Request, tenant: str = Form(...), back: str = Form("/")):
    """Admin only: switch which client you are looking at."""
    if not auth.view_tenant(request, tenant):
        raise HTTPException(403, "only an admin can switch client")
    dest = back if back.startswith("/") and not back.startswith("//") else "/"
    return RedirectResponse(dest, status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, error: str | None = None,
               new_link: str | None = None, new_user: str | None = None,
               pw_error: str | None = None, done: int = 0):
    """Who exists, and what each of them can see.

    `new_link` is shown exactly once, right after it is made. It is never stored in a
    form a person could go back to, and it cannot be recovered afterwards -- only its
    hash is kept. Losing it costs nothing: make another.
    """
    base = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(request, "admin.html", ctx(
        request, "admin", users=auth.users(), all_tenants=auth.tenants(),
        decisions=catalogue.decisions(limit=1),
        error=error, outstanding=auth.links(),
        pw_error=pw_error, done=bool(done),
        new_link=(f"{base}/set-password/{new_link}" if new_link else None),
        new_user=new_user))


# --------------------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, window: str | None = None, refresh: int = 0,
              file: list[str] | None = Query(None)):
    """`?refresh=1` recalculates instead of serving the saved result.

    Everything else reads the cache: the same window, the same purchase data and the same
    catalogue version means the saved answer is still the right answer.

    `?file=a&file=b` asks the other question this page can answer: not "what did we buy
    in FY2025" but "what do these files say". Composed here into one window string rather
    than threaded through as a second concept, so caching, exporting and the per-line
    sheet all keep working without knowing the difference.
    """
    p = me(request)
    # Archived files and files holding no purchase lines are not worth offering: one is
    # deliberately out of the way, the other would only produce an error saying so.
    pickable = [u for u in visible_uploads(p) if not u["archived"] and u["held"]]
    if file:
        # Only files this person can see. Otherwise ?file= is a way to read another
        # tenant's data, or a client's way to reach a file we are deliberately not showing.
        allowed = {u["upload_id"] for u in pickable}
        file = [f for f in file if f in allowed]
    chosen_ids = sorted(set(file or []))
    selected = ("files:" + ",".join(chosen_ids)) if chosen_ids else (
        window or analysis.default_window(p.tenant))

    try:
        result = analysis.run(window=selected, force=bool(refresh), tenant=p.tenant)
    except (analysis.WindowError, catalogue.CatalogueDown) as e:
        return templates.TemplateResponse(
            request, "dashboard.html", ctx(request, "dashboard", error=str(e),
                                           window_options=analysis.windows(p.tenant),
                                           pickable=pickable, chosen_ids=chosen_ids,
                                           selected_window=window))

    relabel(result)
    return templates.TemplateResponse(request, "dashboard.html", ctx(
        request, "dashboard",
        result=result, selected_window=selected, stale=result.get("stale"),
        window_options=analysis.windows(p.tenant),
        pickable=pickable, chosen_ids=chosen_ids,
        conf_bar=charts.confidence(result["headline"]["confidence"]["by_tier"]),
        trend=charts.line(result["by_month"], "period", "co2_kg", "complete"),
        rest_chart=charts.bars(result["by_restaurant"], "restaurant", "co2_kg",
                               secondary_key="intensity_kg_co2_per_kg", limit=20),
        group_chart=charts.bars(result["by_food_group"], "food_group", "co2_kg",
                                width=620, pad_l=150, pad_r=110,
                                secondary_key="pct_of_co2", secondary_suffix="%", limit=14),
        eat_chart=charts.paired(result["eat_lancet"]["rows"])))


@app.get("/data-health", response_class=HTMLResponse)
def data_health(request: Request, window: str | None = None, refresh: int = 0,
                curated: str | None = None, curate_error: str | None = None):
    p = me(request)
    try:
        selected = window or analysis.default_window(p.tenant)
        result = analysis.run(window=selected, force=bool(refresh), tenant=p.tenant)
    except (analysis.WindowError, catalogue.CatalogueDown) as e:
        return templates.TemplateResponse(
            request, "data_health.html", ctx(request, "health", error=str(e),
                                             months=db.months(p.tenant)))

    relabel(result)

    # The work queue arrives inside the same response as everything else, sliced from the
    # same scored frame. This page used to post all 29,000 lines a SECOND time to fetch
    # it, which doubled the wait for a list that was already computed.
    wq = result.get("work_queue")
    wq_err = None if wq else "the catalogue did not return a work queue for this window"

    return templates.TemplateResponse(request, "data_health.html", ctx(
        request, "health", result=result, months=db.months(p.tenant),
        stale=result.get("stale"),
        work_queue=wq, work_queue_error=wq_err, selected_window=selected,
        curated=curated, curate_error=curate_error,
        decisions=catalogue.decisions(limit=1)))


# --------------------------------------------------------------------------- review loop
def _review_products(tenant: str, window: str | None = None) -> tuple[list[dict], str]:
    """This window's curation backlog, shaped for the catalogue's review sheet.

    The weight and the current figure come from the analysis; the barcode comes from the
    app's own product table, because that is where the client's supplier data lives.
    """
    selected = window or analysis.default_window(tenant)
    result = analysis.run(window=selected, tenant=tenant)
    queue = (result.get("work_queue") or {}).get("rows") or []
    con = db.connect()
    bars = {r["artikelnr"]: (r["ean"] or "") for r in con.execute(
        "SELECT artikelnr, ean FROM product WHERE tenant=?", (tenant,))}
    con.close()
    return [dict(artikelnr=r["artikelnr"], description=r["description"] or "",
                 category=r["category"] or "",
                 ean_ce=r.get("gtin") or bars.get(r["artikelnr"], ""),
                 kg=r["food_kg"], co2=r.get("co2_per_kg") or 0.0,
                 bucket=r.get("food_group"), footprint_src=r.get("source"),
                 supplier="sligro")
            for r in queue], selected


@app.get("/review-sheet.xlsx")
def review_sheet(request: Request, window: str | None = None, limit: int = 300):
    """Download the review sheet for this window. Heaviest unresolved products first."""
    p = me(request)
    try:
        products, selected = _review_products(p.tenant, window)
        if not products:
            raise HTTPException(400, "nothing is waiting for review in this window")
        name = f"MiSt_review_{p.tenant}_{selected}.xlsx"
        data, rows = catalogue.review_sheet(products, filename=name, limit=limit)
    except (analysis.WindowError, catalogue.CatalogueDown) as e:
        raise HTTPException(503, str(e))
    if not rows:
        raise HTTPException(400, "every product in this window has already been decided")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/curate")
async def curate(request: Request, file: UploadFile = File(...),
                 check: str | None = Form(None)):
    """Send an answered review sheet to the shared catalogue.

    Deliberately its own action. Every other client sees the result, so it is never a side
    effect of an upload or a page load.

    "Check it first" parses and validates the whole sheet and reports exactly what WOULD be
    filed, writing nothing. Worth doing: a decision here outranks every rule for every
    client, and the only thing worse than a wrong answer is finding out afterwards.
    """
    me(request)                       # admin only; the gate has already checked
    raw = await file.read()
    dry = bool(check)
    try:
        report = catalogue.curate(raw, filename=file.filename or "review.xlsx", dry_run=dry)
    except catalogue.CatalogueDown as e:
        return RedirectResponse(f"/data-health?curate_error={quote(str(e))}", status_code=303)
    lead = ("Checked, nothing written yet — this sheet would file "
            if dry else "Filed ")
    msg = (f"{lead}{report['decisions']} decision(s)"
           + (f"; {report['refused']} would be refused" if dry and report.get("refused")
              else (f", {report['refused']} refused" if report.get("refused") else ""))
           + f". {report.get('portable', 0)} carry a barcode, so they apply to the same "
             "product from any wholesaler, for any client.")
    return RedirectResponse(f"/data-health?curated={quote(msg)}", status_code=303)


@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    p = me(request)
    return templates.TemplateResponse(request, "history.html", ctx(
        request, "history", runs=analysis.history(tenant=p.tenant),
        uploads=uploads.listing(tenant=p.tenant)))


# --------------------------------------------------------------------------- accounts
def _slug(name: str) -> str:
    """A tenant id from a client's name. Lowercase, letters and digits only.

    Derived rather than typed, because it is written into every purchase line that
    client will ever have and a typo in it is permanent.
    """
    out = "".join(c for c in (name or "").lower() if c.isalnum())
    return out[:24] or "client"


def _next_username(tenant: str) -> str:
    """tudelft1, tudelft2, ... One login per PERSON, all pointing at one client."""
    existing = {u["username"] for u in auth.users()}
    n = 1
    while f"{tenant}{n}" in existing:
        n += 1
    return f"{tenant}{n}"


@app.post("/admin/accounts")
def create_account(request: Request, client_name: str = Form(...),
                   note: str = Form(""), tenant: str = Form("")):
    """Create a client and its first login, or another login for an existing client.

    No password field, deliberately. The account is created with no password at all --
    it cannot be signed into -- and the person it belongs to sets one through a
    one-time link. Nobody at MiSt ever types, sees or handles a password that is not
    their own, which is the same principle that used to be served by keeping this on
    the command line.
    """
    p = me(request)
    try:
        if tenant:
            row = next((t for t in auth.tenants() if t["tenant"] == tenant), None)
            if not row:
                raise ValueError(f"no client called {tenant!r}")
            tid, display = row["tenant"], row["display_name"]
        else:
            tid = _slug(client_name)
            display = client_name.strip()
            if not display:
                raise ValueError("a client needs a name")
            if any(t["tenant"] == tid for t in auth.tenants()):
                raise ValueError(f"a client with the id {tid!r} already exists — "
                                 "add another login to it instead")
            auth.add_tenant(tid, display, "")
        username = _next_username(tid)
        auth.create_user(username, None, "client", tid, display)
        raw = auth.make_link(username, "invite", by=p.username, note=note)
    except ValueError as e:
        return admin_home(request, error=str(e))
    return admin_home(request, new_link=raw, new_user=username)


@app.post("/admin/accounts/link")
def reissue_link(request: Request, username: str = Form(...), note: str = Form("")):
    """A fresh link for an existing account — the answer to 'I forgot my password'."""
    p = me(request)
    try:
        kind = "invite" if not auth.has_password(username) else "reset"
        raw = auth.make_link(username, kind, by=p.username, note=note)
    except ValueError as e:
        return admin_home(request, error=str(e))
    return admin_home(request, new_link=raw, new_user=username)


@app.post("/admin/accounts/revoke")
def revoke_link(request: Request, ref: str = Form(...)):
    me(request)
    auth.revoke_link(ref)
    return RedirectResponse("/admin", status_code=303)


@app.get("/set-password/{token}", response_class=HTMLResponse)
def set_password_form(request: Request, token: str, error: str | None = None):
    """Public. The link IS the credential, so there is nothing else to prove."""
    info = auth.check_link(token)
    return templates.TemplateResponse(request, "set_password.html", dict(
        request=request, token=token, info=info, error=error))


@app.post("/set-password/{token}")
def set_password_submit(request: Request, token: str,
                        password: str = Form(...), again: str = Form(...)):
    if password != again:
        return set_password_form(request, token, error="Those two do not match.")
    try:
        auth.use_link(token, password)
    except ValueError as e:
        return set_password_form(request, token, error=str(e))
    return RedirectResponse("/login?set=1", status_code=303)


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, error: str | None = None, done: int = 0):
    return templates.TemplateResponse(request, "account.html", ctx(
        request, "account", error=error, done=bool(done)))


@app.post("/account")
def account_change(request: Request, current: str = Form(...),
                   password: str = Form(...), again: str = Form(...),
                   back: str = Form("")):
    """Changing your OWN password. The only place a password is typed by its owner.

    `back` exists because the same form is on two pages -- an admin changes theirs from
    the Accounts page, a client from theirs -- and landing on the wrong one afterwards
    would be its own small confusion. Only ever '/admin', never an arbitrary URL: an
    open redirect is a phishing tool, and this one would be handed out by a page people
    are told to trust.
    """
    p = me(request)
    home = "/admin" if (back == "/admin" and p.is_admin) else "/account"
    fail = (lambda msg: admin_home(request, pw_error=msg)) if home == "/admin"         else (lambda msg: account_page(request, error=msg))
    if password != again:
        return fail("Those two do not match.")
    try:
        auth.change_password(p.username, current, password)
    except ValueError as e:
        return fail(str(e))
    return RedirectResponse(f"{home}?done=1", status_code=303)


def visible_uploads(p) -> list[dict]:
    """The files this person may see.

    A client sees the files their numbers are made of, and only those. Everything else --
    files kept but not counted, the archive, a month two files both supply -- is MiSt's
    working state, and showing it to a client raises questions about their data that are
    really questions about our housekeeping.

    One function because two screens need the same answer: the Files page lists them and
    the dashboard offers them to choose between. Two copies of this rule would let a
    client pick a file they cannot see.
    """
    held = uploads.listing(limit=200, tenant=p.tenant)
    if p.is_admin:
        return held
    return [u for u in held if u["selected"] and not u["archived"]]


@app.get("/files", response_class=HTMLResponse)
def files_page(request: Request, error: str | None = None):
    """One place for files. Deliberately NOT admin-only.

    A client cannot upload, undo or discard -- every action on this page posts to a
    route that is already admin-gated, so seeing it grants nothing. But what their
    numbers are built from is not a secret from them, and hiding the list would make it
    look like one.
    """
    p = me(request)
    snap = db.snapshot(p.tenant)
    held = visible_uploads(p)
    return templates.TemplateResponse(request, "files.html", ctx(
        request, "files", error=error,
        uploads=held,
        adapters=adapters.listing(),
        contested=selection.contested(p.tenant) if p.is_admin else [],
        months_held=len(snap["months"])))


# --------------------------------------------------------------------------- upload
@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, error: str | None = None):
    """Kept so an old link still works; the upload form itself lives on /files now."""
    return RedirectResponse("/files" + (f"?error={quote(error)}" if error else ""),
                            status_code=303)


@app.get("/upload/template")
def download_template():
    path = os.path.join(str(config.DATA), "MiSt_purchase_template.xlsx")
    mist_template.write_template(path)
    return FileResponse(
        path, filename="MiSt_purchase_template.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...),
                      year: str | None = Form(None)):
    p = me(request)
    suffix = os.path.splitext(file.filename or "upload.xlsx")[1] or ".xlsx"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(file.file, out)
        y = int(year) if year and str(year).strip().isdigit() else None
        try:
            report = uploads.stage(tmp, file.filename, y, tenant=p.tenant)
        except Exception as e:
            return files_page(request, error=str(e))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return RedirectResponse(f"/upload/{report['upload_id']}", status_code=303)


@app.get("/upload/{upload_id}", response_class=HTMLResponse)
def upload_report(request: Request, upload_id: str, error: str | None = None):
    """One file: what was in it, what is wrong with it, and whether it counts."""
    p = me(request)
    report = uploads.get(upload_id, p.tenant)
    if report is None:
        raise HTTPException(404, f"no upload {upload_id}")
    return templates.TemplateResponse(request, "preflight.html", ctx(
        request, "files", report=report, error=error,
        contested=selection.contested(p.tenant)))


@app.post("/upload/{upload_id}/select")
def select_file(request: Request, upload_id: str, on: int = Form(1),
                back: str = Form("/files")):
    """Count this file, or stop counting it.

    The whole of what used to be committing and undoing. Nothing moves; a switch changes,
    and any month now supplied by two selected files gets an owner so it is still counted
    once.
    """
    p = me(request)
    try:
        selection.set_selected(upload_id, p.tenant, bool(on), by=p.username)
    except ValueError as e:
        return files_page(request, error=str(e))
    return RedirectResponse(back if back.startswith("/") else "/files", status_code=303)


@app.post("/upload/{upload_id}/archive")
def archive_file(request: Request, upload_id: str):
    """Step one of getting rid of a file. It stops counting; nothing is destroyed."""
    p = me(request)
    try:
        selection.archive(upload_id, p.tenant, by=p.username)
    except ValueError as e:
        return files_page(request, error=str(e))
    return RedirectResponse("/files", status_code=303)


@app.post("/upload/{upload_id}/restore")
def restore_file(request: Request, upload_id: str):
    p = me(request)
    selection.restore(upload_id, p.tenant)
    return RedirectResponse("/files", status_code=303)


@app.post("/upload/{upload_id}/destroy")
def destroy_file(request: Request, upload_id: str):
    """Step two, and there is no step three -- it takes the purchase lines with it."""
    p = me(request)
    try:
        selection.destroy(upload_id, p.tenant)
    except ValueError as e:
        return files_page(request, error=str(e))
    return RedirectResponse("/files", status_code=303)


@app.post("/files/owner")
def set_month_owner(request: Request, year: int = Form(...), month: int = Form(...),
                    upload_id: str = Form(...)):
    """Decide which file supplies a month two selected files both cover."""
    p = me(request)
    try:
        selection.set_owner(p.tenant, year, month, upload_id, by=p.username)
    except ValueError as e:
        return files_page(request, error=str(e))
    return RedirectResponse("/files", status_code=303)


# --------------------------------------------------------------------------- lines
def _lines_workbook(request: Request, rows, label: str, filename: str):
    """Score rows line by line and hand back a workbook of its own.

    One file at a time. For every counted line across a period the same sheet is the last
    tab of /export.xlsx, where it sits next to the totals it is evidence for.
    """
    p = me(request)
    if not rows:
        raise HTTPException(400, "there are no purchase lines to export")
    scored = catalogue.score_lines(rows, label=label)
    data = lines_export.workbook(
        scored["rows"], client_name_for(p), label,
        version=scored.get("catalogue_version"))
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def client_name_for(p) -> str:
    name, _caterer = auth.tenant_names(p.tenant)
    return name


@app.get("/files/{upload_id}/lines.xlsx")
def file_lines_xlsx(request: Request, upload_id: str):
    """Every line this ONE file supplied, with what we made of it.

    Available whether or not the file is counted -- what a file contains is a fact about
    the file, and being able to look at it before deciding to count it is most of why
    this exists. Available to the client as well, which is why it hangs off /files and
    not off the admin-only /upload.
    """
    p = me(request)
    rep = uploads.get(upload_id, p.tenant)
    if rep is None:
        raise HTTPException(404, f"no file {upload_id}")
    rows = db.lines_from_upload(upload_id, p.tenant)
    stamp = (rep.get("filename") or upload_id).rsplit(".", 1)[0].replace(" ", "_")
    return _lines_workbook(request, rows, f"{rep.get('filename') or upload_id}",
                           f"PLANETmeal_lines_{stamp}.xlsx")


# --------------------------------------------------------------------------- export
def _version_gap(result: dict, scored: dict) -> list[str]:
    """Say so when the summary sheets and the lines came from different catalogues.

    The export serves a held analysis while the client's data is unchanged, but the lines
    are always scored fresh -- so a catalogue published in between would leave one
    workbook carrying two answers. It almost never happens. When it does, a reader who
    discovers it for themselves has learnt something worse than the discrepancy.
    """
    was = (result.get("catalogue_version") or {}).get("etag")
    now = (scored.get("catalogue_version") or {}).get("etag")
    if not was or not now or was == now:
        return []
    return ["NOTE. The summary sheets were scored against catalogue version "
            f"{was} and these lines against {now}, so the two can disagree slightly. "
            "Recalculate on the dashboard and download again for a workbook where every "
            "sheet came from one catalogue."]


@app.get("/export.xlsx")
def export_xlsx(request: Request, window: str | None = None):
    """The full analysis as a workbook, on demand."""
    from openpyxl import Workbook
    p = me(request)
    from openpyxl.styles import Alignment, Font, PatternFill

    win = window or analysis.default_window(p.tenant)
    try:
        _label, y0, m0, y1, m1 = analysis.parse_window(win, tenant=p.tenant)
        result = analysis.run(window=win, save=False, top=60, tenant=p.tenant)
    except (analysis.WindowError, catalogue.CatalogueDown) as e:
        raise HTTPException(409, str(e))

    wb = Workbook()
    head = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="1A4A2A")

    def sheet(name, cols, rows):
        ws = wb.create_sheet(name[:31])
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = head, fill
            c.alignment = Alignment(horizontal="center")
        for r in rows:
            ws.append(r)
        for i, col in enumerate(cols, start=1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = \
                max(12, min(46, len(str(col)) + 6))
        ws.freeze_panes = "A2"
        return ws

    h = result["headline"]
    wb.remove(wb.active)
    sheet("summary", ["metric", "value"], [
        ["window", h["window"]], ["months", h["months"]],
        ["period from", h["period_from"]], ["period to", h["period_to"]],
        ["food purchased (kg)", h["food_kg"]], ["non-food (kg)", h["nonfood_kg"]],
        ["CO2e (kg)", h["co2_kg"]],
        ["intensity (kg CO2e per kg food)", h["intensity_kg_co2_per_kg"]],
        ["EAT-Lancet score", h["eat_lancet_score"]],
        ["EAT-Lancet profile", h.get("eat_profile")],
        ["spend (EUR)", h["spend_eur"]], ["restaurants", h["restaurants"]],
        ["products", h["products"]], ["lines", h["lines"]],
        ["matched to a specific product (% of weight)",
         h["confidence"]["product_specific_pct_of_weight"]],
        ["per-piece share of spend (%)", h["piece_spend_pct"]],
    ])
    sheet("caveats", ["severity", "owner", "what you must know"],
          [[c["severity"], c["owner"], c["message"]] for c in h["caveats"]])
    sheet("by month", ["period", "food kg", "kg CO2e", "intensity", "spend EUR", "quality"],
          [[r["period"], r["food_kg"], r["co2_kg"], r["intensity_kg_co2_per_kg"],
            r["spend_eur"], r.get("quality", "")] for r in result["by_month"]])
    sheet("by restaurant", ["restaurant", "food kg", "kg CO2e", "intensity", "spend EUR", "products"],
          [[r["restaurant"], r["food_kg"], r["co2_kg"], r["intensity_kg_co2_per_kg"],
            r["spend_eur"], r["products"]] for r in result["by_restaurant"]])
    sheet("by food group", ["food group", "food kg", "kg CO2e", "% of weight", "% of CO2",
                            "intensity", "products"],
          [[r["food_group"], r["food_kg"], r["co2_kg"], r["pct_of_weight"], r["pct_of_co2"],
            r["intensity_kg_co2_per_kg"], r["products"]] for r in result["by_food_group"]])
    sheet("top contributors", ["artikelnr", "product", "food group", "food kg", "kg CO2e",
                               "kg CO2e per kg", "% of CO2", "source", "specific?", "confidence"],
          [[r["artikelnr"], r["description"], r["food_group"], r["food_kg"], r["co2_kg"],
            r["co2_per_kg"], r["pct_of_co2"], r["source_label"],
            "yes" if r["product_level"] else "no", r["confidence"]]
           for r in result["top_contributors"]])
    sheet("eat lancet", ["food group", "reference %", "purchased %", "gap"],
          [[r["food_group"], r["reference_pct"], r["purchased_pct"], r["gap_pct"]]
           for r in result["eat_lancet"]["rows"]])
    sheet("data health", ["tier", "what it means", "% of weight", "% of CO2", "products", "specific?"],
          [[t["label"], t["explain"], t["pct_of_weight"], t["pct_of_co2"], t["products"],
            "yes" if t["product_level"] else "no"] for t in result["data_health"]["by_tier"]])
    sheet("zero weight", ["artikelnr", "product", "category", "pack", "unit", "pieces", "spend EUR"],
          [[r["artikelnr"], r["description"], r["category"], r["vp"], r["eenh"],
            r["pieces"], r["spend_eur"]] for r in result["piece_items"]["rows"]])

    # Every line the sheets above are made of. This is the point of the whole download:
    # a reader who doubts a figure can open the rows underneath it here, in the same
    # file, rather than being sent somewhere else to fetch them.
    try:
        scored = catalogue.score_lines(
            analysis.rows_for(win, p.tenant, y0, m0, y1, m1), label=h["window"])
    except catalogue.CatalogueDown as e:
        raise HTTPException(409, str(e))
    lines_export.add_sheet(wb, scored["rows"], client_name_for(p), h["window"],
                           version=scored.get("catalogue_version"),
                           extra_notes=_version_gap(result, scored))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    name = f"PLANETmeal_{p.tenant}_{h['window'].replace(' ', '')}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


# --------------------------------------------------------------------------- json
@app.get("/api/health")
def api_health():
    """Is the app up, and can it reach the catalogue. Nothing about any client.

    This route is in PUBLIC_PATHS because Railway has to reach it before anyone can sign
    in. It used to return db.stats() as well -- and db.stats() with no tenant falls back
    to config.TENANT, so an endpoint that needs no login was reporting one specific
    client's line count, product count, restaurant count and total spend in euros to
    anyone who asked for it. A health check answers whether the service is alive. It has
    no business knowing who the customers are.
    """
    return {"app": "ok", "catalogue": catalogue.health()}


@app.get("/api/analysis")
def api_analysis(request: Request, window: str | None = None):
    p = me(request)
    try:
        return analysis.run(window=window or analysis.default_window(p.tenant),
                            tenant=p.tenant)
    except (analysis.WindowError, catalogue.CatalogueDown) as e:
        return JSONResponse({"error": str(e)}, status_code=409)


@app.get("/api/run/{run_id}")
def api_run(request: Request, run_id: str):
    p = me(request)
    # An admin may read any client's saved run; a client only their own. Passing None
    # here for a client would hand them somebody else's analysis for a guessed id.
    out = analysis.saved(run_id, None if p.is_admin else p.tenant)
    if out is None:
        raise HTTPException(404, f"no analysis run {run_id}")
    return out


@app.get("/api/catalogue-version")
def api_catalogue_version(request: Request):
    """What the catalogue is on now, and whether the saved numbers are behind it."""
    p = me(request)
    return {"live": catalogue.version(), "stale": analysis.staleness(tenant=p.tenant)}


@app.get("/api/months")
def api_months(request: Request):
    return {"rows": db.months()}

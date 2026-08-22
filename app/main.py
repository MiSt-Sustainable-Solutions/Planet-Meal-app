"""
PLANETprocure — the web app.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import adapters
import analysis
import catalogue
import charts
import config
import db
import uploads
from adapters import mist_template

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="PLANETprocure", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))

COMMIT_MODES = [
    dict(key="new_only", label="Only the new months", default=True,
         description=("Import months we do not already hold and skip the rest. Safe with a "
                      "cumulative export, which always repeats earlier months."),
         effect="Nothing existing is touched."),
    dict(key="replace", label="Replace what we hold", default=False,
         description=("Delete the months this file covers, then import it. Use when the "
                      "supplier has re-issued a corrected export."),
         effect="Existing months in this file's range are overwritten."),
    dict(key="all", label="Import everything", default=False,
         description=("Take every line as-is. Refused when it would overlap, because that "
                      "is exactly how a cumulative file double-counts."),
         effect="Only valid when nothing overlaps."),
]


@app.on_event("startup")
def _startup():
    db.init()


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


def ctx(request: Request, page: str, **kw) -> dict:
    """Everything every template needs."""
    base = dict(request=request, page=page,
                client_name=config.CLIENT_NAME, caterer_name=config.CATERER_NAME,
                catalogue_api=config.CATALOGUE_API, api_up=catalogue.health() is not None,
                tier_swatch=charts.TIER_SWATCH, fg=charts.food_group_label)
    base.update(kw)
    return base


# --------------------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, window: str | None = None):
    try:
        selected = window or analysis.default_window()
        result = analysis.run(window=selected)
    except (analysis.WindowError, catalogue.CatalogueDown) as e:
        return templates.TemplateResponse(
            "dashboard.html", ctx(request, "dashboard", error=str(e),
                                  window_options=analysis.windows(), selected_window=window))

    relabel(result)
    return templates.TemplateResponse("dashboard.html", ctx(
        request, "dashboard",
        result=result, selected_window=selected,
        window_options=analysis.windows(),
        conf_bar=charts.confidence(result["headline"]["confidence"]["by_tier"]),
        trend=charts.line(result["by_month"], "period", "co2_kg", "complete"),
        rest_chart=charts.bars(result["by_restaurant"], "restaurant", "co2_kg",
                               secondary_key="intensity_kg_co2_per_kg", limit=20),
        group_chart=charts.bars(result["by_food_group"], "food_group", "co2_kg",
                                width=620, pad_l=150, pad_r=110,
                                secondary_key="pct_of_co2", secondary_suffix="%", limit=14),
        eat_chart=charts.paired(result["eat_lancet"]["rows"])))


@app.get("/data-health", response_class=HTMLResponse)
def data_health(request: Request, window: str | None = None):
    try:
        selected = window or analysis.default_window()
        result = analysis.run(window=selected, save=False)
    except (analysis.WindowError, catalogue.CatalogueDown) as e:
        return templates.TemplateResponse(
            "data_health.html", ctx(request, "health", error=str(e), months=db.months()))

    relabel(result)

    wq, wq_err = None, None
    try:
        label, y0, m0, y1, m1 = analysis.parse_window(selected)
        wq = catalogue.work_queue(db.lines_for(y0, m0, y1, m1), label=label, limit=60)
    except catalogue.CatalogueDown as e:
        wq_err = str(e)

    return templates.TemplateResponse("data_health.html", ctx(
        request, "health", result=result, months=db.months(),
        work_queue=wq, work_queue_error=wq_err, selected_window=selected))


@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    return templates.TemplateResponse("history.html", ctx(
        request, "history", runs=analysis.history(), uploads=uploads.listing()))


# --------------------------------------------------------------------------- upload
@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, error: str | None = None):
    return templates.TemplateResponse("upload.html", ctx(
        request, "upload", error=error, adapters=adapters.listing(),
        uploads=uploads.listing(limit=12)))


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
    suffix = os.path.splitext(file.filename or "upload.xlsx")[1] or ".xlsx"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(file.file, out)
        y = int(year) if year and str(year).strip().isdigit() else None
        try:
            report = uploads.stage(tmp, file.filename, y)
        except Exception as e:
            return templates.TemplateResponse("upload.html", ctx(
                request, "upload", error=str(e), adapters=adapters.listing(),
                uploads=uploads.listing(limit=12)), status_code=422)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return RedirectResponse(f"/upload/{report['upload_id']}", status_code=303)


@app.get("/upload/{upload_id}", response_class=HTMLResponse)
def upload_report(request: Request, upload_id: str, commit_error: str | None = None):
    report = uploads.get(upload_id)
    if report is None:
        raise HTTPException(404, f"no upload {upload_id}")
    return templates.TemplateResponse("preflight.html", ctx(
        request, "upload", report=report, commit_modes=COMMIT_MODES,
        commit_error=commit_error))


@app.post("/upload/{upload_id}/commit")
def commit_upload(request: Request, upload_id: str,
                  mode: str = Form("new_only"), override: str | None = Form(None)):
    try:
        uploads.commit(upload_id, mode, bool(override))
    except uploads.CommitError as e:
        return upload_report(request, upload_id, commit_error=str(e))
    return RedirectResponse(f"/upload/{upload_id}", status_code=303)


@app.get("/upload/{upload_id}/discard")
def discard_upload(upload_id: str):
    try:
        uploads.discard(upload_id)
    except uploads.CommitError:
        return RedirectResponse(f"/upload/{upload_id}", status_code=303)
    return RedirectResponse("/upload", status_code=303)


# --------------------------------------------------------------------------- export
@app.get("/export.xlsx")
def export_xlsx(window: str | None = None):
    """The full analysis as a workbook, on demand."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    try:
        result = analysis.run(window=window or analysis.default_window(), save=False, top=60)
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

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    name = f"PLANETprocure_{config.TENANT}_{h['window'].replace(' ', '')}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


# --------------------------------------------------------------------------- json
@app.get("/api/health")
def api_health():
    return {"app": "ok", "catalogue": catalogue.health(), "data": db.stats()}


@app.get("/api/analysis")
def api_analysis(window: str | None = None):
    try:
        return analysis.run(window=window or analysis.default_window(), save=False)
    except (analysis.WindowError, catalogue.CatalogueDown) as e:
        return JSONResponse({"error": str(e)}, status_code=409)


@app.get("/api/run/{run_id}")
def api_run(run_id: str):
    out = analysis.saved(run_id)
    if out is None:
        raise HTTPException(404, f"no analysis run {run_id}")
    return out


@app.get("/api/months")
def api_months():
    return {"rows": db.months()}

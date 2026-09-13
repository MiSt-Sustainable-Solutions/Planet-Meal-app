"""
What a client sees, and when it changes.

Until 13 Sep 2026 a client saw MiSt's working state. Count a file to see how it looked and
the client saw it too, the same second; a catalogue decision, a recalculation, a file
counted by mistake -- all of it reached them before anyone had checked it.

Now a client sees a PUBLICATION: a frozen copy of their figures, made when MiSt presses
Publish, and nothing else. It holds

    every period the client can choose, and each counted file on its own  (publication_view)
    the Excel download for each of those, and each file's lines          (publication_file)
    which files, which months, which catalogue, and when                   (publication)

as finished answers and finished bytes, not as inputs to be recalculated. That is the
point. Freezing only the choice of files would still let a catalogue decision or a
recalculation change what the client reads; freezing the answers means the only thing
that can change a client's figures is MiSt publishing again.

MiSt keeps working on the live figures exactly as before. The admin bar says whether the
client's copy still matches them, and what has moved if not.

Building a publication scores every period and every file afresh against one catalogue,
which takes minutes for a client with years of data. So it runs in the background and the
previous publication stays in front of the client until the new one is complete. A
publication that fails, or is interrupted by a restart, never replaces anything.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import threading
import uuid

import adjustments
import analysis
import auth
import catalogue
import db
import lines_export
import selection
import uploads
import workbooks

# The top contributors the Excel download lists. The dashboard shows twelve of them.
TOP = 60

# A build that has said nothing for this long is not still running: the process that ran
# it has gone. Minutes, because a long period can take a couple of them to score.
ABANDONED_AFTER = dt.timedelta(minutes=45)

_THREADS: dict[str, threading.Thread] = {}


class PublishError(ValueError):
    pass


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _row(r) -> dict | None:
    if r is None:
        return None
    out = {k: r[k] for k in r.keys()}
    for k in ("parts_json", "windows_json", "files_json", "months_json"):
        out[k[:-5]] = json.loads(out.pop(k) or "null")
    out["windows"], out["files"], out["months"] = (out["windows"] or [], out["files"] or [],
                                                   out["months"] or [])
    # "13 Sep 2026" and "14:02", for people rather than for sorting.
    when = out.get("finished_at") or out.get("started_at")
    if when:
        d = dt.datetime.fromisoformat(when)
        out["on"], out["at"] = f"{d.day} {d.strftime('%b %Y')}", d.strftime("%H:%M")
    return out


# --------------------------------------------------------------------------- what counts now
def counted_files(tenant: str) -> list[dict]:
    """The files a client's figures are made of right now: counted, kept, holding lines."""
    return [dict(upload_id=u["upload_id"], filename=u["filename"], adapter=u["adapter"],
                 uploaded_at=u["uploaded_at"], periods=u["periods"], lines=u["lines"],
                 held=u["held"], selected=True, archived=False, archived_at=None)
            for u in uploads.listing(limit=200, tenant=tenant)
            if u["selected"] and not u["archived"] and u["held"]]


def signature(tenant: str, etag: str | None) -> tuple[str, dict]:
    """A fingerprint of everything that makes the figures what they are. -> (sig, parts)

    In three parts so that "the client's copy is out of date" can say WHY, in words:
    different files, different purchases in them, or a different catalogue.
    """
    ids = sorted(f["upload_id"] for f in counted_files(tenant))
    own = sorted(f"{y}-{m:02d}:{u}" for (y, m), u in selection.owners(tenant).items())
    snap = db.snapshot(tenant)
    data = [f"{m['period']}:{m['lines']}:{m['kg']}:{m['spend_eur']}:{m['quality']}"
            for m in snap["months"]] + [f"products:{snap['products']}:{snap['last_seen']}"]

    def h(xs):
        return hashlib.sha1("|".join(xs).encode()).hexdigest()[:12]

    parts = dict(files=h(ids + own), data=h(data),
                 adjustments=adjustments.digest(tenant), catalogue=etag or "")
    return h([parts[k] for k in ("files", "data", "adjustments", "catalogue")]), parts


REASONS = dict(files="the counted files changed",
               data="the purchases in them changed",
               adjustments="the product adjustments changed",
               catalogue="the catalogue or the way figures are calculated changed")


# --------------------------------------------------------------------------- reading
def live(tenant: str | None) -> dict | None:
    """The publication this client sees. None if nothing has been published."""
    if not tenant:
        return None
    con = db.connect()
    r = con.execute("SELECT * FROM publication WHERE tenant=? AND status='live' "
                    "ORDER BY finished_at DESC LIMIT 1", (tenant,)).fetchone()
    con.close()
    return _row(r)


def latest(tenant: str) -> dict | None:
    """The most recent attempt, whatever became of it."""
    con = db.connect()
    r = con.execute("SELECT * FROM publication WHERE tenant=? "
                    "ORDER BY started_at DESC, id DESC LIMIT 1", (tenant,)).fetchone()
    con.close()
    return _row(r)


def get(pid: str) -> dict | None:
    con = db.connect()
    r = con.execute("SELECT * FROM publication WHERE id=?", (pid,)).fetchone()
    con.close()
    return _row(r)


def choose(pub: dict, window: str | None) -> str:
    """The window to show from a publication: the one asked for if it holds it, else its
    default. Never a window it does not hold -- that would be a figure nobody published."""
    held = {w["key"] for w in pub["windows"]} | {f"files:{f['upload_id']}" for f in pub["files"]}
    return window if window in held else pub["default_window"]


def view(pub: dict, window: str) -> dict | None:
    con = db.connect()
    r = con.execute("SELECT result_json FROM publication_view "
                    "WHERE publication_id=? AND window_key=?", (pub["id"], window)).fetchone()
    con.close()
    if not r:
        return None
    out = json.loads(r["result_json"])
    out["cached"], out["stale"] = True, None
    return out


def view_by_run(pub: dict, run_id: str) -> dict | None:
    """A published result by the run id it carries -- the only run ids a client holds."""
    con = db.connect()
    r = con.execute("SELECT window_key FROM publication_view "
                    "WHERE publication_id=? AND run_id=?", (pub["id"], run_id)).fetchone()
    con.close()
    return view(pub, r["window_key"]) if r else None


def file(pub: dict, name: str) -> tuple[bytes, str] | None:
    con = db.connect()
    r = con.execute("SELECT filename, data FROM publication_file "
                    "WHERE publication_id=? AND name=?", (pub["id"], name)).fetchone()
    con.close()
    if not r:
        return None
    return base64.b64decode(r["data"]), r["filename"]


def state(tenant: str | None, etag: str | None) -> dict | None:
    """Everything the admin bar says about one client's publication."""
    if not tenant:
        return None
    recover(tenant)
    now = live(tenant)
    last = latest(tenant)
    counted = bool(db.months(tenant))
    sig, parts = signature(tenant, etag)
    changed = []
    if now:
        # A copy published before adjustments existed has no record of them, and had none.
        before = dict(dict(adjustments=adjustments.NONE), **(now["parts"] or {}))
        changed = [REASONS[k] for k in ("files", "data", "adjustments", "catalogue")
                   if before.get(k) != parts[k] and not (k == "catalogue" and not etag)]
    building = last if last and last["status"] == "building" else None
    failed = last if last and last["status"] == "failed" else None
    return dict(live=now, building=building, failed=failed, counted=counted,
                changed=changed, reachable=bool(etag),
                can_publish=counted and not building and bool(etag)
                and (not now or bool(changed) or bool(failed)))


# --------------------------------------------------------------------------- building
def recover(tenant: str | None = None, startup: bool = False) -> int:
    """Mark builds whose process has gone as failed. -> how many.

    A restart kills the background thread mid-build and leaves a row saying 'building'
    forever, which would block every later publish. At startup every such row is dead,
    since nothing can be running yet. Otherwise a build this process is not running
    counts as dead once it has said nothing for ABANDONED_AFTER -- so neither a deploy
    nor a crash can wedge a client, and a build running in another worker is left alone.
    """
    cutoff = (dt.datetime.now() - ABANDONED_AFTER).isoformat(timespec="seconds")
    con = db.connect()
    sql = "SELECT id, started_at, finished_at FROM publication WHERE status='building'"
    args: list = []
    if tenant:
        sql += " AND tenant=?"
        args.append(tenant)
    dead = []
    for r in con.execute(sql, args).fetchall():
        t = _THREADS.get(r["id"])
        if t is not None and t.is_alive():
            continue
        if startup or (r["finished_at"] or r["started_at"] or "") < cutoff:
            dead.append(r["id"])
    for pid in dead:
        con.execute("UPDATE publication SET status='failed', finished_at=?, error=? "
                    "WHERE id=?", (_now(), "It was interrupted (the app restarted) before "
                                   "it finished. Nothing changed for the client.", pid))
        con.execute("DELETE FROM publication_view WHERE publication_id=?", (pid,))
        con.execute("DELETE FROM publication_file WHERE publication_id=?", (pid,))
    if dead:
        con.commit()
    con.close()
    return len(dead)


def start(tenant: str, by: str, wait: bool = False) -> str:
    """Begin publishing this client's figures as they are now. -> the publication id.

    `wait` builds in this thread instead, for tests and scripts.
    """
    recover(tenant)
    if latest(tenant) and latest(tenant)["status"] == "building":
        raise PublishError("a publish for this client is already running")
    if not db.months(tenant):
        raise PublishError("nothing is counted yet, so there is nothing to publish")
    pid = uuid.uuid4().hex[:12]
    con = db.connect()
    con.execute("INSERT INTO publication (id, tenant, status, started_at, published_by, "
                "step, steps, doing) VALUES (?,?,?,?,?,?,?,?)",
                (pid, tenant, "building", _now(), by, 0, 0, "getting ready"))
    con.commit()
    con.close()
    if wait:
        _THREADS[pid] = threading.current_thread()
        _build(pid, tenant)
    else:
        t = threading.Thread(target=_build, args=(pid, tenant), daemon=True,
                             name=f"publish-{tenant}")
        _THREADS[pid] = t
        t.start()
    return pid


def _progress(pid: str, step: int, steps: int, doing: str) -> None:
    con = db.connect()
    con.execute("UPDATE publication SET step=?, steps=?, doing=?, finished_at=? WHERE id=?",
                (step, steps, doing, _now(), pid))
    con.commit()
    con.close()


def _fresh(window: str, tenant: str) -> dict:
    """This window's figures, current against the live catalogue, with the full top list.

    A saved result is reused only when it is current and complete. One saved by a page
    view lists twenty contributors, not sixty, and one saved before a catalogue change is
    exactly what publishing must not hand a client.
    """
    r = analysis.run(window=window, tenant=tenant, top=TOP)
    if r.get("stale") or (r.get("cached") and len(r.get("top_contributors") or []) < TOP):
        r = analysis.run(window=window, tenant=tenant, top=TOP, force=True)
    return r


def _scored(window: str, tenant: str, label: str) -> dict:
    _l, y0, m0, y1, m1 = analysis.parse_window(window, tenant=tenant)
    return catalogue.score_lines(analysis.rows_for(window, tenant, y0, m0, y1, m1),
                                 label=label)


def _etag(x: dict) -> str | None:
    return (x.get("catalogue_version") or {}).get("etag")


def _build(pid: str, tenant: str) -> None:
    views: list[tuple] = []
    files: list[tuple] = []
    try:
        live_v = catalogue.version()
        if not live_v:
            raise PublishError("the catalogue cannot be reached, so nothing can be scored")
        etag = live_v["etag"]
        sig, parts = signature(tenant, etag)
        windows = analysis.windows(tenant)
        counted = counted_files(tenant)
        months = db.months(tenant)
        default = analysis.default_window(tenant)
        client, _caterer = auth.tenant_names(tenant)
        steps = len(windows) + len(counted)
        step = 0

        for w in windows:
            _progress(pid, step, steps, w["label"])
            result = _fresh(w["key"], tenant)
            scored = _scored(w["key"], tenant, result["headline"]["window"])
            adjustments.label_rows(scored["rows"], tenant)
            for x in (result, scored):
                if _etag(x) != etag:
                    raise PublishError("the catalogue changed while publishing. "
                                       "Nothing changed for the client; publish again.")
            views.append((pid, w["key"], w["label"], result.get("run_id"), json.dumps(result)))
            files.append((pid, f"export:{w['key']}", workbooks.export_name(tenant, result),
                          base64.b64encode(workbooks.analysis_workbook(
                              result, scored, client)).decode()))
            step += 1

        for f in counted:
            key = f"files:{f['upload_id']}"
            _progress(pid, step, steps, f["filename"])
            result = _fresh(key, tenant)
            # One file alone has no month in dispute, so the rows the analysis counted are
            # every row the file holds -- the same rows its Lines download lists. Scored
            # once, both workbooks come from them.
            scored = _scored(key, tenant, f["filename"])
            adjustments.label_rows(scored["rows"], tenant)
            for x in (result, scored):
                if _etag(x) != etag:
                    raise PublishError("the catalogue changed while publishing. "
                                       "Nothing changed for the client; publish again.")
            views.append((pid, key, f["filename"], result.get("run_id"), json.dumps(result)))
            files.append((pid, f"export:{key}", workbooks.export_name(tenant, result),
                          base64.b64encode(workbooks.analysis_workbook(
                              result, scored, client)).decode()))
            stamp = (f["filename"] or f["upload_id"]).rsplit(".", 1)[0].replace(" ", "_")
            files.append((pid, f"lines:{f['upload_id']}", f"PLANETmeal_lines_{stamp}.xlsx",
                          base64.b64encode(lines_export.workbook(
                              scored["rows"], client, f["filename"],
                              version=scored.get("catalogue_version"))).decode()))
            step += 1

        if signature(tenant, etag)[0] != sig:
            raise PublishError("the counted files or their purchases changed while "
                               "publishing. Nothing changed for the client; publish again.")

        con = db.connect()
        con.executemany("INSERT INTO publication_view (publication_id, window_key, label, "
                        "run_id, result_json) VALUES (?,?,?,?,?)", views)
        con.executemany("INSERT INTO publication_file (publication_id, name, filename, "
                        "data) VALUES (?,?,?,?)", files)
        # The copy being replaced keeps its row, so there is a record of what the client
        # was shown and when. Its downloads go: they are megabytes each, and nobody can
        # reach them any more.
        old = [r["id"] for r in con.execute(
            "SELECT id FROM publication WHERE tenant=? AND status='live'", (tenant,))]
        for o in old:
            con.execute("DELETE FROM publication_file WHERE publication_id=?", (o,))
            con.execute("DELETE FROM publication_view WHERE publication_id=?", (o,))
            con.execute("UPDATE publication SET status='replaced' WHERE id=?", (o,))
        con.execute(
            "UPDATE publication SET status='live', finished_at=?, step=?, steps=?, doing=?, "
            "catalogue_version=?, signature=?, parts_json=?, default_window=?, "
            "windows_json=?, files_json=?, months_json=? WHERE id=?",
            (_now(), steps, steps, "done", etag, sig, json.dumps(parts), default,
             json.dumps(windows), json.dumps(counted), json.dumps(months), pid))
        con.commit()
        con.close()
    except Exception as e:                                  # noqa: BLE001
        msg = str(e) if isinstance(e, (PublishError, catalogue.CatalogueDown,
                                       analysis.WindowError)) \
            else f"{type(e).__name__}: {e}"
        con = db.connect()
        con.execute("DELETE FROM publication_view WHERE publication_id=?", (pid,))
        con.execute("DELETE FROM publication_file WHERE publication_id=?", (pid,))
        con.execute("UPDATE publication SET status='failed', finished_at=?, error=? "
                    "WHERE id=?", (_now(), msg[:600], pid))
        con.commit()
        con.close()
    finally:
        _THREADS.pop(pid, None)


def wait(pid: str, timeout: float | None = None) -> dict | None:
    """Block until a background build ends. For tests."""
    t = _THREADS.get(pid)
    if t:
        t.join(timeout)
    return get(pid)

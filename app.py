"""Kavita age-rater web app.

Long-running container: a background scheduler periodically classifies unrated
Kavita books with Claude (storing results in SQLite), and a small web UI lets you
review, override, and commit those age ratings to Kavita individually or in bulk.
"""
import os
import threading
import time
import traceback
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from waitress import serve

import age_rater as core
import db

HERE = Path(__file__).resolve().parent
for candidate in (HERE / ".env", HERE / "config.env"):
    core.load_env_file(candidate)

WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
INTERVAL_HOURS = float(os.environ.get("INTERVAL_HOURS", "24"))
AUTO_SCAN = os.environ.get("AUTO_SCAN", "true").lower() not in ("0", "false", "no")

app = Flask(__name__)

# ---- scan state / scheduler -------------------------------------------------
_scan_lock = threading.Lock()
_scan_state = {"running": False, "last_run": None, "last_result": None,
               "last_error": None, "progress": ""}


def _run_scan(reclassify: bool = False) -> None:
    if not _scan_lock.acquire(blocking=False):
        return  # a scan is already running
    _scan_state.update(running=True, last_error=None, progress="starting")
    try:
        kav = core.kavita_from_env()
        client = core.anthropic_from_env()

        def log(msg):
            _scan_state["progress"] = msg
            print(f"[scan] {msg}", flush=True)

        result = core.scan_once(kav, client, core.model_from_env(),
                                reclassify=reclassify, log=log)
        _scan_state.update(last_result=result, progress="done")
    except Exception as e:  # noqa: BLE001
        _scan_state["last_error"] = str(e)
        traceback.print_exc()
    finally:
        _scan_state.update(running=False, last_run=time.strftime("%Y-%m-%d %H:%M:%S"))
        db.set_meta("last_scan", _scan_state["last_run"] or "")
        _scan_lock.release()


def _scheduler() -> None:
    # Initial scan on boot if we've never scanned and auto-scan is on.
    if AUTO_SCAN and not db.get_meta("last_scan"):
        _run_scan()
    while AUTO_SCAN:
        time.sleep(max(1.0, INTERVAL_HOURS * 3600))
        _run_scan()


# ---- helpers ----------------------------------------------------------------
def _rating_order(value_to_title: dict) -> list:
    """Committable ratings (value >= 2), youngest -> oldest, for the dropdown."""
    return [value_to_title[v] for v in sorted(value_to_title) if v >= 2]


# ---- routes -----------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    ratings = []
    kav_error = None
    try:
        kav = core.kavita_from_env()
        _, value_to_title = kav.age_rating_enum()
        ratings = _rating_order(value_to_title)
    except Exception as e:  # noqa: BLE001
        kav_error = str(e)
    return jsonify({
        "counts": db.counts(),
        "ratings": ratings,
        "series": db.list_series(status=request.args.get("status", ""),
                                 search=request.args.get("search", "")),
        "scan": _scan_state,
        "kavita_error": kav_error,
        "interval_hours": INTERVAL_HOURS,
        "auto_scan": AUTO_SCAN,
    })


@app.post("/api/override")
def api_override():
    data = request.get_json(force=True)
    db.set_override(int(data["series_id"]), (data.get("override") or "").strip())
    return jsonify(ok=True)


@app.post("/api/skip")
def api_skip():
    ids = request.get_json(force=True).get("ids", [])
    for sid in ids:
        db.set_status(int(sid), "skipped")
    return jsonify(ok=True, skipped=len(ids))


@app.post("/api/unskip")
def api_unskip():
    ids = request.get_json(force=True).get("ids", [])
    for sid in ids:
        db.set_status(int(sid), "pending")
    return jsonify(ok=True, reset=len(ids))


@app.post("/api/commit")
def api_commit():
    data = request.get_json(force=True)
    try:
        kav = core.kavita_from_env()
        title_to_value, _ = kav.age_rating_enum()
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"Kavita: {e}"), 502

    if data.get("all_pending"):
        ids = [r["series_id"] for r in db.list_series(status="pending")]
    else:
        ids = [int(x) for x in data.get("ids", [])]

    results = {"committed": 0, "skipped": 0, "failed": 0, "detail": []}
    for sid in ids:
        row = db.get(sid)
        if not row:
            continue
        rating = (row.get("override_rating") or row.get("proposed_rating") or "").strip()
        try:
            ok, msg = core.commit_series(kav, title_to_value, sid, rating)
            if ok:
                results["committed"] += 1
            elif "unrated" in msg:
                results["skipped"] += 1
            else:
                results["failed"] += 1
            results["detail"].append({"series_id": sid, "name": row["name"], "ok": ok, "msg": msg})
        except Exception as e:  # noqa: BLE001
            results["failed"] += 1
            results["detail"].append({"series_id": sid, "name": row["name"],
                                      "ok": False, "msg": str(e)})
    return jsonify(ok=True, **results)


@app.post("/api/scan")
def api_scan():
    if _scan_state["running"]:
        return jsonify(ok=False, error="scan already running"), 409
    reclassify = bool((request.get_json(silent=True) or {}).get("reclassify"))
    threading.Thread(target=_run_scan, kwargs={"reclassify": reclassify}, daemon=True).start()
    return jsonify(ok=True)


def main() -> None:
    db.init()
    seeded = db.seed_from_cache(Path(os.environ.get("AGE_RATER_DATA_DIR", ".")) / "cache.json")
    if seeded:
        print(f"[init] seeded {seeded} rows from cache.json", flush=True)
    threading.Thread(target=_scheduler, daemon=True).start()
    print(f"[web] serving on 0.0.0.0:{WEB_PORT} (auto_scan={AUTO_SCAN}, "
          f"interval={INTERVAL_HOURS}h)", flush=True)
    serve(app, host="0.0.0.0", port=WEB_PORT, threads=8)


if __name__ == "__main__":
    main()

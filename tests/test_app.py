"""Smoke tests: the page follows the gomez app standard and the endpoints answer
without Kavita or Claude being reachable."""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGE_RATER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KAVITA_URL", "")
    monkeypatch.setenv("AUTO_SCAN", "true")
    import db
    import app as webapp
    importlib.reload(webapp)
    db.init()
    return webapp.app.test_client(), webapp, db


def test_page_follows_standard(client):
    c, webapp, _ = client
    page = c.get("/").get_data(as_text=True)
    assert "gomez-ui.css" in page and "gomez-ui.js" in page
    assert 'class="g-header"' in page and "g-theme" in page and 'class="g-updated"' in page
    # one image is both the header logo and the favicon
    assert 'class="g-app-icon" src="/static/icon.svg?v=' in page and 'rel="icon" href="/static/icon.svg?v=' in page
    assert '<meta name="theme-color" content="#2e5bd9">' in page
    assert f"v{webapp.VERSION}" in page
    assert "confirm(" not in page  # no browser pop-ups


def test_static_and_manifest(client):
    c, _, _ = client
    css = c.get("/static/gomez-ui.css")
    assert css.status_code == 200 and b"gomez-ui v1.1" in css.data and b".g-figure" in css.data
    m = c.get("/static/manifest.webmanifest")
    assert m.status_code == 200 and m.mimetype == "application/manifest+json"
    assert m.get_json()["theme_color"] == "#2e5bd9"


def test_healthz_and_metrics(client):
    c, _, db = client
    r = c.get("/healthz")
    assert r.status_code == 200 and r.get_json()["ok"] is True       # never scanned: still healthy
    db.set_meta("last_success_epoch", "1")                            # very old scan: overdue
    assert c.get("/healthz").status_code == 503
    body = c.get("/metrics").get_data(as_text=True)
    assert "age_rater_build_info" in body and "age_rater_last_scan_timestamp_seconds 1" in body


def test_state_without_kavita(client):
    c, _, db = client
    db.set_meta("last_success_epoch", "1760000000")
    s = c.get("/api/state").get_json()
    assert s["kavita_error"] and s["last_success_iso"].startswith("2025-10-09")

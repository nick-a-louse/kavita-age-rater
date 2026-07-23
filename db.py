"""SQLite store for the age-rater web app.

One row per Kavita series. `proposed_rating` is Claude's suggestion; the user may
set `override_rating`; the effective rating (override or proposed) is what gets
written to Kavita on commit.

status:
  pending   - classified, awaiting review/commit (Kavita still Unknown)
  committed - written to Kavita (locked)
  skipped   - user chose to leave it unrated
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    series_id       INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    authors         TEXT DEFAULT '',
    summary         TEXT DEFAULT '',
    genres          TEXT DEFAULT '',
    content_hash    TEXT DEFAULT '',
    kavita_current  INTEGER DEFAULT 0,
    proposed_rating TEXT DEFAULT '',
    confidence      REAL DEFAULT 0,
    reasoning       TEXT DEFAULT '',
    override_rating TEXT DEFAULT '',
    status          TEXT DEFAULT 'pending',
    committed_rating TEXT DEFAULT '',
    classified_at   TEXT DEFAULT '',
    updated_at      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _db_path() -> Path:
    return Path(os.environ.get("AGE_RATER_DATA_DIR", ".")) / "agerater.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init() -> None:
    with _LOCK, _connect() as c:
        c.executescript(SCHEMA)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def set_meta(key: str, value: str) -> None:
    with _LOCK, _connect() as c:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_meta(key: str, default: str = "") -> str:
    with _connect() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def get(series_id: int):
    with _connect() as c:
        row = c.execute("SELECT * FROM series WHERE series_id=?", (series_id,)).fetchone()
        return dict(row) if row else None


def has_any() -> bool:
    with _connect() as c:
        return c.execute("SELECT 1 FROM series LIMIT 1").fetchone() is not None


def upsert_pending(s: dict) -> None:
    """Insert/refresh a classified-but-uncommitted series. Preserves any existing
    user override and does not downgrade a committed row."""
    with _LOCK, _connect() as c:
        existing = c.execute("SELECT status, override_rating FROM series WHERE series_id=?",
                             (s["series_id"],)).fetchone()
        override = existing["override_rating"] if existing else ""
        status = "pending"
        if existing and existing["status"] == "skipped":
            status = "skipped"  # keep a user's skip decision
        c.execute("""
            INSERT INTO series(series_id,name,authors,summary,genres,content_hash,
                kavita_current,proposed_rating,confidence,reasoning,override_rating,
                status,classified_at,updated_at)
            VALUES(:series_id,:name,:authors,:summary,:genres,:content_hash,
                :kavita_current,:proposed_rating,:confidence,:reasoning,:override,
                :status,:now,:now)
            ON CONFLICT(series_id) DO UPDATE SET
                name=excluded.name, authors=excluded.authors, summary=excluded.summary,
                genres=excluded.genres, content_hash=excluded.content_hash,
                kavita_current=excluded.kavita_current, proposed_rating=excluded.proposed_rating,
                confidence=excluded.confidence, reasoning=excluded.reasoning,
                status=excluded.status, classified_at=excluded.classified_at,
                updated_at=excluded.updated_at
        """, {**s, "override": override, "status": status, "now": _now()})


def refresh_meta(s: dict) -> None:
    """Update display fields for an already-classified series without re-rating."""
    with _LOCK, _connect() as c:
        c.execute("""UPDATE series SET name=:name, authors=:authors, summary=:summary,
                     genres=:genres, content_hash=:content_hash,
                     kavita_current=:kavita_current, updated_at=:now
                     WHERE series_id=:series_id""", {**s, "now": _now()})


def mark_committed_external(s: dict) -> None:
    """A series that already has a rating in Kavita (we or the user set it)."""
    with _LOCK, _connect() as c:
        c.execute("""
            INSERT INTO series(series_id,name,authors,summary,genres,content_hash,
                kavita_current,status,committed_rating,updated_at)
            VALUES(:series_id,:name,:authors,:summary,:genres,:content_hash,
                :kavita_current,'committed',:committed_rating,:now)
            ON CONFLICT(series_id) DO UPDATE SET
                name=excluded.name, authors=excluded.authors, summary=excluded.summary,
                genres=excluded.genres, content_hash=excluded.content_hash,
                kavita_current=excluded.kavita_current, status='committed',
                committed_rating=excluded.committed_rating, updated_at=excluded.updated_at
        """, {**s, "now": _now()})


def set_override(series_id: int, override: str) -> None:
    with _LOCK, _connect() as c:
        c.execute("UPDATE series SET override_rating=?, updated_at=? WHERE series_id=?",
                  (override or "", _now(), series_id))


def set_status(series_id: int, status: str, committed_rating: str = "") -> None:
    with _LOCK, _connect() as c:
        c.execute("UPDATE series SET status=?, committed_rating=?, updated_at=? WHERE series_id=?",
                  (status, committed_rating, _now(), series_id))


def list_series(status: str = "", search: str = "") -> list:
    q = "SELECT * FROM series"
    clauses, args = [], []
    if status and status != "all":
        clauses.append("status=?")
        args.append(status)
    if search:
        clauses.append("(LOWER(name) LIKE ? OR LOWER(authors) LIKE ?)")
        args += [f"%{search.lower()}%"] * 2
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY (status='pending') DESC, confidence ASC, LOWER(name) ASC"
    with _connect() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def counts() -> dict:
    with _connect() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM series GROUP BY status").fetchall()
        d = {r["status"]: r["n"] for r in rows}
        d["total"] = sum(d.values())
        return d


def seed_from_cache(cache_path: Path) -> int:
    """One-time import of the old cache.json (rating/confidence/reasoning by id)
    so upgrading to the DB doesn't re-spend on classification."""
    if has_any() or not cache_path.exists():
        return 0
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    n = 0
    with _LOCK, _connect() as c:
        for sid, row in cache.items():
            c.execute("""INSERT OR IGNORE INTO series(series_id,name,content_hash,
                         proposed_rating,confidence,reasoning,status,classified_at,updated_at)
                         VALUES(?,?,?,?,?,?, 'pending', ?, ?)""",
                      (int(sid), row.get("name", ""), row.get("hash", ""),
                       row.get("rating", ""), row.get("confidence", 0),
                       row.get("reasoning", ""), _now(), _now()))
            n += 1
    return n

"""Core library for the Kavita age-rater: Kavita API client, the Claude
classifier, and DB-backed scan/commit operations. Imported by app.py (web UI).

Kavita doesn't read an age rating from EPUB metadata, so ratings are set via the
API and locked. Classification results live in SQLite (db.py); the web UI is the
review/commit surface.
"""
import hashlib
import json
import os
from pathlib import Path

import requests
import anthropic

import db

DEFAULT_MODEL = "claude-opus-4-8"
BATCH_SIZE = 15
PAGE_SIZE = 500


# --------------------------------------------------------------------------- #
# Env helpers
# --------------------------------------------------------------------------- #
def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, val = raw.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"{name} is not set")
    return val


# --------------------------------------------------------------------------- #
# Kavita API
# --------------------------------------------------------------------------- #
class Kavita:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.api_key = api_key.lstrip("=").strip()
        self.session = requests.Session()

    def authenticate(self) -> None:
        r = self.session.post(
            f"{self.base}/api/Plugin/authenticate",
            params={"apiKey": self.api_key, "pluginName": "age-rater"}, timeout=20)
        r.raise_for_status()
        self.session.headers.update({"Authorization": f"Bearer {r.json()['token']}"})

    def age_rating_enum(self):
        r = self.session.get(f"{self.base}/api/Metadata/age-ratings", timeout=20)
        r.raise_for_status()
        rows = r.json()
        return ({row["title"].strip().lower(): row["value"] for row in rows},
                {row["value"]: row["title"] for row in rows})

    def all_series(self) -> list:
        series, page = [], 1
        while True:
            r = self.session.post(f"{self.base}/api/Series/all-v2",
                                  params={"PageNumber": page, "PageSize": PAGE_SIZE},
                                  json={}, timeout=60)
            r.raise_for_status()
            batch = r.json()
            series.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            page += 1
        return series

    def series_metadata(self, series_id: int) -> dict:
        r = self.session.get(f"{self.base}/api/Series/metadata",
                             params={"seriesId": series_id}, timeout=30)
        r.raise_for_status()
        return r.json()

    def update_series_rating(self, metadata: dict, rating_value: int) -> None:
        metadata = dict(metadata)
        metadata["ageRating"] = rating_value
        metadata["ageRatingLocked"] = True
        r = self.session.post(f"{self.base}/api/Series/metadata",
                              json={"seriesMetadata": metadata}, timeout=30)
        if r.status_code >= 400:
            r = self.session.post(f"{self.base}/api/Series/metadata",
                                  json={"seriesMetadata": metadata, "collectionTags": []},
                                  timeout=30)
        r.raise_for_status()


def kavita_from_env() -> Kavita:
    k = Kavita(require_env("KAVITA_URL"), require_env("KAVITA_API_KEY"))
    k.authenticate()
    return k


# --------------------------------------------------------------------------- #
# Classifier
# --------------------------------------------------------------------------- #
CLASSIFY_SYSTEM = """You are a children's librarian assigning age ratings to \
books on a family ebook server. Restricted kids' accounts will only be shown \
books at or below their allowed rating, so an accurate rating keeps unsuitable \
books away from children while letting them reach everything appropriate.

You will receive a batch of books (title, author, and — when available — a \
summary and genres). For EACH book, choose the single most appropriate rating \
from the exact list of allowed ratings provided in the user message.

How to map a book's real audience onto the (comic-style) rating scale:
- Picture books / ages ~3-6            -> "Early Childhood"
- Early readers / all-ages non-fiction -> "Everyone"
- Middle grade, roughly ages 8-12      -> "Everyone 10+"
- Older middle grade / tween           -> "PG"
- Young adult, roughly ages 13-16      -> "Teen"
- Adult fiction/non-fiction, mature themes but not explicit -> "Mature 17+"
- Explicit sexual content or extreme violence/gore          -> "Adults Only 18+"

Judge on content maturity: sexual content, graphic violence/gore, strong \
language, drug use, and the complexity/darkness of themes. Author reputation \
and series help when you recognise the book.

BE CONSERVATIVE. This protects children. When you are genuinely unsure of a \
book's content, do NOT guess a child-friendly rating — choose "Rating Pending" \
and a low confidence, which keeps it hidden from kids until a human checks it. \
Prefer the higher (more mature) of two plausible ratings.

Return your answer using the provided JSON schema. `rating` must be copied \
verbatim from the allowed list. `confidence` is 0.0-1.0 (how sure you are of \
the book's actual content). `reasoning` is one short sentence."""

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "ratings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "series_id": {"type": "integer"},
                    "rating": {"type": "string"},
                    "confidence": {"type": "number"},
                    "reasoning": {"type": "string"},
                },
                "required": ["series_id", "rating", "confidence", "reasoning"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ratings"],
    "additionalProperties": False,
}


def content_hash(name, summary, authors, genres) -> str:
    return hashlib.sha256(f"{name}|{summary}|{authors}|{genres}".encode()).hexdigest()[:16]


def classify_batch(client, model, books, allowed_titles) -> dict:
    lines = [
        "Allowed ratings (choose one, verbatim), youngest to oldest audience:",
        ", ".join(f'"{t}"' for t in allowed_titles), "", "Books to rate:",
    ]
    for b in books:
        lines.append(json.dumps({
            "series_id": b["series_id"], "title": b["title"], "author": b["author"],
            "genres": b["genres"], "summary": b["summary"][:1200]}, ensure_ascii=False))
    resp = client.messages.create(
        model=model, max_tokens=4000, system=CLASSIFY_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
        messages=[{"role": "user", "content": "\n".join(lines)}])
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    out = {}
    for row in json.loads(text).get("ratings", []):
        out[int(row["series_id"])] = {
            "rating": row["rating"].strip(),
            "confidence": float(row["confidence"]),
            "reasoning": row["reasoning"].strip()}
    return out


def anthropic_from_env():
    return anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))


def model_from_env() -> str:
    return os.environ.get("AGE_RATER_MODEL", DEFAULT_MODEL)


# --------------------------------------------------------------------------- #
# Scan (DB-backed)
# --------------------------------------------------------------------------- #
def scan_once(kav, client, model, limit=0, reclassify=False, log=print) -> dict:
    title_to_value, value_to_title = kav.age_rating_enum()
    allowed = [value_to_title[v] for v in sorted(value_to_title) if v >= 1]
    series = kav.all_series()
    stats = {"series": len(series), "classified": 0, "refreshed": 0, "already_rated": 0}
    to_classify, considered = [], 0

    for s in series:
        # NOTE: the Series list API does NOT include ageRating — only the
        # per-series metadata call does. Read the authoritative value from md.
        md = kav.series_metadata(s["id"])
        current = md.get("ageRating") or 0
        authors = "; ".join(w.get("name", "") for w in md.get("writers", []))
        genres = "; ".join(g.get("title", "") for g in md.get("genres", []))
        summary = (md.get("summary") or "").strip()
        h = content_hash(s["name"], summary, authors, genres)
        base = {"series_id": s["id"], "name": s["name"], "authors": authors,
                "summary": summary, "genres": genres, "content_hash": h,
                "kavita_current": current}

        if current and current != 0:
            base["committed_rating"] = value_to_title.get(current, str(current))
            db.mark_committed_external(base)
            stats["already_rated"] += 1
            continue

        existing = db.get(s["id"])
        if existing and existing.get("content_hash") == h \
                and existing.get("proposed_rating") and not reclassify:
            db.refresh_meta(base)
            stats["refreshed"] += 1
            continue

        if limit and considered >= limit:
            continue
        considered += 1
        to_classify.append(base)

    for i in range(0, len(to_classify), BATCH_SIZE):
        chunk = to_classify[i:i + BATCH_SIZE]
        log(f"Classifying {i + 1}-{i + len(chunk)} of {len(to_classify)}")
        results = classify_batch(client, model, [
            {"series_id": b["series_id"], "title": b["name"],
             "author": b["authors"] or "(unknown)", "genres": b["genres"],
             "summary": b["summary"]} for b in chunk], allowed)
        for b in chunk:
            r = results.get(b["series_id"], {
                "rating": "Rating Pending", "confidence": 0.0,
                "reasoning": "No rating returned by model."})
            db.upsert_pending({**b, "proposed_rating": r["rating"],
                               "confidence": r["confidence"], "reasoning": r["reasoning"]})
            stats["classified"] += 1

    # Drop DB rows for series that no longer exist in Kavita (e.g. a CWA
    # re-import replaced a book with a new series id). Guarded so a transient
    # empty/failed listing can't wipe the DB.
    if series:
        stats["pruned"] = db.prune_missing({s["id"] for s in series})
    return stats


# --------------------------------------------------------------------------- #
# Commit a single series' rating to Kavita
# --------------------------------------------------------------------------- #
def commit_series(kav, title_to_value, series_id: int, rating_title: str):
    """Write rating_title to Kavita (locked) and mark committed. Returns (ok, msg)."""
    value = title_to_value.get((rating_title or "").strip().lower())
    if value is None:
        return False, f"unknown rating '{rating_title}'"
    if value <= 1:  # Unknown / Rating Pending -> leave hidden, don't lock
        db.set_status(series_id, "skipped")
        return False, "left unrated (Rating Pending/Unknown)"
    md = kav.series_metadata(series_id)
    kav.update_series_rating(md, value)
    db.set_status(series_id, "committed", rating_title)
    return True, rating_title

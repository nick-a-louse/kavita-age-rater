#!/usr/bin/env python3
"""
Kavita age-rater
================
Scans a Kavita library, asks Claude to assign an age rating to each unrated
series, and (in apply mode) writes those ratings back to Kavita, locked so a
re-scan never wipes them. Designed to run unattended on a schedule and only
touch books it hasn't already handled.

Modes
-----
  scan   (default)  Find unrated series, classify them with Claude, and write a
                    review file (proposals.csv). Does NOT change Kavita.
  apply             Read proposals.csv (optionally edited by you) and push the
                    ratings to Kavita, locking the age-rating field.
  auto              scan + apply in one pass. Anything Claude is not confident
                    about is left Unknown (hidden from restricted kids' accounts)
                    rather than guessed.

Config (environment variables, or a KEY=VALUE file passed with --env-file)
  KAVITA_URL           e.g. http://your-server:5053
  KAVITA_API_KEY       Kavita API key (the long token; also the one in your OPDS URL)
  ANTHROPIC_API_KEY    from console.anthropic.com
  AGE_RATER_MODEL      optional, default claude-opus-4-8
  AGE_RATER_MIN_CONFIDENCE  optional (auto mode), default 0.6
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests
import anthropic

HERE = Path(__file__).resolve().parent
# State (cache, review file, log) lives in DATA_DIR so it survives across
# container runs when /data is a mounted volume. Defaults to the script folder.
DATA_DIR = Path(os.environ.get("AGE_RATER_DATA_DIR", str(HERE)))
CACHE_PATH = DATA_DIR / "cache.json"
PROPOSALS_PATH = DATA_DIR / "proposals.csv"
LOG_PATH = DATA_DIR / "age_rater.log"

DEFAULT_MODEL = "claude-opus-4-8"
BATCH_SIZE = 15          # books per Claude request
PAGE_SIZE = 500          # Kavita series page size


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE lines into os.environ (does not overwrite existing)."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, val = raw.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(f"ERROR: {name} is not set (env var or --env-file).")
    return val


# --------------------------------------------------------------------------- #
# Kavita API
# --------------------------------------------------------------------------- #
class Kavita:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        # tolerate a stray leading '=' if the key was pasted awkwardly
        self.api_key = api_key.lstrip("=").strip()
        self.session = requests.Session()
        self.token = None

    def authenticate(self) -> None:
        r = self.session.post(
            f"{self.base}/api/Plugin/authenticate",
            params={"apiKey": self.api_key, "pluginName": "age-rater"},
            timeout=20,
        )
        r.raise_for_status()
        self.token = r.json()["token"]
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def age_rating_enum(self) -> dict:
        """Return {title_lower: value} and {value: title} maps from the server."""
        r = self.session.get(f"{self.base}/api/Metadata/age-ratings", timeout=20)
        r.raise_for_status()
        rows = r.json()
        title_to_value = {row["title"].strip().lower(): row["value"] for row in rows}
        value_to_title = {row["value"]: row["title"] for row in rows}
        return title_to_value, value_to_title

    def all_series(self) -> list:
        series = []
        page = 1
        while True:
            r = self.session.post(
                f"{self.base}/api/Series/all-v2",
                params={"PageNumber": page, "PageSize": PAGE_SIZE},
                json={},
                timeout=60,
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            series.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            page += 1
        return series

    def series_metadata(self, series_id: int) -> dict:
        r = self.session.get(
            f"{self.base}/api/Series/metadata",
            params={"seriesId": series_id},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def update_series_rating(self, metadata: dict, rating_value: int) -> None:
        """Set ageRating + lock it, posting the full metadata dto back."""
        metadata = dict(metadata)
        metadata["ageRating"] = rating_value
        metadata["ageRatingLocked"] = True
        r = self.session.post(
            f"{self.base}/api/Series/metadata",
            json={"seriesMetadata": metadata},
            timeout=30,
        )
        # Older Kavita builds also expect a collectionTags key
        if r.status_code >= 400:
            r = self.session.post(
                f"{self.base}/api/Series/metadata",
                json={"seriesMetadata": metadata, "collectionTags": []},
                timeout=30,
            )
        r.raise_for_status()


# --------------------------------------------------------------------------- #
# Classification via Claude
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


def classify_batch(client, model: str, books: list, allowed_titles: list) -> dict:
    """books: list of dicts with series_id/title/author/summary/genres.
    Returns {series_id: {rating, confidence, reasoning}}."""
    lines = [
        "Allowed ratings (choose one, verbatim), from youngest to oldest audience:",
        ", ".join(f'"{t}"' for t in allowed_titles),
        "",
        "Books to rate:",
    ]
    for b in books:
        lines.append(json.dumps({
            "series_id": b["series_id"],
            "title": b["title"],
            "author": b["author"],
            "genres": b["genres"],
            "summary": b["summary"][:1200],
        }, ensure_ascii=False))
    user_msg = "\n".join(lines)

    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=CLASSIFY_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
        messages=[{"role": "user", "content": user_msg}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    data = json.loads(text)
    out = {}
    for row in data.get("ratings", []):
        out[int(row["series_id"])] = {
            "rating": row["rating"].strip(),
            "confidence": float(row["confidence"]),
            "reasoning": row["reasoning"].strip(),
        }
    return out


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def content_hash(name: str, summary: str, authors: str, genres: str) -> str:
    return hashlib.sha256(f"{name}|{summary}|{authors}|{genres}".encode("utf-8")).hexdigest()[:16]


def load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #
def do_scan(kav: Kavita, client, model: str, value_to_title: dict,
            title_to_value: dict, limit: int) -> list:
    allowed_titles = [value_to_title[v] for v in sorted(value_to_title) if v >= 1]
    series = kav.all_series()
    cache = load_cache()
    proposals = []
    pending = []

    considered = 0
    for s in series:
        if limit and considered >= limit:
            break
        current = s.get("ageRating") or 0
        # Skip anything already rated in Kavita (assume a human/earlier run set it)
        if current and current != 0:
            continue
        considered += 1
        md = kav.series_metadata(s["id"])
        authors = "; ".join(w.get("name", "") for w in md.get("writers", []))
        genres = "; ".join(g.get("title", "") for g in md.get("genres", []))
        summary = (md.get("summary") or "").strip()
        h = content_hash(s["name"], summary, authors, genres)

        cached = cache.get(str(s["id"]))
        if cached and cached.get("hash") == h:
            row = cached
        else:
            pending.append({
                "series_id": s["id"],
                "title": s["name"],
                "author": authors or "(unknown)",
                "genres": genres,
                "summary": summary,
                "_hash": h,
                "_current": current,
            })
            continue

        proposals.append(_proposal_row(s, authors, summary, current,
                                        row["rating"], row["confidence"],
                                        row["reasoning"], value_to_title))

    # Classify the ones we couldn't serve from cache
    for i in range(0, len(pending), BATCH_SIZE):
        chunk = pending[i:i + BATCH_SIZE]
        log(f"Classifying books {i+1}-{i+len(chunk)} of {len(pending)} ...")
        results = classify_batch(client, model, chunk, allowed_titles)
        for b in chunk:
            res = results.get(b["series_id"])
            if not res:
                res = {"rating": "Rating Pending", "confidence": 0.0,
                       "reasoning": "No rating returned by model."}
            cache[str(b["series_id"])] = {
                "hash": b["_hash"], "name": b["title"],
                "rating": res["rating"], "confidence": res["confidence"],
                "reasoning": res["reasoning"], "ts": time.time(),
            }
            proposals.append(_proposal_row(
                {"id": b["series_id"], "name": b["title"]},
                b["author"], b["summary"], b["_current"],
                res["rating"], res["confidence"], res["reasoning"], value_to_title))
        save_cache(cache)

    save_cache(cache)
    return proposals


def _proposal_row(s, authors, summary, current, rating, confidence,
                  reasoning, value_to_title):
    return {
        "series_id": s["id"],
        "name": s["name"],
        "authors": authors,
        "current_rating": value_to_title.get(current, current),
        "proposed_rating": rating,
        "confidence": round(float(confidence), 2),
        "final_rating": "",  # blank = accept proposed; or a title, or SKIP
        "reasoning": reasoning,
        "summary_snippet": " ".join(summary.split())[:200],
    }


def write_proposals(rows: list) -> None:
    rows = sorted(rows, key=lambda r: (r["confidence"], r["name"].lower()))
    fields = ["series_id", "name", "authors", "current_rating", "proposed_rating",
              "confidence", "final_rating", "reasoning", "summary_snippet"]
    with PROPOSALS_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    log(f"Wrote {len(rows)} proposals -> {PROPOSALS_PATH}")


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
def resolve_rating(row: dict, title_to_value: dict):
    """Return (value, title) to apply, or None to skip."""
    final = (row.get("final_rating") or "").strip()
    chosen = final if final else row.get("proposed_rating", "").strip()
    if not chosen or chosen.upper() == "SKIP":
        return None
    value = title_to_value.get(chosen.lower())
    if value is None:
        log(f"  ! '{row['name']}': unknown rating '{chosen}', skipping.")
        return None
    if value <= 1:  # Unknown / Rating Pending -> leave hidden, don't lock
        return None
    return value, chosen


def do_apply(kav: Kavita, title_to_value: dict, rows: list,
             min_confidence: float = 0.0) -> None:
    applied = skipped = 0
    for row in rows:
        if min_confidence and float(row.get("confidence", 0)) < min_confidence \
                and not (row.get("final_rating") or "").strip():
            skipped += 1
            continue
        resolved = resolve_rating(row, title_to_value)
        if not resolved:
            skipped += 1
            continue
        value, title = resolved
        sid = int(row["series_id"])
        md = kav.series_metadata(sid)
        if md.get("ageRatingLocked") and (md.get("ageRating") or 0) > 0:
            skipped += 1  # already set & locked, leave it
            continue
        kav.update_series_rating(md, value)
        applied += 1
        log(f"  set '{row['name']}' -> {title}")
    log(f"Apply complete: {applied} set, {skipped} skipped.")


def read_proposals() -> list:
    if not PROPOSALS_PATH.exists():
        sys.exit(f"ERROR: {PROPOSALS_PATH} not found. Run scan first.")
    with PROPOSALS_PATH.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Assign Kavita age ratings with Claude.")
    ap.add_argument("mode", nargs="?", default="scan",
                    choices=["scan", "apply", "auto"])
    ap.add_argument("--env-file", type=Path)
    ap.add_argument("--limit", type=int, default=0,
                    help="Only process the first N unrated series (testing).")
    args = ap.parse_args()

    if args.env_file:
        load_env_file(args.env_file)
    else:
        for candidate in (HERE / ".env", HERE / "config.env"):
            if candidate.exists():
                load_env_file(candidate)
                break

    kav = Kavita(require_env("KAVITA_URL"), require_env("KAVITA_API_KEY"))
    kav.authenticate()
    title_to_value, value_to_title = kav.age_rating_enum()
    model = os.environ.get("AGE_RATER_MODEL", DEFAULT_MODEL)
    min_conf = float(os.environ.get("AGE_RATER_MIN_CONFIDENCE", "0.6"))

    if args.mode in ("scan", "auto"):
        client = anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))
        rows = do_scan(kav, client, model, value_to_title, title_to_value, args.limit)
        write_proposals(rows)

    if args.mode == "apply":
        rows = read_proposals()
        do_apply(kav, title_to_value, rows)

    if args.mode == "auto":
        do_apply(kav, title_to_value, rows, min_confidence=min_conf)


if __name__ == "__main__":
    main()

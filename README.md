# Kavita age-rater

Assigns age ratings to the books in a [Kavita](https://www.kavitareader.com/)
library using Claude, so that restricted kids' accounts — and the
[Readest](https://readest.com/) apps that connect to them over OPDS — only ever
see age-appropriate books.

It runs as a **long-running Docker container** with a **web UI**: a background
scheduler classifies unrated books with Claude, and you review, override, and
commit the ratings from your browser.

## Why

Kavita doesn't read an age rating out of EPUB files, so ratings have to be set
through Kavita's API. This tool:

1. Periodically lists every **series** in Kavita with no age rating.
2. Sends each book's title / author / summary / genres to Claude, which picks
   the best rating from Kavita's own scale (Everyone, Everyone 10+, Teen, …),
   storing the result (with a confidence and a one-line reason) in a local DB.
3. Lets you **review** those proposals in a web UI — override any of them, then
   commit individually or in bulk.
4. On commit, writes the rating back to Kavita and **locks** the field so a
   library re-scan never wipes it.

Classifications are cached, so scheduled runs only classify **new or changed**
books. Anything Claude is unsure about is left **Rating Pending** (hidden from
restricted accounts) until you confirm it.

## Web UI

Browse to `http://<server-ip>:<HOST_PORT>` (default port `5058`). You get a
table of books with their proposed rating, confidence (colour-coded, lowest
first), and reason. From there you can:

- Filter by **Pending / Committed / Skipped / All**, search by title/author, and
  hide low-confidence rows with the **min-confidence** slider.
- Change the **Rating to apply** dropdown on any row (an override).
- **Commit** a single row, a multi-select, or **all pending** at once — each
  commit writes the rating to Kavita, locked.
- **Skip** books to leave them unrated.
- **Rescan now** to classify newly added / changed books immediately.

## Configuration

Copy `.env.example` to `.env` and fill in:

- `KAVITA_URL` — e.g. `http://your-server:5053`
- `KAVITA_API_KEY` — the token from your Kavita OPDS URL (Settings → Account)
- `ANTHROPIC_API_KEY` — from [console.anthropic.com](https://console.anthropic.com)
- `HOST_PORT` — web UI port (default `5058`)
- `AUTO_SCAN` / `INTERVAL_HOURS` — background scan cadence
- `DATA_PATH` — host folder for the SQLite DB + log (server: `/opt/appdata/kavita-age-rater`)

## Run

```bash
docker compose up -d --build
docker compose logs -f          # watch the first scan
```

Then open the web UI. The container keeps running: it serves the UI and
re-scans every `INTERVAL_HOURS`.

## Kids' accounts (the enforcement side)

Ratings only restrict kids if their Kavita account enforces them:

- Give each child a Kavita account with an **Age Restriction** (e.g. *Teen*) and
  turn **"Include unknowns" OFF**, so unrated books are hidden.
- In Readest, connect that child's account via its **OPDS URL** — Readest then
  only ever sees the books Kavita allows for that account.

## Files

- `app.py` — Flask web app + background scheduler
- `age_rater.py` — Kavita API client, Claude classifier, scan/commit logic
- `db.py` — SQLite store
- `templates/index.html` — the review UI (self-contained, no external assets)

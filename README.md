# Kavita age-rater

Assigns age ratings to the books in a [Kavita](https://www.kavitareader.com/)
library using Claude, so that restricted kids' accounts — and the
[Readest](https://readest.com/) apps that connect to them over OPDS — only ever
see age-appropriate books.

## Why

Kavita doesn't read an age rating out of EPUB files, so ratings have to be set
through Kavita's API. This tool:

1. Lists every **series** in Kavita and finds the ones with no age rating.
2. Sends each book's title / author / summary / genres to Claude, which picks
   the best rating from Kavita's own scale (Everyone, Everyone 10+, Teen, …).
3. Writes a **review file** (`proposals.csv`) — nothing changes in Kavita yet.
4. On `apply`, writes the ratings back to Kavita and **locks** the field so a
   library re-scan never wipes them.

It caches its decisions, so scheduled runs only classify **new or changed**
books. Anything Claude is unsure about is left **Rating Pending / Unknown**,
which stays hidden from restricted accounts until a human confirms it.

## Modes

| Mode    | What it does                                                          |
|---------|----------------------------------------------------------------------|
| `scan`  | Classify unrated books, write `proposals.csv`. **Does not touch Kavita.** |
| `apply` | Push the (optionally edited) `proposals.csv` to Kavita, locked.       |
| `auto`  | `scan` + `apply` in one pass; low-confidence books left Unknown.      |

## Configuration

Copy `.env.example` to `.env` and fill in:

- `KAVITA_URL` — e.g. `http://your-server:5053`
- `KAVITA_API_KEY` — the token from your Kavita OPDS URL (Settings → Account)
- `ANTHROPIC_API_KEY` — from [console.anthropic.com](https://console.anthropic.com)
- `RUN_MODE` / `INTERVAL_HOURS` — behaviour of the scheduled loop
- `DATA_PATH` — host folder for persistent state (server: `/opt/appdata/age_rater`)

## Run with Docker (recommended)

```bash
docker compose build

# One-off review pass (writes proposals.csv into the data volume)
docker compose run --rm rater scan --limit 10   # or: scan  (all books)

# Review data/proposals.csv, then push the ratings
docker compose run --rm rater apply

# Start the scheduled loop (uses RUN_MODE / INTERVAL_HOURS from .env)
docker compose up -d
docker compose logs -f
```

`proposals.csv` is sorted lowest-confidence first, so the rows most worth
checking are at the top. Leave a row's `final_rating` blank to accept the
proposal, type a different rating title to override, or `SKIP` to leave it
unrated.

## Run without Docker

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit .env
venv/bin/python age_rater.py scan
venv/bin/python age_rater.py apply
```

## Kids' accounts (the enforcement side)

Ratings only restrict kids if their Kavita account enforces them:

- Give each child a Kavita account with an **Age Restriction** (e.g. *Teen*) and
  turn **"Include unknowns" OFF**, so unrated books are hidden.
- In Readest, connect that child's account via its **OPDS URL** — Readest then
  only ever sees the books Kavita allows for that account.

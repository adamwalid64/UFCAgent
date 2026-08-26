# UFCStats SQLite Scraper

Collects completed UFC events, fight totals, round-by-round statistics, and
fighter career aggregates from UFCStats into a normalized SQLite database.

## Setup

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Usage

```powershell
# Initial historical backfill (oldest to newest for correct aggregates)
python -m pipeline.run --full

# Normal run: fetch new events and stop at the first known event
python -m pipeline.run

# Quick smoke run against a separate database
python -m pipeline.run --full --max-events 1 --db-url sqlite:///./smoke.db

# Detailed diagnostics, only when needed
python -m pipeline.run --debug
```

With no arguments, the scraper uses `ufc_fights.db`, incremental mode, the
fast reusable-browser session, and a one-second request interval. Its main tables are `events`,
`fighters`, `fights`, and `fight_rounds`. Fighter IDs and fight IDs come from
UFCStats profile URLs, avoiding collisions between people with similar names.

Full ingestion deliberately processes cards chronologically. This makes the
win/loss record, streak, rest-days, and head-to-head values stored on each
fight true pre-fight features while the `fighters` table retains current
career aggregates.

The scraper keeps one Chromium context and page alive for the entire run, as
the original drop-in scraper did. `--delay` controls the minimum interval
between navigations; it does not incur browser startup overhead per fight.

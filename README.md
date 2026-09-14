# UFCStats SQLite data pipeline

Builds and maintains a validated SQLite history of completed UFC events, fights,
fighter profiles, fight totals, and round-by-round statistics from UFCStats.
The canonical database is `ufc_fights.db`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Safe recurring refresh

Run this after each UFC card (or let the weekly task run it):

```powershell
.\scripts\update_ufc_data.ps1
```

The updater does not scrape directly into the live database. It:

1. takes a consistent snapshot into a per-run staging directory;
2. rechecks the source listing and the eight newest cards;
3. adds newly completed events and refreshes profiles and missing metadata;
4. deterministically rebuilds pre-fight aggregates;
5. validates source coverage, foreign keys, round totals, chronology, feature
   timing, and no-regression rules; and
6. atomically promotes the candidate only if every hard check passes.

A failed candidate stays under `data_runs/<run-id>/` for diagnosis and never
replaces `ufc_fights.db`. A successful promotion retains the prior live version
as `ufc_fights.previous.db`. The lock file beside the database prevents two
scheduled refreshes from racing; its presence is normal and a crashed process
releases the operating-system lock automatically. Each run also writes
`manifest.json`, `validation.json`, and an append-only `data_runs/audit.jsonl`.
Scheduled executions retain their complete Python console logs under
`data_runs/scheduled_logs/`, including failures that occur before validation.

On Windows, keep SQLite read connections short-lived. The updater retries brief
file-sharing conflicts before promotion, but a process that holds
`ufc_fights.db` open indefinitely can prevent the atomic file replacement. A
blocked promotion leaves the live DB unchanged and is retried by the scheduled
task; the SQL consumer should close/recycle its connection between requests or
during the maintenance window.

### First build

To build the entire database from the source:

```powershell
.\scripts\update_ufc_data.ps1 -Full
```

To seed an empty canonical database from an existing completed scrape, while
still reconciling and validating it:

```powershell
.\scripts\update_ufc_data.ps1 -BootstrapFrom .\ufc_fights_fresh.db
```

`-Full` revisits every fight page and is useful as a deliberate one-time
historical reconciliation. Normal weekly updates use the bounded recent-card
overlap so corrections and newly posted results are picked up without
rescraping the entire history.

### Install the Windows weekly task

The installer defaults to Monday at 06:00 local time, waits for a network,
starts a missed run when the machine next becomes available, rejects overlapping
runs, continues safely when a laptop changes to battery power, retries a failed
run up to three times at 30-minute intervals, and limits each attempt to eight
hours.

```powershell
# Preview without changing Task Scheduler
.\scripts\install_weekly_task.ps1 -WhatIf

# Install (choose another day/time if desired)
.\scripts\install_weekly_task.ps1 -DayOfWeek Monday -At "06:00"

# Retry settings are configurable
.\scripts\install_weekly_task.ps1 -RetryCount 3 -RetryIntervalMinutes 30

# Optional: run even while this Windows account is logged off
$credential = Get-Credential
.\scripts\install_weekly_task.ps1 -Credential $credential
```

The repository and virtual environment must remain at their current paths for
the scheduled task. Confirm one manual refresh succeeds before installing it.
The runner deliberately fails if `.venv\Scripts\python.exe` is missing instead
of silently using a different global Python installation.

The task runs with limited privileges. Without `-Credential`, it intentionally
uses the current user's interactive token and therefore runs only while that
user is logged on. With `-Credential`, Windows Task Scheduler stores the logon
credential and can run the task while the account is logged off; reinstall the
task after that password changes. No password is placed in the task command or
repository. If all retry attempts fail, inspect the run artifacts and rerun the
updater manually. Run the installer again to replace its configuration.

## Validate the live data

The weekly updater applies the authoritative strict promotion gate with a fresh
source manifest automatically. A read-only manual health check is also
available, but without that run's manifest it cannot independently prove that
the source listing is complete:

```powershell
python -m pipeline.validate --db .\ufc_fights.db --temporal-policy bout-start
```

Use `python -m pipeline.weekly --help` and
`python -m pipeline.validate --help` for advanced options.
Updater exit code `0` means promotion passed (warnings may remain), `1` means
validation rejected the candidate, and `2` means the run failed before
promotion.

`python -m pipeline.run` is a low-level development entry point. It refuses to
write directly to the canonical `ufc_fights.db` unless an explicit
`--unsafe-direct-write` recovery override is supplied; routine and scheduled
updates must use the validated weekly entry point.

## Data model

The main SQL surfaces are:

- `events`: one row per completed event, with date, location, source URL, and
  expected source fight count.
- `fights`: one canonical row per bout. `fighter_a_*` and `fighter_b_*`
  pre-fight fields describe information available entering that bout; the
  strike, takedown, control, and ending fields describe the completed bout.
- `fight_rounds`: one row per fighter per source round, including general totals
  and head/body/leg plus distance/clinch/ground significant-strike splits.
- `fighters`: the latest known profile and final/current aggregates. These rows
  are useful for identity and display, but their aggregates are not safe as
  historical model features.
- `fighter_bout_history`: the recommended query view. It emits one
  fighter-perspective row per bout and names fields by time domain:
  `prefight_*`, `opponent_prefight_*`, `bout_*`, and `opponent_bout_*`.

Fighter and fight IDs come from UFCStats URLs. Query by `fighter_id` whenever
possible because names are not unique.

Example: retrieve both fighters' histories strictly before a target date:

```sql
SELECT *
FROM fighter_bout_history
WHERE fighter_id IN (:fighter_1_id, :fighter_2_id)
  AND event_date < :target_date
ORDER BY fighter_id, event_date, fighter_bout_sequence;
```

The view contains two rows per fight. For fight-level training data, explicitly
deduplicate or pivot by `fight_id`; for fighter-level histories, keep the two
perspectives.

## Prediction-data rules and source limits

- For a fight being predicted, use its `prefight_*` values only. The same row's
  `bout_*`, result, winner, method, ending, and round statistics are outcomes
  and would leak the target.
- Completed `bout_*` rows are valid historical observations only when the query
  restricts them to `event_date < :target_date`.
- `fighters` totals are recalculated through the latest stored event, so using
  them for an older target would leak future information.
- Pre-fight wins/losses/draws count prior bouts in the UFCStats dataset; they are
  not a fighter's complete professional record across every promotion. Rest
  days and streaks are likewise based on events covered by this dataset.
- Height, reach, and stance may be filled from the latest profile snapshot.
  `prefight_profile_imputed` and `opponent_prefight_profile_imputed` expose that
  provenance; age is derived from date of birth at the event date.
- `stats_available = 0` means UFCStats did not publish a usable round table.
  Such missing source data remains `NULL`, never a fabricated zero.
- The observed source history begins at UFC 2. A small group of early bouts has
  results but no round table on the source and is retained with explicit
  unavailable-stat flags.
- The database contains completed UFCStats events. It does not supply an
  upcoming matchup, odds, or rankings; provide the target date and stable
  fighter IDs from the appropriate upstream source.
- Normal weekly runs populate detailed significant-strike splits for new and
  recently rechecked cards. Run the deliberate `-Full` reconciliation once if
  complete split coverage across all older fight pages is required.

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
3. adds newly completed events, refreshes profiles/metadata, and saves the next upcoming card;
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

UFCStats can list an event as completed while bouts are still in progress.
Current cards with explicit unplayed matchups are deferred until a later run;
the manifest records them separately. A listing that omits stored historical
events is retried before reconciliation, and persistent coverage gaps still
block promotion.

To reuse the work from a failed run, pass its retained staging database:

```powershell
.\scripts\update_ufc_data.ps1 -ResumeFrom .\data_runs\<run-id>\ufc_fights.staging.db
```

This copies the candidate into a new run, retains its profile backfill, fetches
fresh source data, and applies the same validation against the current live DB.
The original failed candidate remains available. The equivalent Python option
is `python -m pipeline.weekly --resume-from <staging.db>`.

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
- `upcoming_events` and `upcoming_fights`: the latest next-card schedule, replaced
  atomically on each successful scrape. These tables contain no fight results and
  do not contribute to career records or historical features.
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
- The historical tables contain completed UFCStats events. The separate schedule
  tables supply the next announced card. Odds and rankings are not supplied.
- Normal weekly runs populate detailed significant-strike splits for new and
  recently rechecked cards. Run the deliberate `-Full` reconciliation once if
  complete split coverage across all older fight pages is required.

## SQL matchup analysis agent

The Python/LangGraph agent in `sql_agent/` builds complete matchup evidence,
researches with a `plan -> query -> observe -> decide` loop, and drafts a
prediction. Code validates the result and allows bounded corrections before
publication. Paid model review remains optional and off by default. Edit
[`prompts/sql_agent_system.md`](prompts/sql_agent_system.md) for the analysis
instructions and [`prompts/sql_agent_review.md`](prompts/sql_agent_review.md)
for the optional review rubric. Enabled prompts reload for every prediction. Code defines tool
contracts, evidence calculations, limits, and publication checks. See
[`SCHEMA_NOTES.md`](SCHEMA_NOTES.md) for the inspected schema and missing data.

After installing `requirements.txt` and Playwright Chromium (see Setup), put
`OPENAI_API_KEY=your-key` in `.env` at the repository root, or set it in your shell.
The agent loads this file automatically from both the CLI and Python API, even
when launched from another directory. Existing environment variables take
precedence. `.env` and `.env.*` are ignored by Git (except `.env.example`).
Refresh once to populate the schedule:

```powershell
.\scripts\update_ufc_data.ps1

# No fighter arguments: predict every fight on the saved next card
python -m sql_agent

# A card already in progress: fetch it and predict only fights without posted results
python -m sql_agent --event "Allen vs Duncan"

# If the broadcast is ahead of UFCStats, manually exclude an underway bout
python -m sql_agent --event "Allen vs Duncan" --exclude-fight "Allen Frye Jr."

# Single-fight terminal test, with a five-round scheduled distance
python -m sql_agent "Jon Jones" "Daniel Cormier" --as-of-date 2015-01-03 --rounds 5

# Optional full JSON, including the SQL audit
python -m sql_agent "Jon Jones" "Daniel Cormier" --rounds 5 --json
```

Terminal output shows each pick, confidence, ending method/round, justification,
opponent quality and historical style matchups for both fighters, cited bouts,
the case for the other fighter, confidence assessment, validation status, and token/cost usage.
Research and review progress appear on stderr. Card predictions
use the card date as their cutoff and a fresh query budget per bout; failed bouts
are reported while the rest continue (exit 1 for partial failure, 2 for startup
failure). `--json` keeps stdout machine-readable.

Every CLI run also saves a readable `.txt` report in the repository's
`predictions/` folder. Filenames include the event or matchup and a unique run
timestamp, so earlier predictions are retained. Each fight is saved as it
finishes, including failures; an interrupted card keeps its completed reports.
`--json` also saves the full structured result and SQL audit in a matching
`.json` file. Generated reports are ignored by Git.
Use `--output-dir "C:\path\to\reports"` to choose another folder.

Before drafting, every prediction receives opponent-adjusted Elo estimates,
opponents' records entering each historical bout, measured performance against
relative strength tiers, and historical exposure to measurable fighting styles.
Elo starts at 1500 with K=32 and no finish bonus: winning a decision against a
strong opponent can count for more than knocking out a weak opponent. These
ratings are derived estimates, not official rankings or championship status.
Style profiles use strike, takedown and submission-attempt rates, require at
least three prior bouts with usable timed statistics, and are frozen before each
historical matchup. The report must explain unknown or untested style exposure.

Code always validates the output schema, fighter identity, scheduled distance,
and historical citation eligibility. SQL isolation enforces the evidence cutoff.
Invalid drafts are withheld when the request budget is exhausted. A successful default
prediction is labeled `validated` with `mode: deterministic`; this does not
certify that every prose claim is supported or that the forecast is accurate.

The analyst defaults to `gpt-5.4`, `--reasoning-effort medium`, and `--review off`.
These defaults prioritize retaining analysis capability over minimizing cost;
the incomplete retrospective experiment did not establish the best reasoning
level. Opponent-quality evidence, style history, counterarguments and confidence
explanations remain. Use `--model` / `OPENAI_MODEL` to change the analyst.
Adjust native reasoning with `--reasoning-effort none|low|medium|high|xhigh`
or `OPENAI_REASONING_EFFORT`. No effort parameter is automatically sent for
other models; leave it unset for models without reasoning support.

Normal runs also default to `--research on`, `--max-model-calls 24`, and
`--max-output-tokens 12000`. These are ceilings, not request or output targets.
Research can stop early when the evidence suffices. The larger output allowance
leaves room for reasoning and a complete structured explanation; the largest
recorded output in the earlier experiment was 3756 tokens, above the former
2500-token limit. The model sees ordinary named JSON fields, every eligible bout
and exact value. Repeated opponent-quality/style requests point back to the
complete packet already in context, and synthesis references its style sections
instead of inserting duplicate copies. No original evidence is dropped.

Code reserves requests for drafting and validation repairs (six under the normal
24-call/two-revision configuration). Up to two draft correction passes can fix
invalid fields or citations; failed publication checks never silently pass.
Each model invocation can retry a transient transport/rate-limit/server failure
once while requests remain. That retry is explicitly counted and audited;
SDK retries remain disabled. Exhausted credits and permanent errors are not
retried. Unknown charges after failed requests remain marked unknown.

Explicit `--max-model-calls` / `SQL_AGENT_MAX_MODEL_CALLS` takes precedence,
including failures and retries. `--max-output-tokens` /
`SQL_AGENT_MAX_OUTPUT_TOKENS` limits output including native reasoning;
`--max-revisions` never overrides the request cap. These controls bound requests
and generated tokens per fight, not total dollars. Input size, model, card size
and caching still affect the bill.

For deliberate economy testing, use `--research off --review off
--reasoning-effort none --max-model-calls 1 --max-output-tokens 2500`. This path
computes common opponents and MMA paths locally and uses the compact shared-field
encoding. Its lossless round trip was verified offline and its saved packets were
44-52% smaller in characters, but model interpretation/forecasting accuracy has
not been established. It is optional because preserving data bytes does not
prove unchanged model performance. `--review on` without research defaults to
eight requests; research on defaults to 24. Existing environment overrides remain
in effect until changed.

For an explicit paid review, use `--review on` (or `SQL_AGENT_REVIEW_MODE=on`).
It uses `gpt-5.4` in a fresh context by default; override with `--review-model`
or `OPENAI_REVIEW_MODEL`. This reviewer checks factual support, opponent quality,
style history, confidence, internal consistency and temporal integrity. Code
verifies copied values against the same named evidence structure the reviewer
sees, accepting exact plain strings and legacy JSON-encoded strings. Invalid
assessments receive one repair attempt containing the previous invalid verdict.
With review enabled, unresolved issues or an unavailable reviewer withhold the
prediction. `--max-revisions 0` then permits one review without draft revisions.
Review can catch prose errors, but can also make incorrect objections; it does
not guarantee better forecasts. Evidence and review verdicts remain in the audit.

Each model invocation records API-reported input, cached input, output and
reasoning tokens; reasoning is already included in output tokens. Reports show
estimated standard API cost, including failed attempts with reported usage.
Unknown usage stays unknown. Estimates are not invoices, and caching/output
length can make a reasoning run cheaper than another run with reasoning off.
When exhausted credits are detected, subsequent model calls in that process
stop; previously saved predictions remain available. Already-running requests
may still finish.

Both clients use the Responses API. Reports provide concise evidence explanations
and review findings, not private chain-of-thought transcripts. Confidence remains
uncalibrated until evaluated against held-out fight results.

The scraper refreshes changed pairings and drops removed bouts from the schedule.
An upcoming-source failure prevents promotion and preserves the live database;
`python -m pipeline.weekly --skip-upcoming` explicitly refreshes history only.
Expired/completed cards are not selected for predictions. Schedule-only debutants
resolve by name/ID with empty histories and thin-data caveats, without fabricated
career records. Refresh close to the event because cards change.

`--event` accepts a name, UFCStats event URL, or source ID and reads that card
directly without changing either the historical DB or the saved next card.
It skips posted results and rechecks each pairing before and after analysis.
All evidence remains strictly before the event date, including on live cards.
UFCStats can lag the broadcast and does not reliably identify a bout currently
underway; repeat `--exclude-fight` with an exact fighter name or fight ID when
needed. Fighters absent from the DB receive temporary identities with no invented
history. Terminal output is the default; `--json` includes the audit details.

UFCStats does not publish scheduled distances on the observed upcoming pages.
Until a distance is known, card mode assumes five rounds for the main event/title
bouts and three for other bouts, and includes that caveat. Single-fight mode
defaults to three; use `--rounds 5` when appropriate. Decision predictions must end
in the scheduled final round, and finishes cannot exceed the scheduled distance.

Exact names or UFCStats fighter IDs are accepted; ambiguous names require IDs.
`python code/SQLAgent.py` accepts the same arguments. In Python:

```python
from sql_agent.graph import build_sql_agent

agent = build_sql_agent()
result = agent.predict("Jon Jones", "Daniel Cormier", as_of_date="2015-01-03", scheduled_rounds=5)
print(result["summary"])
```

| Option | Default | Meaning |
|---|---|---|
| `--db` | repo's `ufc_fights.db` | Canonical database, opened read-only |
| `--as-of-date` | today for a single fight; card date for a card | Only fights strictly before this date |
| `--rounds` | 3 for a single fight | Set a single fight's scheduled distance to 3 or 5 |
| `--json` | off | Full JSON and query log instead of readable summaries |
| `--output-dir` | repo's `predictions/` | Readable reports and an incremental `.ledger.jsonl` audit; `--json` also writes a complete JSON report |
| `--model` / `OPENAI_MODEL` | `gpt-5.4` | Analysis model supporting structured function outputs |
| `--prompt` | `prompts/sql_agent_system.md` | Runtime system prompt |
| `--review` / `SQL_AGENT_REVIEW_MODE` | off | Optional paid critic; code validation always runs |
| `--research` / `SQL_AGENT_RESEARCH_MODE` | on | Model-directed research with full named evidence; off selects compact direct drafting |
| `--max-model-calls` / `SQL_AGENT_MAX_MODEL_CALLS` | 24 with research; 8 with review only; 1 for direct drafting | Hard request cap per fight, including retries and failures |
| `--max-output-tokens` / `SQL_AGENT_MAX_OUTPUT_TOKENS` | 12000 | Generated-token ceiling per request, including reasoning |
| `--review-model` / `OPENAI_REVIEW_MODEL` | `gpt-5.4` | Model used for the separate final review |
| `--review-prompt` | `prompts/sql_agent_review.md` | Runtime evaluation rubric |
| `--max-revisions` | 2 | Allowed correction attempts, only while model requests remain (0 through 3) |
| `--reasoning-effort` / `OPENAI_REASONING_EFFORT` | medium for `gpt-5.4` | Native reasoning effort, only for compatible analysis models |
| `--max-queries` / `MAX_QUERIES` | 15 | Maximum tool attempts, including mandatory evidence, errors and schema discovery |
| `--row-limit` | 200 | Maximum rows returned by arbitrary SQL |
| `--timeout` | 5 seconds | SQL execution and MMA path-search time limit |
| `--model-timeout` | 120 seconds | Timeout per request; research allows one metered transient retry, SDK retries disabled |

The SELECT/WITH guard, SQLite authorizer, and `query_only` protect a private
in-memory copy of eligible rows. The source is opened with `mode=ro` and closed
after setup; no persistent data or tables change. Even SQL without a date filter
sees only pre-cutoff bouts. Current fighter aggregates are inaccessible.

The mandatory evidence packet consumes one logged query. Without model research,
two further local queries compute common opponents and MMA paths when the budget
allows. With research enabled, 14 optional tool attempts remain at the default
query budget, subject to the model-request cap. Even a one-query budget builds that
packet and requires code validation. Opponent-quality and style operations for the
supplied fighters reuse that packet so research and review use the same tier and
style definitions; cached reads still consume tool attempts. Other helpers run
one parameterized query and read complete eligible histories
for accurate aggregates; the row cap applies to arbitrary `run_sql`, which flags
truncation. `queries_run` includes SQL, purpose, row count, parameters, and errors.
Schema discovery and invalid arguments consume attempts; bootstrap reads are
outside the agent budget. See [calculation notes](SCHEMA_NOTES.md#agent-calculations-and-access)
for MMA weights, historical opposition tiers, style percentiles, and source limits.

With `--json`, output includes prediction fields and a plain-English
`summary`. This abbreviated example illustrates the shape only; its probability
and findings are not a validated model forecast:

```json
{
  "fighter_a": "Jon Jones",
  "fighter_b": "Daniel Cormier",
  "as_of_date": "2015-01-03",
  "predicted_winner": "Jon Jones",
  "win_probability": 0.6,
  "predicted_method": "Decision",
  "predicted_round": 5,
  "scheduled_rounds": 5,
  "key_factors": ["Example finding"],
  "dimension_findings": {
    "form": "Example", "stats": "Example", "opposition": "Example",
    "common_opponents": "Example", "mma_math": "Example", "style": "Example",
    "activity_and_age": "Example"
  },
  "caveats": ["Illustrative output; confidence is not calibrated."],
  "queries_run": [{
    "sql": "SELECT COUNT(*) AS fights FROM fights",
    "purpose": "Count eligible bouts", "rows_returned": 1, "truncated": false
  }],
  "summary": "Short evidence-based explanation from the model."
}
```

Run `python -m pytest tests/test_sql_agent.py tests/test_upcoming.py tests/test_weekly.py -q` for the toy graph, write guards,
timeouts, budget/retry behavior, metric denominators, reviewer rejection/revision,
publication gating, opponent-strength replay and a metamorphic test proving
all helpers are unchanged when target-day/future bouts and future career totals
are added. It also runs the full graph on Jones/Cormier before UFC 182 using the
real DB with a scripted model, and verifies the DB hash is unchanged. A separate
paid live-model test runs when `RUN_LIVE_SQL_AGENT=1` and `OPENAI_API_KEY` are set.
Graph/structured-output interfaces follow the official
[LangGraph graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
and [ChatOpenAI integration](https://docs.langchain.com/oss/python/integrations/chat/openai).

### Post-card evaluation

After the results are posted, run:

```powershell
# Fast path: scrape results for saved card reports and grade them. No OpenAI calls.
.\scripts\post_card.ps1

# Or update the historical database/schedule, validate it, then grade saved reports.
.\scripts\update_ufc_data.ps1 -PostCard

# Select one report; repeat --report to compare specific runs.
.\.venv\Scripts\python.exe -m pipeline.post_card --report predictions\allen-duncan-2026-10-10.txt

# A single-fight test has no event identity: explicitly bind it to the correct card.
.\scripts\post_card.ps1 -Report predictions\your-single-fight-report.txt -Event "http://www.ufcstats.com/event-details/7f98d9d5a10fa25c"
```

The fast path reads the event and fight pages directly. The `-PostCard` update
option grades only after the ordinary staging/validation/promotion succeeds.
Post-card scoring never invokes the prediction agent or changes its original pick,
confidence, or justification. It appends a clearly marked results section to each
text report and replaces that section on subsequent runs. Byte-identical originals,
including existing handwritten annotations and JSON/ledger sidecars, are kept in
`predictions/archive/<original-SHA256>/`.

Every fight shows RIGHT/WRONG for the winner and the actual method, round and time.
Method and round are also scored independently; **exact** requires the winner,
method and round all to match. Decision subtypes map to Decision. Draws, no contests,
unpublished results, and missing/replaced pairings stay unscored, and failed model
attempts remain visible in coverage counts. Matching requires the event plus exact
normalized fighter names (order independent); conflicting fight IDs require review.
Handwritten RIGHT/WRONG notes are never used as source results.

Results and comparisons are saved under:

```text
predictions/evaluations/<UFCStats-event-ID>/comparison.txt
predictions/evaluations/<UFCStats-event-ID>/*.evaluation.json
predictions/evaluations/<UFCStats-event-ID>/sources/<snapshot-hash>.json
predictions/evaluations/weekly_metrics.csv
predictions/evaluations/index.json
```

The weekly CSV has one row per original report, so repeated grading updates that
row. Multiple runs of the same card remain separate; do not count them as independent
fights for calibration. Brier score measures error in the predicted winner's
probability: lower is better; a 50/50 prediction scores 0.25. Ten fights cannot
establish calibration. Retain forecasts before bouts start and assess many cards
chronologically before fitting a calibration mapping on a separate validation set.
Recorded timestamps alone do not prove that live-card predictions preceded a bout.

To compare the updated system with an old card report, explicitly request a paid
retrospective replay, then grade both saved reports:

```powershell
.\.venv\Scripts\python.exe -m sql_agent.replay predictions\allen-duncan-2026-10-10.txt
.\scripts\post_card.ps1
```

Replay uses the same matchups, scheduled distances and evidence cutoff as the
baseline. It records the baseline/database/prompt hashes and full model audit,
uses only pre-cutoff SQL evidence, and passes neither old picks nor actual results
to the agent. Scheduled distances and unambiguous fighter identities must be saved;
missing information is not filled from actual endings. `--workers 1` is the default;
`--workers 2` or `3` permits concurrent requests and `--timeout 30` controls the SQL
query time limit. Failed/rejected predictions are saved, without selecting a better
attempt using known outcomes.

Every replay is labeled **retrospective**, since the event is already known publicly
and model prior knowledge cannot be ruled out. Its paired comparison shows only
shared scored bouts, with coverage displayed separately. It cannot establish a
prospective improvement or be mixed into prospective confidence calibration.
Future normal terminal runs also save an incremental `.ledger.jsonl` containing
full-precision probabilities and validated predictions; JSON does not appear in the
terminal unless requested. Keep the ledger with its corresponding text report.

For reproducible offline regrading, use
`python -m pipeline.post_card --report <report.txt> --results-file <saved-source-snapshot.json>`.
Re-scraping can update an overturned result while retaining each source snapshot
and the original forecast. An outcome score does not by itself establish whether
the forecast's tactical explanation was correct.

To explicitly run a paid comparison of native reasoning and self-review:

```powershell
python -m sql_agent.ablation predictions\allen-duncan-2026-10-10.txt --workers 1
# Use the manifest path printed above; this assessment makes no model calls.
python -m sql_agent.assess_ablation <run.experiment.json> --results-file <saved-source-snapshot.json>
```

The three arms are no reasoning/no review, medium reasoning/no review, and medium
reasoning plus review. The latter two share the exact initial draft, with token
usage frozen before review; that draft is not billed twice. All arms keep the
original matchups and strict cutoff. This explicit paid experiment enables research
with a 24-request cap and 12000 generated tokens per request. Its recorded,
cost-first decision policy requires full coverage, at least
two additional correct winners, lower Brier score and no more than twice the
comparator's cost. This is a practical retention rule, not a significance test.
Checkpoints preserve finished forecasts if a run is interrupted. Assessment
separates billing errors, interrupted fights and reviewer rejections, and shows
matched-fight accuracy rather than treating missing forecasts as losses.

The repaired Allen/Duncan experiment stopped when the API reported exhausted
credits. In the four completed review cycles, initial drafts got 3/4 winners;
review published one wrong pick and withheld three. Estimated cost rose from
$0.28 to $1.46 for those four cycles. Across the four fights both reasoning modes
finished, each got 2/4 winners; medium reasoning happened to cost less under the
observed caching. This incomplete retrospective sample supports removing
mandatory review, but does not establish which reasoning level is best or
cheapest. Its cost-first recommendation remains an archived finding. Current
defaults instead retain medium reasoning and exploratory research in response to
the preference for preserving capability; this is not a claim of measured
accuracy improvement. Self-review remains off. The old report and every completed forecast remain preserved;
the decision report is in `predictions/evaluations/experiments/`.

For betting evaluation, archive predictions before bouts start and record the
odds actually available at prediction time. Current confidence is uncalibrated,
and these retrospective results establish neither a betting edge nor profitability.
Compare calibration and returns against those recorded prices on held-out cards
before treating model confidence as a measured advantage. The current report
pipeline grades fight predictions; it does not yet evaluate bets against odds.

Run `python -m pytest tests/test_post_card.py -q` for report preservation, grading,
event/participant matching, draw/no-contest handling, repeated runs, source corrections,
ledger integrity and retrospective replay isolation.

Optional YouTube analysis coverage is available through
`fight_coverage.get_fight_coverage(fight, event_date)` and is connected to both
single-fight and card predictions. The seven approved channels are registered in
`config/sentiment_sources.yaml`; retrieval settings remain in
`config/youtube_coverage.json`. Setup, actual MCP inspection status, cache policy,
permissions and commands are documented in [YOUTUBE_MCP_NOTES.md](YOUTUBE_MCP_NOTES.md).
Install `requirements-youtube.txt`, add a separate `YOUTUBE_API_KEY` to `.env`, and
configure authorized transcript access before enabling coverage. Whisper stays
off unless explicitly enabled. Reports contain source links and caveats, never
raw transcript payloads. No live YouTube or additional paid model calls are required
by the offline tests: `python -m pytest tests/test_fight_coverage.py -q`.

Discovery includes full-card prediction videos whose titles name only the event or
main event, plus generic recent card previews that require transcript confirmation.
Description chapters select the requested bout when available; otherwise nearby
mentions of both fighters select relevant captions. Each video's transcript is
cached once and reused across the card. Card runs already provide event context;
single-fight runs accept `--event-name` for the same full-card matching. Coverage
preview also accepts repeated `--event-alias` values for verified alternate titles.
Use preview's `--refresh-discovery` for a day-before check of newly uploaded card
videos without waiting for metadata cache expiry or refetching successful transcripts.
This project's `event_timezone` is `America/New_York`: date-only source cutoffs use
local midnight, so late Friday uploads remain eligible for Saturday cards. Collect
before that midnight; explicit timestamp cutoffs are available in coverage preview.

Analyst sentiment is extracted separately from SQL analysis. When source passages
are available, one batched model request extracts technical signals and explicitly
stated picks. This request uses the existing model budget and is recorded under
the `sentiment` phase. With no spare request budget, it is skipped and reported.
Technical breakdowns retain useful observations with `pick: null`; an advantage
in one skill is not converted into a prediction. Reports identify each source's
type and credibility weight. Weights start at 1.0 and are editable priors, not
calibrated accuracy. Duplicate videos do not multiply a source's weight, and
abstentions/conflicting picks are excluded from the analyst pick summary.

The registry contains MMA Guru, White Piece MMA, Horsebeef MMA, Robin Black, Luke
Thomas, Lucas Tracy MMA and MMA Che. All seven IDs were verified against YouTube
handle-page titles and canonical channel links on 2026-10-11. `active: false`
prevents any discovery for that source. The additional-source template is commented
out and inactive. If an older private JSON config contains `channels`, move those
entries into a YAML registry and set `sources_path`; JSON channel registries are
rejected so there cannot be two competing lists.

Optional written and Reddit adapters implement the same
`get_fight_coverage(fight, event_date)` contract and are **off by default**. Install
their dependencies with `python -m pip install -r requirements-sentiment.txt`.
Configure them under `non_youtube` in `config/youtube_coverage.json`:

- Written picks: enable `written_enabled` and an approved outlet only when its
  access terms allow this use. Provide explicit article URLs, allowed hostnames,
  a reviewed `terms_url`, and `automated_access_allowed: true` only for actual
  permission. Configure `entry_selector` for each writer's section,
  `writer_selector`, `reasoning_selector` and `pick_selector` against that site's
  current HTML. Each section must have one identifiable writer, relevant reasoning
  and a pre-cutoff publication time. Defaults deliberately require distinct
  `.staff-pick`/`.expert-pick` sections; unknown layouts are skipped. Multi-writer
  sections are not assigned to the first byline. `writer_weights` maps each writer's
  exact name to their own weight (default 1.0), with a stable outlet/writer source ID.
- Reddit: enable `reddit.enabled` only with approved API access and terms covering
  the use. Put its token in `.env` as `REDDIT_ACCESS_TOKEN`. Supply explicit OAuth
  thread URLs such as `https://oauth.reddit.com/r/MMA/comments/THREAD_ID`; this adapter
  does not scrape public JSON endpoints or search the site. Only r/MMA and
  r/MMAbetting prediction/breakdown threads are eligible. It inspects top-level
  comments sorted by score, requires matchup-specific technical reasoning, excludes
  post-cutoff edits, deleted/bot comments and low scores, and caps accepted comments.
  Upvotes choose which comments to inspect, never become pick percentages or win
  probabilities. Writer weights default to 0.2 and cannot exceed 0.5. At most one
  comment per writer per thread is used; repeated writers retain the same source ID.

Both adapters check terms permission and `robots.txt` before content requests or
cache reuse, honor crawl delays/request rates, use a declared user agent and refuse
redirects, challenge pages, unknown robots responses and access denials. Explicit
AI crawler restrictions are also respected; changing the bot name does not bypass
them. They never use paywall cookies or alternate scraping endpoints. Excluded
sources become report caveats. Article content is privately cached for one hour.

Access checks on 2026-10-11 found these restrictions; they are rechecked at runtime
and are not permanent claims about future policies:

| Source | Current handling |
| --- | --- |
| [MMA Junkie robots](https://mmajunkie.usatoday.com/robots.txt) | Ordinary article paths disallow relevant AI agents; skipped. Sponsored-path exceptions do not authorize staff-pick scraping. |
| [MMA Fighting robots](https://www.mmafighting.com/robots.txt) | Ordinary article paths disallow ChatGPT-User in the observed rules; skipped. No bypass through another user agent. |
| [ESPN robots](https://www.espn.com/robots.txt) | GPTBot and ChatGPT-User disallowed; skipped. |
| [Reddit robots](https://www.reddit.com/robots.txt) and [Data API terms](https://redditinc.com/policies/data-api-terms) | The live public robots response disallowed all crawlers. Public-site scraping is skipped. An explicitly approved OAuth API endpoint still requires its own runtime robots/access checks. |

No live article-layout extraction or Reddit API retrieval has been claimed verified;
the adapters are tested with offline fixtures, including blocked-source behavior.
Enabling a flag does not grant site permission.

Public sentiment and odds are loaded **only by the final central node**, after the
forecast is finalized. They are never supplied to analyst extraction, SQL research,
drafting or review. Source passages mentioning crowd percentages, Tapology polls or
odds are filtered before extraction. Individual reasoned Reddit opinions remain
distinct from community-wide vote totals.

Use an authorized local snapshot with the shape in
`config/market_context.example.json`, containing exact fighter names, event date,
timestamped American odds and optional community percentages totaling 100:

```powershell
python -m sql_agent "Fighter A" "Fighter B" --as-of-date YYYY-MM-DD --market-context data_cache/market_snapshot.json
# The same snapshot may contain multiple fights for a card run.
# Alternatively set CENTRAL_MARKET_CONTEXT_PATH in .env.
```

The central node checks matchup/date identity and excludes post-cutoff or future
observations. The snapshot event date must match the analysis cutoff date; date-only
cutoffs exclude the entire event day. It displays market probabilities with the
bookmaker margin removed, community pick shares and differences from the model's
existing estimate. Positive model-versus-public differences flag potential public
underestimation for investigation. It does not average correlated public and market
signals, change the winner/confidence, size bets or establish a measured edge.
No automated Tapology scraper is installed. The example file is synthetic and is
never loaded automatically.

Run `python -m pytest tests/test_sentiment.py tests/test_fight_coverage.py tests/test_sql_agent.py -q`
for source registration, null picks, writer attribution, robots rules and central-node
isolation tests. No live model calls are made by these tests.

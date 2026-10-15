# Actual database schema

Inspected `ufc_fights.db` read-only on 2026-10-10. This is the canonical database; the old prototype used `ufc_fights_fresh.db`.

788 events, 8,885 fights, 2,757 fighters, 41,740 fighter-round rows, and 17,770 fighter-perspective history rows. Event dates span 1994-03-11 through 2026-09-12.

## Relationships and query semantics

- `events.event_id` is the event key. `fights.event_id` references it.
- `fighters.fighter_id` is a UFCStats identity; names are not unique. The A/B and winner IDs in `fights` reference it.
- `fights.fight_id` is the bout key. `fight_rounds` references fights and both fighter identities; `(fight_id, round_number, fighter_id)` is unique.
- `fighter_bout_history` is a view with two rows per bout, normalizing A/B slots. Its sequence fields are computed window functions, not stored primary keys.
- `event_date` belongs to `events` and is exposed by the history view. Fight time is elapsed time within `ending_round`, not total fight duration. Parse `time_format` for earlier rounds (early events include non-five-minute rounds and overtime).
- `fights.fighter_a_*` / `fighter_b_*` mix pre-fight records/profiles with completed-bout statistics. The history view distinguishes `prefight_*` from `bout_*` and `opponent_bout_*`.

## Available evidence and limits

- Results, winner ID, method/detail, ending round/time, event dates, per-bout totals, and per-round statistics are present.
- Significant strikes (including target/position splits), total strikes, takedowns, knockdowns, submissions attempted, reversals, and control seconds are present. Older detailed splits may be NULL. `stats_available = 0` means missing statistics, not zero.
- Fighter profiles include date of birth, height, reach, and stance. Profiles are latest snapshots, not versioned historical observations; imputed pre-fight profiles have explicit flags. DOB-derived age can be computed, but historical availability of that profile cannot be proved.
- No official rankings, stored Elo, championship status, odds, injury history, counter-strike timing, or explicit submission-defense statistic exists. The agent derives opponent-strength estimates from prior dataset results. Counter-striker and submission-defense claims cannot be directly measured. The original inspected DB had no upcoming schedule; the scraper now creates the separate tables described below.
- Records cover this UFCStats dataset, not complete professional MMA careers. Current `fighters` records/totals, streaks, and last-fight dates include future bouts for a historical target and must never be used as historical features.
- No-contests and draws exist and are excluded from winner-to-loser edges. KO/TKO and Submission are finishes; DQ/Overturned/Other are not treated as finishes.
- Strict historical cutoff means `event_date < as_of_date`, excluding the entire target day (including earlier tournament bouts that day).
- Stored results may have been corrected/overturned later. Without versioned source snapshots, filtering event dates cannot reconstruct exactly what was published at that historical time.

## Agent calculations and access

- The agent opens the source with `mode=ro` in one consistent transaction. It
  copies only events, fights, and rounds strictly before the cutoff into private
  memory, recreates the existing history view there, and closes the source.
  `fighters` exposes identity and DOB only. The query connection has no attached
  source, rejects writes through both an authorizer and `query_only`, and blocks
  SQLite metadata, PRAGMAs, and unapproved functions. Memory use scales with the
  eligible dataset; source setup has a deadline of at least 30 seconds.
- Every raw SQL/helper query attempt is logged with purpose, row count, parameters,
  truncation, and errors. Schema discovery is cached. Tool attempts (including
  invalid arguments and schema discovery) share the configurable cap of 15;
  source bootstrap reads are outside this agent budget. Trusted helper queries
  read complete histories; arbitrary SQL returns at most 200 rows by default.
- Rates use only rows with available stats, the relevant non-null numerator,
  and a valid duration. Accuracy/defense use paired landed/attempted counts;
  zero attempts produce NULL. Duration uses scheduled earlier-round lengths
  from `time_format` plus elapsed ending-round time. Missing/invalid times
  remain unknown. Recent stats use the last 3..5 eligible bouts; trends are
  recent minus career rates, not a fitted forecasting model. Finish rate is
  KO/TKO or submission wins divided by wins. Win streak ends at any non-win,
  including draws and no-contests.
- MMA math enumerates all simple fighter paths in both directions through a
  bout multigraph. Rematches are separate edges; repeated fighters within one
  path are excluded. Default depth is 3 (supported range 1..5). Counts and
  weighted sums cover all paths; only the five strongest examples are returned
  by default. Weight is `product(2^(-age_days/1095.75) * finish_bonus) / edges^2`,
  with a 1.1 KO/submission bonus. Search timeout returns an error, never partial
  counts. This is labeled a weak signal; prediction weighting is prompt-owned.
- The legacy Python `MatchupTools.get_opponent_quality` helper uses `(W + 0.5*D + 1)/(W+L+D+2)` from the dataset record
  strictly before each historical fight date. No prior W/L/D means unknown
  quality. Contemporaneous roster one-third/two-thirds percentiles define
  low/mid/top tiers. Scores exactly on the boundaries remain mid; a tied
  distribution cannot distinguish tiers. Average quality excludes unknowns;
  best win uses the opponent's quality at the time, not their final record.
- The legacy Python `MatchupTools.get_style_matchup` helper uses midrank percentiles across fighters with eligible bouts at
  the target cutoff, excluding missing values separately for each metric. The
  upper quartile in strike output, takedowns, or submission attempts yields
  the corresponding volume/wrestler/submission tag. At least median and positive
  values in all three yields well-rounded. There is no sample-size minimum;
  small samples are reported. Counter-strike timing and direct submission
  defense remain unavailable, with explicit caveats and NULL values.
- Helper keywords configure depth, recency half-life, finish bonus, tier
  percentiles, and style threshold. These calculations are statistical proxies,
  not rankings or calibrated win probabilities. The model's confidence is
  validated to [0.5, 1.0] but has not been empirically calibrated.

### Mandatory evidence and final evaluation

`sql_agent/evidence.py` builds the following evidence for every prediction before
optional research, using one logged full-history query within the query budget:

The graph's `get_opponent_quality` and `get_style_matchup` operations reuse this
packet for the supplied fighters rather than the legacy helpers' definitions.
Cached reads count as tool attempts without adding fictitious SQL log entries.
For other fighters these operations build the same evidence from one full-history
query. This keeps strength and style meanings consistent throughout an analysis.

- Derived Elo starts at 1500, uses K=32 and a 400-point logistic scale, updates
  wins/losses/draws, ignores no-contests, and gives no finish bonus. Both fighter
  perspectives use ratings before their entire event date; updates apply after
  the date batch. Strength therefore reflects opponents' prior opposition as
  well as their records. It is a heuristic, not an official ranking or title.
- Each target fighter's historical opponent receives its prior record, Elo,
  relative strength tier and statistical style profile as of before that bout.
  Profiles cannot use that bout, other bouts on the same date, or later results.
  All target-fight evidence likewise excludes the target date and future dates.
- Peer cohorts use fighters active within 1,096 days and prefer the last known
  weight class when at least eight active peers are available. Otherwise an
  explicitly labeled all-division cohort is used. Elo tiers use midrank thirds
  among peers with at least three prior W/L/D results; less-experienced opponents
  remain provisional with unknown tier. Ties stay at their midrank.
- Historical style profiles need at least three prior bouts with available
  statistics and valid durations. Offensive rates at or above the 75th percentile
  yield high-volume striker, wrestler, or submission-threat proxies. Missing
  metrics remain unknown. Style buckets report actual results, performance and
  bout IDs; they do not establish film-based technique or submission defense.
- Comparable opponents use one minus mean absolute percentile difference across
  at least three available strike/defense/takedown/submission-rate metrics.
  Similarity of at least 0.75 selects analogues, with the best five returned.
  This threshold is a heuristic rather than a trained style or win model.
- Both fighters also receive career/recent metrics, strength-tier performance,
  strongest wins/losses, age and activity. Replay has a deadline of at least 15
  seconds; an incomplete replay fails instead of supplying partial evidence.

The runtime prompts require separate opposition and historical-style assessments
for both fighters, with eligible supporting bout IDs, counterevidence and a
confidence explanation. Code always validates schema, winner identity, scheduled
round constraints and citation eligibility; the SQL workspace enforces the cutoff.
Normal publication requires `evaluation.mode=deterministic`, `status=validated`
and all five code checks. These checks do not certify prose factual support.

Optional `--review on` adds a fresh model context with the draft, mandatory packet
and supplemental observations. In that mode, all six model checks must pass and
no blocking issues may remain before publication. Source values are checked
against the same named evidence view supplied to the reviewer. Exact strings,
legacy JSON-quoted strings, and empty named collections are supported; wrong
values or unavailable source paths are rejected. Whether a fact supports prose
remains a model judgment. Up to two correction passes are allowed; unresolved
review failures withhold the forecast. JSON retains evidence and review verdicts.
Paid review remains off after the coverage/cost assessment. Current defaults
retain medium reasoning and exploratory SQL research to prioritize capability;
neither code validation nor review establishes forecasting accuracy.

Default evidence uses ordinary named JSON fields and retains every eligible bout
and exact value. Repeated same-matchup opponent/style tool requests return source
paths into the original packet, which remains in context. Synthesis references
its style fields rather than duplicating them. Third-party evidence is returned
in full because it is not already in the mandatory packet.

The default 24-request cap reserves six requests for drafting/repairs, and the
output ceiling is 12000 tokens including native reasoning. Research mode can
retry a transient provider failure once; both attempts count toward the cap and
usage audit. SDK retries are disabled and exhausted credit errors stop calls.
`--research off` still offers optional compact direct drafting, with local common
opponent/path calculations and lossless shared-field encoding. No historical
examples are selected or dropped in either mode. Compression round-trip tests
do not establish unchanged model interpretation; prospective evaluation remains
necessary to measure predictive quality.

## Upcoming schedule extension

The next scraper run adds these tables through the existing SQLAlchemy setup in
staging. They are separate from the inspected completed-history schema below.

| Table | Columns and types | Keys |
|---|---|---|
| `upcoming_events` | `event_id`, `event_name`, `event_location`, `event_url` VARCHAR; `event_date` DATE; `source_fight_count` INTEGER; `last_scraped_at` DATETIME | PK `event_id` (UFCStats URL ID); unique `event_url` |
| `upcoming_fights` | `fight_id`, `event_id`, `fighter_a_id`, `fighter_a_name`, `fighter_b_id`, `fighter_b_name`, `weight_class`, `source_fight_url` VARCHAR; `card_order`, `scheduled_rounds` INTEGER; `is_title_bout` BOOLEAN | PK `fight_id`; FK `event_id` -> `upcoming_events.event_id` with cascade; unique `(event_id, card_order)` |

`event_location`, `weight_class`, and `scheduled_rounds` are nullable. All other
columns are required. Fighter IDs intentionally do not reference the historical
`fighters` table: announced debutants need not have a completed fight. Only their
identities are made available to the agent, without importing schedule rows into
its historical SQL workspace.

The observed upcoming event page exposes ordered pairings, fighter IDs/names,
weight classes, and title-bout belt markers; no scheduled round count is shown.
The database preserves that missing value as NULL. Prediction mode discloses its
three/five-round assumption (main event/title versus other bouts), and single
fight callers can supply the scheduled distance explicitly. No current matchup
page career totals are imported as historical features.

Every successful scrape replaces the next-card snapshot, removing canceled or
replaced pairings. A malformed or failed scrape rolls back; the weekly gate
prevents promotion. The schedule has a fetch timestamp but no announcement-version
archive, so historical card reconstruction is not supported. The reader excludes
past and completed cards and flags schedules older than 48 hours. Same-day start
times are unavailable, so same-day output includes an explicit caveat.

## Full column inventory

Types below are SQLite declared types. `(expression)` means SQLite exposes no declared type for a computed view column. View nullability is introspection metadata, not a guarantee.

### `events` (table)

| Column | Declared type | Required | Primary key |
|---|---|---|---|
| `event_id` | VARCHAR | yes | 1 |
| `event_name` | VARCHAR | yes |  |
| `event_date` | DATE | yes |  |
| `event_location` | VARCHAR | no |  |
| `event_url` | VARCHAR | no |  |
| `created_at` | DATETIME | yes |  |
| `updated_at` | DATETIME | yes |  |
| `source_fight_count` | INTEGER | no |  |
| `last_scraped_at` | DATETIME | no |  |

Foreign keys:
- None.

Indexes:
- `ix_events_event_id` (event_id); unique=False.
- `ix_events_event_date` (event_date); unique=False.
- `ix_events_event_name` (event_name); unique=False.
- `sqlite_autoindex_events_1` (event_id); unique=True.

### `fight_rounds` (table)

| Column | Declared type | Required | Primary key |
|---|---|---|---|
| `round_stat_id` | INTEGER | yes | 1 |
| `fight_id` | VARCHAR | yes |  |
| `round_number` | INTEGER | yes |  |
| `fighter_id` | VARCHAR | yes |  |
| `opponent_id` | VARCHAR | yes |  |
| `knockdowns` | INTEGER | yes |  |
| `significant_strikes_landed` | INTEGER | yes |  |
| `significant_strikes_attempted` | INTEGER | yes |  |
| `total_strikes_landed` | INTEGER | yes |  |
| `total_strikes_attempted` | INTEGER | yes |  |
| `takedowns_landed` | INTEGER | yes |  |
| `takedowns_attempted` | INTEGER | yes |  |
| `submission_attempts` | INTEGER | yes |  |
| `reversals` | INTEGER | yes |  |
| `control_time_seconds` | INTEGER | no |  |
| `significant_head_strikes_landed` | INTEGER | no |  |
| `significant_head_strikes_attempted` | INTEGER | no |  |
| `significant_body_strikes_landed` | INTEGER | no |  |
| `significant_body_strikes_attempted` | INTEGER | no |  |
| `significant_leg_strikes_landed` | INTEGER | no |  |
| `significant_leg_strikes_attempted` | INTEGER | no |  |
| `significant_distance_strikes_landed` | INTEGER | no |  |
| `significant_distance_strikes_attempted` | INTEGER | no |  |
| `significant_clinch_strikes_landed` | INTEGER | no |  |
| `significant_clinch_strikes_attempted` | INTEGER | no |  |
| `significant_ground_strikes_landed` | INTEGER | no |  |
| `significant_ground_strikes_attempted` | INTEGER | no |  |

Foreign keys:
- `opponent_id` -> `fighters.fighter_id` (delete: NO ACTION).
- `fighter_id` -> `fighters.fighter_id` (delete: NO ACTION).
- `fight_id` -> `fights.fight_id` (delete: CASCADE).

Indexes:
- `ix_fight_rounds_fight_id` (fight_id); unique=False.
- `ix_fight_rounds_fighter_id` (fighter_id); unique=False.
- `sqlite_autoindex_fight_rounds_1` (fight_id, round_number, fighter_id); unique=True.

### `fighter_bout_history` (view)

| Column | Declared type | Required | Primary key |
|---|---|---|---|
| `fighter_bout_sequence` | (expression) | no |  |
| `fight_id` | VARCHAR | no |  |
| `event_id` | VARCHAR | no |  |
| `event_name` | VARCHAR | no |  |
| `event_date` | DATE | no |  |
| `event_location` | VARCHAR | no |  |
| `event_bout_sequence` | (expression) | no |  |
| `source_card_order` | INTEGER | no |  |
| `weight_class` | VARCHAR | no |  |
| `fighter_slot` | (expression) | no |  |
| `fighter_id` | VARCHAR | no |  |
| `fighter_name` | VARCHAR | no |  |
| `opponent_id` | VARCHAR | no |  |
| `opponent_name` | VARCHAR | no |  |
| `prefight_age` | INTEGER | no |  |
| `prefight_height_inches` | INTEGER | no |  |
| `prefight_reach_inches` | INTEGER | no |  |
| `prefight_stance` | VARCHAR | no |  |
| `prefight_profile_imputed` | INTEGER | no |  |
| `prefight_wins` | INTEGER | no |  |
| `prefight_losses` | INTEGER | no |  |
| `prefight_draws` | INTEGER | no |  |
| `prefight_current_win_streak` | INTEGER | no |  |
| `prefight_current_loss_streak` | INTEGER | no |  |
| `prefight_days_since_last_fight` | INTEGER | no |  |
| `opponent_prefight_age` | INTEGER | no |  |
| `opponent_prefight_height_inches` | INTEGER | no |  |
| `opponent_prefight_reach_inches` | INTEGER | no |  |
| `opponent_prefight_stance` | VARCHAR | no |  |
| `opponent_prefight_profile_imputed` | INTEGER | no |  |
| `opponent_prefight_wins` | INTEGER | no |  |
| `opponent_prefight_losses` | INTEGER | no |  |
| `opponent_prefight_draws` | INTEGER | no |  |
| `opponent_prefight_current_win_streak` | INTEGER | no |  |
| `opponent_prefight_current_loss_streak` | INTEGER | no |  |
| `opponent_prefight_days_since_last_fight` | INTEGER | no |  |
| `prefight_head_to_head_fight_count` | INTEGER | no |  |
| `bout_outcome` | (expression) | no |  |
| `bout_result` | VARCHAR | no |  |
| `bout_winner_fighter_id` | VARCHAR | no |  |
| `bout_winner_name` | VARCHAR | no |  |
| `bout_method_of_victory` | VARCHAR | no |  |
| `bout_method_detail` | VARCHAR | no |  |
| `bout_event_method_code` | VARCHAR | no |  |
| `bout_referee` | VARCHAR | no |  |
| `bout_time_format` | VARCHAR | no |  |
| `bout_ending_round` | INTEGER | no |  |
| `bout_ending_time` | VARCHAR | no |  |
| `bout_stats_available` | BOOLEAN | no |  |
| `bout_knockdowns` | INTEGER | no |  |
| `bout_significant_strikes_landed` | INTEGER | no |  |
| `bout_significant_strikes_attempted` | INTEGER | no |  |
| `bout_total_strikes_landed` | INTEGER | no |  |
| `bout_total_strikes_attempted` | INTEGER | no |  |
| `bout_takedowns_landed` | INTEGER | no |  |
| `bout_takedowns_attempted` | INTEGER | no |  |
| `bout_submission_attempts` | INTEGER | no |  |
| `bout_reversals` | INTEGER | no |  |
| `bout_control_time_seconds` | INTEGER | no |  |
| `bout_significant_head_strikes_landed` | INTEGER | no |  |
| `bout_significant_head_strikes_attempted` | INTEGER | no |  |
| `bout_significant_body_strikes_landed` | INTEGER | no |  |
| `bout_significant_body_strikes_attempted` | INTEGER | no |  |
| `bout_significant_leg_strikes_landed` | INTEGER | no |  |
| `bout_significant_leg_strikes_attempted` | INTEGER | no |  |
| `bout_significant_distance_strikes_landed` | INTEGER | no |  |
| `bout_significant_distance_strikes_attempted` | INTEGER | no |  |
| `bout_significant_clinch_strikes_landed` | INTEGER | no |  |
| `bout_significant_clinch_strikes_attempted` | INTEGER | no |  |
| `bout_significant_ground_strikes_landed` | INTEGER | no |  |
| `bout_significant_ground_strikes_attempted` | INTEGER | no |  |
| `opponent_bout_knockdowns` | INTEGER | no |  |
| `opponent_bout_significant_strikes_landed` | INTEGER | no |  |
| `opponent_bout_significant_strikes_attempted` | INTEGER | no |  |
| `opponent_bout_total_strikes_landed` | INTEGER | no |  |
| `opponent_bout_total_strikes_attempted` | INTEGER | no |  |
| `opponent_bout_takedowns_landed` | INTEGER | no |  |
| `opponent_bout_takedowns_attempted` | INTEGER | no |  |
| `opponent_bout_submission_attempts` | INTEGER | no |  |
| `opponent_bout_reversals` | INTEGER | no |  |
| `opponent_bout_control_time_seconds` | INTEGER | no |  |
| `opponent_bout_significant_head_strikes_landed` | INTEGER | no |  |
| `opponent_bout_significant_head_strikes_attempted` | INTEGER | no |  |
| `opponent_bout_significant_body_strikes_landed` | INTEGER | no |  |
| `opponent_bout_significant_body_strikes_attempted` | INTEGER | no |  |
| `opponent_bout_significant_leg_strikes_landed` | INTEGER | no |  |
| `opponent_bout_significant_leg_strikes_attempted` | INTEGER | no |  |
| `opponent_bout_significant_distance_strikes_landed` | INTEGER | no |  |
| `opponent_bout_significant_distance_strikes_attempted` | INTEGER | no |  |
| `opponent_bout_significant_clinch_strikes_landed` | INTEGER | no |  |
| `opponent_bout_significant_clinch_strikes_attempted` | INTEGER | no |  |
| `opponent_bout_significant_ground_strikes_landed` | INTEGER | no |  |
| `opponent_bout_significant_ground_strikes_attempted` | INTEGER | no |  |
| `source_event_url` | VARCHAR | no |  |
| `source_fight_url` | VARCHAR | no |  |
| `bout_last_scraped_at` | DATETIME | no |  |

Foreign keys:
- None.

Indexes:
- None.

### `fighters` (table)

| Column | Declared type | Required | Primary key |
|---|---|---|---|
| `fighter_id` | VARCHAR | yes | 1 |
| `fighter_name` | VARCHAR | yes |  |
| `fighter_nickname` | VARCHAR | no |  |
| `date_of_birth` | DATE | no |  |
| `height_inches` | INTEGER | no |  |
| `reach_inches` | INTEGER | no |  |
| `stance` | VARCHAR | no |  |
| `profile_url` | VARCHAR | no |  |
| `fights` | INTEGER | yes |  |
| `wins` | INTEGER | yes |  |
| `losses` | INTEGER | yes |  |
| `draws` | INTEGER | yes |  |
| `no_contests` | INTEGER | yes |  |
| `current_win_streak` | INTEGER | yes |  |
| `current_loss_streak` | INTEGER | yes |  |
| `significant_strikes_landed` | INTEGER | yes |  |
| `significant_strikes_attempted` | INTEGER | yes |  |
| `takedowns_landed` | INTEGER | yes |  |
| `takedowns_attempted` | INTEGER | yes |  |
| `control_time_seconds` | INTEGER | yes |  |
| `last_fight_date` | DATE | no |  |
| `created_at` | DATETIME | yes |  |
| `updated_at` | DATETIME | yes |  |
| `profile_scraped_at` | DATETIME | no |  |

Foreign keys:
- None.

Indexes:
- `ix_fighters_last_fight_date` (last_fight_date); unique=False.
- `ix_fighters_fighter_id` (fighter_id); unique=False.
- `ix_fighters_fighter_name` (fighter_name); unique=False.
- `sqlite_autoindex_fighters_1` (fighter_id); unique=True.

### `fights` (table)

| Column | Declared type | Required | Primary key |
|---|---|---|---|
| `fight_id` | VARCHAR | yes | 1 |
| `event_id` | VARCHAR | yes |  |
| `fighter_a_id` | VARCHAR | yes |  |
| `fighter_a_name` | VARCHAR | yes |  |
| `fighter_a_age` | INTEGER | no |  |
| `fighter_a_height_inches` | INTEGER | no |  |
| `fighter_a_reach_inches` | INTEGER | no |  |
| `fighter_a_stance` | VARCHAR | no |  |
| `fighter_a_wins` | INTEGER | no |  |
| `fighter_a_losses` | INTEGER | no |  |
| `fighter_a_draws` | INTEGER | no |  |
| `fighter_a_current_win_streak` | INTEGER | no |  |
| `fighter_a_current_loss_streak` | INTEGER | no |  |
| `fighter_a_significant_strikes_landed` | INTEGER | no |  |
| `fighter_a_significant_strikes_attempted` | INTEGER | no |  |
| `fighter_a_takedowns_landed` | INTEGER | no |  |
| `fighter_a_takedowns_attempted` | INTEGER | no |  |
| `fighter_a_control_time_seconds` | INTEGER | no |  |
| `fighter_a_total_strikes_landed` | INTEGER | no |  |
| `fighter_a_total_strikes_attempted` | INTEGER | no |  |
| `fighter_a_knockdowns` | INTEGER | no |  |
| `fighter_a_submission_attempts` | INTEGER | no |  |
| `fighter_a_reversals` | INTEGER | no |  |
| `fighter_a_days_since_last_fight` | INTEGER | no |  |
| `fighter_b_id` | VARCHAR | yes |  |
| `fighter_b_name` | VARCHAR | yes |  |
| `fighter_b_age` | INTEGER | no |  |
| `fighter_b_height_inches` | INTEGER | no |  |
| `fighter_b_reach_inches` | INTEGER | no |  |
| `fighter_b_stance` | VARCHAR | no |  |
| `fighter_b_wins` | INTEGER | no |  |
| `fighter_b_losses` | INTEGER | no |  |
| `fighter_b_draws` | INTEGER | no |  |
| `fighter_b_current_win_streak` | INTEGER | no |  |
| `fighter_b_current_loss_streak` | INTEGER | no |  |
| `fighter_b_significant_strikes_landed` | INTEGER | no |  |
| `fighter_b_significant_strikes_attempted` | INTEGER | no |  |
| `fighter_b_takedowns_landed` | INTEGER | no |  |
| `fighter_b_takedowns_attempted` | INTEGER | no |  |
| `fighter_b_control_time_seconds` | INTEGER | no |  |
| `fighter_b_total_strikes_landed` | INTEGER | no |  |
| `fighter_b_total_strikes_attempted` | INTEGER | no |  |
| `fighter_b_knockdowns` | INTEGER | no |  |
| `fighter_b_submission_attempts` | INTEGER | no |  |
| `fighter_b_reversals` | INTEGER | no |  |
| `fighter_b_days_since_last_fight` | INTEGER | no |  |
| `winner_fighter_id` | VARCHAR | no |  |
| `winner_name` | VARCHAR | no |  |
| `method_of_victory` | VARCHAR | no |  |
| `method_detail` | VARCHAR | no |  |
| `result` | VARCHAR | no |  |
| `referee` | VARCHAR | no |  |
| `time_format` | VARCHAR | no |  |
| `ending_round` | INTEGER | no |  |
| `ending_time` | VARCHAR | no |  |
| `head_to_head_fight_count` | INTEGER | no |  |
| `source_event_url` | VARCHAR | no |  |
| `source_fight_url` | VARCHAR | no |  |
| `created_at` | DATETIME | yes |  |
| `updated_at` | DATETIME | yes |  |
| `weight_class` | VARCHAR | no |  |
| `card_order` | INTEGER | no |  |
| `event_method_code` | VARCHAR | no |  |
| `stats_available` | BOOLEAN | yes |  |
| `fighter_a_profile_imputed` | INTEGER | yes |  |
| `fighter_b_profile_imputed` | INTEGER | yes |  |
| `fighter_a_significant_head_strikes_landed` | INTEGER | no |  |
| `fighter_a_significant_head_strikes_attempted` | INTEGER | no |  |
| `fighter_a_significant_body_strikes_landed` | INTEGER | no |  |
| `fighter_a_significant_body_strikes_attempted` | INTEGER | no |  |
| `fighter_a_significant_leg_strikes_landed` | INTEGER | no |  |
| `fighter_a_significant_leg_strikes_attempted` | INTEGER | no |  |
| `fighter_a_significant_distance_strikes_landed` | INTEGER | no |  |
| `fighter_a_significant_distance_strikes_attempted` | INTEGER | no |  |
| `fighter_a_significant_clinch_strikes_landed` | INTEGER | no |  |
| `fighter_a_significant_clinch_strikes_attempted` | INTEGER | no |  |
| `fighter_a_significant_ground_strikes_landed` | INTEGER | no |  |
| `fighter_a_significant_ground_strikes_attempted` | INTEGER | no |  |
| `fighter_b_significant_head_strikes_landed` | INTEGER | no |  |
| `fighter_b_significant_head_strikes_attempted` | INTEGER | no |  |
| `fighter_b_significant_body_strikes_landed` | INTEGER | no |  |
| `fighter_b_significant_body_strikes_attempted` | INTEGER | no |  |
| `fighter_b_significant_leg_strikes_landed` | INTEGER | no |  |
| `fighter_b_significant_leg_strikes_attempted` | INTEGER | no |  |
| `fighter_b_significant_distance_strikes_landed` | INTEGER | no |  |
| `fighter_b_significant_distance_strikes_attempted` | INTEGER | no |  |
| `fighter_b_significant_clinch_strikes_landed` | INTEGER | no |  |
| `fighter_b_significant_clinch_strikes_attempted` | INTEGER | no |  |
| `fighter_b_significant_ground_strikes_landed` | INTEGER | no |  |
| `fighter_b_significant_ground_strikes_attempted` | INTEGER | no |  |
| `last_scraped_at` | DATETIME | no |  |

Foreign keys:
- `winner_fighter_id` -> `fighters.fighter_id` (delete: NO ACTION).
- `fighter_b_id` -> `fighters.fighter_id` (delete: NO ACTION).
- `fighter_a_id` -> `fighters.fighter_id` (delete: NO ACTION).
- `event_id` -> `events.event_id` (delete: NO ACTION).

Indexes:
- `ix_fights_weight_class` (weight_class); unique=False.
- `ix_fights_fight_id` (fight_id); unique=False.
- `ix_fights_winner_fighter_id` (winner_fighter_id); unique=False.
- `ix_fights_fighter_a_id` (fighter_a_id); unique=False.
- `ix_fights_fighter_b_name` (fighter_b_name); unique=False.
- `ix_fights_fighter_a_name` (fighter_a_name); unique=False.
- `ix_event_fighters` (event_id, fighter_a_id, fighter_b_id); unique=False.
- `ix_fights_event_id` (event_id); unique=False.
- `ix_fights_fighter_b_id` (fighter_b_id); unique=False.
- `sqlite_autoindex_fights_1` (fight_id); unique=True.

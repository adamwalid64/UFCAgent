from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


FIGHTER_BOUT_HISTORY_VIEW = "fighter_bout_history"

# These names intentionally describe when a value is knowable. The canonical
# fights table stores both participants in A/B columns; this view turns that
# into one row per fighter and bout so history queries never need side-aware
# CASE expressions. ``prefight_*`` values are safe entering-bout snapshots,
# while ``bout_*`` values describe the completed bout itself.
_PREFIGHT_FIELDS = (
    "age",
    "height_inches",
    "reach_inches",
    "stance",
    "profile_imputed",
    "wins",
    "losses",
    "draws",
    "current_win_streak",
    "current_loss_streak",
    "days_since_last_fight",
)

_BOUT_STAT_FIELDS = (
    "knockdowns",
    "significant_strikes_landed",
    "significant_strikes_attempted",
    "total_strikes_landed",
    "total_strikes_attempted",
    "takedowns_landed",
    "takedowns_attempted",
    "submission_attempts",
    "reversals",
    "control_time_seconds",
    "significant_head_strikes_landed",
    "significant_head_strikes_attempted",
    "significant_body_strikes_landed",
    "significant_body_strikes_attempted",
    "significant_leg_strikes_landed",
    "significant_leg_strikes_attempted",
    "significant_distance_strikes_landed",
    "significant_distance_strikes_attempted",
    "significant_clinch_strikes_landed",
    "significant_clinch_strikes_attempted",
    "significant_ground_strikes_landed",
    "significant_ground_strikes_attempted",
)


def _fighter_perspective_select(side: str, opponent_side: str) -> str:
    prefight = [
        f"f.fighter_{side}_{field} AS prefight_{field}"
        for field in _PREFIGHT_FIELDS
    ]
    opponent_prefight = [
        f"f.fighter_{opponent_side}_{field} AS opponent_prefight_{field}"
        for field in _PREFIGHT_FIELDS
    ]
    bout_stats = [
        f"f.fighter_{side}_{field} AS bout_{field}"
        for field in _BOUT_STAT_FIELDS
    ]
    opponent_bout_stats = [
        f"f.fighter_{opponent_side}_{field} AS opponent_bout_{field}"
        for field in _BOUT_STAT_FIELDS
    ]
    columns = [
        "f.fight_id",
        "f.event_id",
        "f.event_name",
        "f.event_date",
        "f.event_location",
        "f.event_bout_sequence",
        "f.card_order AS source_card_order",
        "f.weight_class",
        f"'{side}' AS fighter_slot",
        f"f.fighter_{side}_id AS fighter_id",
        f"f.fighter_{side}_name AS fighter_name",
        f"f.fighter_{opponent_side}_id AS opponent_id",
        f"f.fighter_{opponent_side}_name AS opponent_name",
        *prefight,
        *opponent_prefight,
        "f.head_to_head_fight_count AS prefight_head_to_head_fight_count",
        (
            "CASE "
            "WHEN lower(coalesce(f.result, '')) = 'draw' THEN 'draw' "
            "WHEN lower(coalesce(f.result, '')) IN "
            "('no_contest', 'no contest', 'nc') THEN 'no_contest' "
            f"WHEN f.winner_fighter_id = f.fighter_{side}_id THEN 'win' "
            f"WHEN f.winner_fighter_id = f.fighter_{opponent_side}_id THEN 'loss' "
            "ELSE NULL END AS bout_outcome"
        ),
        "f.result AS bout_result",
        "f.winner_fighter_id AS bout_winner_fighter_id",
        "f.winner_name AS bout_winner_name",
        "f.method_of_victory AS bout_method_of_victory",
        "f.method_detail AS bout_method_detail",
        "f.event_method_code AS bout_event_method_code",
        "f.referee AS bout_referee",
        "f.time_format AS bout_time_format",
        "f.ending_round AS bout_ending_round",
        "f.ending_time AS bout_ending_time",
        "f.stats_available AS bout_stats_available",
        *bout_stats,
        *opponent_bout_stats,
        "f.source_event_url",
        "f.source_fight_url",
        "f.last_scraped_at AS bout_last_scraped_at",
    ]
    return "SELECT\n            " + ",\n            ".join(columns) + "\n        FROM ordered_fights AS f"


FIGHTER_BOUT_HISTORY_VIEW_SQL = f"""
CREATE VIEW {FIGHTER_BOUT_HISTORY_VIEW} AS
WITH ordered_fights AS (
    SELECT
        f.*,
        e.event_name,
        e.event_date,
        e.event_location,
        row_number() OVER (
            PARTITION BY f.event_id
            ORDER BY f.card_order DESC, f.fight_id
        ) AS event_bout_sequence
    FROM fights AS f
    JOIN events AS e ON e.event_id = f.event_id
),
fighter_perspectives AS (
    {_fighter_perspective_select("a", "b")}
    UNION ALL
    {_fighter_perspective_select("b", "a")}
)
SELECT
    row_number() OVER (
        PARTITION BY p.fighter_id
        ORDER BY p.event_date, p.event_bout_sequence, p.event_id, p.fight_id
    ) AS fighter_bout_sequence,
    p.*
FROM fighter_perspectives AS p
""".strip()


# SQLite's CREATE TABLE IF NOT EXISTS does not add columns to an existing
# database. Keep the small, additive migrations here so a weekly sync can
# upgrade the prior week's file in place before ORM queries begin.
SQLITE_COLUMNS: dict[str, dict[str, str]] = {
    "fighters": {
        "profile_scraped_at": "DATETIME",
    },
    "events": {
        "source_fight_count": "INTEGER",
        "last_scraped_at": "DATETIME",
    },
    "fights": {
        "weight_class": "VARCHAR",
        "card_order": "INTEGER",
        "event_method_code": "VARCHAR",
        "stats_available": "BOOLEAN NOT NULL DEFAULT 0",
        "fighter_a_profile_imputed": "INTEGER NOT NULL DEFAULT 0",
        "fighter_b_profile_imputed": "INTEGER NOT NULL DEFAULT 0",
        "fighter_a_significant_head_strikes_landed": "INTEGER",
        "fighter_a_significant_head_strikes_attempted": "INTEGER",
        "fighter_a_significant_body_strikes_landed": "INTEGER",
        "fighter_a_significant_body_strikes_attempted": "INTEGER",
        "fighter_a_significant_leg_strikes_landed": "INTEGER",
        "fighter_a_significant_leg_strikes_attempted": "INTEGER",
        "fighter_a_significant_distance_strikes_landed": "INTEGER",
        "fighter_a_significant_distance_strikes_attempted": "INTEGER",
        "fighter_a_significant_clinch_strikes_landed": "INTEGER",
        "fighter_a_significant_clinch_strikes_attempted": "INTEGER",
        "fighter_a_significant_ground_strikes_landed": "INTEGER",
        "fighter_a_significant_ground_strikes_attempted": "INTEGER",
        "fighter_b_significant_head_strikes_landed": "INTEGER",
        "fighter_b_significant_head_strikes_attempted": "INTEGER",
        "fighter_b_significant_body_strikes_landed": "INTEGER",
        "fighter_b_significant_body_strikes_attempted": "INTEGER",
        "fighter_b_significant_leg_strikes_landed": "INTEGER",
        "fighter_b_significant_leg_strikes_attempted": "INTEGER",
        "fighter_b_significant_distance_strikes_landed": "INTEGER",
        "fighter_b_significant_distance_strikes_attempted": "INTEGER",
        "fighter_b_significant_clinch_strikes_landed": "INTEGER",
        "fighter_b_significant_clinch_strikes_attempted": "INTEGER",
        "fighter_b_significant_ground_strikes_landed": "INTEGER",
        "fighter_b_significant_ground_strikes_attempted": "INTEGER",
        "last_scraped_at": "DATETIME",
    },
    "fight_rounds": {
        "significant_head_strikes_landed": "INTEGER",
        "significant_head_strikes_attempted": "INTEGER",
        "significant_body_strikes_landed": "INTEGER",
        "significant_body_strikes_attempted": "INTEGER",
        "significant_leg_strikes_landed": "INTEGER",
        "significant_leg_strikes_attempted": "INTEGER",
        "significant_distance_strikes_landed": "INTEGER",
        "significant_distance_strikes_attempted": "INTEGER",
        "significant_clinch_strikes_landed": "INTEGER",
        "significant_clinch_strikes_attempted": "INTEGER",
        "significant_ground_strikes_landed": "INTEGER",
        "significant_ground_strikes_attempted": "INTEGER",
    },
}


def upgrade_schema(engine: Engine) -> None:
    """Apply idempotent additive migrations needed by the sync pipeline."""
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table_name, definitions in SQLITE_COLUMNS.items():
            if table_name not in tables:
                continue
            existing = {column["name"] for column in inspect(engine).get_columns(table_name)}
            for column_name, ddl in definitions.items():
                if column_name in existing:
                    continue
                connection.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {ddl}'))
                existing.add(column_name)

        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_fights_weight_class ON fights (weight_class)"))
        if {"events", "fights"}.issubset(tables):
            # Recreate the read-only query surface transactionally so future
            # additive migrations can evolve it without a separate version
            # table. No stored rows are copied or mutated by this operation.
            connection.execute(text(f'DROP VIEW IF EXISTS "{FIGHTER_BOUT_HISTORY_VIEW}"'))
            connection.execute(text(FIGHTER_BOUT_HISTORY_VIEW_SQL))


__all__ = [
    "FIGHTER_BOUT_HISTORY_VIEW",
    "FIGHTER_BOUT_HISTORY_VIEW_SQL",
    "upgrade_schema",
]

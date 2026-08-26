from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class Fighter(Base):
    """Normalized fighter dimension used to join historical performance across fights."""

    __tablename__ = "fighters"

    fighter_id = Column(String, primary_key=True, index=True)
    fighter_name = Column(String, nullable=False, index=True)
    fighter_nickname = Column(String, nullable=True)
    date_of_birth = Column(Date, nullable=True)
    height_inches = Column(Integer, nullable=True, comment="Height in inches at time of last known profile.")
    reach_inches = Column(Integer, nullable=True, comment="Reach in inches at time of fight.")
    stance = Column(String, nullable=True, comment="Fighter stance, e.g. orthodox, southpaw, switch.")
    profile_url = Column(String, nullable=True)
    fights = Column(Integer, nullable=False, default=0)
    wins = Column(Integer, nullable=False, default=0)
    losses = Column(Integer, nullable=False, default=0)
    draws = Column(Integer, nullable=False, default=0)
    no_contests = Column(Integer, nullable=False, default=0)
    current_win_streak = Column(Integer, nullable=False, default=0)
    current_loss_streak = Column(Integer, nullable=False, default=0)
    significant_strikes_landed = Column(Integer, nullable=False, default=0)
    significant_strikes_attempted = Column(Integer, nullable=False, default=0)
    takedowns_landed = Column(Integer, nullable=False, default=0)
    takedowns_attempted = Column(Integer, nullable=False, default=0)
    control_time_seconds = Column(Integer, nullable=False, default=0)
    last_fight_date = Column(Date, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Event(Base):
    """Single UFC event, keyed by a stable slug derived from the event date and name."""

    __tablename__ = "events"

    event_id = Column(String, primary_key=True, index=True)
    event_name = Column(String, nullable=False, index=True)
    event_date = Column(Date, nullable=False, index=True)
    event_location = Column(String, nullable=True)
    event_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Fight(Base):
    """One row per fight. This is the canonical table for downstream SQL queries."""

    __tablename__ = "fights"
    __table_args__ = (
        UniqueConstraint("fight_id", name="uq_fight_id"),
        Index("ix_event_fighters", "event_id", "fighter_a_id", "fighter_b_id"),
    )

    fight_id = Column(String, primary_key=True, index=True)
    event_id = Column(String, ForeignKey("events.event_id"), nullable=False, index=True)

    fighter_a_id = Column(String, ForeignKey("fighters.fighter_id"), nullable=False, index=True)
    fighter_a_name = Column(String, nullable=False, index=True)
    fighter_a_age = Column(Integer, nullable=True, comment="Age of Fighter A at the time of the fight.")
    fighter_a_height_inches = Column(Integer, nullable=True, comment="Height of Fighter A in inches.")
    fighter_a_reach_inches = Column(Integer, nullable=True, comment="Reach of Fighter A in inches.")
    fighter_a_stance = Column(String, nullable=True, comment="Stance of Fighter A.")
    fighter_a_wins = Column(Integer, nullable=True, comment="Career wins for Fighter A entering the fight.")
    fighter_a_losses = Column(Integer, nullable=True, comment="Career losses for Fighter A entering the fight.")
    fighter_a_draws = Column(Integer, nullable=True, comment="Career draws for Fighter A entering the fight.")
    fighter_a_current_win_streak = Column(Integer, nullable=True, comment="Current win streak for Fighter A entering the fight.")
    fighter_a_current_loss_streak = Column(Integer, nullable=True, comment="Current loss streak for Fighter A entering the fight.")
    fighter_a_significant_strikes_landed = Column(Integer, nullable=True, comment="Significant strikes landed by Fighter A.")
    fighter_a_significant_strikes_attempted = Column(Integer, nullable=True, comment="Significant strikes attempted by Fighter A.")
    fighter_a_takedowns_landed = Column(Integer, nullable=True, comment="Takedowns landed by Fighter A.")
    fighter_a_takedowns_attempted = Column(Integer, nullable=True, comment="Takedowns attempted by Fighter A.")
    fighter_a_control_time_seconds = Column(Integer, nullable=True, comment="Control time in seconds for Fighter A.")
    fighter_a_total_strikes_landed = Column(Integer, nullable=True)
    fighter_a_total_strikes_attempted = Column(Integer, nullable=True)
    fighter_a_knockdowns = Column(Integer, nullable=True)
    fighter_a_submission_attempts = Column(Integer, nullable=True)
    fighter_a_reversals = Column(Integer, nullable=True)
    fighter_a_days_since_last_fight = Column(Integer, nullable=True, comment="Days since Fighter A's last fight before this bout.")

    fighter_b_id = Column(String, ForeignKey("fighters.fighter_id"), nullable=False, index=True)
    fighter_b_name = Column(String, nullable=False, index=True)
    fighter_b_age = Column(Integer, nullable=True, comment="Age of Fighter B at the time of the fight.")
    fighter_b_height_inches = Column(Integer, nullable=True, comment="Height of Fighter B in inches.")
    fighter_b_reach_inches = Column(Integer, nullable=True, comment="Reach of Fighter B in inches.")
    fighter_b_stance = Column(String, nullable=True, comment="Stance of Fighter B.")
    fighter_b_wins = Column(Integer, nullable=True, comment="Career wins for Fighter B entering the fight.")
    fighter_b_losses = Column(Integer, nullable=True, comment="Career losses for Fighter B entering the fight.")
    fighter_b_draws = Column(Integer, nullable=True, comment="Career draws for Fighter B entering the fight.")
    fighter_b_current_win_streak = Column(Integer, nullable=True, comment="Current win streak for Fighter B entering the fight.")
    fighter_b_current_loss_streak = Column(Integer, nullable=True, comment="Current loss streak for Fighter B entering the fight.")
    fighter_b_significant_strikes_landed = Column(Integer, nullable=True, comment="Significant strikes landed by Fighter B.")
    fighter_b_significant_strikes_attempted = Column(Integer, nullable=True, comment="Significant strikes attempted by Fighter B.")
    fighter_b_takedowns_landed = Column(Integer, nullable=True, comment="Takedowns landed by Fighter B.")
    fighter_b_takedowns_attempted = Column(Integer, nullable=True, comment="Takedowns attempted by Fighter B.")
    fighter_b_control_time_seconds = Column(Integer, nullable=True, comment="Control time in seconds for Fighter B.")
    fighter_b_total_strikes_landed = Column(Integer, nullable=True)
    fighter_b_total_strikes_attempted = Column(Integer, nullable=True)
    fighter_b_knockdowns = Column(Integer, nullable=True)
    fighter_b_submission_attempts = Column(Integer, nullable=True)
    fighter_b_reversals = Column(Integer, nullable=True)
    fighter_b_days_since_last_fight = Column(Integer, nullable=True, comment="Days since Fighter B's last fight before this bout.")

    winner_fighter_id = Column(String, ForeignKey("fighters.fighter_id"), nullable=True, index=True)
    winner_name = Column(String, nullable=True)
    method_of_victory = Column(String, nullable=True, comment="Victory method such as KO/TKO, submission, or decision.")
    method_detail = Column(String, nullable=True, comment="More detailed description of the ending sequence.")
    result = Column(String, nullable=True)
    referee = Column(String, nullable=True)
    time_format = Column(String, nullable=True)
    ending_round = Column(Integer, nullable=True, comment="Round in which the fight ended.")
    ending_time = Column(String, nullable=True, comment="Time when the fight ended, such as 05:00.")
    head_to_head_fight_count = Column(Integer, nullable=True, default=0, comment="Number of prior fights between these two fighters before this bout.")
    source_event_url = Column(String, nullable=True)
    source_fight_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class FightRound(Base):
    """One row per fighter per completed round, retained for granular analysis."""

    __tablename__ = "fight_rounds"
    __table_args__ = (UniqueConstraint("fight_id", "round_number", "fighter_id", name="uq_fight_round_fighter"),)

    round_stat_id = Column(Integer, primary_key=True, autoincrement=True)
    fight_id = Column(String, ForeignKey("fights.fight_id", ondelete="CASCADE"), nullable=False, index=True)
    round_number = Column(Integer, nullable=False)
    fighter_id = Column(String, ForeignKey("fighters.fighter_id"), nullable=False, index=True)
    opponent_id = Column(String, ForeignKey("fighters.fighter_id"), nullable=False)
    knockdowns = Column(Integer, nullable=False, default=0)
    significant_strikes_landed = Column(Integer, nullable=False, default=0)
    significant_strikes_attempted = Column(Integer, nullable=False, default=0)
    total_strikes_landed = Column(Integer, nullable=False, default=0)
    total_strikes_attempted = Column(Integer, nullable=False, default=0)
    takedowns_landed = Column(Integer, nullable=False, default=0)
    takedowns_attempted = Column(Integer, nullable=False, default=0)
    submission_attempts = Column(Integer, nullable=False, default=0)
    reversals = Column(Integer, nullable=False, default=0)
    control_time_seconds = Column(Integer, nullable=True)


__all__ = ["Base", "Event", "Fight", "FightRound", "Fighter"]

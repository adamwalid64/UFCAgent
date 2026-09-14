from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from pipeline import db as database
from pipeline.models import Fight, Fighter
from pipeline.pipeline import (
    _validate_profile_identity,
    _validate_stats_refresh_authority,
    _validated_event_fight_manifest,
    _profile_refresh_due,
    ensure_fighter,
    run_pipeline,
)


def _rounds(count: int) -> list[dict]:
    return [
        {"round_number": number, "fighter_a": {}, "fighter_b": {}}
        for number in range(1, count + 1)
    ]


def test_event_manifest_requires_exact_unique_url_and_identity_sets():
    url_a = "http://www.ufcstats.com/fight-details/aaaaaaaaaaaaaaaa"
    url_b = "http://www.ufcstats.com/fight-details/bbbbbbbbbbbbbbbb"
    valid = {
        "fight_urls": [url_a, url_b],
        "fight_summaries": [
            {"fight_id": "aaaaaaaaaaaaaaaa", "fight_url": url_a},
            {"fight_id": "bbbbbbbbbbbbbbbb", "fight_url": url_b},
        ],
    }
    assert _validated_event_fight_manifest(valid) == {
        url_a: "aaaaaaaaaaaaaaaa",
        url_b: "bbbbbbbbbbbbbbbb",
    }

    mismatch = {**valid, "fight_summaries": [valid["fight_summaries"][0]]}
    with pytest.raises(ValueError, match="does not exactly match"):
        _validated_event_fight_manifest(mismatch)

    wrong_id = {
        **valid,
        "fight_summaries": [
            {"fight_id": "cccccccccccccccc", "fight_url": url_a},
            valid["fight_summaries"][1],
        ],
    }
    with pytest.raises(ValueError, match="identity mismatch"):
        _validated_event_fight_manifest(wrong_id)


def test_stats_authority_rejects_partial_and_complete_downgrades():
    assert _validate_stats_refresh_authority(
        "fight-1",
        {"stats_parse_status": "complete", "ending_round": 3},
        _rounds(3),
        existing_has_complete_stats=True,
    )

    with pytest.raises(ValueError, match="returned partial stats"):
        _validate_stats_refresh_authority(
            "fight-1",
            {
                "stats_parse_status": "partial",
                "stats_parse_detail": "significant round rows differ",
                "ending_round": 3,
            },
            _rounds(2),
            existing_has_complete_stats=False,
        )

    with pytest.raises(ValueError, match="complete->unavailable"):
        _validate_stats_refresh_authority(
            "fight-1",
            {"stats_parse_status": "unavailable", "ending_round": 3},
            [],
            existing_has_complete_stats=True,
        )


def test_profile_identity_guard_preserves_legitimate_source_nulls():
    profile_url = "http://www.ufcstats.com/fighter-details/aaaaaaaaaaaaaaaa"
    profile = {
        "fighter_id": "aaaaaaaaaaaaaaaa",
        "fighter_name": "Source Null",
        "fighter_nickname": None,
        "date_of_birth": None,
        "height_inches": None,
        "reach_inches": None,
        "stance": None,
        "profile_url": profile_url,
    }
    _validate_profile_identity("aaaaaaaaaaaaaaaa", profile_url, profile)

    engine = database.init_db("sqlite:///:memory:")
    with Session(engine) as session:
        fighter = ensure_fighter(
            session,
            "aaaaaaaaaaaaaaaa",
            "Source Null",
            profile_url,
            profile=profile,
        )
        session.commit()
        assert fighter.profile_scraped_at is not None
        assert fighter.fighter_nickname is None
        assert fighter.date_of_birth is None
        assert fighter.height_inches is None
        assert fighter.reach_inches is None
        assert fighter.stance is None

        fighter.fighter_nickname = "Known Nickname"
        fighter.date_of_birth = date(1990, 1, 2)
        fighter.height_inches = 72
        fighter.reach_inches = 74
        fighter.stance = "Southpaw"
        session.commit()
        ensure_fighter(
            session,
            "aaaaaaaaaaaaaaaa",
            "Source Null",
            profile_url,
            profile=profile,
        )
        session.commit()
        assert fighter.fighter_nickname == "Known Nickname"
        assert fighter.date_of_birth == date(1990, 1, 2)
        assert fighter.height_inches == 72
        assert fighter.reach_inches == 74
        assert fighter.stance == "Southpaw"

    wrong = {**profile, "fighter_id": "bbbbbbbbbbbbbbbb"}
    with pytest.raises(ValueError, match="identity mismatch"):
        _validate_profile_identity("aaaaaaaaaaaaaaaa", profile_url, wrong)


def test_profile_staleness_policy_refreshes_missing_or_ninety_day_old_profiles():
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    fighter = Fighter(fighter_id="fighter-a", fighter_name="Fighter A")
    assert _profile_refresh_due(fighter, max_age_days=90, now=now)

    fighter.profile_scraped_at = now - timedelta(days=89)
    assert not _profile_refresh_due(fighter, max_age_days=90, now=now)

    # SQLite commonly returns naive datetimes even for timezone=True; treat
    # those timestamps as UTC and refresh on the boundary.
    fighter.profile_scraped_at = (now - timedelta(days=90)).replace(tzinfo=None)
    assert _profile_refresh_due(fighter, max_age_days=90, now=now)


class _OneFightScraper:
    def __init__(self, fight_id: str, *, profile_failure: bool = False):
        self.fight_id = fight_id
        self.fight_url = f"http://www.ufcstats.com/fight-details/{fight_id}"
        self.profile_failure = profile_failure

    def crawl_event_listing(self, max_events=None):
        return type("Summary", (), {"event_urls": ["event-1"], "errors": []})()

    def crawl_event_details(self, event_url):
        return {
            "event_name": "UFC Authority Test",
            "event_date": "2026-01-01",
            "event_location": "Las Vegas",
            "event_url": event_url,
            "fight_urls": [self.fight_url],
            "fight_summaries": [
                {
                    "fight_id": self.fight_id,
                    "fight_url": self.fight_url,
                    "card_order": 1,
                }
            ],
        }

    def crawl_fight_card(self, fight_url):
        assert fight_url == self.fight_url
        return {
            "fight_id": self.fight_id,
            "fighter_a_id": "aaaaaaaaaaaaaaaa",
            "fighter_a_name": "Fighter A",
            "fighter_a_url": "http://www.ufcstats.com/fighter-details/aaaaaaaaaaaaaaaa",
            "fighter_b_id": "bbbbbbbbbbbbbbbb",
            "fighter_b_name": "Fighter B",
            "fighter_b_url": "http://www.ufcstats.com/fighter-details/bbbbbbbbbbbbbbbb",
            "winner_name": "Fighter A",
            "method_of_victory": "Decision - Unanimous",
            "ending_round": 3,
            "ending_time": "5:00",
            "stats_parse_status": "unavailable",
            "rounds": [],
            "source_fight_url": fight_url,
        }

    def crawl_fighter_profile(self, fighter_url):
        if self.profile_failure:
            raise TimeoutError("profile timeout after retries")
        fighter_id = fighter_url.rsplit("/", 1)[-1]
        return {
            "fighter_id": fighter_id,
            "fighter_name": "Fighter A" if fighter_id.startswith("a") else "Fighter B",
            "fighter_nickname": None,
            "date_of_birth": None,
            "height_inches": None,
            "reach_inches": None,
            "stance": None,
            "profile_url": fighter_url,
        }


def test_profile_failure_does_not_roll_back_canonical_event():
    engine = database.init_db("sqlite:///:memory:")
    events, fights, errors = run_pipeline(
        _OneFightScraper("1111111111111111", profile_failure=True),
        full=True,
    )

    assert (events, fights) == (1, 1)
    assert len([error for error in errors if error.startswith("Fighter profile failed:")]) == 2
    with Session(engine) as session:
        assert session.get(Fight, "1111111111111111") is not None
        assert session.get(Fighter, "aaaaaaaaaaaaaaaa") is not None
        assert session.get(Fighter, "bbbbbbbbbbbbbbbb") is not None


def test_source_removal_fails_closed_without_explicit_deletion_opt_in():
    engine = database.init_db("sqlite:///:memory:")
    first = _OneFightScraper("1111111111111111")
    assert run_pipeline(first, full=True, scrape_profiles=False)[:2] == (1, 1)

    replacement = _OneFightScraper("2222222222222222")
    events, fights, errors = run_pipeline(replacement, full=True, scrape_profiles=False)
    assert (events, fights) == (0, 0)
    assert any("refusing by default" in error for error in errors)
    with Session(engine) as session:
        assert session.get(Fight, "1111111111111111") is not None
        assert session.get(Fight, "2222222222222222") is None

    events, fights, errors = run_pipeline(
        replacement,
        full=True,
        scrape_profiles=False,
        allow_source_deletions=True,
    )
    assert (events, fights, errors) == (1, 1, [])
    with Session(engine) as session:
        assert session.get(Fight, "1111111111111111") is None
        assert session.get(Fight, "2222222222222222") is not None

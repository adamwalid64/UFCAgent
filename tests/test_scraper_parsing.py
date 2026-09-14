from datetime import date

import pytest

from pipeline.scraper import (
    RateLimitedPlaywrightScraper,
    STATS_PARSE_COMPLETE,
    STATS_PARSE_PARTIAL,
    STATS_PARSE_UNAVAILABLE,
    classify_stats_parse,
    normalize_event_method,
    parse_clock,
    parse_event_fight_summary,
    parse_fighter_profile_values,
    parse_labeled_metadata,
    parse_of,
    validate_loaded_source_identity,
)


def test_fight_metadata_parser_includes_first_class_method_item():
    metadata = parse_labeled_metadata(
        [
            "Method: Decision - Unanimous",
            "Round: 5",
            "Time: 5:00",
            "Time format: 5 Rnd (5-5-5-5-5)",
            "Referee: Herb Dean",
        ]
    )

    assert metadata == {
        "method": "Decision - Unanimous",
        "round": "5",
        "time": "5:00",
        "time_format": "5 Rnd (5-5-5-5-5)",
        "referee": "Herb Dean",
    }


def test_event_row_metadata_uses_headers_and_preserves_main_event_order():
    headers = ["W/L", "Fighter", "KD", "Str", "TD", "Sub", "Weight class", "Method", "Round", "Time"]
    cells = [{"text": "", "paragraphs": []} for _ in headers]
    cells[6] = {"text": "Light Heavyweight", "paragraphs": ["Light Heavyweight"]}
    cells[7] = {"text": "KO/TKO\nPunch", "paragraphs": ["KO/TKO", "Punch"]}

    summary = parse_event_fight_summary(
        headers,
        cells,
        "http://www.ufcstats.com/fight-details/e4931f3ab3bf4141",
        card_order=1,
    )

    assert summary == {
        "fight_id": "e4931f3ab3bf4141",
        "fight_url": "http://www.ufcstats.com/fight-details/e4931f3ab3bf4141",
        "weight_class": "Light Heavyweight",
        "method_of_victory": "KO/TKO",
        "event_method_code": "KO/TKO",
        "card_order": 1,
    }


def test_event_method_codes_expand_without_dropping_unrecognized_values():
    assert normalize_event_method("SUB") == "Submission"
    assert normalize_event_method("U-DEC") == "Decision - Unanimous"
    assert normalize_event_method("S-DEC") == "Decision - Split"
    assert normalize_event_method("M-DEC") == "Decision - Majority"
    assert normalize_event_method("CNC") == "Could Not Continue"
    assert normalize_event_method("Overturned") == "Overturned"
    assert normalize_event_method("Future Method") == "Future Method"
    assert normalize_event_method("--") is None


def test_fighter_profile_values_parse_dimensions_date_and_missing_markers():
    profile = parse_fighter_profile_values(
        "http://www.ufcstats.com/fighter-details/150ff4cc642270b9",
        " Max Holloway ",
        " Blessed ",
        ["Height: 5' 11\"", "Weight: 155 lbs.", "Reach: 69\"", "STANCE: Orthodox", "DOB: Dec 04, 1991"],
    )

    assert profile == {
        "fighter_id": "150ff4cc642270b9",
        "fighter_name": "Max Holloway",
        "fighter_nickname": "Blessed",
        "date_of_birth": date(1991, 12, 4),
        "height_inches": 71,
        "reach_inches": 69,
        "stance": "Orthodox",
        "profile_url": "http://www.ufcstats.com/fighter-details/150ff4cc642270b9",
    }

    missing = parse_fighter_profile_values(
        "http://www.ufcstats.com/fighter-details/e8efeb9cf33b1941",
        "Cesar Marscucci",
        "",
        ["Height: --", "Weight: --", "Reach: --", "STANCE:", "DOB: --"],
    )
    assert missing["fighter_nickname"] is None
    assert missing["date_of_birth"] is None
    assert missing["height_inches"] is None
    assert missing["reach_inches"] is None
    assert missing["stance"] is None


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("Height", "five eleven"),
        ("Height", "5' 12\""),
        ("Reach", "unknown-ish"),
        ("DOB", "1991-12-04"),
        ("DOB", "Feb 30, 1991"),
    ],
)
def test_fighter_profile_rejects_malformed_non_missing_values(field, bad_value):
    items = ["Height: 5' 11\"", "Weight: 155 lbs.", "Reach: 69\"", "STANCE: Orthodox", "DOB: Dec 04, 1991"]
    items = [f"{field}: {bad_value}" if item.startswith(f"{field}:") else item for item in items]

    with pytest.raises(ValueError, match="Malformed non-missing"):
        parse_fighter_profile_values(
            "http://www.ufcstats.com/fighter-details/150ff4cc642270b9",
            "Max Holloway",
            "Blessed",
            items,
        )


def test_fighter_profile_requires_complete_bio_and_valid_source_identity():
    with pytest.raises(ValueError, match="missing bio field.*reach"):
        parse_fighter_profile_values(
            "http://www.ufcstats.com/fighter-details/150ff4cc642270b9",
            "Max Holloway",
            "Blessed",
            ["Height: 5' 11\"", "Weight: 155 lbs.", "STANCE: Orthodox", "DOB: Dec 04, 1991"],
        )

    with pytest.raises(ValueError, match="Malformed UFCStats fighter profile URL"):
        parse_fighter_profile_values(
            "http://www.ufcstats.com/fighter-details/not-a-source-id",
            "Max Holloway",
            "Blessed",
            ["Height: --", "Weight: --", "Reach: --", "STANCE:", "DOB: --"],
        )


def test_stats_parse_classifier_requires_all_tables_and_round_alignment():
    complete = classify_stats_parse(
        table_count=4,
        general_total_count=1,
        general_round_count=3,
        significant_total_count=1,
        significant_round_count=3,
        ending_round=3,
        unavailable_marker=False,
    )
    unavailable = classify_stats_parse(
        table_count=0,
        general_total_count=0,
        general_round_count=0,
        significant_total_count=0,
        significant_round_count=0,
        ending_round=1,
        unavailable_marker=True,
        table_shapes_valid=False,
        fighter_alignment_valid=False,
    )
    partial = classify_stats_parse(
        table_count=4,
        general_total_count=1,
        general_round_count=3,
        significant_total_count=1,
        significant_round_count=2,
        ending_round=3,
        unavailable_marker=False,
    )

    assert complete[0] == STATS_PARSE_COMPLETE
    assert unavailable[0] == STATS_PARSE_UNAVAILABLE
    assert partial[0] == STATS_PARSE_PARTIAL
    assert "significant round rows (2) do not match ending round (3)" in partial[1]

    mismatched_values = classify_stats_parse(
        table_count=4,
        general_total_count=1,
        general_round_count=1,
        significant_total_count=1,
        significant_round_count=1,
        ending_round=1,
        unavailable_marker=False,
        stat_values_aligned=False,
    )
    assert mismatched_values[0] == STATS_PARSE_PARTIAL
    assert "not aligned across paired tables" in mismatched_values[1]


def test_page_load_retries_are_bounded_and_final_failure_is_explicit():
    class FlakyPage:
        def __init__(self, failures):
            self.failures = failures
            self.goto_calls = 0

        def goto(self, *_args, **_kwargs):
            self.goto_calls += 1
            if self.goto_calls <= self.failures:
                raise TimeoutError("transient timeout")

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def wait_for_selector(self, *_args, **_kwargs):
            return None

        def wait_for_timeout(self, *_args, **_kwargs):
            return None

    scraper = RateLimitedPlaywrightScraper(delay_seconds=0, max_attempts=3)
    eventually_works = FlakyPage(failures=2)
    scraper._open(eventually_works, "http://example.test/event", "table")
    assert eventually_works.goto_calls == 3

    always_fails = FlakyPage(failures=3)
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        scraper._open(always_fails, "http://example.test/fight", "table")
    assert always_fails.goto_calls == 3


def test_stats_keep_legacy_raw_splits_and_add_parsed_structure():
    stats = RateLimitedPlaywrightScraper._stats(
        {
            "kd": "1",
            "sig_str": "12 of 25",
            "total_str": "20 of 34",
            "td": "2 of 3",
            "sub_att": "1",
            "rev": "0",
            "ctrl": "1:23",
        },
        {
            "head": "8 of 18",
            "body": "2 of 4",
            "leg": "2 of 3",
            "distance": "7 of 17",
            "clinch": "3 of 5",
            "ground": "2 of 3",
        },
    )

    assert stats["head"] == "8 of 18"
    assert stats["ground"] == "2 of 3"
    assert stats["significant_strike_splits"] == {
        "head": {"landed": 8, "attempted": 18},
        "body": {"landed": 2, "attempted": 4},
        "leg": {"landed": 2, "attempted": 3},
        "distance": {"landed": 7, "attempted": 17},
        "clinch": {"landed": 3, "attempted": 5},
        "ground": {"landed": 2, "attempted": 3},
    }


@pytest.mark.parametrize("value", [None, "", "x of y", "1 of 2 extra", "3 of 2"])
def test_required_landed_attempted_counts_fail_closed(value):
    with pytest.raises(ValueError):
        parse_of(value)


def test_required_numeric_counts_fail_closed_but_explicit_missing_control_is_allowed():
    general = {
        "kd": "",
        "sig_str": "12 of 25",
        "total_str": "20 of 34",
        "td": "2 of 3",
        "sub_att": "1",
        "rev": "0",
        "ctrl": "--",
    }
    sig = {name: "0 of 0" for name in ("head", "body", "leg", "distance", "clinch", "ground")}
    with pytest.raises(ValueError, match="knockdown count"):
        RateLimitedPlaywrightScraper._stats(general, sig)

    general["kd"] = "0"
    assert RateLimitedPlaywrightScraper._stats(general, sig)["control_time_seconds"] is None
    assert parse_clock("--") is None
    with pytest.raises(ValueError, match="control time"):
        parse_clock("")


def test_loaded_page_identity_allows_scheme_redirect_only():
    requested = "http://www.ufcstats.com/fight-details/aaaaaaaaaaaaaaaa"
    loaded = "https://www.ufcstats.com/fight-details/aaaaaaaaaaaaaaaa"
    assert validate_loaded_source_identity(requested, loaded, "Fight page") == "aaaaaaaaaaaaaaaa"

    with pytest.raises(ValueError, match="identity mismatch"):
        validate_loaded_source_identity(
            requested,
            "https://www.ufcstats.com/fight-details/bbbbbbbbbbbbbbbb",
            "Fight page",
        )

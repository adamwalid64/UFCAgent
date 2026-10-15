from types import SimpleNamespace

import pytest

from sql_agent.usage import UsageMeter, QuotaExhaustedError, ModelCallLimitError
from sql_agent.assess_ablation import usage_summary, worthwhile, failure_categories, compare_subset
from sql_agent.ablation import POLICY


def test_reported_usage_includes_cached_and_reasoning_tokens_without_double_counting():
    meter = UsageMeter()
    raw = SimpleNamespace(usage_metadata={"input_tokens": 1000, "output_tokens": 500,
        "input_token_details": {"cache_read": 600}, "output_token_details": {"reasoning": 300}}, response_metadata={"id": "response"})
    runner = SimpleNamespace(invoke=lambda _: {"raw": raw, "parsed": {"answer": 1}, "parsing_error": None})
    assert meter.invoke(runner, [], "draft", "gpt-5.4") == {"answer": 1}
    summary = meter.summary()
    assert summary["total_tokens"] == 1500 and summary["reasoning_tokens"] == 300
    assert summary["estimated_usd"] == pytest.approx((400*2.5+600*.25+500*15)/1e6)
    snapshot = meter.summary()
    runner.invoke = lambda _: {"raw": raw, "parsed": None, "parsing_error": ValueError("bad schema")}
    with pytest.raises(ValueError, match="bad schema"):
        meter.invoke(runner, [], "review", "gpt-5.4")
    assert meter.summary()["total_tokens"] == 3000 and snapshot["total_tokens"] == 1500


def test_unreported_transport_error_is_not_presented_as_free():
    meter = UsageMeter()
    def fail(_):
        raise TimeoutError("unavailable")
    with pytest.raises(TimeoutError):
        meter.invoke(SimpleNamespace(invoke=fail), [], "review", "gpt-5.4")
    assert meter.summary()["estimated_usd"] is None
    assert not meter.summary()["usage_complete"]


def test_call_budget_counts_failed_requests_and_blocks_before_invocation():
    meter = UsageMeter(max_calls=1)
    calls = []
    def fail(_):
        calls.append(1)
        raise TimeoutError("Unknown outcome")
    runner = SimpleNamespace(invoke=fail)
    with pytest.raises(TimeoutError):
        meter.invoke(runner, [], "draft", "gpt-5.4")
    with pytest.raises(ModelCallLimitError, match="no further request sent"):
        meter.invoke(runner, [], "draft", "gpt-5.4")
    assert len(calls) == 1 and meter.remaining_calls == 0


def test_failed_forecast_costs_remain_in_cost_comparison():
    results = [{"error": "withheld", "model_usage": {"request_count": 3, "usage_complete": True, "estimated_usd": .5}},
               {"model_usage": {"request_count": 1, "usage_complete": True, "estimated_usd": .1}}]
    assert usage_summary(results)["estimated_usd"] == pytest.approx(.6)
    assert usage_summary(results)["request_count"] == 4
    results[0]["model_usage"]["estimated_usd"] = None
    assert usage_summary(results)["estimated_usd"] is None


def test_transient_recovery_is_bounded_and_counts_each_request(monkeypatch):
    import sql_agent.usage as usage
    monkeypatch.setattr(usage.time, "sleep", lambda _: None)
    calls = []
    def recover(_):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("Temporary outage")
        return {"answer": 1}
    meter = UsageMeter(max_calls=3, transient_retries=1)
    assert meter.invoke(SimpleNamespace(invoke=recover), [], "draft", "gpt-5.4") == {"answer": 1}
    assert meter.summary()["request_count"] == 2 and meter.remaining_calls == 1
    assert meter.summary()["calls"][0]["retry_scheduled"]
    calls.clear()
    with pytest.raises(TimeoutError):
        UsageMeter(max_calls=1, transient_retries=1).invoke(SimpleNamespace(invoke=recover), [], "draft", "gpt-5.4")
    assert len(calls) == 1


def test_retries_do_not_repeat_permanent_errors_or_run_forever(monkeypatch):
    import sql_agent.usage as usage
    monkeypatch.setattr(usage.time, "sleep", lambda _: None)
    for error, expected in [(ValueError("invalid schema"), 1), (TimeoutError("still unavailable"), 2)]:
        def fail(_):
            raise error
        meter = UsageMeter(max_calls=5, transient_retries=1)
        with pytest.raises(type(error)):
            meter.invoke(SimpleNamespace(invoke=fail), [], "draft", "gpt-5.4")
        assert meter.summary()["request_count"] == expected


def test_policy_does_not_keep_expensive_or_incomplete_variants_just_for_higher_accuracy():
    base = {"summary": {"attempted": 10, "published": 10, "winner": {"correct": 6, "scored": 10}, "brier_score": .25},
            "usage": {"estimated_usd": 1.0}}
    candidate = {"summary": {"attempted": 10, "published": 10, "winner": {"correct": 8, "scored": 10}, "brier_score": .20},
                 "usage": {"estimated_usd": 1.9}}
    assert worthwhile(candidate, base, POLICY)["passes"]
    candidate["usage"]["estimated_usd"] = 2.1
    assert not worthwhile(candidate, base, POLICY)["passes"]
    candidate["usage"]["estimated_usd"] = 1.1
    candidate["summary"]["published"] = 9
    assert not worthwhile(candidate, base, POLICY)["passes"]


def test_exhausted_quota_stops_subsequent_requests_across_meters(monkeypatch):
    from threading import Event
    import sql_agent.usage as usage
    monkeypatch.setattr(usage, "QUOTA_EXHAUSTED", Event())
    calls = []
    def fail(_):
        calls.append(1)
        raise RuntimeError("You have no credits remaining")
    runner = SimpleNamespace(invoke=fail)
    first, second = UsageMeter(transient_retries=1), UsageMeter(transient_retries=1)
    with pytest.raises(QuotaExhaustedError, match="credits exhausted"):
        first.invoke(runner, [], "draft", "gpt-5.4")
    with pytest.raises(QuotaExhaustedError, match="calls stopped"):
        second.invoke(runner, [], "draft", "gpt-5.4")
    assert len(calls) == 1 and first.summary()["calls"][0]["quota_exhausted"]
    assert second.summary()["request_count"] == 0


def test_transient_rate_limit_does_not_disable_later_calls(monkeypatch):
    from threading import Event
    import sql_agent.usage as usage
    monkeypatch.setattr(usage, "QUOTA_EXHAUSTED", Event())
    def fail(_):
        raise RuntimeError("429 rate limit, retry later")
    with pytest.raises(RuntimeError):
        UsageMeter().invoke(SimpleNamespace(invoke=fail), [], "draft", "gpt-5.4")
    assert not usage.QUOTA_EXHAUSTED.is_set()


def test_assessment_separates_billing_review_and_interrupted_failures():
    predictions = [{"predicted_winner": "A"}, {"error": "no credits remaining", "error_type": "RateLimitError"},
                   {"error": "Not completed: no credits remaining", "error_type": "ExperimentInterrupted"},
                   {"error": "Unsupported claim", "error_type": "PredictionReviewError"}]
    assert failure_categories(predictions) == {"published": 1, "billing_failure": 1, "interrupted": 1,
                                              "review_withheld": 1, "other_failure": 0}


def test_paired_comparison_uses_matched_bouts_and_never_scores_withheld_as_wrong():
    def row(a, b, right):
        return {"fighter_a": a, "fighter_b": b, "win_probability": .6,
                **{f"{key}_correct": right for key in ("winner", "method", "round", "exact")}}
    a, b = row("A", "B", True), row("C", "D", False)
    withheld = {**a, **{f"{k}_correct": None for k in ("winner", "method", "round", "exact")},
                "error": "Unsupported", "error_type": "PredictionReviewError"}
    left, right = {"predictions": [a, b]}, {"predictions": [b, withheld]}
    le, re = {"fights": [a, b]}, {"fights": [b, withheld]}
    paired = compare_subset(left, right, le, re)
    assert paired["fight_count"] == 1
    assert paired["left"]["summary"]["winner"]["correct"] == 0
    trials = compare_subset(left, right, le, re, completed_reviews=True)
    assert trials["fight_count"] == 2 and trials["right"]["summary"]["published"] == 1
    assert trials["right"]["summary"]["winner"]["scored"] == 1

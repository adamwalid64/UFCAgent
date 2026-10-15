import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from fight_coverage import FightCoverage, CoveragePipeline
from fight_coverage.config import CoverageConfig, Channel, WrittenOutlet, load_registry, load_config
from fight_coverage.discovery import Discovery, utc
from fight_coverage.non_youtube import AccessSkipped, PermittedHTTP, WrittenPicksAdapter, RedditAdapter
from fight_coverage.signals import ExtractionBatch, extract_signals, explicit_pick, weighted_picks
from fight_coverage.store import Store
from sql_agent.central import CentralNode
from sql_agent.usage import UsageMeter

FIGHT = {"fighter_a": "Brendan Allen", "fighter_b": "Chris Duncan"}
SOURCE = {"source_id": "test-channel", "content_id": "video1", "channel": "Technical Analyst",
          "url": "https://www.youtube.com/watch?v=abcdefghijk", "type": "technical breakdown",
          "credibility_weight": 1.0, "layer": "mcp", "cache_hit": False}
ANALYSIS = "Distance management could favor Allen, while clinch exchanges create risks."


class Model:
    def __init__(self, sources):
        self.sources, self.messages = sources, []

    def with_structured_output(self, schema, **kwargs):
        assert schema is ExtractionBatch
        def invoke(messages):
            self.messages.append(messages)
            return ExtractionBatch.model_validate({"sources": self.sources})
        return SimpleNamespace(invoke=invoke)


def coverage(text):
    result = FightCoverage(sources=[dict(SOURCE)], _analysis=[{
        "content_id": "video1", "segments": [{"text": paragraph} for paragraph in text.split("\n")]}])
    result.protect(result._analysis[0]["segments"])
    return result


def extraction(pick=None, evidence=None, analysis=ANALYSIS, **kwargs):
    return {"content_id": "video1", "pick": pick, "pick_evidence": evidence,
        "signals": [{"fighter": "Brendan Allen", "kind": "matchup", "analysis": analysis, "evidence_segments": [0]}], **kwargs}


def test_registry_verified_ids_types_weights_and_only_approved_sources():
    cfg = load_config()
    assert {c.name for c in cfg.channels if c.active} == {"MMA Guru", "White Piece MMA", "Horsebeef MMA",
        "Robin Black", "Luke Thomas", "Lucas Tracy MMA", "MMA Che"}
    assert all(c.channel_id and c.credibility_weight == 1.0 for c in cfg.channels)
    assert next(c for c in cfg.channels if c.name == "Robin Black").type == "technical breakdown"
    assert not cfg.non_youtube.written_enabled and not cfg.non_youtube.reddit.enabled


def test_registry_rejects_unknown_fields_duplicate_ids_and_handle_only(tmp_path):
    path = tmp_path / "sources.yaml"
    for sources in ([{"name": "Unknown", "handle": "@Unknown"}],
                    [{"name": "One", "channel_id": "UC" + "a" * 22}] * 2,
                    [{"name": "Unknown", "channel_id": "UC" + "a" * 22, "public_sentiment": 99}]):
        path.write_text(json.dumps({"sources": sources}))
        with pytest.raises(ValueError):
            load_registry(path)


def test_inactive_source_causes_no_discovery_requests(tmp_path):
    cfg = CoverageConfig(channels=[Channel(name="Off", channel_id="UC" + "a" * 22, active=False)])
    http = SimpleNamespace(get=lambda *a, **kw: pytest.fail("inactive channel fetched"))
    assert Discovery(cfg, Store(tmp_path), http=http).discover(FIGHT, "2026-10-10") == ([], [])


@pytest.mark.parametrize("text,pick,evidence", [
    ("Allen has a sharper jab but Duncan offers clinch pressure and difficult exchanges.", None, None),
    ("Allen has a sharper jab but Duncan offers clinch pressure and difficult exchanges.", "Brendan Allen", "Allen has a sharper jab"),
    ("If Allen could keep distance, Allen will win. Duncan remains dangerous in close range.", "Brendan Allen", "If Allen could keep distance, Allen will win."),
])
def test_breakdown_without_explicit_pick_keeps_null_and_analysis(text, pick, evidence):
    result = coverage(text)
    model = Model([extraction(pick, evidence)])
    meter = UsageMeter(max_calls=3)
    extract_signals(result, FIGHT, model, meter)
    assert result.signals[0]["pick"] is None
    assert result.signals[0]["signals"][0]["analysis"] == ANALYSIS
    assert result.signals[0]["credibility_weight"] == 1.0
    assert meter.summary()["request_count"] == 1
    assert "pick_evidence" not in json.dumps(result.public())


def test_explicit_pick_requires_real_verbatim_source_evidence():
    text = "My pick is Brendan Allen. His movement is useful against the forward pressure Duncan brings."
    result = coverage(text)
    extract_signals(result, FIGHT, Model([extraction("Brendan Allen", "My pick is Brendan Allen.")]), UsageMeter(max_calls=3))
    assert result.signals[0]["pick"] == "Brendan Allen"
    assert not explicit_pick("Chris Duncan", "My pick is Chris Duncan.", text, FIGHT)
    assert not explicit_pick("Brendan Allen", "Duncan says my pick is Brendan Allen.", "Duncan says my pick is Brendan Allen.", FIGHT)
    for text in ("I favor Allen's wrestling in this matchup.", "I pick Allen's jab as the more interesting weapon."):
        assert not explicit_pick("Brendan Allen", text, text, FIGHT)


def test_poll_and_odds_are_removed_before_extraction_and_never_extracted_as_signals():
    result = coverage("Allen has useful footwork against Duncan's pressure and strong clinch entries.\n"
                      "The range battle should shape who can sustain their offense.\n"
                      "A different discussion follows here.\n"
                      "Tapology fan picks are 99% Allen.\nMoneyline odds are -500 for Allen.")
    model = Model([extraction(analysis="Public picks are 99% for Allen.")])
    extract_signals(result, {**FIGHT, "public_sentiment": "NEVER_SEND"}, model, UsageMeter(max_calls=3))
    # The system prompt says not to extract polls; the source payload contains none.
    supplied = model.messages[0][1].content
    assert "99%" not in supplied and "-500" not in supplied and "NEVER_SEND" not in supplied
    assert not result.signals


def test_invalid_source_and_evidence_reference_are_discarded():
    result = coverage("Allen controls distance against Duncan but cage exchanges remain a challenge.")
    bad = extraction()
    bad["signals"][0]["evidence_segments"] = [99]
    extract_signals(result, FIGHT, Model([extraction(content_id="unknown"), bad]), UsageMeter(max_calls=3))
    assert not result.signals and result.caveats


def test_no_paid_extraction_without_budget_or_content():
    model = Model([])
    result = coverage("Source reasoning with enough detail for private matchup analysis.")
    extract_signals(result, FIGHT, model, UsageMeter(max_calls=1))
    extract_signals(FightCoverage(), FIGHT, model, UsageMeter(max_calls=3))
    assert not model.messages


def test_credibility_weights_deduplicate_sources_and_exclude_null_or_conflicting_picks():
    def item(source, pick, weight):
        return {"source_id": source, "pick": pick, "credibility_weight": weight}
    result = weighted_picks([item("a", "Allen", 1), item("a", "Allen", 1), item("b", "Duncan", 0.2),
                             item("technical", None, 1), item("conflict", "Allen", 1), item("conflict", "Duncan", 1)])
    assert result["total_pick_weight"] == 1.2
    assert result["weighted_explicit_picks"] == {"Allen": 1, "Duncan": 0.2}
    assert result["sources_without_unambiguous_pick"] == 2


def test_adapter_interface_filters_market_fields(tmp_path):
    captured = []
    def get(fight, cutoff):
        captured.append(fight)
        return FightCoverage()
    CoveragePipeline(CoverageConfig(cache_dir=tmp_path), adapters=[SimpleNamespace(get_fight_coverage=get)]).get_fight_coverage(
        {**FIGHT, "odds": {"secret": 1}, "public_sentiment": {"secret": 2}}, "2026-10-10")
    assert captured == [FIGHT]


@pytest.fixture
def settings(tmp_path):
    cfg = CoverageConfig(cache_dir=tmp_path)
    store = Store(tmp_path, now=lambda: utc("2026-10-08T12:00:00Z"))
    return cfg, store


class FakeHTTP:
    def __init__(self, text):
        self.text, self.calls = text, []
    def read(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return {"text": self.text, "fetched_at": "2026-10-08T12:00:00Z"}


def test_optional_modules_off_makes_no_requests(settings):
    cfg, store = settings
    http = FakeHTTP("")
    assert not WrittenPicksAdapter(cfg, store, http).get_fight_coverage(FIGHT, "2026-10-10").sources
    assert not RedditAdapter(cfg, store, http).get_fight_coverage(FIGHT, "2026-10-10").sources
    assert not http.calls


def test_written_picks_separate_each_writer_and_weight(settings):
    cfg, store = settings
    cfg.non_youtube.written_enabled = True
    cfg.non_youtube.outlets = [WrittenOutlet(name="Approved outlet", active=True,
        urls=["https://outlet.example/picks"], allowed_hosts=["outlet.example"], automated_access_allowed=True,
        terms_url="https://outlet.example/terms", entry_selector=".staff-pick", writer_weights={"Alice": 1.4})]
    reasons = "Brendan Allen uses distance and a steady jab against Chris Duncan, who can instead close into clinch exchanges."
    html = '<meta property="article:published_time" content="2026-10-07T12:00:00Z">' + ''.join(
        f'<section class="staff-pick"><a rel="author">{writer}</a><b class="pick">Pick: {pick}</b><p>{reasons}</p></section>'
        for writer, pick in (("Alice", "Brendan Allen"), ("Bob", "Chris Duncan")))
    result = WrittenPicksAdapter(cfg, store, FakeHTTP(html)).get_fight_coverage(FIGHT, "2026-10-10")
    assert [s["writer"] for s in result.sources] == ["Alice", "Bob"]
    assert [s["credibility_weight"] for s in result.sources] == [1.4, 1.0]
    assert len({s["source_id"] for s in result.sources}) == 2
    assert reasons not in json.dumps(result.public())


def test_robots_denial_unknown_terms_and_redirects_skip_before_content(settings):
    cfg, store = settings
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=200, content=b"User-agent: *\nDisallow: /", text="User-agent: *\nDisallow: /")
    client = PermittedHTTP(cfg, store, SimpleNamespace(get=get), sleep=lambda _: None)
    with pytest.raises(AccessSkipped, match="terms"):
        client.read("https://outlet.example/picks", allowed_hosts=["outlet.example"], authorized=False, terms_url="https://outlet.example/terms")
    assert not calls
    with pytest.raises(AccessSkipped, match="robots"):
        client.read("https://outlet.example/picks", allowed_hosts=["outlet.example"], authorized=True, terms_url="https://outlet.example/terms")
    assert calls == ["https://outlet.example/robots.txt"]


def test_allowed_robots_and_cached_article_are_shared(settings):
    cfg, store = settings
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        text = "User-agent: *\nAllow: /" if url.endswith("robots.txt") else "Permitted article content"
        return SimpleNamespace(status_code=200, content=text.encode(), text=text)
    client = PermittedHTTP(cfg, store, SimpleNamespace(get=get), sleep=lambda _: None)
    kwargs = dict(allowed_hosts=["outlet.example"], authorized=True, terms_url="https://outlet.example/terms")
    assert client.read("https://outlet.example/picks", **kwargs) == client.read("https://outlet.example/picks", **kwargs)
    assert len(calls) == 2


def test_reddit_top_reasoned_comments_only_with_low_writer_weights(settings, monkeypatch):
    cfg, store = settings
    cfg.non_youtube.reddit.enabled = True
    cfg.non_youtube.reddit.thread_urls = ["https://oauth.reddit.com/r/MMA/comments/abc123"]
    cfg.non_youtube.reddit.top_comments = 1
    monkeypatch.setenv("REDDIT_ACCESS_TOKEN", "private-test-token")
    stamp = utc("2026-10-07").timestamp()
    reason = ("I pick Brendan Allen against Chris Duncan because the jab and footwork should let him dictate range, "
              "but Duncan's wrestling and clinch pressure present a meaningful problem if Allen backs toward the cage.")
    def comment(cid, body, score, edited=False):
        return {"kind": "t1", "data": {"id": cid, "author": cid, "score": score, "body": body,
            "created_utc": stamp, "edited": edited, "permalink": f"/r/MMA/comments/abc123/title/{cid}/"}}
    response = [{"data": {"children": [{"data": {"title": "UFC predictions thread", "subreddit": "MMA", "id": "abc123", "created_utc": stamp}}]}},
                {"data": {"children": [comment("popular", "Allen easy win!", 500), comment("good", reason, 20),
                                       comment("late", reason, 100, utc("2026-10-11").timestamp()),
                                       comment("low", reason, 1)]}}]
    http = FakeHTTP(json.dumps(response))
    result = RedditAdapter(cfg, store, http).get_fight_coverage(FIGHT, "2026-10-10")
    assert len(result.sources) == 1 and result.sources[0]["writer"] == "good"
    assert result.sources[0]["credibility_weight"] == 0.2
    assert "sort=top" in http.calls[0][0]
    assert "private-test-token" not in json.dumps(result.public())


def market_fixture(tmp_path, **changes):
    record = {"event_date": "2026-10-10", **FIGHT,
        "odds": {"bookmaker": "FixtureBook", "observed_at": "2026-10-09T12:00:00Z",
                 "moneyline": {"Brendan Allen": -150, "Chris Duncan": 130}},
        "public_sentiment": {"source": "FixturePoll", "observed_at": "2026-10-09T12:00:00Z",
                             "percentages": {"Brendan Allen": 80, "Chris Duncan": 20}}}
    record.update(changes)
    path = tmp_path / "market.json"
    path.write_text(json.dumps({"fights": [record]}))
    return path


PREDICTION = {**FIGHT, "as_of_date": "2026-10-10", "predicted_winner": "Brendan Allen", "win_probability": 0.6, "caveats": []}


def test_central_compares_market_and_public_without_changing_forecast(tmp_path):
    result = CentralNode(market_fixture(tmp_path), now=lambda: utc("2026-10-11")).finalize(PREDICTION)
    assert result["win_probability"] == 0.6 and result["predicted_winner"] == "Brendan Allen"
    comparisons = {c["fighter"]: c for c in result["market_context"]["comparisons"]}
    assert sum(c["market_probability_no_vig"] for c in comparisons.values()) == pytest.approx(1)
    assert comparisons["Chris Duncan"]["model_minus_public"] == pytest.approx(0.2)
    assert comparisons["Chris Duncan"]["potentially_overlooked_by_public"]
    assert not comparisons["Brendan Allen"]["potentially_overlooked_by_public"]


def test_central_rejects_late_observations_bad_percentages_and_wrong_fights(tmp_path):
    path = market_fixture(tmp_path)
    data = json.loads(path.read_text())
    data["fights"][0]["public_sentiment"]["observed_at"] = "2026-10-11T00:00:00Z"
    path.write_text(json.dumps(data))
    result = CentralNode(path).finalize(PREDICTION)
    assert result["market_context"]["public_sentiment"] is None
    data["fights"][0]["public_sentiment"]["percentages"]["Chris Duncan"] = 80
    path.write_text(json.dumps(data))
    result = CentralNode(path).finalize(PREDICTION)
    assert "market_context" not in result and result["caveats"]
    assert "market_context" not in CentralNode(market_fixture(tmp_path, fighter_a="Unrelated Fighter")).finalize(PREDICTION)

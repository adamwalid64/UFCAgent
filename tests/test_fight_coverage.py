"""Offline contracts for discovery, fallback ordering, cache isolation and publication."""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
import requests

from fight_coverage import CoveragePipeline, FightCoverage
from fight_coverage.config import Channel, CoverageConfig, MCPSettings, load_config
from fight_coverage.discovery import Discovery, evidence_cutoff, match_video, utc
from fight_coverage.mcp_client import MCPClient
from fight_coverage.store import QuotaExceeded, Store
from fight_coverage.transcripts import TranscriptAPI, YtDlp, normalize, worker
from fight_coverage.service import excerpts

VIDEO = "abcdefghijk"
CHANNEL = "UC" + "a" * 22
FIGHT = {"fighter_a": "Brendan Allen", "fighter_b": "Chris Duncan", "event_name": "UFC 500"}
TEXT = "Brendan Allen has a persistent pressure game while Chris Duncan offers a different striking threat."
SEGMENTS = [{"text": TEXT, "start": 12.5, "duration": 8}]
LAYERS = ["mcp", "youtube-transcript-api", "yt-dlp-subtitles", "whisper"]


@pytest.fixture
def setup(tmp_path):
    now = [utc("2026-10-08T15:00:00Z")]
    cfg = CoverageConfig(enabled=True, cache_dir=tmp_path / "cache", channels=[
        Channel(name="Test Channel", channel_id=CHANNEL, transcript_access_authorized=True,
                audio_download_authorized=True)])
    store = Store(cfg.cache_dir, now=lambda: now[0])
    return cfg, store, now


def video(**updates):
    return {"video_id": VIDEO, "channel_id": CHANNEL, "channel": "Test Channel", "match": "fight",
            "title": "Brendan Allen vs Chris Duncan prediction", "description": "UFC 500 preview",
            "published_at": "2026-10-07T12:00:00Z", "observed_at": "2026-10-08T15:00:00Z", **updates}


class Provider:
    def __init__(self, payload=SEGMENTS, fail=False):
        self.calls, self.payload, self.fail = [], payload, fail

    def fetch(self, video_id):
        self.calls.append(video_id)
        if self.fail:
            raise RuntimeError("secret-proxy-password must not appear in output")
        return self.payload


def pipeline(setup, providers, videos=None):
    cfg, store, now = setup
    return CoveragePipeline(cfg, store=store, providers=providers,
                            discovery=SimpleNamespace(discover=lambda *a: (videos or [video()], [])))


@pytest.mark.parametrize("winner", range(4))
def test_each_layer_falls_back_in_order_and_cache_never_refetches(setup, winner, caplog):
    cfg, store, now = setup
    cfg.whisper_enabled = winner == 3
    providers = [(layer, Provider(fail=i < winner)) for i, layer in enumerate(LAYERS)]
    p = pipeline(setup, providers)
    with caplog.at_level("INFO"):
        first = p.get_fight_coverage(FIGHT, "2026-10-10")
        # New pipeline object models a new CLI invocation using the same on-disk cache.
        second = pipeline(setup, providers).get_fight_coverage(FIGHT, "2026-10-10")
    assert first.sources[0]["layer"] == LAYERS[winner]
    assert not first.sources[0]["cache_hit"] and second.sources[0]["cache_hit"]
    assert [len(p.calls) for _, p in providers] == [1 if i <= winner else 0 for i in range(4)]
    assert f"layer={LAYERS[winner]}" in caplog.text
    assert TEXT not in caplog.text and "secret-proxy-password" not in caplog.text
    assert TEXT not in json.dumps(first.public())
    assert TEXT in json.dumps(first.for_analysis())
    assert TEXT not in repr(first)


def test_all_failed_skipped_with_safe_caveats_and_negative_cache(setup):
    providers = [(layer, Provider(fail=True)) for layer in LAYERS]
    p = pipeline(setup, providers)
    result = p.get_fight_coverage(FIGHT, "2026-10-10")
    p.get_fight_coverage(FIGHT, "2026-10-10")
    assert not result.sources
    assert "skipped" in " ".join(result.caveats)
    assert "secret-proxy-password" not in json.dumps(result.public())
    assert [len(p.calls) for _, p in providers] == [1, 1, 1, 0]
    setup[2][0] += timedelta(hours=7)
    p.get_fight_coverage(FIGHT, "2026-10-10")
    assert len(providers[0][1].calls) == 2


def test_missing_mcp_and_empty_transcripts_fall_through(setup):
    last = Provider()
    result = pipeline(setup, [("mcp", None), ("youtube-transcript-api", Provider(payload=[])),
                              ("yt-dlp-subtitles", last)]).get_fight_coverage(FIGHT, "2026-10-10")
    assert result.sources[0]["layer"] == "yt-dlp-subtitles"
    assert result.sources[0]["failed_layers"] == ["mcp: not configured or disabled", "youtube-transcript-api: ValueError"]


def test_permissions_and_disabled_whisper_prevent_access(setup):
    cfg, store, now = setup
    providers = [("mcp", Provider(fail=True)), ("whisper", Provider())]
    cfg.channels[0].transcript_access_authorized = False
    result = pipeline(setup, providers).get_fight_coverage(FIGHT, "2026-10-10")
    assert not any(p.calls for _, p in providers)
    assert "permissions" in str(result.caveats)
    cfg.channels[0].transcript_access_authorized = True
    pipeline(setup, providers).get_fight_coverage(FIGHT, "2026-10-10")
    assert not providers[1][1].calls


def test_expiry_and_forget_never_refetch_success(setup):
    provider = Provider()
    p = pipeline(setup, [("mcp", provider)])
    p.get_fight_coverage(FIGHT, "2026-10-10")
    setup[2][0] += timedelta(days=31)
    setup[1].prune()
    assert setup[1].get("transcript", VIDEO) is None
    result = p.get_fight_coverage(FIGHT, "2026-12-01")
    assert "refetch suppressed" in str(result.caveats)
    assert len(provider.calls) == 1
    setup[1].forget(VIDEO)
    assert setup[1].was_fetched(VIDEO)
    assert TEXT.encode() not in setup[1].path.read_bytes()


def test_concurrent_runs_fetch_video_once(setup):
    p = pipeline(setup, [("mcp", Provider())])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: p.get_fight_coverage(FIGHT, "2026-10-10"), range(2)))
    assert len(p.providers[0][1].calls) == 1
    assert len(results[0].sources) == len(results[1].sources) == 1


def test_excludes_post_event_metadata_without_transcript_requests(setup):
    provider = Provider()
    result = pipeline(setup, [("mcp", provider)], [video(observed_at="2026-10-10T01:00:00Z")]).get_fight_coverage(FIGHT, "2026-10-10")
    assert not result.sources and not provider.calls
    assert "metadata" in str(result.caveats)


def test_post_event_transcript_is_never_used_for_historical_replay(setup):
    cfg, store, now = setup
    now[0] = utc("2026-10-11")
    store.put("transcript", VIDEO, dict(segments=SEGMENTS, layer="mcp", fetched_at=now[0].isoformat()), 86400)
    result = pipeline(setup, [("mcp", Provider())]).get_fight_coverage(FIGHT, "2026-10-10")
    assert not result.sources and "first fetched" in str(result.caveats)
    assert not result._analysis


def test_unregistered_channels_postcutoff_and_unrelated_event_text_are_excluded(setup):
    provider = Provider(payload=[dict(text="Another unrelated pair of competitors have an upcoming contest.")])
    results = pipeline(setup, [("mcp", provider)], [video(channel_id="unknown"),
        video(published_at="2026-10-10"), video(match="event")]).get_fight_coverage(FIGHT, "2026-10-10")
    assert not results.sources and provider.calls == [VIDEO]


class HTTP:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(status_code=response[0], json=lambda: response[1])


def upload(vid=VIDEO, published="2026-10-07T12:00:00Z", title="Allen vs Duncan", description=""):
    return dict(snippet=dict(title=title, description=description, videoOwnerChannelId=CHANNEL),
                contentDetails=dict(videoId=vid, videoPublishedAt=published))


def test_official_discovery_pagination_matching_cutoff_and_shared_cache(setup, monkeypatch):
    cfg, store, now = setup
    monkeypatch.setenv("YOUTUBE_API_KEY", "secret-test-key")
    cfg.max_pages_per_channel = 2
    http = HTTP([(200, {"items": [{"id": CHANNEL, "contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}}}]}),
                 (200, {"items": [upload(), upload("postevent01", "2026-10-10T00:00:00Z"),
                                 upload("oldvideo001", "2025-01-01")], "nextPageToken": "next"}),
                 (200, {"items": [upload("otherfight1", title="Full card", description="UFC 500 predictions")],
                        "nextPageToken": "more"})])
    discovery = Discovery(cfg, store, http=http)
    videos, caveats = discovery.discover(FIGHT, "2026-10-10")
    assert [v["video_id"] for v in videos] == [VIDEO, "otherfight1"]
    assert "page limit" in str(caveats)
    discovery.discover(FIGHT, "2026-10-10")
    assert len(http.calls) == 3
    assert [call[0].split("/")[-1] for call in http.calls] == ["channels", "playlistItems", "playlistItems"]
    assert http.calls[-1][1]["params"]["pageToken"] == "next"
    assert b"secret-test-key" not in store.path.read_bytes()


def test_refresh_finds_late_upload_once_and_reuses_transcript(setup, monkeypatch):
    cfg, store, now = setup
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")
    params = {"part": "snippet,contentDetails", "playlistId": "UUtest", "maxResults": 50}
    old_http = HTTP([(200, {"items": []})])
    assert not Discovery(cfg, store, http=old_http).request("playlistItems", params)["items"]
    now[0] += timedelta(minutes=5)
    provider = Provider()
    p = pipeline(setup, [("mcp", provider)])
    p.get_fight_coverage(FIGHT, "2026-10-10")
    http = HTTP([(200, {"items": [upload()]})])
    refreshed = Discovery(cfg, store, http=http, refresh=True)
    assert refreshed.request("playlistItems", params)["items"][0]["contentDetails"]["videoId"] == VIDEO
    refreshed.request("playlistItems", params)
    p.get_fight_coverage(FIGHT, "2026-10-10")
    assert len(http.calls) == len(provider.calls) == 1
    assert store.get("transcript", VIDEO) is not None
    with store.connect() as db:
        assert db.execute("SELECT SUM(used) FROM quota").fetchone()[0] == 2


@pytest.mark.parametrize("day,expected", [
    ("2026-10-17", "2026-10-17T04:00:00Z"),
    ("2026-12-12", "2026-12-12T05:00:00Z"),
])
def test_date_only_cutoff_honors_eastern_daylight_saving(day, expected):
    assert evidence_cutoff(day, "America/New_York") == utc(expected)
    assert evidence_cutoff(day) == utc(day)  # Legacy configurations stay UTC.
    explicit = day + "T15:00:00-07:00"
    assert evidence_cutoff(explicit, "America/New_York") == utc(explicit)


def test_late_friday_preview_is_eligible_but_local_event_day_is_not(setup, monkeypatch):
    cfg, store, now = setup
    cfg.event_timezone = "America/New_York"
    now[0] = utc("2026-10-17T03:30:00Z")  # Friday 11:30 p.m. Eastern.
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")
    http = HTTP([(200, {"items": [{"id": CHANNEL, "contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}}}]}),
                 (200, {"items": [upload(published="2026-10-17T03:00:00Z"),
                                 upload("postevent01", published="2026-10-17T04:00:00Z")]})])
    provider = Provider()
    p = CoveragePipeline(cfg, store=store, discovery=Discovery(cfg, store, http=http), providers=[("mcp", provider)])
    coverage = p.get_fight_coverage(FIGHT, "2026-10-17")
    assert [source["video_id"] for source in coverage.sources] == [VIDEO]
    assert coverage.sources[0]["evidence_cutoff"] == "2026-10-17T04:00:00+00:00"
    assert provider.calls == [VIDEO]


def test_eastern_cutoff_still_rejects_transcripts_collected_on_event_day(setup):
    cfg, store, now = setup
    cfg.event_timezone = "America/New_York"
    now[0] = utc("2026-10-17T05:00:00Z")
    result = pipeline(setup, [("mcp", Provider())], [video(
        published_at="2026-10-17T03:00:00Z", observed_at="2026-10-17T03:30:00Z"
    )]).get_fight_coverage(FIGHT, "2026-10-17")
    assert not result.sources and "first fetched" in str(result.caveats)


def test_event_timezone_rejects_unknown_zone():
    with pytest.raises(ValueError, match="event_timezone"):
        CoverageConfig(event_timezone="not-a-real-zone")


def test_handle_resolution_uses_official_api(setup, monkeypatch):
    cfg, store, now = setup
    cfg.channels = [Channel(name="White Piece MMA", handle="@white-piece-mma")]
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")
    http = HTTP([(200, {"items": [{"id": CHANNEL, "contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}}}]}),
                 (200, {"items": [upload()]})])
    videos, _ = Discovery(cfg, store, http=http).discover(FIGHT, "2026-10-10")
    assert http.calls[0][1]["params"]["forHandle"] == "@white-piece-mma"
    assert videos[0]["registry_key"] == "@white-piece-mma"


def test_quota_atomic_and_pacific_midnight_not_utc_midnight(setup):
    cfg, store, now = setup
    now[0] = utc("2026-10-09T01:00:00Z")  # Still Oct 8 in California.
    store.quota(1)
    Store(cfg.cache_dir, now=lambda: now[0]).prune()
    with pytest.raises(QuotaExceeded):
        store.quota(1)
    now[0] = utc("2026-10-09T07:00:00Z")
    store.quota(1)
    with pytest.raises(QuotaExceeded):
        store.quota(1)


def test_discovery_retry_and_remote_quota_stop(setup, monkeypatch):
    cfg, store, _ = setup
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")
    sleeps = []
    http = HTTP([requests.Timeout(), (503, {}), (200, {"items": []}),
                 (403, {"error": {"errors": [{"reason": "quotaExceeded"}]}})])
    discovery = Discovery(cfg, store, http=http, sleep=sleeps.append)
    assert discovery.request("channels", {"id": "a"})["items"] == []
    assert sleeps == [1, 2]
    with pytest.raises(QuotaExceeded):
        discovery.request("channels", {"id": "b"})
    with pytest.raises(QuotaExceeded):
        discovery.request("channels", {"id": "c"})
    assert len(http.calls) == 4


def test_word_boundaries_accent_normalization_and_pair_titles():
    assert match_video(video(title="ALLEN vs. DUNCAN preview", description=""), FIGHT) == "fight"
    assert match_video(video(title="Allens vs Duncans", description=""), FIGHT) is None
    assert match_video(video(title="Preview", description="UFC 500 picks"), FIGHT) == "event"
    assert match_video(video(title="José Aldo vs. Chris Duncan", description=""),
                       {**FIGHT, "fighter_a": "Jose Aldo"}) == "fight"


@pytest.mark.parametrize("title", [
    "UFC Fight Night Malott versus Buckley - Full Card Predictions",
    "Buckley v. Malott Full Card Picks",
    "Buckley & Malott Full Card Breakdown",
])
def test_full_card_main_event_titles_cover_undercard_fights(title):
    fight = dict(fighter_a="Erin Blanchfield", fighter_b="Jasmine Jasudavicius",
                 event_name="UFC Fight Night: Buckley vs. Malott")
    assert match_video(video(title=title, description=""), fight) == "event"


@pytest.mark.parametrize("title", [
    "My Full Card Predictions For This Weekend!",
    "UFC Vegas 123 Full Card Betting Breakdown",
    "UFC Weekend Preview | Live Chat",
])
def test_generic_card_previews_are_provisional_candidates(title):
    assert match_video(video(title=title, description=""), FIGHT) == "card_candidate"


@pytest.mark.parametrize("title", [
    "UFC 500 Full Card Recap and Breakdown",
    "UFC 500 Watchalong and Predictions",
    "UFC 500 Results Reaction",
    "UFC 500 Full Fight Companion",
    "Dana White Has a Mental Breakdown",
])
def test_card_recaps_and_unrelated_breakdowns_are_excluded(title):
    assert match_video(video(title=title, description=""), FIGHT) is None


def test_card_candidates_require_requested_pair_in_transcript(setup):
    candidate = video(match="card_candidate", title="My Full Card Predictions")
    irrelevant = Provider(payload=[dict(text="Brendan Allen takes on an entirely different opponent.")])
    result = pipeline(setup, [("mcp", irrelevant)], [candidate]).get_fight_coverage(FIGHT, "2026-10-10")
    assert not result.sources and not result._analysis
    assert "both requested fighters" in str(result.caveats)


def test_card_candidate_transcript_is_shared_and_reused_for_each_fight(setup):
    segments = [dict(text="Welcome to my full card predictions", start=0, duration=5),
                dict(text="Brendan Allen will use pressure against Chris Duncan.", start=60, duration=5),
                dict(text="They have different approaches to grappling.", start=70, duration=5),
                dict(text="Erin Blanchfield will wrestle Jasmine Jasudavicius.", start=300, duration=5),
                dict(text="The clinch exchanges favor her here.", start=310, duration=5),
                dict(text="The next fight features two other athletes.", start=500, duration=5)]
    description = "0:00 Intro\n1:00 Allen vs Duncan\n5:00 Blanchfield vs Jasudavicius\n8:20 Next fight"
    provider = Provider(payload=segments)
    p = pipeline(setup, [("mcp", provider)], [video(match="card_candidate", description=description)])
    first = p.get_fight_coverage(FIGHT, "2026-10-10")
    second = p.get_fight_coverage(dict(fighter_a="Erin Blanchfield", fighter_b="Jasmine Jasudavicius"), "2026-10-10")
    assert len(provider.calls) == 1
    assert first._analysis[0]["segments"] == segments[1:3]
    assert second._analysis[0]["segments"] == segments[3:5]
    assert second.sources[0]["cache_hit"]
    assert second.sources[0]["matched_by"] == "card_candidate"
    assert "will wrestle" not in json.dumps(second.public())


def test_full_card_mentions_of_fighters_in_separate_bouts_do_not_match():
    text = "Brendan Allen faces someone else. " + "unrelated discussion " * 100 + "Chris Duncan faces another fighter."
    selected, _ = excerpts([dict(text=text)], FIGHT, 12000, False)
    assert not selected


def test_shared_surname_alone_does_not_confirm_both_fighters():
    from fight_coverage.discovery import matchup_discussed
    fight = dict(fighter_a="Justin Tafa", fighter_b="Junior Tafa")
    assert not matchup_discussed("Tafa has an excellent striking game.", fight)
    assert matchup_discussed("Justin Tafa and Junior Tafa have similar styles.", fight)


def test_numbered_event_alias_matches_full_card_without_bout_names():
    fight = {**FIGHT, "event_name": "UFC Fight Night: Allen vs Duncan", "event_aliases": ["UFC Vegas 122"]}
    assert match_video(video(title="UFC Vegas 122 Full Card Predictions", description=""), fight) == "event"


def test_discovery_prioritizes_identified_card_and_preserves_time_filters(setup, monkeypatch, caplog):
    cfg, store, _ = setup
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")
    http = HTTP([(200, {"items": [{"id": CHANNEL, "contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}}}]}),
                 (200, {"items": [upload("generic0001", title="My Full Card Predictions"),
                                 upload("eventvideo1", title="UFC 500 Full Card Predictions"),
                                 upload("postevent01", "2026-10-10", title="My Full Card Predictions"),
                                 upload("oldvideo001", "2025-01-01", title="My Full Card Predictions"),
                                 upload("recapvideo1", title="UFC 500 Full Card Recap")]})])
    with caplog.at_level("INFO"):
        found, _ = Discovery(cfg, store, http=http).discover(FIGHT, "2026-10-10")
    assert [v["video_id"] for v in found] == ["eventvideo1", "generic0001"]
    assert "card_candidates=1" in caplog.text and "in_window=3" in caplog.text


def test_transcript_api_proxy_timeout_retry_and_permanent_failure(setup, monkeypatch):
    import youtube_transcript_api
    cfg, store, _ = setup
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_PROXY", "http://name:password@proxy:8080")
    captured, sleeps, attempts = {}, [], []
    class API:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        def fetch(self, video_id, languages):
            attempts.append((video_id, languages))
            if len(attempts) < 3:
                raise requests.Timeout()
            return SEGMENTS
    monkeypatch.setattr(youtube_transcript_api, "YouTubeTranscriptApi", API)
    result = TranscriptAPI(cfg, sleep=sleeps.append).fetch(VIDEO)
    assert result == SEGMENTS and sleeps == [1, 2]
    assert captured["proxy_config"].to_requests_dict()["https"] == "http://name:password@proxy:8080"
    def denied(*args, **kwargs):
        raise ValueError("Permanent denial")
    monkeypatch.setattr(API, "fetch", denied)
    sleeps.clear()
    with pytest.raises(ValueError):
        TranscriptAPI(cfg, sleep=sleeps.append).fetch(VIDEO)
    assert not sleeps


def test_ytdlp_worker_downloads_only_auto_subtitles(setup, tmp_path, monkeypatch):
    import yt_dlp
    cfg, _, _ = setup
    captured = {}
    class Downloader:
        def __init__(self, options):
            captured.update(options)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def extract_info(self, url, download):
            assert url.endswith(VIDEO) and download
            (tmp_path / "captions.en.json3").write_text(json.dumps({"events": [
                {"tStartMs": 12500, "dDurationMs": 8000, "segs": [{"utf8": TEXT}]}]}))
    monkeypatch.setattr(yt_dlp, "YoutubeDL", Downloader)
    worker(dict(video_id=VIDEO, audio=False, directory=str(tmp_path), languages=["en"],
                request_timeout=30, retries=2, proxy_env=None, whisper_model="base", max_audio_seconds=7200))
    assert captured["skip_download"] is True and captured["writeautomaticsub"] is True
    assert captured["writesubtitles"] is False and "format" not in captured
    assert json.loads((tmp_path / "result.json").read_text()) == SEGMENTS


def test_audio_only_worker_and_local_whisper(setup, tmp_path, monkeypatch):
    import yt_dlp
    captured = {}
    class Downloader:
        def __init__(self, options):
            captured.update(options)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def extract_info(self, *a, **kw):
            (tmp_path / "audio.webm").write_bytes(b"fake audio")
    def load_model(name, device):
        assert name == "base" and device == "cpu"
        return SimpleNamespace(transcribe=lambda path, fp16: {"segments": SEGMENTS})
    monkeypatch.setattr(yt_dlp, "YoutubeDL", Downloader)
    monkeypatch.setitem(sys.modules, "whisper", SimpleNamespace(load_model=load_model))
    worker(dict(video_id=VIDEO, audio=True, directory=str(tmp_path), languages=["en"],
                request_timeout=30, retries=2, proxy_env=None, whisper_model="base", max_audio_seconds=7200))
    assert captured["format"] == "bestaudio" and not captured["writeautomaticsub"]
    assert captured["match_filter"]({"duration": 10}) is None
    assert captured["match_filter"]({"duration": 9000})
    assert captured["match_filter"]({"is_live": True})


def test_mcp_actual_stdio_inventory_schema_validation_and_fetch(tmp_path):
    pytest.importorskip("mcp")
    server = tmp_path / "fake_mcp.py"
    server.write_text('''from mcp.server.fastmcp import FastMCP
mcp = FastMCP("offline-test")
@mcp.tool()
def verified_captions(identifier: str) -> dict:
    return {"segments": [{"text": "This is an offline test transcript for " + identifier, "start": 0}]}
if __name__ == "__main__":
    mcp.run()
''')
    cfg = MCPSettings(command=sys.executable, args=[str(server)], tool="verified_captions", video_argument="identifier")
    client = MCPClient(cfg, timeout=15)
    inventory = client.inspect()
    assert [t["name"] for t in inventory] == ["verified_captions"]
    assert "identifier" in inventory[0]["inputSchema"]["required"]
    assert VIDEO in normalize(client.fetch(VIDEO))[0]["text"]
    cfg.tool = "invented_tool"
    with pytest.raises(Exception):
        client.fetch(VIDEO)
    cfg.tool, cfg.video_argument = "verified_captions", "wrong_argument"
    with pytest.raises(Exception):
        client.fetch(VIDEO)


def test_disabled_pipeline_is_offline_and_config_registers_requested_channels(tmp_path):
    result = CoveragePipeline(CoverageConfig(cache_dir=tmp_path / "absent")).get_fight_coverage(FIGHT, "2026-10-10")
    assert not result.sources and not (tmp_path / "absent").exists()
    config = load_config()
    assert len(config.channels) == 7 and not config.whisper_enabled


def test_publication_guard_rejects_and_redacts_copied_transcripts(setup):
    result = pipeline(setup, [("mcp", Provider())]).get_fight_coverage(FIGHT, "2026-10-10")
    assert result.publication_issues({"summary": TEXT})
    assert not result.publication_issues({"summary": "The commentator favors sustained offensive pressure."})
    assert TEXT not in json.dumps(result.redact_publication({"review": [{"summary": TEXT}]}))


def test_provider_subprocess_timeout_and_no_proxy_in_arguments(setup, monkeypatch):
    cfg, _, _ = setup
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        settings = json.loads(kwargs["input"])
        from pathlib import Path
        (Path(settings["directory"]) / "result.json").write_text(json.dumps(SEGMENTS))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("fight_coverage.transcripts.subprocess.run", run)
    assert YtDlp(cfg).fetch(VIDEO) == SEGMENTS
    assert calls[0][0] == [sys.executable, "-m", "fight_coverage.transcripts"]
    assert calls[0][1]["timeout"] == 120
    assert "proxy:8080" not in calls[0][1]["input"]


def test_long_plain_mcp_text_selects_matchup_in_middle():
    text = "Unrelated introductory discussion. " * 1000 + TEXT + " More discussion." * 1000
    selected, partial = excerpts([dict(text=text, start=0, duration=0)], FIGHT, 5000, False)
    assert partial and TEXT in str(selected)
    assert sum(len(s["text"]) for s in selected) <= 5000


def test_no_successful_video_hides_failed_video_caveats_and_candidate_cap(setup):
    cfg, _, _ = setup
    cfg.max_candidates_per_fight = 2
    class Mixed:
        def fetch(self, video_id):
            if video_id == VIDEO:
                raise RuntimeError("missing")
            return SEGMENTS
    result = pipeline(setup, [("mcp", Mixed())], [video(), video(video_id="secondvideo"),
        video(video_id="thirdvideo1")]).get_fight_coverage(FIGHT, "2026-10-10")
    assert len(result.sources) == 1 and result.sources[0]["video_id"] == "secondvideo"
    assert "skipped" in str(result.caveats) and "Candidate video limit" in str(result.caveats)


@pytest.mark.parametrize("payload", ["No transcript available", {"error": "Unavailable"}, [],
                                     [{"text": "Bad time", "start": float("nan")}], {"unexpected": "field"}])
def test_invalid_provider_responses_are_not_cached_as_success(payload):
    with pytest.raises(ValueError):
        normalize(payload)

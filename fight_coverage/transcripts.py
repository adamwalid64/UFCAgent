"""Transcript adapters. No discovery, model calls, or transcript publication here."""
from __future__ import annotations

import html
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def normalize(payload):
    """Convert supported MCP/API/subtitle responses into a common timed sequence."""
    if hasattr(payload, "to_raw_data"):
        payload = payload.to_raw_data()
    if isinstance(payload, dict):
        if payload.get("error") or payload.get("isError"):
            raise ValueError("Transcript provider returned an error")
        for key in ("segments", "transcript", "text"):
            if key in payload:
                return normalize(payload[key])
        raise ValueError("Unknown transcript response; configure MCP result_path")
    if isinstance(payload, str):
        if re.match(r"^\s*(?:error\b|failed to\b|no transcripts?\b|could not retrieve\b)", payload, re.I):
            raise ValueError("Transcript provider returned an error message")
        try:
            decoded = json.loads(payload)
        except ValueError:
            decoded = None
        if decoded is not None and not isinstance(decoded, str):
            return normalize(decoded)
        payload = [{"text": payload, "start": 0, "duration": 0}]
    if not isinstance(payload, list):
        raise ValueError("Expected transcript text or segments")
    result = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ValueError("Invalid transcript segment")
        text = html.unescape(re.sub(r"<[^>]+>", "", item["text"])).strip()
        start = float(item.get("start", 0))
        duration = float(item.get("duration", float(item.get("end", start)) - start))
        if not math.isfinite(start + duration) or min(start, duration) < 0:
            raise ValueError("Invalid transcript timing")
        if text and (not result or text != result[-1]["text"]):
            result.append(dict(text=text, start=start, duration=duration))
    if not result or sum(len(x["text"]) for x in result) > 2_000_000:
        raise ValueError("Empty or oversized transcript")
    return result


class TranscriptAPI:
    def __init__(self, config, sleep=time.sleep):
        self.config, self.sleep = config, sleep

    def fetch(self, video_id):
        import requests
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api.proxies import GenericProxyConfig

        timeout = self.config.request_timeout
        class TimedSession(requests.Session):
            def request(self, *args, **kwargs):
                kwargs.setdefault("timeout", timeout)
                response = super().request(*args, **kwargs)
                # The library wraps HTTP failures and discards the status. Preserve
                # transient status here so the outer bounded backoff can handle it.
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(response=response)
                return response

        proxy = os.getenv(self.config.proxy_env) if self.config.proxy_env else None
        with TimedSession() as session:
            api = YouTubeTranscriptApi(http_client=session,
                    proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy) if proxy else None)
            for attempt in range(self.config.retries + 1):
                try:
                    return normalize(api.fetch(video_id, languages=self.config.languages))
                except Exception as exc:
                    # Never retry permanent denial, missing captions, or switch proxies to evade a block.
                    transient = isinstance(exc, (requests.Timeout, requests.ConnectionError))
                    response = getattr(exc, "response", None)
                    if response is not None:
                        transient |= response.status_code == 429 or response.status_code >= 500
                    if not transient or attempt == self.config.retries:
                        raise
                    self.sleep(self.config.backoff_seconds * 2 ** attempt)


def parse_subtitles(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    segments = []
    for event in payload.get("events", []):
        text = "".join(segment.get("utf8", "") for segment in event.get("segs", []))
        if text.strip():
            segments.append(dict(text=text, start=event.get("tStartMs", 0) / 1000,
                                 duration=event.get("dDurationMs", 0) / 1000))
    return normalize(segments)


def subtitle_options(config, directory):
    return dict(skip_download=True, writesubtitles=False, writeautomaticsub=True,
                subtitleslangs=[re.escape(lang) for lang in config.languages], subtitlesformat="json3",
                outtmpl=str(Path(directory) / "captions.%(ext)s"), noplaylist=True,
                quiet=True, no_warnings=True, socket_timeout=config.request_timeout,
                retries=config.retries, fragment_retries=config.retries,
                ignore_no_formats_error=True)


class YtDlp:
    def __init__(self, config, audio=False):
        self.config, self.audio = config, audio

    def fetch(self, video_id):
        # A child process supplies a hard timeout for downloader and optional local inference.
        # Secret-bearing proxy settings use inherited env, never command arguments or logs.
        with tempfile.TemporaryDirectory(prefix="ufc-youtube-") as directory:
            settings = dict(video_id=video_id, audio=self.audio, directory=directory,
                            languages=self.config.languages, request_timeout=self.config.request_timeout,
                            retries=self.config.retries, proxy_env=self.config.proxy_env,
                            whisper_model=self.config.whisper_model,
                            max_audio_seconds=self.config.max_audio_seconds)
            completed = subprocess.run([sys.executable, "-m", "fight_coverage.transcripts"],
                input=json.dumps(settings), text=True, capture_output=True,
                timeout=self.config.whisper_timeout if self.audio else max(60, self.config.request_timeout * 4),
                cwd=Path(__file__).resolve().parents[1], check=False)
            if completed.returncode:
                raise RuntimeError("Audio transcription failed" if self.audio else "Auto subtitles unavailable")
            return normalize(json.loads((Path(directory) / "result.json").read_text(encoding="utf-8")))


def worker(settings):
    from types import SimpleNamespace
    from yt_dlp import YoutubeDL

    cfg = SimpleNamespace(**settings)
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", cfg.video_id):
        raise ValueError("Invalid video ID")
    directory = Path(cfg.directory)
    options = subtitle_options(cfg, directory)
    proxy = os.getenv(cfg.proxy_env) if cfg.proxy_env else None
    if proxy:
        options["proxy"] = proxy
    if cfg.audio:
        options.update(skip_download=False, writesubtitles=False, writeautomaticsub=False,
                       format="bestaudio", outtmpl=str(directory / "audio.%(ext)s"),
                       match_filter=lambda info, *, incomplete=False:
                           "Live/unknown/oversized audio is excluded" if info.get("is_live") or not info.get("duration")
                           or info["duration"] > cfg.max_audio_seconds else None)
    with YoutubeDL(options) as downloader:
        downloader.extract_info("https://www.youtube.com/watch?v=" + cfg.video_id, download=True)
    if cfg.audio:
        import whisper
        files = [p for p in directory.glob("audio.*") if p.suffix not in (".part", ".ytdl")]
        if len(files) != 1:
            raise ValueError("Audio not downloaded")
        # Model weights may be downloaded on first explicit opt-in; inference is local.
        model = whisper.load_model(cfg.whisper_model, device="cpu")
        transcript = normalize(model.transcribe(str(files[0]), fp16=False)["segments"])
    else:
        files = list(directory.glob("captions.*.json3"))
        if not files:
            raise ValueError("No auto-generated subtitles in the requested languages")
        files.sort(key=lambda p: next((i for i, lang in enumerate(cfg.languages)
                                     if p.name == f"captions.{lang}.json3"), len(cfg.languages)))
        transcript = parse_subtitles(files[0])
    (directory / "result.json").write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    worker(json.loads(sys.stdin.read()))

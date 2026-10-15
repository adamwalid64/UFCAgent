"""Discovery uses only official channels.list and playlistItems.list (one unit each)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from .store import QuotaExceeded

LOG = logging.getLogger(__name__)


class MissingAPIKey(ValueError):
    pass


def utc(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def evidence_cutoff(value, event_timezone="UTC"):
    """Calendar dates use local event-day midnight; explicit timestamps keep their meaning."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
        return datetime.fromisoformat(str(value)).replace(tzinfo=ZoneInfo(event_timezone)).astimezone(timezone.utc)
    return utc(value)


def words(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    return " ".join(re.findall(r"[a-z0-9]+", value))


def mentions(text, phrase):
    phrase = words(phrase)
    return bool(phrase) and f" {phrase} " in f" {words(text)} "


def fighter_terms(fight, side):
    name = fight[f"fighter_{side}"]
    surname = name.split()[-1]
    return [name, *([surname] if len(surname) >= 4 else []),
            *fight.get("aliases", {}).get(name, [])]


def matchup_discussed(text, fight, max_gap=1500):
    """Both fighters must occur near each other, including in split captions."""
    text = words(text)
    positions = []
    side_terms = [{words(term) for term in fighter_terms(fight, side) if words(term)}
                  for side in ("a", "b")]
    shared = side_terms[0] & side_terms[1]
    for terms in side_terms:
        terms = terms - shared
        if not terms:
            return False
        pattern = r"\b(?:" + "|".join(re.escape(term) for term in sorted(terms)) + r")\b"
        positions.append([match.start() for match in re.finditer(pattern, text)])
    a, b = positions
    i = j = 0
    while i < len(a) and j < len(b):
        if abs(a[i] - b[j]) <= max_gap:
            return True
        if a[i] < b[j]:
            i += 1
        else:
            j += 1
    return False


def paired_title(text, left, right):
    # Creators use vs., versus, v, &, x, dashes, and reversed bout order.
    text = words(text)
    for a, b in ((words(left), words(right)), (words(right), words(left))):
        if re.search(r"\b" + re.escape(a) + r" (?:vs |versus |v |and |x )?" + re.escape(b) + r"\b", text):
            return True
    return False


def event_matches(text, fight):
    event = fight.get("event_name", "")
    labels = [event, *fight.get("event_aliases", [])]
    if ":" in event:
        prefix, matchup = event.split(":", 1)
        labels.append(matchup.strip())
        if re.fullmatch(r"UFC\s+(?:(?:Vegas|Fight Night)\s+)?\d+", prefix.strip(), re.I):
            labels.append(prefix.strip())
    for label in labels:
        if words(label) in {"ufc", "ufc fight night", "ufc vegas", "mma"}:
            continue
        if mentions(text, label):
            return True
        pair = re.split(r"\s+(?:vs\.?|versus|v\.?)\s+", label, flags=re.I)
        if len(pair) == 2 and paired_title(text, *pair):
            return True
    return False


def card_preview(title):
    title = words(title)
    intent = re.search(r"\b(?:predictions?|picks?|preview|breakdown|betting tips)\b", title)
    context = re.search(r"\b(?:ufc|mma|full card|entire card)\b", title)
    return bool(intent and context)


def match_video(video, fight):
    text = video["title"] + " " + video["description"]
    # A full-card recap/companion is not a pre-fight prediction video.
    if re.search(r"\b(?:recap|results|reaction|watchalong|companion|post fight)\b", words(video["title"])):
        return None
    aliases = fight.get("aliases", {})
    def found(side):
        name = fight[f"fighter_{side}"]
        return any(mentions(text, alias) for alias in [name, *aliases.get(name, [])])
    if found("a") and found("b"):
        return "fight"
    # Titles often omit first names. Require the actual paired matchup phrase,
    # rather than treating two common surnames anywhere in a description as a match.
    a, b = (fight[f"fighter_{side}"].split()[-1] for side in ("a", "b"))
    if paired_title(text, a, b):
        return "fight"
    if event_matches(text, fight):
        return "event"
    if card_preview(video["title"]):
        # This is provisional: transcripts must identify the requested matchup.
        return "card_candidate"
    return None


class Discovery:
    def __init__(self, config, store, http=None, sleep=time.sleep, *, refresh=False):
        self.config, self.store = config, store
        self.http, self.sleep = http or requests.Session(), sleep
        self.refresh = refresh
        self._refreshed = set()

    def request(self, endpoint, params):
        cache_key = hashlib.sha256(json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()
        with self.store.lock("discovery:" + cache_key):
            cached = self.store.get("discovery", cache_key)
            if cached is not None and (not self.refresh or cache_key in self._refreshed):
                return cached
            key = os.getenv(self.config.api_key_env)
            if not key:
                raise MissingAPIKey("Missing YouTube Data API key environment variable")
            for attempt in range(self.config.retries + 1):
                self.store.quota(self.config.daily_quota_units)
                try:
                    response = self.http.get("https://www.googleapis.com/youtube/v3/" + endpoint,
                                             params={**params, "key": key}, timeout=self.config.request_timeout)
                    if response.status_code in (403, 429):
                        try:
                            reasons = {e.get("reason") for e in response.json().get("error", {}).get("errors", [])}
                        except (ValueError, AttributeError):
                            reasons = set()
                        if reasons & {"quotaExceeded", "dailyLimitExceeded"}:
                            self.store.quota(self.config.daily_quota_units, block=True)
                            raise QuotaExceeded("YouTube API project quota exhausted")
                    if response.status_code == 429 or response.status_code >= 500:
                        raise requests.Timeout()
                    if response.status_code != 200:
                        raise ValueError(f"YouTube Data API HTTP {response.status_code}")
                    payload = response.json()
                    if not isinstance(payload.get("items"), list):
                        raise ValueError("Malformed YouTube Data API response")
                    payload["_observed_at"] = self.store.now().isoformat()
                    self.store.put("discovery", cache_key, payload, self.config.discovery_ttl_hours * 3600)
                    self._refreshed.add(cache_key)
                    return payload
                except (requests.Timeout, requests.ConnectionError):
                    if attempt == self.config.retries:
                        raise RuntimeError("YouTube Data API request timed out or failed") from None
                    self.sleep(self.config.backoff_seconds * 2 ** attempt)

    def discover(self, fight, event_date):
        cutoff = evidence_cutoff(event_date, self.config.event_timezone)
        earliest = cutoff - timedelta(days=self.config.lookback_days)
        videos, caveats = {}, []
        LOG.info("YouTube discovery publication window: %s through %s (exclusive)",
                 earliest.isoformat(), cutoff.isoformat())
        for channel in self.config.channels:
            if not channel.active:
                continue
            try:
                selector = {"id": channel.channel_id} if channel.channel_id else {"forHandle": channel.handle}
                info = self.request("channels", {"part": "contentDetails", **selector})
                channel_id = info["items"][0]["id"]
                playlist = info["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
                token = None
                scanned = eligible = matched = candidates = 0
                newest = None
                for page in range(self.config.max_pages_per_channel):
                    params = {"part": "snippet,contentDetails", "playlistId": playlist, "maxResults": 50}
                    if token:
                        params["pageToken"] = token
                    response = self.request("playlistItems", params)
                    for item in response["items"]:
                        scanned += 1
                        try:
                            snippet, detail = item["snippet"], item["contentDetails"]
                            video_id = detail["videoId"]
                            published = detail["videoPublishedAt"]
                            published_time = utc(published)
                            newest = max(newest, published_time) if newest else published_time
                            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) or not earliest <= published_time < cutoff:
                                continue
                            if snippet.get("videoOwnerChannelId", channel_id) != channel_id:
                                continue
                            eligible += 1
                            video = dict(video_id=video_id, channel_id=channel_id,
                                         registry_key=channel.channel_id or channel.handle, channel=channel.name,
                                         title=snippet["title"], description=snippet.get("description", ""),
                                         published_at=published, observed_at=response["_observed_at"])
                            match = match_video(video, fight)
                            if match:
                                matched += match != "card_candidate"
                                candidates += match == "card_candidate"
                                video["match"] = match
                                videos[video_id] = video
                        except (ValueError, KeyError, TypeError):
                            caveats.append(f"{channel.name}: skipped malformed upload metadata.")
                    token = response.get("nextPageToken")
                    if not token:
                        break
                    # Playlist insertion order is not necessarily publication order; do not stop at one old video.
                LOG.info("YouTube discovery channel=%s uploads=%s in_window=%s identified_matches=%s "
                         "card_candidates=%s newest=%s", channel.name, scanned, eligible, matched,
                         candidates, newest.isoformat() if newest else "none")
                if token:
                    caveats.append(f"{channel.name}: discovery page limit reached; coverage may be incomplete.")
            except QuotaExceeded:
                caveats.append("YouTube discovery quota reached; some channels were not checked.")
                break
            except MissingAPIKey:
                caveats.append(f"YouTube discovery unavailable: set {self.config.api_key_env} in .env.")
                break
            except Exception as exc:
                # URLs in HTTP/library exception messages may contain API keys or proxy passwords.
                caveats.append(f"{channel.name}: discovery unavailable ({type(exc).__name__}).")
        priority = {"fight": 2, "event": 1, "card_candidate": 0}
        ordered = sorted(videos.values(), key=lambda v: (priority[v["match"]], utc(v["published_at"])), reverse=True)
        return ordered, list(dict.fromkeys(caveats))

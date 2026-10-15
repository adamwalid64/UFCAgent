"""Optional written/reddit adapters; permitted access and robots checks precede reads."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from .discovery import evidence_cutoff, mentions, utc, words
from .service import FightCoverage
from .signals import analyst_text

USER_AGENT = "UFCAgentSentiment/1.0"


class AccessSkipped(RuntimeError):
    pass


class PermittedHTTP:
    def __init__(self, config, store, session=None, sleep=time.sleep):
        self.config, self.store = config, store
        self.session, self.sleep = session or requests.Session(), sleep

    def _get(self, url, headers=None):
        response = self.session.get(url, headers={"User-Agent": USER_AGENT, **(headers or {})},
                                    timeout=self.config.request_timeout, allow_redirects=False)
        # Do not follow a redirect to evade another origin's terms/robots.
        if response.status_code != 200:
            raise AccessSkipped(f"HTTP {response.status_code}; no automated fallback")
        if len(response.content) > 2_000_000:
            raise AccessSkipped("Response exceeds analysis size limit")
        return response

    def read(self, url, *, allowed_hosts, authorized, terms_url, headers=None):
        if not authorized or not terms_url.startswith("https://"):
            raise AccessSkipped("Automated access not authorized under the site's terms")
        target = urlsplit(url)
        if target.scheme != "https" or target.username or target.password or target.port not in (None, 443):
            raise AccessSkipped("Only registered HTTPS sources are supported")
        if target.hostname not in allowed_hosts:
            raise AccessSkipped("Host is not registered")
        from protego import Protego
        origin = f"https://{target.hostname}"
        # Fresh robots decision before cached content too: a denial revokes reuse.
        robots = self.store.get("robots", origin)
        if robots is None:
            robots = self._get(origin + "/robots.txt").text
            if re.search(r"<html|<!doctype|captcha", robots, re.I):
                raise AccessSkipped("robots.txt returned an unverifiable challenge page")
            self.store.put("robots", origin, robots, 3600)
        rules = Protego.parse(robots)
        for agent in (USER_AGENT, "GPTBot", "ChatGPT-User"):
            if not rules.can_fetch(url, agent):
                raise AccessSkipped("robots.txt disallows automated/AI access")
        key = hashlib.sha256(url.encode()).hexdigest()
        cached = self.store.get("written", key)
        if cached is not None:
            return cached
        delay = rules.crawl_delay(USER_AGENT) or 1
        rate = rules.request_rate(USER_AGENT)
        if rate:
            delay = max(delay, rate.seconds / rate.requests)
        if delay > 30:
            raise AccessSkipped("Crawl delay exceeds this run's request budget")
        self.sleep(max(1, delay))
        response = self._get(url, headers=headers)
        data = {"text": response.text, "fetched_at": self.store.now().isoformat()}
        self.store.put("written", key, data, 3600)
        return data


def matched(text, fight):
    return all(any(mentions(text, alias) for alias in [fight[f"fighter_{s}"],
               fight[f"fighter_{s}"].split()[-1], *fight.get("aliases", {}).get(fight[f"fighter_{s}"], [])])
               for s in ("a", "b"))


def append_document(result, *, source_id, content_id, name, writer, url, weight, text, published_at, fetched_at, layer):
    clean = analyst_text(text)
    if not clean:
        return
    segments = [{"text": p, "start": 0, "duration": 0}
                for p in re.split(r"(?<=[.!?])\s+|\n+", clean) if p.strip()]
    result.sources.append(dict(source_id=source_id, content_id=content_id, name=name, writer=writer,
        type="written prediction" if layer == "written" else "reasoned community analysis",
        url=url, credibility_weight=weight, published_at=published_at, fetched_at=fetched_at,
        layer=layer, cache_hit=False))
    result._analysis.append({"content_id": content_id, "segments": segments})
    result.protect(segments)


class WrittenPicksAdapter:
    def __init__(self, config, store, http=None):
        self.config, self.store = config, store
        self.http = http or PermittedHTTP(config, store)

    def get_fight_coverage(self, fight, event_date):
        result = FightCoverage()
        if not self.config.non_youtube.written_enabled:
            return result
        from bs4 import BeautifulSoup
        cutoff = evidence_cutoff(event_date, self.config.event_timezone)
        for outlet in self.config.non_youtube.outlets:
            if not outlet.active:
                continue
            for url in outlet.urls[:10]:
                try:
                    data = self.http.read(url, allowed_hosts=outlet.allowed_hosts,
                        authorized=outlet.automated_access_allowed, terms_url=outlet.terms_url)
                    if utc(data["fetched_at"]) >= cutoff:
                        raise AccessSkipped("Article first fetched after prediction cutoff")
                    soup = BeautifulSoup(data["text"], "html.parser")
                    published = soup.select_one('meta[property="article:published_time"], time[datetime]')
                    stamp = (published.get("content") or published.get("datetime")) if published else None
                    if not stamp or utc(stamp) >= cutoff:
                        raise AccessSkipped("Missing or post-cutoff article publication time")
                    found = 0
                    for entry in soup.select(outlet.entry_selector):
                        writers = entry.select(outlet.writer_selector)
                        if len(writers) != 1:
                            continue
                        writer = writers[0]
                        author = writer.get_text(" ", strip=True)
                        if not author:
                            continue
                        context = entry.get_text(" ", strip=True)
                        if not matched(context, fight):
                            continue
                        reasons = "\n".join(p.get_text(" ", strip=True) for p in entry.select(outlet.reasoning_selector))
                        pick_node = entry.select_one(outlet.pick_selector)
                        # Explicitly labelled pick text is source evidence, not an inferred outcome.
                        pick_text = pick_node.get_text(" ", strip=True) if pick_node else ""
                        text = "\n".join([pick_text, reasons]).strip()
                        if len(reasons.split()) < 12:
                            continue
                        weight = outlet.writer_weights.get(author, 1.0)
                        if not 0 <= weight <= 10:
                            raise ValueError("Invalid writer credibility weight")
                        source_id = "written:" + urlsplit(url).hostname + ":" + words(author).replace(" ", "-")
                        content_id = hashlib.sha256((url + source_id).encode()).hexdigest()[:24]
                        append_document(result, source_id=source_id, content_id=content_id, name=outlet.name,
                            writer=author, url=url, weight=weight, text=text[:self.config.max_excerpt_chars],
                            published_at=stamp, fetched_at=data["fetched_at"], layer="written")
                        found += 1
                    if not found:
                        result.caveats.append(f"{outlet.name}: no attributable writer reasoning for this matchup; check configured selectors.")
                except AccessSkipped as exc:
                    result.caveats.append(f"{outlet.name}: skipped ({exc}).")
                except Exception as exc:
                    result.caveats.append(f"{outlet.name}: unavailable ({type(exc).__name__}).")
        return result


class RedditAdapter:
    def __init__(self, config, store, http=None):
        self.config, self.store = config, store
        self.http = http or PermittedHTTP(config, store)

    def get_fight_coverage(self, fight, event_date):
        result = FightCoverage()
        cfg = self.config.non_youtube.reddit
        if not cfg.enabled:
            return result
        cutoff = evidence_cutoff(event_date, self.config.event_timezone)
        for url in cfg.thread_urls[:10]:
            try:
                parsed = urlsplit(url)
                path = re.fullmatch(r"/r/(MMA|MMAbetting)/comments/([a-z0-9]+)/?", parsed.path, re.I)
                if parsed.hostname != "oauth.reddit.com" or not path:
                    raise AccessSkipped("Use an approved Reddit OAuth API thread URL for r/MMA or r/MMAbetting")
                token = os.getenv(cfg.token_env)
                if not token:
                    raise AccessSkipped("Reddit API access token is not configured")
                endpoint = f"https://oauth.reddit.com{parsed.path.rstrip('/')}?sort=top&limit=50&depth=1&raw_json=1"
                data = self.http.read(endpoint, allowed_hosts=["oauth.reddit.com"], authorized=cfg.automated_access_allowed,
                    terms_url=cfg.terms_url, headers={"Authorization": "Bearer " + token})
                if utc(data["fetched_at"]) >= cutoff:
                    raise AccessSkipped("Thread first fetched after prediction cutoff")
                listing = json.loads(data["text"])
                thread = listing[0]["data"]["children"][0]["data"]
                if str(thread.get("subreddit", "")).casefold() != path[1].casefold():
                    raise AccessSkipped("Subreddit identity mismatch")
                if not re.search(r"prediction|pre.fight|breakdown|picks?", thread["title"], re.I):
                    raise AccessSkipped("Thread is not a prediction/breakdown discussion")
                if datetime.fromtimestamp(thread["created_utc"], timezone.utc) >= cutoff:
                    raise AccessSkipped("Thread was created after the evidence cutoff")
                comments = [item["data"] for item in listing[1]["data"]["children"] if item["kind"] == "t1"]
                comments.sort(key=lambda c: c.get("score", 0), reverse=True)
                count = 0
                seen_authors = set()
                for comment in comments:
                    body, author = comment.get("body", ""), comment.get("author", "")
                    if (comment.get("score", 0) < cfg.min_score or comment.get("stickied")
                            or author in ("", "[deleted]", "AutoModerator") or author in seen_authors):
                        continue
                    latest = max(comment["created_utc"], float(comment.get("edited") or 0))
                    if datetime.fromtimestamp(latest, timezone.utc) >= cutoff:
                        continue
                    clean = analyst_text(body)
                    if (not matched(clean, fight) or len(clean.split()) < 25
                            or not re.search(r"\b(takedown|wrestling|grappling|striking|cardio|pace|jab|kicks?|clinch|reach|footwork|defen[cs]e)\b", clean, re.I)
                            or not re.search(r"\b(because|since|due to|whereas|against|but|while)\b", clean, re.I)):
                        continue
                    weight = cfg.writer_weights.get(author, cfg.credibility_weight)
                    if not 0 < weight <= 0.5:
                        raise ValueError("Reddit credibility weights must remain low (0 < weight <= 0.5)")
                    permalink = comment.get("permalink", "")
                    if not permalink.startswith(f"/r/{thread['subreddit']}/comments/{thread['id']}/"):
                        continue
                    append_document(result, source_id="reddit:" + author.casefold(), content_id="reddit:" + comment["id"],
                        name="r/" + thread["subreddit"], writer=author, url="https://www.reddit.com" + permalink,
                        weight=weight, text=clean[:self.config.max_excerpt_chars],
                        published_at=datetime.fromtimestamp(comment["created_utc"], timezone.utc).isoformat(),
                        fetched_at=data["fetched_at"], layer="reddit")
                    count += 1
                    seen_authors.add(author)
                    if count >= cfg.top_comments:
                        break
                if not count:
                    result.caveats.append("Reddit: no eligible top comments with matchup reasoning.")
            except AccessSkipped as exc:
                result.caveats.append(f"Reddit: skipped ({exc}).")
            except Exception as exc:
                result.caveats.append(f"Reddit: unavailable ({type(exc).__name__}).")
        return result

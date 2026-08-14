from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from playwright.sync_api import Page, sync_playwright

UFC_EVENTS_URL = "https://www.ufc.com/events"
DEFAULT_REQUEST_DELAY_SECONDS = 1.5


@dataclass
class ScrapeError:
    source_url: str
    step: str
    message: str


@dataclass
class ScrapeSummary:
    event_urls: list[str] = field(default_factory=list)
    fight_urls: list[str] = field(default_factory=list)
    errors: list[ScrapeError] = field(default_factory=list)


class RateLimitedPlaywrightScraper:
    def __init__(self, delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS, headless: bool = True):
        self.delay_seconds = delay_seconds
        self.headless = headless
        self._last_request_at = 0.0

    def _respect_robots(self, url: str) -> bool:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            parser.read()
        except Exception:
            return True
        return parser.can_fetch("*", url)

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay_seconds:
            time.sleep(self.delay_seconds - elapsed)
        self._last_request_at = time.monotonic()

    def _abs_url(self, url: str, base_url: str) -> str:
        return urljoin(base_url, url)

    def _normalize_text(self, value: str | None) -> str | None:
        if value is None:
            return None
        return re.sub(r"\s+", " ", value).strip() or None

    def _find_link_hrefs(self, page: Page, keywords: Iterable[str]) -> list[str]:
        hrefs: list[str] = []
        for selector in ["a[href]", "link[href]"]:
            nodes = page.locator(selector).evaluate_all(
                "(elements) => elements.map((el) => el.getAttribute('href')).filter(Boolean)"
            )
            for href in nodes or []:
                lowered = href.lower()
                if any(keyword.lower() in lowered for keyword in keywords):
                    hrefs.append(href)
        seen: set[str] = set()
        unique: list[str] = []
        for href in hrefs:
            if href not in seen:
                seen.add(href)
                unique.append(href)
        return unique

    def _extract_text_by_selectors(self, page: Page, selectors: Iterable[str]) -> str | None:
        for selector in selectors:
            texts = page.locator(selector).all_text_contents()
            for text in texts:
                cleaned = self._normalize_text(text)
                if cleaned:
                    return cleaned
        return None

    def _extract_event_payload(self, page: Page, event_url: str) -> dict[str, Any]:
        body_text = self._normalize_text(page.locator("body").inner_text()) or ""
        event_name = self._extract_text_by_selectors(
            page,
            [
                "h1",
                ".event-name",
                ".event-title",
                ".page-title",
                ".hero-title",
                "[data-event-name]",
            ],
        )
        event_date = self._extract_text_by_selectors(page, [".event-date", ".date", "time", "[data-event-date]"])
        event_location = self._extract_text_by_selectors(page, [".event-location", ".location", "address", "[data-event-location]"])

        if not event_name:
            for pattern in [r"UFC\s+[^\n\r]+", r"[A-Z][A-Za-z0-9'\-. ]+\s+\d{4}"]:
                match = re.search(pattern, body_text, flags=re.IGNORECASE)
                if match:
                    event_name = match.group(0)
                    break

        script_text = "".join(page.locator("script[type='application/ld+json']").all_text_contents())
        if script_text:
            try:
                parsed = json.loads(script_text)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict) and item.get("@type") in {"SportsEvent", "Event"}:
                            event_name = event_name or item.get("name")
                            event_date = event_date or item.get("startDate")
                            if not event_location:
                                location = item.get("location")
                                if isinstance(location, dict):
                                    event_location = location.get("name") or location.get("address")
            except json.JSONDecodeError:
                pass

        return {
            "event_name": self._normalize_text(event_name) or "Unknown UFC Event",
            "event_date": event_date,
            "event_location": self._normalize_text(event_location) or "Unknown Location",
            "event_url": event_url,
        }

    def _extract_fight_payload(self, page: Page, fight_url: str) -> dict[str, Any]:
        body_text = self._normalize_text(page.locator("body").inner_text()) or ""
        fighter_names = []
        for selector in [
            ".fighter-name",
            ".name",
            "h2",
            "h3",
            "[data-fighter-name]",
            "[itemprop='name']",
        ]:
            values = page.locator(selector).all_text_contents()
            for value in values:
                cleaned = self._normalize_text(value)
                if cleaned:
                    fighter_names.append(cleaned)

        unique_names = []
        for candidate in fighter_names:
            if candidate not in unique_names:
                unique_names.append(candidate)

        fighter_a_name = unique_names[0] if len(unique_names) > 0 else "Unknown Fighter A"
        fighter_b_name = unique_names[1] if len(unique_names) > 1 else "Unknown Fighter B"

        winner_name = None
        for pattern in [
            r"(?:Winner|winner)[:\-]\s*([^\n]+)",
            r"([A-Z][a-zA-Z'\-.]+(?:\s+[A-Z][a-zA-Z'\-.]+)*)\s+won\b",
            r"Won by\s+([A-Z][a-zA-Z'\-.]+(?:\s+[A-Z][a-zA-Z'\-.]+)*)",
        ]:
            match = re.search(pattern, body_text, flags=re.IGNORECASE)
            if match:
                winner_name = self._normalize_text(match.group(1))
                break

        method_of_victory = None
        method_match = re.search(r"(KO/TKO|TKO|Submission|Decision|technical decision|majority decision|unanimous decision)", body_text, flags=re.IGNORECASE)
        if method_match:
            method_of_victory = method_match.group(1).strip()

        ending_round = None
        round_match = re.search(r"Round\s+(\d+)", body_text, flags=re.IGNORECASE)
        if round_match:
            ending_round = int(round_match.group(1))

        ending_time = None
        time_match = re.search(r"(\d+:\d{2})\s*(?:of\s*the\s*round|of\s*round|of\s*the\s*fight)?", body_text, flags=re.IGNORECASE)
        if time_match:
            ending_time = time_match.group(1)

        return {
            "fight_id": fight_url,
            "fighter_a_name": fighter_a_name,
            "fighter_b_name": fighter_b_name,
            "winner_name": winner_name,
            "method_of_victory": method_of_victory,
            "ending_round": ending_round,
            "ending_time": ending_time,
            "source_fight_url": fight_url,
        }

    def fetch_page(self, url: str, timeout_seconds: float = 30.0) -> Page | None:
        if not self._respect_robots(url):
            raise PermissionError(f"robots.txt disallows access to {url}")
        self._rate_limit()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.headless)
            page = browser.new_page(viewport={"width": 1440, "height": 2200})
            page.goto(url, wait_until="networkidle", timeout=int(timeout_seconds * 1000))
            time.sleep(0.5)
            return page

    def crawl_event_listing(self, listing_url: str = UFC_EVENTS_URL, max_events: int | None = None) -> ScrapeSummary:
        summary = ScrapeSummary()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.headless)
                page = browser.new_page(viewport={"width": 1440, "height": 2200})
                self._rate_limit()
                page.goto(listing_url, wait_until="networkidle", timeout=30000)
                time.sleep(1.0)
                hrefs = self._find_link_hrefs(page, ["/event/", "/events/", "event"])
                for href in hrefs:
                    absolute = self._abs_url(href, listing_url)
                    if absolute not in summary.event_urls:
                        summary.event_urls.append(absolute)
                browser.close()
        except Exception as exc:  # pragma: no cover - network issues are handled at the pipeline layer
            summary.errors.append(ScrapeError(source_url=listing_url, step="events_listing", message=str(exc)))
        if max_events is not None:
            summary.event_urls = summary.event_urls[:max_events]
        return summary

    def crawl_event_details(self, event_url: str) -> dict[str, Any]:
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.headless)
                page = browser.new_page(viewport={"width": 1440, "height": 2200})
                self._rate_limit()
                page.goto(event_url, wait_until="networkidle", timeout=30000)
                time.sleep(1.0)
                payload = self._extract_event_payload(page, event_url)
                fight_links = self._find_link_hrefs(page, ["/fight/", "fight-card", "fight-card/", "fight"])
                browser.close()
                payload["fight_urls"] = [self._abs_url(href, event_url) for href in fight_links]
                return payload
        except Exception as exc:
            raise RuntimeError(f"Failed to scrape event page {event_url}: {exc}") from exc

    def crawl_fight_card(self, fight_url: str) -> dict[str, Any]:
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.headless)
                page = browser.new_page(viewport={"width": 1440, "height": 2200})
                self._rate_limit()
                page.goto(fight_url, wait_until="networkidle", timeout=30000)
                time.sleep(1.0)
                payload = self._extract_fight_payload(page, fight_url)
                browser.close()
                return payload
        except Exception as exc:
            raise RuntimeError(f"Failed to scrape fight page {fight_url}: {exc}") from exc

from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Page, sync_playwright

UFC_EVENTS_URL = "http://www.ufcstats.com/statistics/events/completed?page=all"
DEFAULT_REQUEST_DELAY_SECONDS = 1.0
log = logging.getLogger(__name__)


def clean(value: str | None) -> str:
    return " ".join((value or "").split()).strip()


def parse_of(value: str | None) -> tuple[int, int]:
    match = re.search(r"(\d+)\s+of\s+(\d+)", value or "", re.I)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def parse_clock(value: str | None) -> int | None:
    match = re.fullmatch(r"\s*(\d+):(\d{2})\s*", value or "")
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def stable_id(url: str | None) -> str:
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


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
    """Synchronous UFCStats scraper implementing the ingestion contract."""

    def __init__(self, delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS, headless: bool = True):
        self.delay_seconds = max(0.0, delay_seconds)
        self.headless = headless
        self._last_request_at = 0.0
        self._event_metadata: dict[str, dict[str, str]] = {}
        self._playwright = None
        self._browser = None
        self._page: Page | None = None

    def _rate_limit(self) -> None:
        remaining = self.delay_seconds - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _open(self, page: Page, url: str) -> None:
        self._rate_limit()
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # UFCStats can perform a late HTTP/HTTPS navigation after DOMContentLoaded.
        # Waiting for a quiet network prevents locators from binding to the old document.
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            page.wait_for_timeout(500)

    def _ensure_page(self) -> Page:
        """Reuse one browser and page, matching the fast drop-in scraper."""
        if self._page is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
            context = self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                )
            )
            self._page = context.new_page()
            log.debug("Started reusable Chromium scraping session")
        return self._page

    def close(self) -> None:
        """Release the reusable browser session. Safe to call repeatedly."""
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._page = None
        self._browser = None
        self._playwright = None
        log.debug("Closed Chromium scraping session")

    @staticmethod
    def _pair(cell) -> tuple[str, str]:
        values = [clean(v) for v in cell.locator("p").all_text_contents()]
        if len(values) >= 2:
            return values[0], values[1]
        value = clean(cell.inner_text())
        return value, value

    def crawl_event_listing(self, listing_url: str = UFC_EVENTS_URL, max_events: int | None = None) -> ScrapeSummary:
        summary = ScrapeSummary()
        try:
            log.info("Fetching completed-events listing...")
            page = self._ensure_page()
            self._open(page, listing_url)
            page.wait_for_selector("tr.b-statistics__table-row", timeout=30_000)
            # Read all 700+ rows in one browser call. Per-row Playwright calls
            # add tens of seconds of protocol overhead on the full listing.
            rows = page.locator("tr.b-statistics__table-row").evaluate_all(
                """rows => rows.map(row => {
                    const link = row.querySelector('a.b-link_style_black');
                    const cells = row.querySelectorAll('td');
                    const date = row.querySelector('span.b-statistics__date');
                    return link ? {
                        url: link.getAttribute('href') || '',
                        name: link.textContent || '',
                        date: date ? date.textContent || '' : '',
                        location: cells.length > 1 ? cells[1].textContent || '' : ''
                    } : null;
                }).filter(Boolean)"""
            )
            for row in rows:
                url = clean(row["url"])
                self._event_metadata[url] = {
                    "event_name": clean(row["name"]),
                    "event_date": clean(row["date"]),
                    "event_location": clean(row["location"]),
                }
                summary.event_urls.append(url)
            log.info("Found %s completed events", len(summary.event_urls))
        except Exception as exc:
            summary.errors.append(ScrapeError(listing_url, "events_listing", str(exc)))
        if max_events is not None:
            summary.event_urls = summary.event_urls[:max_events]
        return summary

    def crawl_event_details(self, event_url: str) -> dict[str, Any]:
        page = self._ensure_page()
        self._open(page, event_url)
        page.wait_for_selector("tr.b-fight-details__table-row", timeout=15_000)
        meta = dict(self._event_metadata.get(event_url, {}))
        title = page.locator("h2.b-content__title")
        if title.count():
            meta["event_name"] = clean(title.first.inner_text())
        for item in page.locator("li.b-list__box-list-item").all_text_contents():
            value = clean(item)
            if value.lower().startswith("date:"):
                meta["event_date"] = value.split(":", 1)[1].strip()
            elif value.lower().startswith("location:"):
                meta["event_location"] = value.split(":", 1)[1].strip()
        fight_urls = [clean(row.get_attribute("data-link")) for row in page.locator("tr.b-fight-details__table-row[data-link]").all()]
        log.debug("Event %s contains %s fights", meta.get("event_name") or event_url, len(fight_urls))
        return {**meta, "event_url": event_url, "fight_urls": [url for url in fight_urls if url]}

    def crawl_fight_card(self, fight_url: str) -> dict[str, Any]:
        page = self._ensure_page()
        self._open(page, fight_url)
        return self._extract_fight(page, fight_url)

    def _extract_fight(self, page: Page, fight_url: str) -> dict[str, Any]:
        page.wait_for_selector("h3.b-fight-details__person-name", timeout=15_000)
        fighter_links = page.locator("h3.b-fight-details__person-name a")
        names = [clean(v) for v in fighter_links.all_text_contents()]
        urls = [clean(fighter_links.nth(i).get_attribute("href")) for i in range(fighter_links.count())]
        if len(names) < 2:
            raise ValueError(f"Could not find two fighters on {fight_url}")
        statuses = [clean(v).lower() for v in page.locator("i.b-fight-details__person-status").all_text_contents()]
        winner_index = next((i for i, value in enumerate(statuses[:2]) if value == "w"), None)
        metadata: dict[str, str] = {}
        for item in page.locator("i.b-fight-details__text-item").all_text_contents():
            value = clean(item)
            if ":" in value:
                key, val = value.split(":", 1)
                metadata[key.strip().lower().replace(" ", "_")] = val.strip()
        method_detail = ""
        for value in page.locator("p.b-fight-details__text").all_text_contents():
            if clean(value).lower().startswith("details:"):
                method_detail = clean(value).split(":", 1)[1].strip()

        general_cols = ["fighter", "kd", "sig_str", "sig_str_pct", "total_str", "td", "td_pct", "sub_att", "rev", "ctrl"]
        sig_cols = ["fighter", "sig_str", "sig_str_pct", "head", "body", "leg", "distance", "clinch", "ground"]
        tables = page.locator("table")
        totals = self._parse_table(tables.nth(0), general_cols)[0] if tables.count() else ({}, {})
        sig_totals = self._parse_table(tables.nth(2), sig_cols)[0] if tables.count() >= 3 else ({}, {})
        general_rounds = self._parse_table(tables.nth(1), general_cols) if tables.count() >= 2 else []
        sig_rounds = self._parse_table(tables.nth(3), sig_cols) if tables.count() >= 4 else []

        rounds = []
        for i, pair in enumerate(general_rounds):
            sig_pair = sig_rounds[i] if i < len(sig_rounds) else ({}, {})
            rounds.append({"round_number": i + 1, "fighter_a": self._stats(pair[0], sig_pair[0]), "fighter_b": self._stats(pair[1], sig_pair[1])})
        round_raw = metadata.get("round", "")
        result = "draw" if "d" in statuses else ("no_contest" if "nc" in statuses else "completed")
        return {"fight_id": stable_id(fight_url), "fighter_a_name": names[0], "fighter_b_name": names[1],
                "fighter_a_id": stable_id(urls[0]), "fighter_b_id": stable_id(urls[1]), "fighter_a_url": urls[0], "fighter_b_url": urls[1],
                "winner_name": names[winner_index] if winner_index is not None else None, "result": result,
                "method_of_victory": metadata.get("method"), "method_detail": method_detail, "ending_round": int(round_raw) if round_raw.isdigit() else None,
                "ending_time": metadata.get("time"), "time_format": metadata.get("time_format"), "referee": metadata.get("referee"),
                "fighter_a_stats": self._stats(totals[0], sig_totals[0]), "fighter_b_stats": self._stats(totals[1], sig_totals[1]),
                "rounds": rounds, "source_fight_url": fight_url}

    def _parse_table(self, table, columns: list[str]) -> list[tuple[dict[str, str], dict[str, str]]]:
        results = []
        for row in table.locator("tbody tr").all():
            cells = row.locator("td")
            if cells.count() < 2:
                continue
            a: dict[str, str] = {}
            b: dict[str, str] = {}
            for i, column in enumerate(columns[:cells.count()]):
                a[column], b[column] = self._pair(cells.nth(i))
            results.append((a, b))
        return results

    @staticmethod
    def _stats(general: dict[str, str], sig: dict[str, str]) -> dict[str, Any]:
        sig_l, sig_a = parse_of(general.get("sig_str"))
        total_l, total_a = parse_of(general.get("total_str"))
        td_l, td_a = parse_of(general.get("td"))
        return {"knockdowns": int(general.get("kd") or 0), "significant_strikes_landed": sig_l, "significant_strikes_attempted": sig_a,
                "total_strikes_landed": total_l, "total_strikes_attempted": total_a, "takedowns_landed": td_l, "takedowns_attempted": td_a,
                "submission_attempts": int(general.get("sub_att") or 0), "reversals": int(general.get("rev") or 0), "control_time_seconds": parse_clock(general.get("ctrl")),
                "head": sig.get("head"), "body": sig.get("body"), "leg": sig.get("leg"), "distance": sig.get("distance"), "clinch": sig.get("clinch"), "ground": sig.get("ground")}

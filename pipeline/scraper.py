from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from playwright.sync_api import Page, sync_playwright

UFC_EVENTS_URL = "http://www.ufcstats.com/statistics/events/completed?page=all"
DEFAULT_REQUEST_DELAY_SECONDS = 1.0
DEFAULT_MAX_ATTEMPTS = 3
log = logging.getLogger(__name__)


EVENT_METHOD_NAMES = {
    "KO/TKO": "KO/TKO",
    "SUB": "Submission",
    "U-DEC": "Decision - Unanimous",
    "S-DEC": "Decision - Split",
    "M-DEC": "Decision - Majority",
    "DQ": "DQ",
    "CNC": "Could Not Continue",
    "OVERTURNED": "Overturned",
}

SIGNIFICANT_STRIKE_SPLITS = ("head", "body", "leg", "distance", "clinch", "ground")
STATS_PARSE_COMPLETE = "complete"
STATS_PARSE_UNAVAILABLE = "unavailable"
STATS_PARSE_PARTIAL = "partial"
NO_ROUND_STATS_MESSAGE = "round-by-round stats not currently available."
FIGHTER_PROFILE_FIELDS = frozenset({"height", "weight", "reach", "stance", "dob"})


def clean(value: str | None) -> str:
    return " ".join((value or "").split()).strip()


def parse_of(value: str | None) -> tuple[int, int]:
    raw = clean(value)
    match = re.fullmatch(r"(\d+)\s+of\s+(\d+)", raw, re.I)
    if not match:
        raise ValueError(f"Malformed non-missing 'landed of attempted' stat: {raw!r}")
    landed, attempted = int(match.group(1)), int(match.group(2))
    if landed > attempted:
        raise ValueError(
            f"Stat has landed greater than attempted: {landed} of {attempted}"
        )
    return landed, attempted


def parse_clock(value: str | None) -> int | None:
    raw = clean(value)
    if raw.lower() in {"--", "n/a", "na"}:
        return None
    match = re.fullmatch(r"(\d+):(\d{2})", raw)
    if not match or int(match.group(2)) >= 60:
        raise ValueError(f"Malformed non-missing control time: {raw!r}")
    return int(match.group(1)) * 60 + int(match.group(2))


def parse_count(value: str | None, *, label: str) -> int:
    raw = clean(value)
    if not re.fullmatch(r"\d+", raw):
        raise ValueError(f"Malformed non-missing {label}: {raw!r}")
    return int(raw)


def stable_id(url: str | None) -> str:
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


def validate_loaded_source_identity(requested_url: str, loaded_url: str, resource: str) -> str:
    """Allow protocol redirects but reject navigation to a different source object."""

    requested_id = stable_id(clean(requested_url))
    loaded_id = stable_id(clean(loaded_url))
    if not requested_id or not loaded_id or requested_id != loaded_id:
        raise ValueError(
            f"{resource} identity mismatch: requested {requested_id!r}, loaded {loaded_id!r}"
        )
    return loaded_id


def parse_labeled_metadata(items: list[str]) -> dict[str, str]:
    """Parse UFCStats labels such as ``Method: KO/TKO`` without depending on item class variants."""
    metadata: dict[str, str] = {}
    for item in items:
        value = clean(item)
        if ":" not in value:
            continue
        key, val = value.split(":", 1)
        metadata[key.strip().lower().replace(" ", "_")] = val.strip()
    return metadata


def normalize_event_method(value: str | None) -> str | None:
    """Expand the abbreviated method shown on event pages to the fight-page spelling."""
    raw = clean(value)
    if not raw or raw == "--":
        return None
    return EVENT_METHOD_NAMES.get(raw.upper(), raw)


def _header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", clean(value).lower()).strip("_")


def _primary_cell_text(cell: dict[str, Any]) -> str:
    paragraphs = [clean(value) for value in cell.get("paragraphs", []) if clean(value)]
    return paragraphs[0] if paragraphs else clean(cell.get("text"))


def parse_event_fight_summary(
    headers: list[str],
    cells: list[dict[str, Any]],
    fight_url: str,
    card_order: int,
) -> dict[str, Any]:
    """Parse one event-table row. ``card_order=1`` is the top (main-event) row."""
    indexes = {_header_key(header): index for index, header in enumerate(headers)}
    missing = {key for key in ("weight_class", "method") if key not in indexes}
    if missing:
        raise ValueError(f"Event table is missing expected column(s): {', '.join(sorted(missing))}")
    if len(cells) <= max(indexes["weight_class"], indexes["method"]):
        raise ValueError(f"Event fight row has {len(cells)} cells for {len(headers)} headers")

    weight_class = _primary_cell_text(cells[indexes["weight_class"]]) or None
    event_method_code = _primary_cell_text(cells[indexes["method"]]) or None
    return {
        "fight_id": stable_id(fight_url),
        "fight_url": clean(fight_url),
        "weight_class": weight_class,
        "method_of_victory": normalize_event_method(event_method_code),
        "event_method_code": event_method_code,
        "card_order": card_order,
    }


def _none_if_missing(value: str | None) -> str | None:
    result = clean(value)
    return None if not result or result.lower() in {"--", "n/a", "na"} else result


def _parse_height_inches(value: str | None) -> int | None:
    value = _none_if_missing(value)
    if value is None:
        return None
    match = re.fullmatch(r"(\d+)\s*'\s*(\d+)\s*\"?", value)
    if not match:
        raise ValueError(f"Malformed non-missing fighter height: {value!r}")
    feet, inches = int(match.group(1)), int(match.group(2))
    if inches >= 12:
        raise ValueError(f"Malformed non-missing fighter height: {value!r}")
    return feet * 12 + inches


def _parse_reach_inches(value: str | None) -> int | None:
    value = _none_if_missing(value)
    if value is None:
        return None
    match = re.fullmatch(r"(\d+)(?:\.0+)?\s*\"?", value)
    if not match:
        raise ValueError(f"Malformed non-missing fighter reach: {value!r}")
    return int(match.group(1))


def _parse_profile_date(value: str | None) -> date | None:
    value = _none_if_missing(value)
    if value is None:
        return None
    match = re.fullmatch(r"([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{4})", value)
    months = {month: index for index, month in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
    )}
    if not match or match.group(1).title() not in months:
        raise ValueError(f"Malformed non-missing fighter DOB: {value!r}")
    try:
        return date(int(match.group(3)), months[match.group(1).title()], int(match.group(2)))
    except ValueError as exc:
        raise ValueError(f"Malformed non-missing fighter DOB: {value!r}") from exc


def parse_fighter_profile_values(
    fighter_url: str,
    fighter_name: str,
    nickname: str | None,
    bio_items: list[str],
) -> dict[str, Any]:
    """Convert the profile bio box into normalized database-ready values."""
    source_url = clean(fighter_url)
    fighter_id = stable_id(source_url)
    if not re.fullmatch(r"[0-9a-f]{16}", fighter_id, re.I):
        raise ValueError(f"Malformed UFCStats fighter profile URL: {fighter_url!r}")
    normalized_name = clean(fighter_name)
    if not normalized_name:
        raise ValueError(f"Fighter profile {source_url} has no fighter name")

    values: dict[str, str] = {}
    for item in bio_items:
        value = clean(item)
        if ":" not in value:
            continue
        key, raw = value.split(":", 1)
        normalized_key = _header_key(key)
        if normalized_key in values:
            raise ValueError(
                f"Fighter profile {source_url} repeats bio field {normalized_key!r}"
            )
        values[normalized_key] = raw.strip()
    missing_fields = FIGHTER_PROFILE_FIELDS - values.keys()
    if missing_fields:
        raise ValueError(
            f"Fighter profile {source_url} is missing bio field(s): "
            f"{', '.join(sorted(missing_fields))}"
        )
    return {
        "fighter_id": fighter_id,
        "fighter_name": normalized_name,
        "fighter_nickname": _none_if_missing(nickname),
        "date_of_birth": _parse_profile_date(values.get("dob")),
        "height_inches": _parse_height_inches(values.get("height")),
        "reach_inches": _parse_reach_inches(values.get("reach")),
        "stance": _none_if_missing(values.get("stance")),
        "profile_url": source_url,
    }


def classify_stats_parse(
    *,
    table_count: int,
    general_total_count: int,
    general_round_count: int,
    significant_total_count: int,
    significant_round_count: int,
    ending_round: int | None,
    unavailable_marker: bool,
    table_shapes_valid: bool = True,
    fighter_alignment_valid: bool = True,
    stat_values_aligned: bool = True,
) -> tuple[str, str]:
    """Classify whether a fight page can authoritatively replace stored stats."""

    if table_count == 0 and unavailable_marker:
        return STATS_PARSE_UNAVAILABLE, "UFCStats explicitly reports round stats unavailable"

    issues: list[str] = []
    if table_count != 4:
        issues.append(f"expected 4 stats tables, found {table_count}")
    if general_total_count != 1:
        issues.append(f"expected 1 general-total row, found {general_total_count}")
    if significant_total_count != 1:
        issues.append(f"expected 1 significant-total row, found {significant_total_count}")
    if not table_shapes_valid:
        issues.append("one or more stats rows are missing expected columns")
    if not fighter_alignment_valid:
        issues.append("fighter order is not aligned across stats tables/rounds")
    if not stat_values_aligned:
        issues.append("significant-strike totals are not aligned across paired tables")
    if not isinstance(ending_round, int) or isinstance(ending_round, bool) or ending_round < 1:
        issues.append(f"invalid ending round {ending_round!r}")
    else:
        if general_round_count != ending_round:
            issues.append(
                f"general round rows ({general_round_count}) do not match ending round ({ending_round})"
            )
        if significant_round_count != ending_round:
            issues.append(
                f"significant round rows ({significant_round_count}) do not match ending round ({ending_round})"
            )
    if general_round_count != significant_round_count:
        issues.append(
            f"general/significant round rows differ ({general_round_count}/{significant_round_count})"
        )
    if unavailable_marker:
        issues.append("unavailable-stats marker conflicts with parsed tables")

    if issues:
        return STATS_PARSE_PARTIAL, "; ".join(issues)
    return STATS_PARSE_COMPLETE, "all four stats tables and round rows are aligned"


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

    def __init__(
        self,
        delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
        headless: bool = True,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ):
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self.delay_seconds = max(0.0, delay_seconds)
        self.headless = headless
        self.max_attempts = max_attempts
        self._last_request_at = 0.0
        self._event_metadata: dict[str, dict[str, str]] = {}
        self._fight_metadata: dict[str, dict[str, Any]] = {}
        self._playwright = None
        self._browser = None
        self._page: Page | None = None

    def _rate_limit(self) -> None:
        remaining = self.delay_seconds - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _open(self, page: Page, url: str, required_selector: str | None = None) -> None:
        """Load a source page with bounded retries and an optional DOM readiness check."""

        for attempt in range(1, self.max_attempts + 1):
            try:
                self._rate_limit()
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                # UFCStats can perform a late HTTP/HTTPS navigation after DOMContentLoaded.
                # Waiting for a quiet network prevents locators from binding to the old document.
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    page.wait_for_timeout(500)
                if required_selector:
                    page.wait_for_selector(required_selector, timeout=30_000)
                return
            except Exception as exc:
                if attempt >= self.max_attempts:
                    raise RuntimeError(
                        f"Failed to load {url} after {self.max_attempts} attempts: {exc}"
                    ) from exc
                log.warning(
                    "Page load failed [%s/%s] %s: %s; retrying",
                    attempt,
                    self.max_attempts,
                    url,
                    exc,
                )

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
            self._open(page, listing_url, "tr.b-statistics__table-row")
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
        self._open(page, event_url, "tr.b-fight-details__table-row[data-link]")
        validate_loaded_source_identity(event_url, page.url, "Event page")
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
        headers = [clean(value) for value in page.locator("thead.b-fight-details__table-head th").all_text_contents()]
        raw_rows = page.locator("tr.b-fight-details__table-row[data-link]").evaluate_all(
            """rows => rows.map(row => ({
                fight_url: row.getAttribute('data-link') || '',
                cells: Array.from(row.querySelectorAll(':scope > td')).map(cell => ({
                    text: cell.textContent || '',
                    paragraphs: Array.from(cell.querySelectorAll(':scope > p')).map(p => p.textContent || '')
                }))
            }))"""
        )
        fight_summaries = []
        for card_order, row in enumerate(raw_rows, 1):
            fight_url = clean(row.get("fight_url"))
            if not fight_url:
                continue
            summary = parse_event_fight_summary(headers, row.get("cells") or [], fight_url, card_order)
            fight_summaries.append(summary)
            self._fight_metadata[summary["fight_id"]] = summary
        fight_urls = [summary["fight_url"] for summary in fight_summaries]
        log.debug("Event %s contains %s fights", meta.get("event_name") or event_url, len(fight_urls))
        return {**meta, "event_url": event_url, "fight_urls": fight_urls, "fight_summaries": fight_summaries}

    def crawl_fighter_profile(self, fighter_url: str) -> dict[str, Any]:
        """Fetch one fighter profile, preserving source nulls as ``None`` instead of zeroes."""
        page = self._ensure_page()
        self._open(page, fighter_url, "span.b-content__title-highlight")
        validate_loaded_source_identity(fighter_url, page.url, "Fighter profile")
        bio_list = page.locator("ul.b-list__box-list:not(.b-list__box-list_margin-top)")
        if not bio_list.count():
            raise ValueError(f"Could not find fighter bio fields on {fighter_url}")
        fighter_name = clean(page.locator("span.b-content__title-highlight").first.inner_text())
        if not fighter_name:
            raise ValueError(f"Could not find fighter name on {fighter_url}")
        nickname_locator = page.locator("p.b-content__Nickname")
        nickname = clean(nickname_locator.first.inner_text()) if nickname_locator.count() else None
        bio_items = bio_list.first.locator(":scope > li.b-list__box-list-item").all_text_contents()
        return parse_fighter_profile_values(page.url, fighter_name, nickname, bio_items)

    def crawl_fight_card(self, fight_url: str) -> dict[str, Any]:
        page = self._ensure_page()
        self._open(page, fight_url, "h3.b-fight-details__person-name")
        validate_loaded_source_identity(fight_url, page.url, "Fight page")
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
        # Method and the other metadata use two sibling class names on UFCStats.
        # Scope to the first paragraph so score/detail items cannot overwrite them.
        metadata_paragraph = page.locator("p.b-fight-details__text").first
        metadata = parse_labeled_metadata(metadata_paragraph.locator(
            ":scope > i.b-fight-details__text-item, :scope > i.b-fight-details__text-item_first"
        ).all_text_contents())
        method_detail = ""
        for value in page.locator("p.b-fight-details__text").all_text_contents():
            if clean(value).lower().startswith("details:"):
                method_detail = clean(value).split(":", 1)[1].strip()

        general_cols = ["fighter", "kd", "sig_str", "sig_str_pct", "total_str", "td", "td_pct", "sub_att", "rev", "ctrl"]
        sig_cols = ["fighter", "sig_str", "sig_str_pct", "head", "body", "leg", "distance", "clinch", "ground"]
        tables = page.locator("table")
        table_count = tables.count()
        total_rows = self._parse_table(tables.nth(0), general_cols) if table_count >= 1 else []
        general_rounds = self._parse_table(tables.nth(1), general_cols) if table_count >= 2 else []
        sig_total_rows = self._parse_table(tables.nth(2), sig_cols) if table_count >= 3 else []
        sig_rounds = self._parse_table(tables.nth(3), sig_cols) if table_count >= 4 else []
        totals = total_rows[0] if len(total_rows) == 1 else ({}, {})
        sig_totals = sig_total_rows[0] if len(sig_total_rows) == 1 else ({}, {})

        round_raw = metadata.get("round", "")
        ending_round = int(round_raw) if round_raw.isdigit() else None
        all_rows = total_rows + general_rounds
        all_sig_rows = sig_total_rows + sig_rounds
        table_shapes_valid = all(
            set(general_cols).issubset(side)
            for pair in all_rows
            for side in pair
        ) and all(
            set(sig_cols).issubset(side)
            for pair in all_sig_rows
            for side in pair
        )
        expected_fighters = (
            tuple(clean(side.get("fighter")).casefold() for side in total_rows[0])
            if len(total_rows) == 1
            else None
        )
        fighter_alignment_valid = bool(expected_fighters and all(expected_fighters)) and all(
            tuple(clean(side.get("fighter")).casefold() for side in pair) == expected_fighters
            for pair in all_rows + all_sig_rows
        )
        stat_values_aligned = len(all_rows) == len(all_sig_rows) and all(
            clean(general_pair[side].get(key)) == clean(sig_pair[side].get(key))
            for general_pair, sig_pair in zip(all_rows, all_sig_rows)
            for side in (0, 1)
            for key in ("sig_str", "sig_str_pct")
        )
        body_text = clean(page.locator("body").inner_text()).casefold()
        stats_parse_status, stats_parse_detail = classify_stats_parse(
            table_count=table_count,
            general_total_count=len(total_rows),
            general_round_count=len(general_rounds),
            significant_total_count=len(sig_total_rows),
            significant_round_count=len(sig_rounds),
            ending_round=ending_round,
            unavailable_marker=NO_ROUND_STATS_MESSAGE in body_text,
            table_shapes_valid=table_shapes_valid,
            fighter_alignment_valid=fighter_alignment_valid,
            stat_values_aligned=stat_values_aligned,
        )
        rounds = []
        if stats_parse_status == STATS_PARSE_COMPLETE:
            for i, (pair, sig_pair) in enumerate(zip(general_rounds, sig_rounds), 1):
                rounds.append(
                    {
                        "round_number": i,
                        "fighter_a": self._stats(pair[0], sig_pair[0]),
                        "fighter_b": self._stats(pair[1], sig_pair[1]),
                    }
                )
        result = "draw" if "d" in statuses else ("no_contest" if "nc" in statuses else "completed")
        event_summary = self._fight_metadata.get(stable_id(fight_url), {})
        return {"fight_id": stable_id(fight_url), "fighter_a_name": names[0], "fighter_b_name": names[1],
                "fighter_a_id": stable_id(urls[0]), "fighter_b_id": stable_id(urls[1]), "fighter_a_url": urls[0], "fighter_b_url": urls[1],
                "winner_name": names[winner_index] if winner_index is not None else None, "result": result,
                "method_of_victory": metadata.get("method") or event_summary.get("method_of_victory"), "method_detail": method_detail, "ending_round": ending_round,
                "ending_time": metadata.get("time"), "time_format": metadata.get("time_format"), "referee": metadata.get("referee"),
                "weight_class": event_summary.get("weight_class"), "event_method_code": event_summary.get("event_method_code"),
                "card_order": event_summary.get("card_order"),
                "fighter_a_stats": self._stats(totals[0], sig_totals[0]) if stats_parse_status == STATS_PARSE_COMPLETE else {},
                "fighter_b_stats": self._stats(totals[1], sig_totals[1]) if stats_parse_status == STATS_PARSE_COMPLETE else {},
                "rounds": rounds if stats_parse_status != STATS_PARSE_UNAVAILABLE else [],
                "stats_parse_status": stats_parse_status, "stats_parse_detail": stats_parse_detail,
                "source_fight_url": fight_url}

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
        split_values = {name: parse_of(sig.get(name)) for name in SIGNIFICANT_STRIKE_SPLITS}
        split_payload = {
            name: {"landed": landed, "attempted": attempted}
            for name, (landed, attempted) in split_values.items()
        }
        return {"knockdowns": parse_count(general.get("kd"), label="knockdown count"), "significant_strikes_landed": sig_l, "significant_strikes_attempted": sig_a,
                "total_strikes_landed": total_l, "total_strikes_attempted": total_a, "takedowns_landed": td_l, "takedowns_attempted": td_a,
                "submission_attempts": parse_count(general.get("sub_att"), label="submission-attempt count"),
                "reversals": parse_count(general.get("rev"), label="reversal count"), "control_time_seconds": parse_clock(general.get("ctrl")),
                "head": sig.get("head"), "body": sig.get("body"), "leg": sig.get("leg"), "distance": sig.get("distance"), "clinch": sig.get("clinch"), "ground": sig.get("ground"),
                "significant_strike_splits": split_payload}

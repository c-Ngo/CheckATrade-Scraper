"""
Checkatrade Web Scraper
=======================
Scrapes trader profiles from Checkatrade search results, extracting
membership dates, contact info, owner details, company type, ratings,
locations, and trade categories. Exports to CSV.

Usage:
    python scraper.py
    python scraper.py --search-path "/Search/Plumber/in/Manchester"
    python scraper.py --max-pages 10 --no-headless
    python scraper.py --help
"""

import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys
from datetime import datetime
from urllib.parse import urljoin

from playwright.async_api import async_playwright, TimeoutError as PwTimeout
from playwright_stealth import Stealth
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table
from rich.panel import Panel
from rich import box

import config

# ── Fix Windows console encoding for Rich & Unicode characters ───────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console(force_terminal=True, legacy_windows=False)

CSV_COLUMNS = [
    "company_name",
    "profile_url",
    "member_since",
    "location",
    "phone_number",
    "owner",
    "company_type",
    "rating",
    "review_count",
    "trade_categories",
]


# ── Helpers ───────────────────────────────────────────────────────────
async def random_delay():
    """Wait a random amount of time between requests."""
    delay = random.uniform(config.DELAY_MIN, config.DELAY_MAX)
    await asyncio.sleep(delay)


def sanitise(text: str | None) -> str:
    """Collapse whitespace and strip a string."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def format_phone_number(raw: str | None) -> str:
    """Normalize phone numbers to clean standard UK format."""
    if not raw:
        return ""
    cleaned = raw.replace("tel:", "").replace("+44", "0").strip()
    return sanitise(cleaned)


def format_member_since(raw: str | None) -> str:
    """Convert raw text or ISO date to 'Month Year' (or 'Year') format."""
    if not raw:
        return ""
    raw = sanitise(raw)
    
    # Check for "Approved member since Month Year" or "Month Year"
    match = re.search(
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}",
        raw,
        re.IGNORECASE,
    )
    if match:
        return match.group(0)

    # Check for ISO date like "2020-02-15"
    iso_match = re.match(r"(\d{4})-(\d{2})", raw)
    if iso_match:
        try:
            d = datetime.strptime(f"{iso_match.group(1)}-{iso_match.group(2)}", "%Y-%m")
            return d.strftime("%B %Y")
        except ValueError:
            pass

    # Check for Year only like "2024"
    year_match = re.search(r"\b(19\d\d|20\d\d)\b", raw)
    if year_match:
        return year_match.group(1)

    return raw


# ── Renewal window calculation ────────────────────────────────────────
MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


def get_renewal_months() -> list[tuple[str, int]]:
    """Calculate the two target (month, year) pairs for first-year renewal leads.

    Returns the (month_name, year) tuples that are exactly 10 and 11 months
    before the current month. For example, in September 2026 the targets are
    (november, 2025) and (october, 2025).

    Only first-year renewals are targeted — both the month AND year must match.
    """
    now = datetime.now()
    targets = []
    for offset in [10, 11]:
        # Subtract offset months from the current date
        # month is 1-12, so we do modular arithmetic
        total_months = (now.month - 1) - offset  # 0-indexed month minus offset
        target_month_idx = total_months % 12       # 0-indexed result
        # Year wraps back when we cross January
        target_year = now.year + (total_months // 12)
        targets.append((MONTH_NAMES[target_month_idx], target_year))
    return targets


def format_renewal_targets(targets: list[tuple[str, int]]) -> str:
    """Format a list of (month, year) tuples for display, e.g. 'November 2025 or October 2025'."""
    return " or ".join(f"{m.title()} {y}" for m, y in targets)


def member_since_matches_months(member_since: str, targets: list[tuple[str, int]]) -> bool:
    """Check if a 'Month Year' member-since string matches any of the target (month, year) pairs.

    Only exact month+year matches count, so traders who joined in the same month
    but a different year will be filtered out (first-year renewals only).
    """
    if not member_since:
        return False
    # Extract month name and year from strings like "October 2025"
    match = re.match(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
        member_since,
        re.IGNORECASE,
    )
    if match:
        month = match.group(1).lower()
        year = int(match.group(2))
        return any(month == tm and year == ty for tm, ty in targets)
    return False


# ── Scraper Class ─────────────────────────────────────────────────────
class CheckatradeScraper:
    """Orchestrates scraping of Checkatrade search results and trader profiles."""

    def __init__(self, search_path: str, max_pages: int, headless: bool, filter_months: list[str] | None = None):
        self.search_path = search_path
        self.max_pages = max_pages
        self.headless = headless
        self.filter_months = filter_months  # list of lowercase month names or None
        self.results: list[dict] = []
        self.filtered_count: int = 0        # traders skipped by renewal filter

    # ── Search-results pages ──────────────────────────────────────────
    async def collect_profile_urls(self, page) -> list[str]:
        """Iterate through search-result pages and collect unique profile URLs."""
        urls: list[str] = []
        seen: set[str] = set()
        consecutive_empty = 0  # track consecutive pages with no new traders
        max_consecutive_empty = 5  # stop after this many empty pages in a row

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Scanning search pages...", total=self.max_pages)

            for page_num in range(1, self.max_pages + 1):
                search_url = (
                    f"{config.BASE_URL}{self.search_path}?page={page_num}"
                )
                console.print(
                    f"  [dim]Page {page_num}/{self.max_pages}:[/dim] {search_url}"
                )

                try:
                    await page.goto(
                        search_url,
                        wait_until="domcontentloaded",
                        timeout=config.NAVIGATION_TIMEOUT,
                    )
                    # Check for Cloudflare challenge
                    await self._wait_for_real_content(page)
                    # Dismiss cookie banner if present
                    await self._dismiss_cookies(page)
                    # Wait briefly for Next.js hydration / DOM rendering
                    await page.wait_for_timeout(2000)
                except PwTimeout:
                    console.print(f"  [yellow][!] Timeout loading search page {page_num}, skipping.[/yellow]")
                    progress.advance(task)
                    continue
                except Exception as exc:
                    console.print(f"  [yellow][!] Error loading search page {page_num}: {exc}[/yellow]")
                    progress.advance(task)
                    continue

                # Extract profile links
                links = await page.locator(config.SEL_TRADER_CARD_LINK).all()
                page_count = 0
                for link in links:
                    href = await link.get_attribute("href")
                    if href and href.startswith("/trades/"):
                        # Strip query params and fragment (e.g. #categoryId=2&location=London)
                        clean_href = href.split("?")[0].split("#")[0].rstrip("/")
                        slug = clean_href.split("/")[-1]
                        if slug and clean_href not in seen:
                            full_url = urljoin(config.BASE_URL, clean_href)
                            urls.append(full_url)
                            seen.add(clean_href)
                            page_count += 1

                console.print(f"  [green][+] Found {page_count} new trader links[/green]")

                # Track consecutive empty pages — stop after 5 in a row
                if page_count == 0:
                    consecutive_empty += 1
                    remaining = max_consecutive_empty - consecutive_empty
                    if consecutive_empty >= max_consecutive_empty:
                        console.print(
                            f"  [yellow]{max_consecutive_empty} consecutive pages with no new traders "
                            f"— ending search scan.[/yellow]"
                        )
                        progress.update(task, completed=self.max_pages)
                        break
                    else:
                        console.print(
                            f"  [dim]  ({consecutive_empty}/{max_consecutive_empty} consecutive empty — "
                            f"{remaining} more before stopping)[/dim]"
                        )
                else:
                    consecutive_empty = 0  # reset counter when new traders found

                progress.advance(task)
                await random_delay()

        console.print(
            f"\n[bold green]Total unique trader profiles found: {len(urls)}[/bold green]\n"
        )
        return urls

    # ── Individual profile scraping ───────────────────────────────────
    async def scrape_profile(self, page, url: str) -> dict | None:
        """Visit a single trader profile and extract all available data.

        Uses a multi-tier extraction pipeline:
        1. Schema.org application/ld+json (fast, structured, highly reliable)
        2. Next.js App Router RSC stream parsing (self.__next_f.push)
        3. DOM scraping and click fallback for any remaining fields
        """
        data = {col: "" for col in CSV_COLUMNS}
        data["profile_url"] = url

        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=config.NAVIGATION_TIMEOUT,
            )
            # Wait for Cloudflare challenge to resolve (if any)
            if not await self._wait_for_real_content(page):
                console.print(f"  [yellow][!] Blocked by Cloudflare: {url}[/yellow]")
                return None

            await page.wait_for_timeout(1000)
        except PwTimeout:
            console.print(f"  [yellow][!] Timeout loading profile: {url}[/yellow]")
            return None
        except Exception as exc:
            console.print(f"  [yellow][!] Error loading profile: {exc}[/yellow]")
            return None

        # Dismiss cookie banner if it reappears
        await self._dismiss_cookies(page)

        # Get full HTML content for fast offline parsing
        html = await page.content()

        # ── Tier 1: Schema.org application/ld+json ───────────────────
        self._extract_from_ldjson(data, html)

        # ── Tier 2: Next.js App Router (RSC) Stream Data ────────────
        self._extract_from_rsc(data, html)

        # ── Tier 3: DOM Fallbacks & Click Interactions ──────────────
        # Company name fallback (h1)
        if not data["company_name"]:
            try:
                h1 = page.locator(config.SEL_COMPANY_NAME).first
                name_text = sanitise(await h1.text_content(timeout=2000))
                if name_text and name_text.lower() not in ["www.checkatrade.com", "checkatrade.com"]:
                    data["company_name"] = name_text
            except Exception:
                pass

        # Member-since fallback (header badge / text)
        if not data["member_since"]:
            try:
                member_el = page.get_by_text("member since", exact=False).first
                raw_ms = sanitise(await member_el.text_content(timeout=2000))
                data["member_since"] = format_member_since(raw_ms)
            except Exception:
                pass

        # Rating fallback
        if not data["rating"]:
            try:
                rating_el = page.get_by_text(re.compile(r"\d+\.?\d*/10")).first
                raw_rating = sanitise(await rating_el.text_content(timeout=2000))
                rating_match = re.search(r"(\d+\.?\d*)\s*/\s*10", raw_rating)
                if rating_match:
                    data["rating"] = rating_match.group(1)
            except Exception:
                pass

        # Review count fallback
        if not data["review_count"]:
            try:
                review_el = page.locator(config.SEL_REVIEW_BUTTON).first
                raw_rev = sanitise(await review_el.text_content(timeout=2000))
                rev_match = re.search(r"(\d+)\s*reviews?", raw_rev, re.IGNORECASE)
                if rev_match:
                    data["review_count"] = rev_match.group(1)
            except Exception:
                pass

        # Phone number fallback — click button to reveal if missing
        if not data["phone_number"]:
            try:
                phone_btn = page.locator(config.SEL_PHONE_BUTTON).first
                if await phone_btn.is_visible(timeout=1500):
                    await phone_btn.click()
                    await page.wait_for_timeout(1000)
                    phone_link = page.locator(config.SEL_PHONE_LINK).first
                    href = await phone_link.get_attribute("href", timeout=2000)
                    if href and href.startswith("tel:"):
                        data["phone_number"] = format_phone_number(href)
                    else:
                        phone_text = sanitise(await phone_link.text_content(timeout=1500))
                        if phone_text:
                            data["phone_number"] = format_phone_number(phone_text)
            except Exception:
                pass

        # Company Info fallback (owner, company type)
        if not data["owner"] or not data["company_type"]:
            try:
                company_tab = page.locator(config.SEL_COMPANY_INFO_TAB).first
                if await company_tab.is_visible(timeout=1500):
                    await company_tab.click()
                    await page.wait_for_timeout(800)

                    company_section = page.locator("#tradesCompanyInfo")
                    if await company_section.is_visible(timeout=2000):
                        section_text = sanitise(await company_section.text_content(timeout=3000))
                        if section_text:
                            if not data["owner"]:
                                owner_match = re.search(
                                    r"Owner\s+(.+?)(?:Ltd Company|Sole Trader|Partnership|VAT|Member since|Domestic|Free|Insurance|$)",
                                    section_text,
                                    re.IGNORECASE,
                                )
                                if owner_match:
                                    data["owner"] = sanitise(owner_match.group(1))

                            if not data["company_type"]:
                                if "Ltd Company" in section_text:
                                    data["company_type"] = "Ltd Company"
                                elif "Sole Trader" in section_text:
                                    data["company_type"] = "Sole Trader"
                                elif "Partnership" in section_text:
                                    data["company_type"] = "Partnership"
            except Exception:
                pass

        # Trade categories fallback
        if not data["trade_categories"]:
            try:
                skills_link = page.locator('a[href="#tradesSkills"]').first
                if await skills_link.is_visible(timeout=1500):
                    await skills_link.click()
                    await page.wait_for_timeout(800)

                skills_section = page.locator("#tradesSkills")
                if await skills_section.is_visible(timeout=1500):
                    headings = await skills_section.locator("h3").all()
                    categories = []
                    for h in headings:
                        text = sanitise(await h.text_content(timeout=1500))
                        clean = re.sub(r"\s*\(\d+\)\s*$", "", text)
                        if clean:
                            categories.append(clean)
                    if categories:
                        data["trade_categories"] = "; ".join(categories)
            except Exception:
                pass

        return data

    # ── Tier 1: Schema.org application/ld+json ────────────────────────
    def _extract_from_ldjson(self, data: dict, html: str):
        """Extract structured data from Schema.org LD+JSON blocks."""
        ld_jsons = re.findall(
            r'<script[^>]*type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        for ld in ld_jsons:
            try:
                parsed = json.loads(ld)
                schema_type = parsed.get("@type")

                if schema_type == "LocalBusiness":
                    # Company Name
                    if not data["company_name"] and parsed.get("name"):
                        data["company_name"] = sanitise(parsed["name"])

                    # Phone number
                    if not data["phone_number"] and parsed.get("telephone"):
                        data["phone_number"] = format_phone_number(parsed["telephone"])

                    # Location / Address
                    if not data["location"]:
                        addr = parsed.get("address")
                        if isinstance(addr, dict):
                            loc_parts = []
                            for k in ["addressLocality", "addressRegion"]:
                                val = addr.get(k)
                                if val and isinstance(val, str) and sanitise(val):
                                    loc_parts.append(sanitise(val))
                            if loc_parts:
                                data["location"] = ", ".join(loc_parts)
                        elif isinstance(addr, str) and addr.strip():
                            data["location"] = sanitise(addr)

                    # Rating and reviews
                    agg_rating = parsed.get("aggregateRating")
                    if isinstance(agg_rating, dict):
                        rating_val = agg_rating.get("ratingValue")
                        if rating_val is not None and str(rating_val) != "0":
                            data["rating"] = str(rating_val)
                        rev_count = agg_rating.get("reviewCount")
                        if rev_count is not None:
                            data["review_count"] = str(rev_count)

                    # Skills / Trade categories
                    knows = parsed.get("knowsAbout")
                    if isinstance(knows, list) and knows and not data["trade_categories"]:
                        data["trade_categories"] = "; ".join(sanitise(k) for k in knows if sanitise(k))

                elif schema_type == "Service":
                    if not data["trade_categories"] and parsed.get("serviceType"):
                        services = parsed["serviceType"].split(",")
                        cleaned = [sanitise(s) for s in services if sanitise(s)]
                        if cleaned:
                            data["trade_categories"] = "; ".join(cleaned)

            except Exception:
                continue

    # ── Tier 2: Next.js App Router (RSC) Stream Data ───────────────────
    def _extract_from_rsc(self, data: dict, html: str):
        """Extract server-rendered metadata from Next.js App Router (self.__next_f) chunks."""
        chunks = re.findall(
            r'self\.__next_f\.push\(\[1,\s*"(.*?)"\]\)',
            html,
            re.DOTALL,
        )
        if not chunks:
            return

        full_rsc = ""
        for chunk in chunks:
            try:
                full_rsc += json.loads(f'"{chunk}"')
            except Exception:
                full_rsc += chunk

        if not full_rsc:
            return

        # Company Name fallback
        if not data["company_name"]:
            name_m = re.findall(r'["\\]+companyName["\\]+:\s*["\\]([^"\\]+)["\\]', full_rsc)
            if name_m:
                data["company_name"] = sanitise(name_m[0])

        # Business Owner(s)
        if not data["owner"]:
            owner_m = re.findall(
                r'["\\]+Business Owners["\\]+.*?["\\]+value["\\]+:\s*["\\]([^"\\]*)["\\]',
                full_rsc,
            )
            if not owner_m:
                owner_m = re.findall(
                    r'["\\]+Owner["\\]+.*?["\\]+value["\\]+:\s*["\\]([^"\\]*)["\\]',
                    full_rsc,
                )
            if owner_m and sanitise(owner_m[0]):
                data["owner"] = sanitise(owner_m[0])

        # Company Type
        if not data["company_type"]:
            ctype_m = re.findall(
                r'["\\]+Company Type["\\]+.*?["\\]+value["\\]+:\s*["\\]([^"\\]*)["\\]',
                full_rsc,
            )
            if ctype_m and sanitise(ctype_m[0]):
                data["company_type"] = sanitise(ctype_m[0])

        # Member Since
        if not data["member_since"]:
            ms_m = re.findall(
                r'Approved member since\s+((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})',
                full_rsc,
                re.IGNORECASE,
            )
            if not ms_m:
                ms_m = re.findall(
                    r'["\\]+memberSince["\\]+:\s*["\\]([^"\\]+)["\\]',
                    full_rsc,
                )
            if ms_m and sanitise(ms_m[0]):
                data["member_since"] = format_member_since(ms_m[0])

        # Location fallback
        if not data["location"]:
            loc_m = re.findall(
                r'["\\]+relatedSearchTown["\\]+:\s*["\\]([^"\\]+)["\\]',
                full_rsc,
            )
            if loc_m and sanitise(loc_m[0]):
                data["location"] = sanitise(loc_m[0])

        # Total Reviews fallback
        if not data["review_count"]:
            rev_m = re.findall(r'["\\]+totalReviews["\\]+:\s*(\d+)', full_rsc)
            if rev_m:
                data["review_count"] = rev_m[0]

        # Rating fallback
        if not data["rating"]:
            rat_m = re.findall(r'["\\]+averageRating["\\]+:\s*([0-9.]+)', full_rsc)
            if rat_m and rat_m[0] != "0":
                data["rating"] = rat_m[0]

    # ── Cookie banner dismiss ─────────────────────────────────────────
    async def _dismiss_cookies(self, page):
        """Attempt to dismiss cookie consent banners."""
        try:
            for selector in [
                'button:has-text("Accept all")',
                'button:has-text("Accept All")',
                'button:has-text("Accept")',
                'button:has-text("I agree")',
                'button:has-text("Got it")',
                "#onetrust-accept-btn-handler",
                ".cookie-accept",
            ]:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=400):
                    await btn.click()
                    await page.wait_for_timeout(400)
                    break
        except Exception:
            pass

    # ── Cloudflare challenge detection ────────────────────────────────
    async def _wait_for_real_content(self, page, max_wait: int = 15):
        """Detect Cloudflare challenge pages and wait for them to resolve.

        Returns True if real content loaded, False if still blocked.
        """
        for attempt in range(max_wait):
            title = await page.title()
            # Cloudflare challenge pages have distinctive titles
            if any(kw in title.lower() for kw in [
                "just a moment",
                "attention required",
                "checking your browser",
                "access denied",
            ]):
                if attempt == 0:
                    console.print("    [dim]Cloudflare challenge detected, waiting for clearance...[/dim]")
                await page.wait_for_timeout(1000)
                continue

            # Also check if h1 is the domain name (another sign of challenge)
            try:
                h1_text = await page.locator("h1").first.text_content(timeout=1000)
                if h1_text and h1_text.strip().lower() in [
                    "www.checkatrade.com",
                    "checkatrade.com",
                ]:
                    await page.wait_for_timeout(1000)
                    continue
            except Exception:
                pass

            return True

        return False

    # ── Main orchestration ────────────────────────────────────────────
    async def run(self):
        """Execute the full scraping pipeline."""
        if self.filter_months:
            month_list = format_renewal_targets(self.filter_months)
            filter_label = f"1st-year renewals — joined in [bold]{month_list}[/bold]"
        else:
            filter_label = "None (all traders)"
        console.print(
            Panel(
                "[bold white]Checkatrade Web Scraper[/bold white]\n"
                f"[dim]Search:[/dim]  {config.BASE_URL}{self.search_path}\n"
                f"[dim]Pages:[/dim]   up to {self.max_pages}\n"
                f"[dim]Mode:[/dim]    {'Headless' if self.headless else 'Visible browser'}\n"
                f"[dim]Filter:[/dim]  {filter_label}",
                title="[>>] Starting",
                border_style="blue",
                box=box.ROUNDED,
            )
        )

        async with async_playwright() as pw:
            launch_args = {
                "headless": self.headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            }
            # Prefer the system Chrome if available (harder to detect)
            try:
                browser = await pw.chromium.launch(
                    channel="chrome", **launch_args
                )
                console.print("  [dim]Browser: System Chrome[/dim]")
            except Exception:
                browser = await pw.chromium.launch(**launch_args)
                console.print("  [dim]Browser: Bundled Chromium[/dim]")

            context = await browser.new_context(
                viewport={
                    "width": config.VIEWPORT_WIDTH,
                    "height": config.VIEWPORT_HEIGHT,
                },
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
                locale="en-GB",
                timezone_id="Europe/London",
            )
            page = await context.new_page()
            # Apply stealth patches to avoid bot detection
            stealth = Stealth()
            await stealth.apply_stealth_async(page)

            # Phase 1 — Collect profile URLs from search pages
            console.rule("[bold]Phase 1 — Collecting trader profile URLs[/bold]")
            profile_urls = await self.collect_profile_urls(page)

            if not profile_urls:
                console.print("[red]No trader profiles found. Exiting.[/red]")
                await browser.close()
                return

            # Phase 2 — Scrape each profile
            console.rule("[bold]Phase 2 — Scraping trader profiles[/bold]")

            with Progress(
                SpinnerColumn(),
                TextColumn("[bold cyan]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console,
            ) as progress:
                task = progress.add_task(
                    "Scraping profiles...", total=len(profile_urls)
                )

                for i, url in enumerate(profile_urls, 1):
                    progress.update(
                        task,
                        description=f"Scraping [{i}/{len(profile_urls)}]...",
                    )
                    data = await self.scrape_profile(page, url)
                    if data and data.get("company_name"):
                        # Apply renewal filter if configured
                        if self.filter_months and not member_since_matches_months(
                            data.get("member_since", ""), self.filter_months
                        ):
                            self.filtered_count += 1
                            ms = data.get('member_since', 'unknown')
                            want = format_renewal_targets(self.filter_months)
                            console.print(
                                f"  [dim][-] Filtered out: {data.get('company_name', 'Unknown')} "
                                f"(joined {ms}, want {want})[/dim]"
                            )
                        else:
                            self.results.append(data)
                            info_parts = []
                            if data.get("member_since"):
                                info_parts.append(data["member_since"])
                            if data.get("location"):
                                info_parts.append(data["location"])
                            if data.get("phone_number"):
                                info_parts.append(data["phone_number"])
                            info_str = f"[dim]- {' | '.join(info_parts)}[/dim]" if info_parts else ""
                            console.print(
                                f"  [green][+][/green] {data.get('company_name', 'Unknown')} {info_str}"
                            )
                    else:
                        console.print(f"  [yellow][-] Skipped: {url}[/yellow]")

                    progress.advance(task)
                    await random_delay()

            await browser.close()

        # Phase 3 — Export
        console.rule("[bold]Phase 3 — Exporting to CSV[/bold]")
        filepath = self.export_csv()

        # Summary table
        self._print_summary(filepath)

    # ── CSV export ────────────────────────────────────────────────────
    def export_csv(self) -> str:
        """Write results to a timestamped CSV file."""
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Derive a filename from the search path
        slug = self.search_path.strip("/").replace("/", "_").replace(" ", "_")
        filename = f"checkatrade_{slug}_{timestamp}.csv"
        filepath = os.path.join(config.OUTPUT_DIR, filename)

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(self.results)

        console.print(f"[bold green][+] CSV saved:[/bold green] {filepath}")
        return filepath

    # ── Summary table ─────────────────────────────────────────────────
    def _print_summary(self, filepath: str):
        """Print a summary table of scraped data."""
        table = Table(
            title="Scrape Summary",
            box=box.ROUNDED,
            show_lines=True,
            header_style="bold magenta",
        )
        table.add_column("Company", style="bold")
        table.add_column("Member Since")
        table.add_column("Phone")
        table.add_column("Location")
        table.add_column("Owner")
        table.add_column("Type")
        table.add_column("Rating")

        for row in self.results[:20]:
            table.add_row(
                row.get("company_name", "-") or "-",
                row.get("member_since", "-") or "-",
                row.get("phone_number", "-") or "-",
                row.get("location", "-") or "-",
                row.get("owner", "-") or "-",
                row.get("company_type", "-") or "-",
                (row.get("rating", "") + ("/10" if row.get("rating") else "-")) if row.get("rating") else "-",
            )

        if len(self.results) > 20:
            table.add_row(
                f"[dim]... and {len(self.results) - 20} more[/dim]",
                "", "", "", "", "", "",
            )

        console.print()
        console.print(table)
        filter_info = ""
        if self.filter_months:
            month_list = format_renewal_targets(self.filter_months)
            filter_info = (
                f"\n[bold]Renewal filter:[/bold]      {month_list} (1st-year only)"
                f"\n[bold]Filtered out:[/bold]        {self.filtered_count}"
            )
        console.print(
            Panel(
                f"[bold]Total traders scraped:[/bold] {len(self.results)}{filter_info}\n"
                f"[bold]Output file:[/bold]          {filepath}",
                title="[OK] Complete",
                border_style="green",
                box=box.ROUNDED,
            )
        )


# ── CLI ───────────────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape trader profiles from Checkatrade.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python scraper.py\n'
            '  python scraper.py --search-path "/Search/Plumber/in/Manchester"\n'
            '  python scraper.py --max-pages 10 --no-headless\n'
        ),
    )
    parser.add_argument(
        "--search-path",
        default=config.DEFAULT_SEARCH_PATH,
        help=(
            "Checkatrade search path, e.g. "
            '"/Search/Electrician/in/Birmingham" '
            f"(default: {config.DEFAULT_SEARCH_PATH})"
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=config.MAX_PAGES,
        help=f"Maximum search-result pages to scan (default: {config.MAX_PAGES})",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show the browser window while scraping",
    )
    parser.add_argument(
        "--renewal",
        action="store_true",
        default=config.RENEWAL_TARGETING,
        help="Enable renewal targeting — only keep traders who joined 10 or 11 months ago (default: on)",
    )
    parser.add_argument(
        "--no-renewal",
        action="store_true",
        help="Disable renewal targeting — keep all traders",
    )
    parser.add_argument(
        "--delay-min",
        type=float,
        default=config.DELAY_MIN,
        help=f"Minimum delay between requests in seconds (default: {config.DELAY_MIN})",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=config.DELAY_MAX,
        help=f"Maximum delay between requests in seconds (default: {config.DELAY_MAX})",
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    # Override config with CLI args
    config.DELAY_MIN = args.delay_min
    config.DELAY_MAX = args.delay_max
    headless = config.HEADLESS and not args.no_headless

    # Resolve renewal targeting
    renewal_enabled = args.renewal and not args.no_renewal
    filter_months = None
    if renewal_enabled:
        filter_months = get_renewal_months()
        month_list = format_renewal_targets(filter_months)
        console.print(
            f"[bold cyan][i] Renewal targeting active (1st-year only):[/bold cyan] "
            f"only traders who joined in [bold]{month_list}[/bold]\n"
        )

    scraper = CheckatradeScraper(
        search_path=args.search_path,
        max_pages=args.max_pages,
        headless=headless,
        filter_months=filter_months,
    )
    await scraper.run()


if __name__ == "__main__":
    asyncio.run(main())

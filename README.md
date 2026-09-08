# Checkatrade Web Scraper

A Python scraper that automatically collects trader profile data from [Checkatrade](https://www.checkatrade.com), including membership dates, contact numbers, owners, company types, locations, ratings, review counts, and trade categories. Results are exported to CSV.

Built for **lead generation** — includes a **renewal targeting** mode that automatically identifies traders approaching their 1st-year membership renewal.

## How It Works

The scraper operates in **three phases**:

### Phase 1 — Collect Profile URLs
The scraper navigates to a Checkatrade search results page (e.g. *Builders in London*) and extracts unique trader profile links. It paginates through multiple pages using the `?page=X` query parameter.

### Phase 2 — Multi-Tier Profile Extraction
For each trader profile URL, the scraper loads the page using Playwright stealth mode and extracts information across three resilient tiers:
1. **Tier 1 (Schema.org `application/ld+json`)**: Instant extraction of structured JSON-LD graphs (`LocalBusiness` and `Service`) containing unmasked telephone numbers, official business names, geographic locations (town, county, region), ratings, review counts, and skill sets.
2. **Tier 2 (Next.js App Router RSC Stream Parsing)**: Reconstructs and parses React Server Components stream chunks (`self.__next_f.push`) to extract verified owner names (`Business Owners`), company structures (`Company Type` — Ltd Company, Sole Trader, Partnership), membership dates (`Approved member since`), and fallbacks.
3. **Tier 3 (DOM Fallbacks & Interactions)**: If any field is missing from the structured layers, the scraper falls back to DOM selectors, text searches, and interactive button clicks (e.g., telephone reveal button).

### Phase 3 — Export to CSV
All collected data is validated, sanitised, and exported to a timestamped CSV file in the `output/` directory.

### Anti-Bot Evasion
Checkatrade uses Cloudflare protection. The scraper handles this by:
- Using **playwright-stealth** to patch browser fingerprinting signals
- Preferring the system Chrome browser (harder to detect than bundled Chromium)
- Detecting Cloudflare challenge pages (`_wait_for_real_content`) and waiting for clearance
- Adding configurable random delays between requests to mimic human browsing

## Data Collected

| Field | Description | Example |
|-------|-------------|---------|
| `company_name` | Trader's business name | EVConstructing Limited |
| `profile_url` | Full URL to their Checkatrade profile | `https://www.checkatrade.com/trades/evconstructing` |
| `member_since` | When they joined Checkatrade | November 2017 |
| `location` | Trader's listed area/town/region | London, Greater London |
| `phone_number` | Contact phone number | 07447 194171 |
| `owner` | Business owner's name | Mr Vugar Gulamaliyev, Ilgar Gulamaliyev |
| `company_type` | Ltd Company, Sole Trader, or Partnership | Ltd Company |
| `rating` | Average rating out of 10 | 9.47 |
| `review_count` | Total number of reviews | 208 |
| `trade_categories` | Listed trade skills | Bathroom Fitter; Bath Resurfacing; General Building |

## Installation

```bash
# 1. Install Python dependencies
py -m pip install -r requirements.txt
# (or pip install -r requirements.txt)

# 2. Install browser binaries (one-time setup)
py -m playwright install chromium
```

## Usage

### Basic (default: Builders in London, 50 pages)
```bash
py scraper.py
```

### Custom search
```bash
# Plumbers in Manchester
py scraper.py --search-path "/Search/Plumber/in/Manchester"

# Electricians in Birmingham, 10 pages
py scraper.py --search-path "/Search/Electrician/in/Birmingham" --max-pages 10
```

### Disable renewal filtering (keep all traders)
```bash
py scraper.py --no-renewal
```

### All options
```bash
py scraper.py --help
```

| Flag | Default | Description |
|------|---------|-------------|
| `--search-path` | `/Search/Builder/in/London-Greater-London` | Checkatrade search URL path |
| `--max-pages` | `50` | Max search result pages to scan |
| `--renewal` | On | Enable 1st-year renewal targeting (auto-calculates target months) |
| `--no-renewal` | Off | Disable renewal filter — keep all traders regardless of join date |
| `--no-headless` | Off | Show the browser window while scraping |
| `--delay-min` | `2.0` | Min seconds between requests |
| `--delay-max` | `4.0` | Max seconds between requests |

## Output

CSV files are saved to the `output/` directory with timestamped filenames:

```
output/
  checkatrade_Search_Builder_in_London-Greater-London_20260821_143022.csv
```

## Configuration

Edit [`config.py`](config.py) to change default settings without CLI flags:

```python
DEFAULT_SEARCH_PATH = "/Search/Builder/in/London-Greater-London"
MAX_PAGES = 50
DELAY_MIN = 2.0
DELAY_MAX = 4.0
HEADLESS = True
RENEWAL_TARGETING = True   # Auto-target 1st-year renewals (10-11 months ago)
```

### Renewal Targeting

When `RENEWAL_TARGETING = True` (default), the scraper automatically calculates which two months to target based on today's date — specifically traders who joined **exactly 10 or 11 months ago**. Only first-year members are matched (both month and year must match).

For example, running in **September 2026** targets traders who joined in:
- **November 2025** (10 months ago)
- **October 2025** (11 months ago)

This auto-updates every month — no manual changes needed. Set `RENEWAL_TARGETING = False` (or use `--no-renewal`) to disable filtering and collect all traders.

### Stopping the Scraper

To stop execution mid-run, press **`Ctrl + C`** in the terminal. Note that results are only saved to CSV after all profiles are scraped, so stopping early will not produce a CSV file.

## Project Structure

```
Medway Scraper/
├── scraper.py         # Main scraper script (Multi-tier extraction engine)
├── config.py          # Configuration constants & selectors
├── requirements.txt   # Python dependencies
├── README.md          # Documentation & usage guide
└── output/            # CSV output directory (created automatically)
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `playwright install` fails | Run `py -m playwright install chromium` |
| Blocked by Cloudflare | Try `--no-headless` to use visible browser mode or increase `--delay-min` |
| Empty/missing phone numbers | Some traders do not list a phone number on their profile |
| Empty CSV | Check the search path is valid by visiting the URL in your browser first |

## Disclaimer

This scraper is provided for educational and personal research purposes only. Please:
- **Respect Checkatrade's Terms of Service** and `robots.txt`
- **Use reasonable delays** between requests
- **Don't overload their servers** — keep `--max-pages` reasonable
- **Store data responsibly** in accordance with data protection laws

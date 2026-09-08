"""
Checkatrade Scraper Configuration
==================================
Edit these values to customise scraper behaviour.
"""

# ── Base URL ──────────────────────────────────────────────────────────
BASE_URL = "https://www.checkatrade.com"

# Default search path (trade/location). Override via CLI --search-path.
DEFAULT_SEARCH_PATH = "/Search/Builder/in/London-Greater-London"

# ── Pagination ────────────────────────────────────────────────────────
# Maximum number of search-result pages to iterate through.
MAX_PAGES = 20

# ── Rate Limiting ─────────────────────────────────────────────────────
# Random delay (seconds) between requests to avoid detection / throttling.
DELAY_MIN = 2.0
DELAY_MAX = 4.0

# ── Browser ───────────────────────────────────────────────────────────
# Run the browser in headless mode (True) or visible mode (False).
HEADLESS = True

# Browser viewport size
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 900

# Navigation timeout in milliseconds
NAVIGATION_TIMEOUT = 25_000

# ── Membership / Renewal Targeting ────────────────────────────────
# When True, only keep traders whose "member since" month falls
# exactly 10 or 11 months before the current month. This auto-
# calculates the target months so you never need to update it.
#
# Example: if today is September 2026, the scraper targets traders
# who joined in October (11 months ago) or November (10 months ago).
RENEWAL_TARGETING = True

# ── Output ────────────────────────────────────────────────────────────
# Directory where CSV files will be saved (relative to project root).
OUTPUT_DIR = "output"

# ── Selectors ─────────────────────────────────────────────────────────
# CSS / XPath selectors for live DOM extraction fallback
SEL_COMPANY_NAME = "h1"
SEL_PHONE_BUTTON = 'button[aria-label="Call trader"]'
SEL_PHONE_LINK = 'a[aria-label="Call trader"]'
SEL_REVIEW_BUTTON = 'button[aria-label="Review scroll button"]'
SEL_COMPANY_INFO_TAB = 'a[href="#tradesCompanyInfo"]'

# Search results page — trader card links
SEL_TRADER_CARD_LINK = 'a[href^="/trades/"]'

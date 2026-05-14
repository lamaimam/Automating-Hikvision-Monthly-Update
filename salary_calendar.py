"""
Salary-date detector for Saudi payroll.

There is no public API for "when does salary drop this month" — WPS/Mudad is
an employer submission system, not a calendar service. We scrape
saudicalendars.com which lists the expected drop date per month.

Behavior:
  - Only checks during the window of day-of-month 21..29 (configurable).
  - Returns (salary_date, days_until) so the orchestrator can decide whether
    to fire (when days_until <= SALARY_FIRE_DAYS_BEFORE).
  - If the scrape fails, returns (None, None) and logs LOUDLY — payroll
    cannot silently miss a deadline.
"""
import logging
import re
from datetime import date, datetime
from typing import Optional, Tuple

import requests
from bs4 import BeautifulSoup

import config

log = logging.getLogger(__name__)

# Realistic browser User-Agent to bypass the 403 the site returns for default
# requests UA. If this stops working, switch to playwright/selenium.
SCRAPER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}


def in_check_window(today: Optional[date] = None) -> bool:
    """True if today's day-of-month is within the polling window."""
    today = today or date.today()
    start, end = config.SALARY_CHECK_WINDOW
    return start <= today.day <= end


def fetch_next_salary_date(today: Optional[date] = None) -> Optional[date]:
    """Scrape saudicalendars.com for the next salary drop date.

    Returns None if the scrape fails or no future date is parseable.
    """
    today = today or date.today()

    try:
        resp = requests.get(
            config.SALARY_CALENDAR_URL,
            headers=SCRAPER_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("SALARY CALENDAR SCRAPE FAILED: %s — payroll cannot run blind!", e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # The site's exact DOM may change. We look for any date string in the
    # current/next month. This is fragile by nature; review log output the
    # first few times the script runs.
    candidate_dates = []

    # Strategy 1: look for ISO-format dates in the body
    iso_matches = re.findall(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", soup.get_text())
    for y, m, d in iso_matches:
        try:
            candidate_dates.append(date(int(y), int(m), int(d)))
        except ValueError:
            continue

    # Strategy 2: look for "DD Month YYYY" patterns (English + Arabic-friendly)
    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
        "december": 12,
    }
    for match in re.finditer(
        r"\b(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})\b", soup.get_text()
    ):
        day_str, month_str, year_str = match.groups()
        month_num = month_names.get(month_str.lower())
        if not month_num:
            continue
        try:
            candidate_dates.append(date(int(year_str), month_num, int(day_str)))
        except ValueError:
            continue

    # Filter to "current month and not in the past"
    future_this_month = [
        d for d in candidate_dates
        if d.year == today.year and d.month == today.month and d >= today
    ]

    if not future_this_month:
        log.warning(
            "No future salary dates found on saudicalendars.com for %s — "
            "page DOM may have changed",
            today.strftime("%B %Y"),
        )
        return None

    # The earliest future date in the current month is the next drop.
    next_date = min(future_this_month)
    log.info("Detected next salary date: %s", next_date)
    return next_date


def should_fire_today(today: Optional[date] = None) -> Tuple[bool, Optional[date]]:
    """Decide whether today is the day to send the report.

    Returns (should_fire, next_salary_date_for_logging).
    """
    today = today or date.today()

    if not in_check_window(today):
        return False, None

    salary_date = fetch_next_salary_date(today)
    if salary_date is None:
        return False, None

    days_until = (salary_date - today).days
    log.info("Today: %s | Salary: %s | Days until: %d", today, salary_date, days_until)

    return days_until == config.SALARY_FIRE_DAYS_BEFORE, salary_date

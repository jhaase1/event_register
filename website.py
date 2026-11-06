from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import (
    TimeoutException,
    ElementClickInterceptedException,
    NoSuchElementException,
    WebDriverException,
    InvalidSessionIdException,
    StaleElementReferenceException,
    NoSuchWindowException,
)
from selenium.webdriver.support.wait import WebDriverWait
import selenium.webdriver.support.expected_conditions as EC

import re
import os
import time
import json
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from logging_config import get_logger
from user_config import get_website_token_file

logger = get_logger(__name__)
logger.setLevel("DEBUG")

# The site renders these as stable QA hooks across the member portal, so we
# prefer them over CSS classes (which are generated/hashed and churn on rebuilds).
# The booking site redesigned its portal in late Oct 2026: the events list now shows
# one card per recurring series (its next date), registration is a JS button
# with no link, and per-date registration countdowns live on the series'
# details page.

# Events list page
LIST_RESULTS_HEADER = "[data-testid='events-list-results-header']"  # "20 events found"
LIST_CARD_PREFIX = "events-list-card-"
LIST_CARD_ANY = f"[data-testid^='{LIST_CARD_PREFIX}']"
LIST_CARD_ID_RE = re.compile(rf"^{LIST_CARD_PREFIX}(\d+)$")


def _testid(value):
    return f"[data-testid='{value}']"


def LIST_CARD(card_id):
    return _testid(f"events-list-card-{card_id}")


def LIST_CARD_NAME(card_id):
    return _testid(f"events-list-card-name-{card_id}")


def LIST_CARD_CATEGORY(card_id):
    return _testid(f"events-list-card-category-tag-{card_id}")


def LIST_CARD_DATE(card_id):
    return _testid(f"events-list-card-date-{card_id}")


def LIST_CARD_COST(card_id):
    return _testid(f"events-list-card-cost-{card_id}")


def LIST_CARD_SLOTS(card_id):
    return _testid(f"events-list-slots-{card_id}")


def LIST_CARD_BUTTON(card_id):
    return _testid(f"events-list-register-btn-{card_id}")


def LIST_CARD_UPCOMING(card_id):
    return _testid(f"events-list-view-upcoming-dates-btn-{card_id}")


# Series details page
DETAILS_PAGE = _testid("events-details-page")
DETAILS_DATES_LIST = _testid("events-details-dates-list")
DETAILS_HEADER_DATE = _testid("events-details-date")
DETAILS_HEADER_TIME = _testid("events-details-time")
DETAILS_SLOTS = _testid("events-details-slots")
DETAILS_SIGNUP_MESSAGE = _testid("events-details-signup-message")
# data-testid holds several space-separated values here, hence ~=.
DETAILS_REGISTER_BTN = "[data-testid~='events-details-register-btn']"
DETAILS_RESTRICTION = "[data-testid^='events-details-side-restriction-']"
DETAILS_ROW_PREFIX = "events-details-date-row-"
DETAILS_ROW_ID_RE = re.compile(rf"^{DETAILS_ROW_PREFIX}(\d+)$")


def DETAILS_ROW(index):
    return _testid(f"events-details-date-row-{index}")


def DETAILS_ROW_DATE(index):
    return _testid(f"events-details-date-row-date-{index}")


def DETAILS_ROW_TIME(index):
    return _testid(f"events-details-date-row-time-{index}")


def DETAILS_ROW_SLOTS(index):
    return _testid(f"events-details-date-row-slots-{index}")


def DETAILS_ROW_NOT_OPEN(index):
    return _testid(f"events-details-date-not-open-{index}")


# Button labels (lowercased) on list cards and the details page.
LABEL_REGISTER = "register"
LABEL_NOT_OPEN = "details"
LABEL_WAITLIST = "join waitlist"
REGISTERED_LABELS = ("edit registration", "cancel registration", "registered")
OPEN_LABELS = (LABEL_REGISTER, LABEL_WAITLIST) + REGISTERED_LABELS

# Signup page the details-page Register button navigates to
# (/Online/Events/SignUpToEvent/...). Nothing is registered until
# "Finalize Registration" is clicked (verified on the live site, Nov 2026).
SIGNUP_DATE = _testid("event-signup-header-date")      # "Thu, Nov 12, 2026"
SIGNUP_SUBMIT_BTN = _testid("event-signup-submit-btn")  # "Finalize Registration"
CONTINUE_BUTTON = "button[data-testid='Continue']"
INELIGIBLE_XPATH = (
    "//*[contains(translate(., "
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'not eligible')]"
)

FAILURE_DIR = os.path.join("logs", "failures")
FAILURE_RETENTION_DAYS = 14

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

MONTH_DAY_RE = re.compile(r"(?P<month>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b")

# Matches both user-typed queries ("9:00am - 10:00am") and the site's own
# rendering ("10a - 12p"). The start period is optional since users and the
# site both sometimes omit it when it matches the end period.
TIME_RANGE_RE = re.compile(
    r"(?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*(?P<ap1>[ap]\.?m?\.?)?\s*-\s*"
    r"(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\s*(?P<ap2>[ap]\.?m?\.?)",
    re.IGNORECASE,
)

# Matches each "<number> <unit>" chunk independently (rather than one fixed
# days-hours-minutes shape) because the countdown's granularity changes as it
# nears zero - e.g. "1 day and 23 h", "23 h and 49 min", down to just "45 sec"
# with no larger unit at all in the final moments before opening.
OPENS_IN_UNIT_RE = re.compile(r"(\d+)\s*(days?|h|min|sec)\b", re.IGNORECASE)
OPENS_IN_UNIT_TO_KWARG = {"day": "days", "h": "hours", "min": "minutes", "sec": "seconds"}


def _parse_month_day(text):
    """Extracts (month, day) from text like 'MON, MAY 5' or 'Tue, Sep 1st,'."""
    match = MONTH_DAY_RE.search(text)
    if not match:
        return None
    month = MONTHS.get(match.group("month")[:3].lower())
    if not month:
        return None
    return month, int(match.group("day"))


def _parse_row_month_day(text):
    """Details-page date rows render as stacked 'Tue' / '10' / 'Nov'."""
    parts = [p.strip() for p in (text or "").split() if p.strip()]
    day = next((int(p) for p in parts if p.isdigit()), None)
    month = next((MONTHS[p[:3].lower()] for p in parts if p[:3].lower() in MONTHS), None)
    return (month, day) if month and day else None


def _to_24h(hour, period):
    hour = hour % 12
    return hour + 12 if period == "p" else hour


def _parse_time_range(text):
    """Extracts (start_hour, start_min, end_hour, end_min) in 24h time."""
    match = TIME_RANGE_RE.search(text or "")
    if not match:
        return None
    ap2 = match.group("ap2")[0].lower()
    ap1 = match.group("ap1")
    ap1 = ap1[0].lower() if ap1 else ap2
    h1 = _to_24h(int(match.group("h1")), ap1)
    h2 = _to_24h(int(match.group("h2")), ap2)
    return h1, int(match.group("m1") or 0), h2, int(match.group("m2") or 0)


def parse_clock(text):
    """'20:00:00' or '20:00' -> time; None if malformed."""
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(text).strip(), fmt).time()
        except (TypeError, ValueError):
            continue
    return None


def _parse_opens_in(text):
    """Parses 'Registration opens in 1 day and 23 h' / '23 h and 49 min' / '45 sec' into a timedelta."""
    matches = OPENS_IN_UNIT_RE.findall(text or "")
    if not matches:
        return None
    kwargs = {}
    for value, unit in matches:
        key = OPENS_IN_UNIT_TO_KWARG[unit.lower().rstrip("s")]
        kwargs[key] = kwargs.get(key, 0) + int(value)
    return timedelta(**kwargs)


def resolve_year(month, day, today):
    """Site and user dates carry no year; pick the one closest to today."""
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue  # Feb 29 outside a leap year
    if not candidates:
        # Only Feb 29 can get here; the nearest leap year may be further out.
        year = today.year + 1
        while True:
            try:
                return date(year, month, day)
            except ValueError:
                year += 1
    return min(candidates, key=lambda d: abs((d - today).days))


def _snap_to_clock(when, registration_clock):
    """Moves a countdown-derived opening onto the configured registration clock.

    The site's countdown is coarse ("1 day and 23 h"), so the day it lands
    on is reliable but the time of day isn't."""
    clock = parse_clock(registration_clock) if registration_clock else None
    if registration_clock and clock is None:
        logger.warning(
            f"Ignoring malformed default_registration_time {registration_clock!r}; "
            "using the countdown time as is."
        )
    if clock:
        return when.replace(hour=clock.hour, minute=clock.minute, second=clock.second, microsecond=0)
    return when


class SkillLevelIneligible(Exception):
    """Raised when an event's skill-level restriction excludes the configured skill_level.

    Kept separate from the generic "couldn't determine registration time" outcome
    so callers can reply politely instead of treating this as a system failure.
    """


class EventNotFound(Exception):
    """Raised when no event matches the requested date and time range.

    Distinct from "found but couldn't read a registration time" so callers can
    fall back to speculative pre-registration for sessions not posted yet.
    """


class EventFull(Exception):
    """Raised when registration is open but the session is full (waitlist only)."""


class RegistrationError(RuntimeError):
    """Raised when the registration step didn't go through or couldn't be confirmed."""


class Website:
    def __init__(self, headless=True, wait_time=30):
        """Initializes the web driver for the website interaction.
        Args:
            headless (bool): Whether to run the browser in headless mode.
            wait_time (int): The maximum wait time for elements to load.
        """
        logger.info("Initializing the web driver.")

        if headless:
            service = ChromeService(executable_path="/usr/bin/chromedriver")
            options = Options()
            options.headless = headless

            self.driver = webdriver.Chrome(service=service, options=options)
        else:
            self.driver = webdriver.Chrome()

        try:
            # Selenium's default page-load timeout is 300 s; a hung load must
            # not block a time-critical registration that long.
            self.driver.set_page_load_timeout(60)

            self.logged_in = False
            self.user_tag = None

            self.wait_time = wait_time
            self.wait = WebDriverWait(self.driver, timeout=self.wait_time)
        except Exception:
            # The caller never gets this object, so it can't close Chrome.
            self.driver.quit()
            raise
        logger.info("Web driver initialized.")

    # --- diagnostics ------------------------------------------------------

    def _page_context(self):
        """URL and title for error messages; never raises."""
        try:
            return f"url={self.driver.current_url!r} title={self.driver.title!r}"
        except Exception:
            return "url=? title=?"

    def _wait(self, condition, what, timeout=None):
        """WebDriverWait.until with a readable timeout message.

        Selenium's own TimeoutException message is empty, which made past
        failures ("Message: " plus a native stack trace) hard to diagnose.
        """
        seconds = getattr(self, "wait_time", 30) if timeout is None else timeout
        wait = WebDriverWait(
            self.driver, timeout=seconds, ignored_exceptions=[StaleElementReferenceException]
        )
        try:
            return wait.until(condition)
        except TimeoutException as e:
            raise TimeoutException(
                f"Timed out after {seconds}s waiting for {what} ({self._page_context()})"
            ) from e

    def capture_failure(self, label):
        """Saves a screenshot and the page HTML under logs/failures for diagnosis.

        Returns the saved paths (empty if nothing could be saved). Never raises:
        it's called from error handlers.
        """
        paths = []
        try:
            os.makedirs(FAILURE_DIR, exist_ok=True)
            self._prune_failures()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            base = os.path.join(FAILURE_DIR, f"{stamp}_{re.sub(r'[^A-Za-z0-9_-]+', '_', label)[:60]}")
            try:
                if self.driver.save_screenshot(base + ".png"):
                    paths.append(base + ".png")
            except Exception:
                logger.warning("Could not save failure screenshot.", exc_info=True)
            try:
                # Build the content first so a dead driver doesn't leave an empty file.
                content = f"<!-- {self._page_context()} -->\n{self.driver.page_source}"
                with open(base + ".html", "w", encoding="utf-8") as f:
                    f.write(content)
                paths.append(base + ".html")
            except Exception:
                logger.warning("Could not save failure page source.", exc_info=True)
        except Exception:
            logger.warning("Could not capture failure artifacts.", exc_info=True)
        if paths:
            logger.info(f"Saved failure snapshot ({self._page_context()}): {', '.join(paths)}")
        return paths

    @staticmethod
    def _prune_failures():
        cutoff = time.time() - FAILURE_RETENTION_DAYS * 86400
        for name in os.listdir(FAILURE_DIR):
            path = os.path.join(FAILURE_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass

    # --- login ------------------------------------------------------------

    def login(self, user_tag=None):
        """Logs into the website using the provided credentials."""
        if self.logged_in:
            logger.info("Already logged in.")
            return

        self.user_tag = user_tag or "default"
        logger.info(f"Logging into the website for user tag: {self.user_tag}")

        website_file = get_website_token_file(self.user_tag)

        if not os.path.exists(website_file):
            logger.error(f"Website token file not found: {website_file}")
            raise FileNotFoundError(f"Website token file not found: {website_file}")

        with open(website_file, "r") as file:
            website_info = json.load(file)
        logger.debug(f"Website information loaded from {website_file}.")

        self.default_registration_time = website_info.get(
            "default_registration_time", None
        )
        self.skill_level = website_info.get("skill_level", None)

        login_url = website_info["login_url"]
        self.website_domain = urlparse(login_url).netloc.lower()
        logger.debug(f"Website domain parsed: {self.website_domain}")

        self.events_url = website_info["events_url"]

        self._get_with_retry(login_url)
        logger.debug(f"Navigated to login URL: {login_url}")

        email_field = self._wait(EC.element_to_be_clickable((By.NAME, "email")), "the login form")
        email_field.send_keys(website_info["email"])
        logger.debug("Entered email.")
        self.driver.find_element(By.NAME, "password").send_keys(
            website_info["password"]
        )
        logger.debug("Entered password.")
        self._wait(
            EC.element_to_be_clickable((By.CSS_SELECTOR, CONTINUE_BUTTON)), "the login Continue button"
        ).click()
        logger.debug("Clicked continue button.")

        # The login form is replaced by the member portal on success; waiting for
        # it to go stale is a reliable "navigation happened" signal.
        self._wait(EC.staleness_of(email_field), "the portal to load after login")

        logger.info("Successfully logged into the website.")
        self.logged_in = True

    # --- events list --------------------------------------------------------

    def _go_to_events_page(self, attempts=3, wait_time=None):
        """Navigates to the events list, reloading if it's slow to render.

        Time-critical polling passes attempts=1 and a short wait_time so its
        own schedule decides when to reload.
        """
        for attempt in range(1, attempts + 1):
            logger.info(f"Navigating to events page: {self.events_url}")
            # The results header ("N events found") is rendered even when there
            # are no events, so it's the "list finished loading" signal.
            try:
                self.driver.get(self.events_url)
                self._wait(
                    EC.presence_of_element_located((By.CSS_SELECTOR, LIST_RESULTS_HEADER)),
                    "the events list",
                    timeout=wait_time,
                )
                return
            except TimeoutException as e:
                if attempt == attempts:
                    raise
                logger.warning(f"Events page didn't render (attempt {attempt}/{attempts}); reloading. {e}")

    def _list_card_ids(self):
        ids = []
        for element in self.driver.find_elements(By.CSS_SELECTOR, LIST_CARD_ANY):
            match = LIST_CARD_ID_RE.match(element.get_attribute("data-testid") or "")
            if match:
                ids.append(match.group(1))
        return ids

    def _total_events_count(self):
        try:
            text = self.driver.find_element(By.CSS_SELECTOR, LIST_RESULTS_HEADER).text
        except NoSuchElementException:
            return None
        match = re.search(r"(\d+)\s+events?\s+found", text, re.IGNORECASE)
        return int(match.group(1)) if match else None

    def display_all_events(self):
        """Navigates to the events page and scrolls until every listed series is loaded."""
        self._go_to_events_page()
        self._display_all_events_by_scrolling()
        logger.info("All events displayed.")

    def _scroll_down(self, amount=1200):
        """Jumps to the bottom of the page via JS, then dispatches a real
        mouse-wheel scroll event, since infinite-scroll loaders commonly only
        respond to genuine wheel input."""
        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        ActionChains(self.driver).scroll_by_amount(0, amount).perform()

    def _display_all_events_by_scrolling(self):
        """Loads cards by scrolling until the loaded count matches the page's
        reported total, or stalls for max_stalled_rounds consecutive scrolls."""
        previous_count = len(self._list_card_ids())
        stalled_rounds = 0
        max_stalled_rounds = 3
        scroll_probe_timeout = min(3, getattr(self, "wait_time", 30))
        probe_wait = WebDriverWait(self.driver, timeout=scroll_probe_timeout)

        while True:
            total = self._total_events_count()
            if total is not None and previous_count >= total:
                logger.debug("Loaded card count matches reported total; all events displayed.")
                break

            self._scroll_down()

            def progressed(_driver):
                return len(self._list_card_ids()) > previous_count

            try:
                probe_wait.until(progressed)
            except TimeoutException:
                pass

            current_count = len(self._list_card_ids())
            if current_count <= previous_count:
                stalled_rounds += 1
                if stalled_rounds >= max_stalled_rounds:
                    logger.debug("Card count stalled; assuming all events are loaded.")
                    break
            else:
                stalled_rounds = 0
            previous_count = current_count

    def _text(self, selector):
        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
        return " ".join(elements[0].text.split()) if elements else None

    def _list_card(self, card_id):
        """Reads one list card into plain data (by testid, so nothing goes stale)."""
        date_text = self._text(LIST_CARD_DATE(card_id)) or ""
        return {
            "card_id": card_id,
            "month_day": _parse_month_day(date_text),
            "time": _parse_time_range(date_text),
            "date_text": date_text,
            "name": self._text(LIST_CARD_NAME(card_id)),
            "category": self._text(LIST_CARD_CATEGORY(card_id)),
            "cost": self._text(LIST_CARD_COST(card_id)),
            "slots": self._text(LIST_CARD_SLOTS(card_id)),
            "label": (self._text(LIST_CARD_BUTTON(card_id)) or "").lower(),
        }

    def scan_listed_events(self):
        """Parses every loaded series card into plain data.

        Each card is a recurring series showing its next date. Assumes the list
        is already loaded (display_all_events). The list no longer shows a
        registration countdown, so opens_in is always None here.
        """
        scanned = []
        for card_id in self._list_card_ids():
            card = self._list_card(card_id)
            if not card["month_day"] or not card["time"]:
                continue
            scanned.append(
                {
                    "month_day": card["month_day"],
                    "time": card["time"],
                    "name": card["name"],
                    "category": card["category"],
                    "actionable": card["label"] in OPEN_LABELS,
                    "opens_in": None,
                }
            )
        logger.info(f"Scanned {len(scanned)} listed series.")
        return scanned

    def _series_candidates(self, event_date, time_range):
        """List cards whose series could include the requested date: same start
        and end time, same weekday, and a next date on or before it. Cards
        whose next date is the requested date come first."""
        query_md = _parse_month_day(event_date or "")
        query_time = _parse_time_range(time_range or "")
        if not query_md or not query_time:
            logger.error(f"Could not parse requested date/time: {event_date!r} {time_range!r}")
            return []
        today = datetime.now().date()
        query_day = resolve_year(*query_md, today)

        exact, later = [], []
        for card_id in self._list_card_ids():
            card = self._list_card(card_id)
            if not card["month_day"] or card["time"] != query_time:
                continue
            card_day = resolve_year(*card["month_day"], today)
            if card_day == query_day:
                exact.append(card)
            elif card_day.weekday() == query_day.weekday() and card_day < query_day:
                later.append(card)
        return exact + later

    # --- series details page --------------------------------------------------

    def _open_details_from_card(self, card_id):
        """Opens a series' details page via its 'VIEW UPCOMING DATES' button
        (which only navigates; it never registers). Returns the page URL."""
        button = self._wait(
            EC.presence_of_element_located((By.CSS_SELECTOR, LIST_CARD_UPCOMING(card_id))),
            f"the 'view upcoming dates' button of card {card_id}",
            timeout=5,
        )
        self.driver.execute_script("arguments[0].click();", button)
        self._wait_for_details_page()
        logger.debug(f"Opened series details: {self.driver.current_url}")
        return self.driver.current_url

    def _wait_for_details_page(self, timeout=None):
        self._wait(
            EC.presence_of_element_located((By.CSS_SELECTOR, DETAILS_DATES_LIST)),
            "the event details page",
            timeout=timeout,
        )

    def _details_row_indexes(self):
        indexes = []
        for element in self.driver.find_elements(By.CSS_SELECTOR, f"[data-testid^='{DETAILS_ROW_PREFIX}']"):
            match = DETAILS_ROW_ID_RE.match(element.get_attribute("data-testid") or "")
            if match:
                indexes.append(int(match.group(1)))
        return sorted(indexes)

    def _details_rows(self):
        """Every date of the series on the details page, as plain data."""
        rows = []
        for index in self._details_row_indexes():
            not_open = self._text(DETAILS_ROW_NOT_OPEN(index))
            rows.append(
                {
                    "index": index,
                    "month_day": _parse_row_month_day(self._text(DETAILS_ROW_DATE(index))),
                    "time": _parse_time_range(self._text(DETAILS_ROW_TIME(index))),
                    "slots": self._text(DETAILS_ROW_SLOTS(index)),
                    "not_open": not_open,
                    "opens_in": _parse_opens_in(not_open) if not_open else None,
                }
            )
        return rows

    def _details_restriction(self):
        """Skill restriction text such as 'Intermediate, Advanced', or None."""
        for element in self.driver.find_elements(By.CSS_SELECTOR, DETAILS_RESTRICTION):
            lines = [line.strip() for line in element.text.splitlines() if line.strip()]
            if lines and "skill" in lines[0].lower():
                return ", ".join(lines[1:]) or None
        return None

    def _details_summary(self, card=None):
        parts = []
        if card:
            parts += [card.get("category"), card.get("name"), card.get("cost")]
        restriction = self._details_restriction()
        if restriction:
            parts.append(f"Skill Level Restriction: {restriction}")
        return " - ".join(p for p in parts if p)

    def _find_occurrence(self, event_date, time_range, list_wait=None, attempts=3):
        """Finds the requested date within a listed series.

        Returns (details_url, row, card). Raises EventNotFound when no listed
        series has that date (it may not be posted yet).
        """
        self._go_to_events_page(attempts=attempts, wait_time=list_wait)
        candidates = self._series_candidates(event_date, time_range)
        if not candidates:
            self._display_all_events_by_scrolling()
            candidates = self._series_candidates(event_date, time_range)

        query_md = _parse_month_day(event_date or "")
        query_time = _parse_time_range(time_range or "")
        for index, card in enumerate(candidates):
            if index:
                self._go_to_events_page(attempts=attempts, wait_time=list_wait)
            url = self._open_details_from_card(card["card_id"])
            for row in self._details_rows():
                if row["month_day"] == query_md and (row["time"] in (None, query_time)):
                    self.last_details_card = card
                    self.last_details_rows = self._rows_as_observations(card)
                    logger.info(
                        f"Found {event_date} {time_range} in series '{card.get('name')}' "
                        f"(row {row['index']}, {'not open: ' + row['not_open'] if row['not_open'] else 'open: ' + str(row['slots'])})"
                    )
                    return url, row, card
        raise EventNotFound(
            f"No listed series has {event_date} {time_range} "
            f"({len(candidates)} candidate series checked)"
        )

    def _rows_as_observations(self, card):
        """Every date on the current details page in scan_listed_events format,
        with each date's exact registration countdown."""
        rows = []
        for row in self._details_rows():
            if not row["month_day"] or not (row["time"] or card.get("time")):
                continue
            rows.append(
                {
                    "month_day": row["month_day"],
                    "time": row["time"] or card.get("time"),
                    "name": card.get("name"),
                    "category": card.get("category"),
                    "actionable": row["not_open"] is None,
                    "opens_in": row["opens_in"],
                }
            )
        return rows

    # --- skill level ------------------------------------------------------------

    def _skill_level_allowed(self, restriction_text):
        """Checks the configured skill_level against a restriction like 'Intermediate, Advanced'."""
        if not self.skill_level:
            logger.warning("No skill_level configured; cannot verify restriction, assuming eligible.")
            return True
        return self.skill_level.strip().lower() in restriction_text.lower()

    def _check_restriction(self):
        restriction = self._details_restriction()
        if restriction and not self._skill_level_allowed(restriction):
            logger.info(
                f"Configured skill level '{self.skill_level}' does not meet restriction '{restriction}'."
            )
            raise SkillLevelIneligible(
                f"That's a {restriction} skill level session, current settings list you as {self.skill_level}."
            )

    # --- public lookups -----------------------------------------------------------

    def determine_access_date(
        self, event_date: str, time_range: str, registration_time: datetime = None
    ):
        """Returns (when registration opens, summary) for the requested date.

        Raises EventNotFound if no listed series has the date,
        SkillLevelIneligible if the skill restriction excludes the user, and
        EventFull if registration is open but the session is full.
        """
        logger.info(f"Determining access date for event: {event_date}, {time_range}")
        if registration_time is None:
            registration_time = self.default_registration_time

        url, row, card = self._find_occurrence(event_date, time_range)
        self.last_details_url = url
        summary = self._details_summary(card)
        self._check_restriction()

        if row["not_open"] is None:
            if row["slots"] and "full" in row["slots"].lower():
                raise EventFull(f"{event_date} {time_range} is full (waitlist only).")
            logger.info(f"Registration for {event_date} {time_range} is already open ({row['slots']}).")
            return datetime.now(), summary

        if row["opens_in"] is None:
            logger.warning(f"Could not parse registration countdown: {row['not_open']!r}")
            return None, summary

        when = _snap_to_clock(datetime.now() + row["opens_in"], registration_time)
        logger.info(f"Registration for {event_date} {time_range} opens {when} ({row['not_open']}).")
        return when, summary

    def get_event_url(self, event_date: str, time_range: str):
        """The details-page URL of the series containing the requested date, or None."""
        logger.info(f"Finding event URL for event: {event_date}, {time_range}")
        try:
            url, _row, _card = self._find_occurrence(event_date, time_range)
        except EventNotFound as e:
            logger.error(str(e))
            return None
        logger.info(f"Event URL: {url}")
        return url

    def find_event_url(self, event_date: str, time_range: str, poll_times, page_wait=10):
        """Like get_event_url, but looks again at each of `poll_times`.

        For speculative registrations the session may only be posted the
        moment registration opens, and popular sessions fill within seconds,
        so it looks at specific moments around the expected opening rather
        than on a slow interval. A time already past is polled immediately.
        Returns the details URL as soon as the date shows up, or None after
        the last poll.
        """
        # Whether any poll got a rendered list, so callers can tell "not posted"
        # apart from "couldn't see the list at all".
        self.list_seen_while_polling = False
        ordered = sorted(poll_times)
        for index, poll_time in enumerate(ordered):
            delay = (poll_time - datetime.now()).total_seconds()
            if delay > 0:
                time.sleep(delay)
            # A short wait keeps a slow load from eating the next poll; the
            # last poll has no next one, so it gets the full wait.
            wait = page_wait if index < len(ordered) - 1 else None
            try:
                url = self._poll_for_event(event_date, time_range, wait)
            except (InvalidSessionIdException, NoSuchWindowException) as e:
                # The browser is gone; further polls would fail instantly.
                logger.error(f"Browser session lost while polling: {e}", exc_info=True)
                return None
            except WebDriverException as e:
                # A slow or broken page load is just a missed poll.
                logger.warning(f"Poll at {poll_time} failed: {type(e).__name__}: {e}")
                url = None
            if url:
                logger.info(f"Speculative event posted (poll scheduled {poll_time}): {url}")
                return url
            logger.info(f"Event {event_date} {time_range} not posted at {poll_time}.")
        return None

    def _poll_for_event(self, event_date, time_range, page_wait):
        """One look for the date: a single list load, then its series' details."""
        try:
            url, _row, _card = self._find_occurrence(
                event_date, time_range, list_wait=page_wait, attempts=1
            )
        except EventNotFound:
            url = None
        # Only a completed lookup counts as having seen the list.
        self.list_seen_while_polling = True
        return url

    def _get_with_retry(self, url, attempts=2):
        """driver.get, retried once if the page load times out (the page-load
        timeout is capped so a hung load can't stall a registration)."""
        for attempt in range(1, attempts + 1):
            try:
                self.driver.get(url)
                return
            except TimeoutException:
                if attempt == attempts:
                    raise
                logger.warning(f"Page load timed out ({attempt}/{attempts}); retrying: {url}")

    # --- registration ---------------------------------------------------------------

    def _header_month_day(self):
        return _parse_month_day(self._text(DETAILS_HEADER_DATE) or "")

    def _details_button_label(self):
        return (self._text(DETAILS_REGISTER_BTN) or "").lower()

    def _select_date(self, event_date, time_range):
        """Makes sure the details page header shows the requested date, since
        its Register button registers for the header's date. Clicks the date's
        row if it's showing another one."""
        target = _parse_month_day(event_date or "")
        if self._header_month_day() == target:
            return
        query_time = _parse_time_range(time_range or "")
        for row in self._details_rows():
            if row["month_day"] == target and row["time"] in (None, query_time):
                element = self.driver.find_element(By.CSS_SELECTOR, DETAILS_ROW(row["index"]))
                self.driver.execute_script("arguments[0].click();", element)
                try:
                    self._wait(lambda d: self._header_month_day() == target, f"{event_date} to be selected", 5)
                    return
                except TimeoutException:
                    break
        raise RegistrationError(
            f"The details page shows {self._text(DETAILS_HEADER_DATE)!r}, not {event_date}, "
            "and selecting that date didn't work"
        )

    def register_for_event(self, event_date: str, time_range: str, event_url: str):
        """Registers for the requested date and confirms it went through.

        Raises EventFull, SkillLevelIneligible, EventNotFound or
        RegistrationError (including when the outcome can't be confirmed);
        callers capture a failure snapshot.
        """
        if event_url:
            logger.info(f"Navigating to event URL: {event_url}")
            self._get_with_retry(event_url)
            self._wait_for_details_page()
        else:
            event_url, _row, _card = self._find_occurrence(event_date, time_range)

        self._select_date(event_date, time_range)
        details_url = self.driver.current_url
        self._check_restriction()

        label = self._details_button_label()
        if label in REGISTERED_LABELS:
            logger.info(f"Already registered for {event_date} {time_range} ('{label}').")
            return
        if label == LABEL_WAITLIST:
            raise EventFull(f"{event_date} {time_range} is full (waitlist only).")
        if label != LABEL_REGISTER:
            raise RegistrationError(
                f"No Register button for {event_date} {time_range} (button: {label or 'none'}); "
                f"registration may not be open. {self._text(DETAILS_SIGNUP_MESSAGE) or ''}".strip()
            )

        button = self.driver.find_element(By.CSS_SELECTOR, DETAILS_REGISTER_BTN)
        try:
            button.click()
        except (ElementClickInterceptedException, WebDriverException):
            logger.warning("Native click on Register was intercepted; falling back to JS click.")
            self.driver.execute_script("arguments[0].click();", button)
        logger.info(f"Clicked Register for {event_date} {time_range} (user '{self.user_tag}').")

        self._finalize_signup(event_date)
        self._verify_registered(event_date, time_range, details_url)
        logger.info(f"Registered for {event_date} {time_range} (user '{self.user_tag}').")

    def _finalize_signup(self, event_date):
        """On the signup page Register leads to, checks it's for the requested
        date and clicks 'Finalize Registration' - the step that registers."""

        def signup_page(driver):
            ineligible = driver.find_elements(By.XPATH, INELIGIBLE_XPATH)
            if ineligible:
                return "ineligible", ineligible[0].text.strip()
            submit = driver.find_elements(By.CSS_SELECTOR, SIGNUP_SUBMIT_BTN)
            if submit:
                return "ready", submit[0]
            return False

        kind, found = self._wait(signup_page, "the signup page's Finalize Registration button")
        if kind == "ineligible":
            raise RegistrationError(f"Cannot register for event: {found}")

        # Never finalize a different date than the one requested.
        signup_date = self._text(SIGNUP_DATE)
        if _parse_month_day(signup_date or "") != _parse_month_day(event_date or ""):
            raise RegistrationError(
                f"Signup page is for {signup_date!r}, not {event_date}; not finalizing."
            )

        try:
            found.click()
        except (ElementClickInterceptedException, WebDriverException):
            logger.warning("Native click on Finalize Registration was intercepted; falling back to JS click.")
            self.driver.execute_script("arguments[0].click();", found)
        logger.info(f"Clicked Finalize Registration for {signup_date} (user '{self.user_tag}').")

        # Wait until the signup page is done (it navigates away or reports a
        # problem) before checking the result.
        def finished(driver):
            ineligible = driver.find_elements(By.XPATH, INELIGIBLE_XPATH)
            if ineligible:
                return "ineligible", ineligible[0].text.strip()
            if not driver.find_elements(By.CSS_SELECTOR, SIGNUP_SUBMIT_BTN):
                return "done", None
            return False

        try:
            kind, message = self._wait(finished, "the signup to complete")
        except TimeoutException:
            # Still on the signup page: the details page check below decides.
            kind, message = "unknown", None
        if kind == "ineligible":
            raise RegistrationError(f"Cannot register for event: {message}")

    def _verify_registered(self, event_date, time_range, details_url):
        """Reloads the details page and requires the date to show as registered."""
        self._get_with_retry(details_url)
        self._wait_for_details_page()
        self._select_date(event_date, time_range)
        label = self._details_button_label()
        if label not in REGISTERED_LABELS:
            raise RegistrationError(
                f"Clicked Finalize Registration for {event_date} {time_range}, but the details "
                f"page still shows '{label or 'no button'}' rather than a registration."
            )

    def close(self):
        """Closes the browser."""
        logger.info("Closing the web driver.")
        self.driver.quit()

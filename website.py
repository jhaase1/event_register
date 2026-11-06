from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import (
    TimeoutException,
    ElementClickInterceptedException,
    ElementNotInteractableException,
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
# Own text only (text(), not the whole subtree), and never script or style
# contents, which can mention eligibility without the page saying so.
INELIGIBLE_XPATH = (
    "//*[not(self::script) and not(self::style)][contains(translate(text(), "
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'not eligible')]"
)

# Clicks that a JS click can get past: an overlay covering the button, or a
# button scrolled out of reach. Anything else (a dead session, a stale page)
# would fail a JS click too, or hide a real problem behind it.
JS_CLICK_FALLBACK_ERRORS = (ElementClickInterceptedException, ElementNotInteractableException)

# When a date's countdown is this close at registration time (the scheduler
# clicks a few seconds after the expected opening, and the site's clock may
# differ a little), reload until Register appears rather than give up.
OPENING_SOON = timedelta(minutes=3)
OPENING_WAIT = timedelta(seconds=90)
OPENING_RELOAD_SECONDS = 5

# How long a loaded details page gets to show its Register button or signup
# message; the header renders before them, so reading it too early would see
# no button at all.
DETAILS_HYDRATE_SECONDS = 5

# How long the signup page gets to finish after Finalize Registration. The
# details page reload decides the outcome, so this only needs to be long
# enough for the request to go out.
FINALIZE_SETTLE_SECONDS = 10
# The Finalize button can vanish behind a spinner while the request is still
# going; only this long without it (still on the signup page) counts as done.
SUBMIT_GONE_GRACE_SECONDS = 2

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


class RegistrationNotOpen(RegistrationError):
    """Raised when the requested date has no Register button because
    registration hasn't opened (yet). opens_in is the site's countdown, if any."""

    def __init__(self, message, opens_in=None):
        super().__init__(message)
        self.opens_in = opens_in


class RegistrationUnverified(RegistrationError):
    """Raised when Finalize Registration was clicked but the details page
    couldn't be brought back to the requested date to check the result.

    The registration most likely went through, so callers shouldn't report
    it as a plain failure.
    """


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

    def _open_details_from_card(self, card_id, wait_time=None):
        """Opens a series' details page via its 'VIEW UPCOMING DATES' button
        (which only navigates; it never registers). Returns the page URL, or
        None if the card isn't on the list."""
        selector = LIST_CARD_UPCOMING(card_id)
        if not self.driver.find_elements(By.CSS_SELECTOR, selector):
            # A freshly loaded list only renders its first cards until it's
            # scrolled, so a card found on an earlier load may be missing now.
            self._display_all_events_by_scrolling()
        try:
            button = self._wait(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector)),
                f"the 'view upcoming dates' button of card {card_id}",
                timeout=min(5, getattr(self, "wait_time", 30)),
            )
        except TimeoutException as e:
            logger.warning(f"Card {card_id} isn't on the reloaded list; skipping it. {e}")
            return None
        self.driver.execute_script("arguments[0].click();", button)
        self._wait_for_details_page(timeout=wait_time)
        logger.debug(f"Opened series details: {self.driver.current_url}")
        return self.driver.current_url

    def _wait_for_details_page(self, timeout=None):
        # The dates list container renders before its rows, and the header's
        # Register button or signup message after the header itself; reading
        # either too early would see an empty series or no button.
        self._wait(
            lambda _driver: self._details_row_indexes(),
            "the event details page's dates",
            timeout=timeout,
        )
        hydrate = min(DETAILS_HYDRATE_SECONDS, getattr(self, "wait_time", 30) if timeout is None else timeout)
        try:
            self._wait(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, f"{DETAILS_REGISTER_BTN}, {DETAILS_SIGNUP_MESSAGE}")
                ),
                "the details page's Register button or signup message",
                timeout=hydrate,
            )
        except TimeoutException as e:
            # Not fatal: the label check that follows reports what it sees.
            logger.debug(str(e))

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
        # Read now, while the list is loaded: if every card was rendered,
        # there's nothing more to look at once these candidates are checked.
        fully_rendered = self._list_fully_rendered()

        query_md = _parse_month_day(event_date or "")
        query_time = _parse_time_range(time_range or "")
        # Several series can share a slot (e.g. an Advanced and an
        # Intermediate session at the same time), so a match the user can't
        # register for is only used if no other series fits.
        eligible_fallback = None  # passes the skill check but is full
        any_fallback = None
        visited = set()
        skipped = 0
        on_list = True
        batch = candidates
        while True:
            for card in batch:
                if not on_list:
                    self._go_to_events_page(attempts=attempts, wait_time=list_wait)
                visited.add(card["card_id"])
                url = self._open_details_from_card(card["card_id"], wait_time=list_wait)
                on_list = url is None
                if url is None:
                    skipped += 1
                    continue
                row = next(
                    (
                        r for r in self._details_rows()
                        if r["month_day"] == query_md and r["time"] in (None, query_time)
                    ),
                    None,
                )
                if row is None:
                    continue
                match = (url, row, card, self._rows_as_observations(card))
                skill_ok = self._skill_allowed_here()
                if skill_ok and not self._row_full(row):
                    return self._use_occurrence(event_date, time_range, match)
                logger.info(
                    f"{event_date} {time_range} in series '{card.get('name')}' is "
                    f"{'full' if skill_ok else 'skill-restricted'}; checking the other series in that slot."
                )
                if skill_ok and eligible_fallback is None:
                    eligible_fallback = match
                if any_fallback is None:
                    any_fallback = match
            if fully_rendered:
                break
            # Part of the list never rendered, so the series that fits (or has
            # the date at all) may be further down: scroll and check the rest.
            fully_rendered = True
            if not on_list:
                self._go_to_events_page(attempts=attempts, wait_time=list_wait)
                on_list = True
            self._display_all_events_by_scrolling()
            batch = [
                c for c in self._series_candidates(event_date, time_range) if c["card_id"] not in visited
            ]
            if not batch:
                break

        # A full series the user may join explains itself (EventFull) better
        # than a skill restriction on another series in the slot.
        fallback = eligible_fallback or any_fallback
        if fallback is not None:
            if self.driver.current_url != fallback[0]:
                # Callers read the restriction off the page, so show this series.
                self._get_with_retry(fallback[0])
                self._wait_for_details_page(timeout=list_wait)
            return self._use_occurrence(event_date, time_range, fallback)
        raise EventNotFound(
            f"No listed series has {event_date} {time_range} "
            f"({len(visited)} candidate series checked"
            + (f", {skipped} not on the reloaded list" if skipped else "")
            + ")"
        )

    def _list_fully_rendered(self):
        total = self._total_events_count()
        return total is None or len(self._list_card_ids()) >= total

    @staticmethod
    def _row_full(row):
        return row["not_open"] is None and bool(row["slots"]) and "full" in row["slots"].lower()

    def _skill_allowed_here(self):
        """Whether the current details page's skill restriction allows this user."""
        restriction = self._details_restriction()
        return not restriction or self._skill_level_allowed(restriction)

    def _use_occurrence(self, event_date, time_range, match):
        url, row, card, observations = match
        self.last_details_card = card
        self.last_details_rows = observations
        logger.info(
            f"Found {event_date} {time_range} in series '{card.get('name')}' "
            f"(row {row['index']}, {'not open: ' + row['not_open'] if row['not_open'] else 'open: ' + str(row['slots'])})"
        )
        return url, row, card

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
                    self._wait(
                        lambda d: self._header_month_day() == target,
                        f"{event_date} to be selected",
                        min(5, getattr(self, "wait_time", 30)),
                    )
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
        opens_in = None
        if label not in OPEN_LABELS:
            label, opens_in = self._wait_for_opening(event_date, time_range, details_url)
        if label in REGISTERED_LABELS:
            logger.info(f"Already registered for {event_date} {time_range} ('{label}').")
            return
        if label == LABEL_WAITLIST:
            raise EventFull(f"{event_date} {time_range} is full (waitlist only).")
        if label != LABEL_REGISTER:
            if opens_in is None:
                # No countdown for the date: it's open (or the page is broken),
                # so a missing or unexpected button is a real failure, not
                # "not open yet".
                raise RegistrationError(
                    f"No Register button for {event_date} {time_range} (button: {label or 'none'}) "
                    "and no registration countdown for that date."
                )
            raise RegistrationNotOpen(
                f"No Register button for {event_date} {time_range} (button: {label or 'none'}); "
                f"registration may not be open. {self._text(DETAILS_SIGNUP_MESSAGE) or ''}".strip(),
                opens_in=opens_in,
            )

        self._click(self.driver.find_element(By.CSS_SELECTOR, DETAILS_REGISTER_BTN), "Register")
        logger.info(f"Clicked Register for {event_date} {time_range} (user '{self.user_tag}').")

        outcome = self._finalize_signup(event_date)
        self._verify_registered(event_date, time_range, details_url, outcome)
        logger.info(f"Registered for {event_date} {time_range} (user '{self.user_tag}').")

    def _click(self, element, what):
        try:
            element.click()
        except JS_CLICK_FALLBACK_ERRORS as e:
            logger.warning(f"Native click on {what} failed ({type(e).__name__}); falling back to JS click.")
            self.driver.execute_script("arguments[0].click();", element)

    def _selected_opens_in(self, event_date, time_range):
        """The requested date's registration countdown: its row's not-open
        text, else the header's signup message when the header is that date."""
        target = _parse_month_day(event_date or "")
        query_time = _parse_time_range(time_range or "")
        for row in self._details_rows():
            if (
                row["month_day"] == target
                and row["time"] in (None, query_time)
                and row["opens_in"] is not None
            ):
                return row["opens_in"]
        if self._header_month_day() == target:
            return _parse_opens_in(self._text(DETAILS_SIGNUP_MESSAGE))
        return None

    def _wait_for_opening(self, event_date, time_range, details_url):
        """When the date opens within OPENING_SOON, reloads the details page
        until its Register button shows up (or OPENING_WAIT passes).

        The scheduler clicks seconds after the expected opening, so a
        countdown of a minute or two usually means the site's clock is a
        little behind, not that the time is wrong. Returns (button label,
        countdown last seen)."""
        label = self._details_button_label()
        opens_in = self._selected_opens_in(event_date, time_range)
        if opens_in is None or opens_in > OPENING_SOON:
            return label, opens_in
        logger.info(
            f"{event_date} {time_range} opens in {opens_in}; reloading until Register appears."
        )
        deadline = time.monotonic() + OPENING_WAIT.total_seconds()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # No point waiting the full interval when the countdown is shorter.
            time.sleep(min(OPENING_RELOAD_SECONDS, max(opens_in.total_seconds(), 1), remaining))
            try:
                self._get_with_retry(details_url)
                self._wait_for_details_page()
            except WebDriverException as e:  # includes TimeoutException
                # The site is busiest right at the opening; keep trying.
                logger.warning(f"Reload while waiting for {event_date} to open failed: {e}")
                continue
            self._select_date(event_date, time_range)
            label = self._details_button_label()
            if label in OPEN_LABELS:
                return label, None
            seen = self._selected_opens_in(event_date, time_range)
            if seen is not None:
                opens_in = seen
        logger.warning(f"{event_date} {time_range} still not open after {OPENING_WAIT}.")
        return label, opens_in

    def _on_signup_page(self):
        try:
            if "SignUpToEvent" in (self.driver.current_url or ""):
                return True
        except WebDriverException:
            pass
        return bool(
            self.driver.find_elements(By.CSS_SELECTOR, SIGNUP_DATE)
            or self.driver.find_elements(By.CSS_SELECTOR, SIGNUP_SUBMIT_BTN)
        )

    def _ineligible_text(self):
        """Visible 'not eligible' text on the page, or None."""
        for element in self.driver.find_elements(By.XPATH, INELIGIBLE_XPATH):
            try:
                if element.is_displayed():
                    return element.text.strip()
            except StaleElementReferenceException:
                continue
        return None

    def _finalize_signup(self, event_date):
        """On the signup page Register leads to, checks it's for the requested
        date and clicks 'Finalize Registration' - the step that registers.

        Returns (whether the browser left the signup page, a description)."""

        def signup_page(driver):
            # "not eligible" can appear in a details page's description, so
            # it only counts once the signup page is showing.
            if not self._on_signup_page():
                return False
            # The date is checked before finalizing, so wait for it too: it
            # can render after the button.
            submit = [e for e in driver.find_elements(By.CSS_SELECTOR, SIGNUP_SUBMIT_BTN) if e.is_enabled()]
            if submit and _parse_month_day(self._text(SIGNUP_DATE) or ""):
                return "ready", submit[0]
            ineligible = self._ineligible_text()
            if ineligible:
                return "ineligible", ineligible
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

        # Text already there next to an enabled Finalize button isn't the
        # signup's answer; only a new message after the click is.
        stray_ineligible = self._ineligible_text()
        self._click(found, "Finalize Registration")
        logger.info(f"Clicked Finalize Registration for {signup_date} (user '{self.user_tag}').")

        # Wait until the signup page is done (it navigates away or reports a
        # problem) before checking the result. Whether the site leaves the
        # signup page on success isn't known, so staying there isn't an error.
        submit_gone_since = []

        def finished(driver):
            if not self._on_signup_page():
                return "done", None
            ineligible = self._ineligible_text()
            if ineligible and ineligible != stray_ineligible:
                return "ineligible", ineligible
            if driver.find_elements(By.CSS_SELECTOR, SIGNUP_SUBMIT_BTN):
                submit_gone_since.clear()
                return False
            if not submit_gone_since:
                submit_gone_since.append(time.monotonic())
            if time.monotonic() - submit_gone_since[0] >= SUBMIT_GONE_GRACE_SECONDS:
                return "done", None
            return False

        settle = min(FINALIZE_SETTLE_SECONDS, getattr(self, "wait_time", 30))
        try:
            kind, message = self._wait(finished, "the signup to complete", timeout=settle)
        except TimeoutException:
            # Still on the signup page: the details page check below decides.
            kind, message = "unknown", None
        if kind == "ineligible":
            raise RegistrationError(f"Cannot register for event: {message}")
        if kind == "unknown":
            return False, f"the signup page still showed Finalize Registration after {settle} s"
        return True, "the browser left the signup page"

    def _verify_registered(self, event_date, time_range, details_url, outcome=None):
        """Reloads the details page and requires the date to show as registered.

        The reloaded header may show another date of the series (it resets
        on every load), and whether clicking a date's row selects it isn't
        known. Failing to get back to the date says nothing about the
        registration, so that raises RegistrationUnverified rather than a
        plain failure - unless the signup page never finished either, in
        which case nothing suggests it went through.

        `outcome` is (left the signup page, description) from _finalize_signup."""
        self._get_with_retry(details_url)
        self._wait_for_details_page()
        try:
            self._select_date(event_date, time_range)
        except RegistrationError:
            left_signup, description = outcome or (True, "signup outcome unknown")
            message = (
                f"Finalize Registration was clicked for {event_date} {time_range} but the result "
                f"couldn't be checked: {description}; details page shows "
                f"{self._text(DETAILS_HEADER_DATE)!r}"
            )
            if not left_signup:
                raise RegistrationError(message)
            raise RegistrationUnverified(message)
        label = self._details_button_label()
        if label in REGISTERED_LABELS:
            return
        if label in (LABEL_REGISTER, LABEL_WAITLIST):
            raise RegistrationError(
                f"Clicked Finalize Registration for {event_date} {time_range}, but the details "
                f"page still shows '{label}' rather than a registration."
            )
        # What a registered date's button says hasn't been seen on the live
        # site, so anything else may well be a registration.
        raise RegistrationUnverified(
            f"Finalize Registration was clicked for {event_date} {time_range} but the result "
            f"couldn't be checked: the details page button reads '{label or 'nothing'}'"
        )

    def close(self):
        """Closes the browser."""
        logger.info("Closing the web driver.")
        self.driver.quit()

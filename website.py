from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import (
    TimeoutException,
    ElementClickInterceptedException,
    NoSuchElementException,
)
from selenium.webdriver.support.wait import WebDriverWait
import selenium.webdriver.support.expected_conditions as EC

import re
import os
import time
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse
from logging_config import get_logger
from user_config import get_website_token_file

logger = get_logger(__name__)
logger.setLevel("DEBUG")

# The site renders these as stable QA hooks across the member portal, so we
# prefer them over CSS classes (which are generated/hashed and churn on rebuilds).
EVENT_CARD = "[data-testid='event-card']"
EVENTS_COUNT_SPAN = "#events-count-span"
DATE_TIME_SECTION = "[data-testid='date-time-section']"
REGISTER_BTN = "[data-testid='register-btn']"
DROPIN_MSG = "[data-testid='dropin-msg']"
CATEGORY_NAME = "[data-testid='category-name']"
EVENT_NAME = "[data-testid='event-name']"
COST = "[data-testid='cost']"
SLOTS_INFO = "[data-testid='slots-info']"
RATING_NAMES = "[data-testid='rating-names']"
SAVE_BTN = "[data-testid='save-btn']"
CONTINUE_BUTTON = "button[data-testid='Continue']"
INELIGIBLE_XPATH = (
    "//*[contains(translate(., "
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'not eligible')]"
)

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


def _to_24h(hour, period):
    hour = hour % 12
    return hour + 12 if period == "p" else hour


def _parse_time_range(text):
    """Extracts (start_hour, start_min, end_hour, end_min) in 24h time."""
    match = TIME_RANGE_RE.search(text)
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
    matches = OPENS_IN_UNIT_RE.findall(text)
    if not matches:
        return None
    kwargs = {}
    for value, unit in matches:
        key = OPENS_IN_UNIT_TO_KWARG[unit.lower().rstrip("s")]
        kwargs[key] = kwargs.get(key, 0) + int(value)
    return timedelta(**kwargs)


class SkillLevelIneligible(Exception):
    """Raised when an event's skill-level restriction excludes the configured skill_level.

    Kept separate from the generic "couldn't determine registration time" outcome
    so callers can reply politely instead of treating this as a system failure.
    """


class EventNotFound(Exception):
    """Raised when no event card matches the requested date and time range.

    Distinct from "found but couldn't read a registration time" so callers can
    fall back to speculative pre-registration for sessions not posted yet.
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

        self.logged_in = False
        self.user_tag = None

        self.wait_time = wait_time
        self.wait = WebDriverWait(self.driver, timeout=self.wait_time)
        logger.info("Web driver initialized.")

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

        self.driver.get(login_url)
        logger.debug(f"Navigated to login URL: {login_url}")

        email_field = self.wait.until(EC.element_to_be_clickable((By.NAME, "email")))
        email_field.send_keys(website_info["email"])
        logger.debug("Entered email.")
        self.driver.find_element(By.NAME, "password").send_keys(
            website_info["password"]
        )
        logger.debug("Entered password.")
        self.wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, CONTINUE_BUTTON))
        ).click()
        logger.debug("Clicked continue button.")

        # The login form is replaced by the member portal on success; waiting for
        # it to go stale is a reliable "navigation happened" signal.
        self.wait.until(EC.staleness_of(email_field))

        logger.info("Successfully logged into the website.")
        self.logged_in = True

    def _go_to_events_page(self):
        """Navigates to the events page."""
        logger.info(f"Navigating to events page: {self.events_url}")
        self.driver.get(self.events_url)
        logger.debug(f"Events page loaded: {self.events_url}")

        # Rendered even when there are zero matching events, so it's a reliable
        # "the list finished its initial load" signal either way.
        self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, EVENTS_COUNT_SPAN)))

    def _total_events_count(self):
        try:
            text = self.driver.find_element(By.CSS_SELECTOR, EVENTS_COUNT_SPAN).text
        except NoSuchElementException:
            return None
        match = re.search(r"\d+", text)
        return int(match.group(0)) if match else None

    def display_all_events(self):
        """Navigates to the events page and scrolls until every matching event is loaded."""
        self._go_to_events_page()
        self._display_all_events_by_scrolling()
        logger.info("All events displayed.")

    def _scroll_down(self, amount=1200):
        """Jumps to the bottom of the page via JS, then dispatches a real
        mouse-wheel scroll event.

        JS-only scrolling (window.scrollTo) moves the DOM scroll position
        without firing a native 'wheel' event, and infinite-scroll loaders
        commonly only respond to genuine wheel input - so the JS jump alone
        doesn't reliably trigger loading. Doing it first just gets us near the
        new content so the wheel scroll that follows only needs to travel a
        short distance.
        """
        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        ActionChains(self.driver).scroll_by_amount(0, amount).perform()

    def _display_all_events_by_scrolling(self):
        """Loads events by scrolling until the loaded count matches the page's
        reported total (authoritative when present) or stalls for
        max_stalled_rounds consecutive scrolls (fallback for when the total
        can't be read).
        """
        previous_count = len(self.driver.find_elements(By.CSS_SELECTOR, EVENT_CARD))
        stalled_rounds = 0
        max_stalled_rounds = 3
        scroll_probe_timeout = min(3, getattr(self, "wait_time", 30))
        probe_wait = WebDriverWait(self.driver, timeout=scroll_probe_timeout)

        while True:
            total = self._total_events_count()
            if total is not None and previous_count >= total:
                logger.debug("Loaded event count matches reported total; all events displayed.")
                break

            self._scroll_down()
            logger.debug(f"Scrolled events page: {previous_count = }")

            def progressed(driver):
                return len(driver.find_elements(By.CSS_SELECTOR, EVENT_CARD)) > previous_count

            try:
                probe_wait.until(progressed)
            except TimeoutException:
                logger.debug(
                    f"No additional events loaded after scrolling within {scroll_probe_timeout}s."
                )

            current_count = len(self.driver.find_elements(By.CSS_SELECTOR, EVENT_CARD))

            if current_count <= previous_count:
                stalled_rounds += 1
                logger.debug(f"No new events loaded after scroll: {stalled_rounds = }")
                if stalled_rounds >= max_stalled_rounds:
                    logger.debug(
                        "Event count stalled for max_stalled_rounds; assuming end of loaded events."
                    )
                    break
            else:
                stalled_rounds = 0

            previous_count = current_count

    def _find_event(self, event_date: str, time_range: str, timeout=None):
        """Finds the event card matching the given date and time range.

        Site-rendered text ('Tue, Sep 1st, 10a - 12p') and user-typed queries
        ('MON, MAY 5', '9:00am - 10:00am') use different formatting, so both
        sides are parsed into (month, day) / 24h time tuples and compared
        structurally rather than by substring matching.
        """
        query_date = _parse_month_day(event_date)
        query_time = _parse_time_range(time_range)
        if not query_date or not query_time:
            logger.error(f"Could not parse requested date/time: {event_date!r} {time_range!r}")
            return None

        def scan(driver):
            for card in driver.find_elements(By.CSS_SELECTOR, EVENT_CARD):
                try:
                    dt_text = card.find_element(By.CSS_SELECTOR, DATE_TIME_SECTION).text
                except NoSuchElementException:
                    continue
                if _parse_month_day(dt_text) == query_date and _parse_time_range(dt_text) == query_time:
                    return card
            return False

        wait = self.wait if timeout is None else WebDriverWait(self.driver, timeout=timeout)
        try:
            return wait.until(scan)
        except TimeoutException:
            # Short-timeout lookups are polling for a card that may not be
            # posted yet; a miss there is expected, not an error.
            log = logger.error if timeout is None else logger.debug
            log(f"No event found for date: {event_date}, time range: {time_range}")
            return None

    @staticmethod
    def _card_text(card, selector):
        elements = card.find_elements(By.CSS_SELECTOR, selector)
        return elements[0].text.strip() if elements else None

    def scan_listed_events(self):
        """Parses every loaded event card into plain data.

        Assumes the list is already fully loaded (display_all_events). Reads
        everything up front so nothing holds a card reference that a later
        navigation would make stale.
        """
        scanned = []
        for card in self.driver.find_elements(By.CSS_SELECTOR, EVENT_CARD):
            dt_text = self._card_text(card, DATE_TIME_SECTION)
            if not dt_text:
                continue
            month_day = _parse_month_day(dt_text)
            time_tuple = _parse_time_range(dt_text)
            if not month_day or not time_tuple:
                continue

            register_label = (self._card_text(card, REGISTER_BTN) or "").lower()
            dropin_text = self._card_text(card, DROPIN_MSG)
            scanned.append(
                {
                    "month_day": month_day,
                    "time": time_tuple,
                    "name": self._card_text(card, EVENT_NAME),
                    "category": self._card_text(card, CATEGORY_NAME),
                    "actionable": bool(register_label) and register_label != "details",
                    "opens_in": _parse_opens_in(dropin_text) if dropin_text else None,
                }
            )
        logger.info(f"Scanned {len(scanned)} listed event(s).")
        return scanned

    def find_event_url(self, event_date: str, time_range: str, until: datetime, poll_seconds=10):
        """Like get_event_url, but keeps reloading the list until `until`.

        For speculative registrations the session may only be posted at the
        moment registration opens, so a single lookup isn't enough. Always
        tries at least once. Returns None if it never shows up.
        """
        while True:
            self.display_all_events()
            event = self._find_event(event_date, time_range, timeout=2)
            if event:
                buttons = event.find_elements(By.CSS_SELECTOR, REGISTER_BTN)
                href = buttons[0].get_attribute("href") if buttons else None
                if href:
                    logger.info(f"Speculative event posted: {href}")
                    return href
            if datetime.now() >= until:
                logger.info(f"Event {event_date} {time_range} not posted by {until}.")
                return None
            time.sleep(poll_seconds)

    def _card_summary(self, event):
        """Builds a human-readable summary line from an event card's visible fields."""
        parts = []
        for selector in (CATEGORY_NAME, EVENT_NAME, COST, SLOTS_INFO):
            try:
                text = event.find_element(By.CSS_SELECTOR, selector).text.strip()
            except NoSuchElementException:
                continue
            if text:
                parts.append(" ".join(text.split()))
        return " - ".join(parts)

    def _get_skill_restriction(self, details_url):
        """Reads the skill-level restriction (if any) off an event's details page.

        Only visible on the details page, not the list card, so this costs a
        navigation. Returns None if the event has no skill restriction at all.
        """
        self.driver.get(details_url)
        self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, EVENT_NAME)))
        rating_elements = self.driver.find_elements(By.CSS_SELECTOR, RATING_NAMES)
        return rating_elements[0].text.strip() if rating_elements else None

    def _skill_level_allowed(self, restriction_text):
        """Checks the configured skill_level against a restriction like 'Intermediate, Advanced'."""
        if not self.skill_level:
            logger.warning("No skill_level configured; cannot verify restriction, assuming eligible.")
            return True
        return self.skill_level.strip().lower() in restriction_text.lower()

    def determine_access_date(
        self, event_date: str, time_range: str, registration_time: datetime = None
    ):
        """Determines the access date for the event."""
        logger.info(f"Determining access date for event: {event_date}, {time_range}")

        if registration_time is None:
            registration_time = self.default_registration_time
        logger.debug(f"Using registration time: {registration_time}")

        self.display_all_events()
        event = self._find_event(event_date, time_range)
        if not event:
            # The list page is still loaded, so callers can scan it right away.
            raise EventNotFound(f"No event found for date: {event_date}, time range: {time_range}")

        # Everything needed from the card must be read now: checking the skill
        # restriction navigates away from the list page, which would leave
        # `event` stale.
        summary = self._card_summary(event)
        register_label = event.find_element(By.CSS_SELECTOR, REGISTER_BTN).text.strip().lower()
        details_url = event.find_element(By.CSS_SELECTOR, REGISTER_BTN).get_attribute("href")
        dropin_elements = event.find_elements(By.CSS_SELECTOR, DROPIN_MSG)
        dropin_text = dropin_elements[0].text if dropin_elements else None

        restriction = self._get_skill_restriction(details_url) if details_url else None
        if restriction:
            summary = f"{summary} - Skill Level Restriction: {restriction}"
            if not self._skill_level_allowed(restriction):
                logger.info(
                    f"Configured skill level '{self.skill_level}' does not meet restriction '{restriction}'."
                )
                raise SkillLevelIneligible(
                    f"That's a {restriction} skill level session, current settings list you as {self.skill_level}."
                )

        if register_label and register_label != "details":
            logger.info(f"Event is already actionable ('{register_label}').")
            return datetime.now(), summary

        if not dropin_text:
            logger.warning("No 'Registration opens' message found; cannot determine access date.")
            return None, summary

        opens_in = _parse_opens_in(dropin_text)
        if opens_in is None:
            logger.warning(f"Could not parse registration-opens message: {dropin_text!r}")
            return None, summary

        date = datetime.now() + opens_in
        logger.debug(f"Registration opens in {opens_in}, landing on {date}.")

        reg_time = parse_clock(registration_time) if registration_time else None
        if registration_time and reg_time is None:
            logger.warning(
                f"Ignoring malformed default_registration_time {registration_time!r}; "
                "using the countdown time as is."
            )
        if reg_time:
            date = date.replace(
                hour=reg_time.hour,
                minute=reg_time.minute,
                second=reg_time.second,
                microsecond=0,
            )
            logger.debug(f"Registration time set: {reg_time}")

        logger.info(f"Extracted date: {date}")
        return date, summary

    def get_event_url(self, event_date: str, time_range: str):
        """Finds the persistent details-page URL for the specified event."""
        logger.info(f"Finding event URL for event: {event_date}, {time_range}")

        self.display_all_events()
        event = self._find_event(event_date, time_range)

        if not event:
            logger.error(
                f"No event found for date: {event_date}, time range: {time_range}"
            )
            return None

        href = event.find_element(By.CSS_SELECTOR, REGISTER_BTN).get_attribute("href")

        if href:
            logger.info(f"Extracted event URL: {href}")
        else:
            logger.warning("Register link had no href; could not extract event URL.")

        return href

    def register_for_event(self, event_date: str, time_range: str, event_url: str):
        """Registers for the event."""

        if event_url:
            logger.info(f"Navigating to event URL: {event_url}")
            self.driver.get(event_url)
            register_link = self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, REGISTER_BTN))
            )
        else:
            self.display_all_events()
            event = self._find_event(event_date, time_range)
            if not event:
                raise EventNotFound(f"No event found for date: {event_date}, time range: {time_range}")
            register_link = event.find_element(By.CSS_SELECTOR, REGISTER_BTN)

        signup_url = register_link.get_attribute("href")
        if not signup_url:
            raise RuntimeError(
                f"Register link has no destination for {event_date} {time_range}; "
                "event may already be closed."
            )

        # This is a plain href, not an SPA action, so following it directly
        # sidesteps click-interception entirely instead of fighting overlays.
        self.driver.get(signup_url)
        logger.debug(f"Navigated to signup page: {signup_url}")

        def signup_outcome(driver):
            ineligible = driver.find_elements(By.XPATH, INELIGIBLE_XPATH)
            if ineligible:
                return "ineligible", ineligible[0]
            save_buttons = driver.find_elements(By.CSS_SELECTOR, SAVE_BTN)
            if save_buttons:
                return "ready", save_buttons[0]
            return False

        kind, element = self.wait.until(signup_outcome)

        if kind == "ineligible":
            message = element.text.strip()
            logger.error(f"Cannot register for event (user '{self.user_tag}'): {message}")
            raise RuntimeError(f"Cannot register for event: {message}")

        logger.debug(f"Finalize-registration button found for user '{self.user_tag}'.")

        try:
            element.click()
        except ElementClickInterceptedException:
            logger.warning(
                "Native click on finalize button was intercepted; falling back to JS click."
            )
            self.driver.execute_script("arguments[0].click();", element)

        logger.info(f"Clicked finalize-registration button for user '{self.user_tag}'.")

        time.sleep(30)
        logger.info(f"Successfully registered for the event (user '{self.user_tag}').")

    def close(self):
        """Closes the browser."""
        logger.info("Closing the web driver.")
        self.driver.quit()

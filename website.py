from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
import selenium.webdriver.support.expected_conditions as EC

from selenium.webdriver.chrome.options import Options

import re
from datetime import datetime
import time
import json
from urllib.parse import urlparse
from logging_config import get_logger

logger = get_logger(__name__)
logger.setLevel("DEBUG")

DATE_BOX = ".css-5j348m"
EVENT_BOX = "css-dik0t"
EXTRA_CONTENT_BOX = ".MuiGrid-root.MuiGrid-container.MuiGrid-wrap-xs-nowrap.css-a2e4ud"


class Website:
    def __init__(self, headless=True, wait_time=30):
        """ Initializes the web driver for the website interaction.
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

        self.wait_time = wait_time
        self.wait = WebDriverWait(self.driver, timeout=self.wait_time)
        logger.info("Web driver initialized.")

    def login(self, website_file="website_token.json"):
        """Logs into the website using the provided credentials."""
        if self.logged_in:
            logger.info("Already logged in.")
            return

        logger.info("Logging into the website.")
        with open(website_file, "r") as file:
            website_info = json.load(file)
        logger.debug("Website information loaded from file.")

        self.default_registration_time = website_info.get(
            "default_registration_time", None
        )

        login_url = website_info["login_url"]
        self.website_domain = urlparse(login_url).netloc.lower()
        logger.debug(f"Website domain parsed: {self.website_domain}")

        self.events_url = website_info["events_url"]

        self.driver.get(login_url)
        logger.debug(f"Navigated to login URL: {login_url}")
        self.wait.until(EC.element_to_be_clickable((By.NAME, "email"))).send_keys(
            website_info["email"]
        )
        logger.debug("Entered email.")
        self.wait.until(EC.element_to_be_clickable((By.NAME, "password"))).send_keys(
            website_info["password"]
        )
        logger.debug("Entered password.")
        login_button = self.wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(text(), 'Login')]")
            )
        )
        login_button.click()
        logger.debug("Clicked login button.")
        self.wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//button[contains(text(), 'Join')]")
            )
        )

        logger.info("Successfully logged into the website.")
        self.logged_in = True

    def _go_to_events_page(self):
        """Navigates to the events page."""
        logger.info(f"Navigating to events page: {self.events_url}")
        self.driver.get(self.events_url)
        logger.debug(f"Events page loaded: {self.events_url}")

        # Wait for the events to load
        self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, DATE_BOX)))

    def display_all_events(self):
        """
        Continuously clicks the "Load more" button on the events page until all events are loaded.
        This method counts the number of currently loaded event elements and clicks the "Load more" button
        as long as new events are being loaded. It waits for the "Load more" button to appear, clicks it,
        and repeats the process until no more new events are loaded.
        Raises:
            AssertionError: If the "Load more" button cannot be found.
        """

        # Ensure we are on the events page
        self._go_to_events_page()

        num_days_loaded = 0

        while num_days_loaded < (
            num_days_loaded := len(self.driver.find_elements(By.CSS_SELECTOR, DATE_BOX))
        ):
            logger.debug(f"clicking load more {num_days_loaded = }")

            try:
                load_more_button = self.wait.until(
                    EC.presence_of_element_located(
                        (By.XPATH, "//button[text()='Load more']")
                    )
                )
                logger.debug("Load more button found.")
            except Exception as e:
                logger.error("Button not found within 10 seconds:", e)
                raise AssertionError("Load more button not found")

            load_more_button.click()
            logger.debug("Clicked load more button.")

        logger.info("All events displayed.")

    def _find_event(self, event_date: str, time_range: str):
        """
        Finds an event based on the provided date and time range.
        This method searches for an event that matches the specified date and time range and returns its parent element.
        Args:
            event_date (str): The date string to search for in the event elements.
            time_range (str): The time range string to search for in the event elements.
        Returns:
            WebElement: The parent element of the found event.
        Raises:
            AssertionError: If no matching event is found.
        """

        date_time_elements = self.wait.until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    f"//h6[contains(text(), '{event_date}')]/following-sibling::h6[contains(text(), '{time_range}')]",
                )
            )
        )
        logger.debug(f"Event date and time elements found: {date_time_elements}")

        event = date_time_elements.find_element(
            By.XPATH,
            f"./ancestor::*[contains(@class, 'MuiCardContent-root') and contains(@class, '{EVENT_BOX}')]",
        )
        logger.debug(f"Parent element found: {event}")

        return event

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
            logger.error(
                f"No event found for date: {event_date}, time range: {time_range}"
            )
            return None, None

        try:
            access_date_element = event.find_element(
                By.XPATH, ".//*[contains(text(), 'not joinable')]"
            )
            logger.debug("Access date element found.")
        except Exception as e:
            logger.error("Access date element not found.", exc_info=True)
            return None, None
        
        logger.info("Access date element found.")
        join_date_text = access_date_element.text

        date_pattern = r"\b[A-Z][a-z]{2} \d{1,2}\b"
        match = re.search(date_pattern, join_date_text)

        if match:
            date_str = match.group(0)
            date = datetime.strptime(date_str, "%b %d")
            logger.debug(f"Extracted date string: {date_str}")

            if registration_time:
                registration_time = datetime.strptime(
                    registration_time, "%H:%M:%S"
                ).time()

                date = date.replace(
                    hour=registration_time.hour,
                    minute=registration_time.minute,
                    second=registration_time.second,
                )
                logger.debug(f"Registration time set: {registration_time}")

            now = datetime.now()

            # Adjust the year if the date has already passed this year
            if date.replace(year=now.year) < now:
                date = date.replace(year=now.year + 1)
            else:
                date = date.replace(year=now.year)

            logger.info(f"Extracted date: {date}")
        else:
            logger.info("No date found in the text.")
            date = None

        try:
            extra_content = event.find_element(By.CSS_SELECTOR, EXTRA_CONTENT_BOX)

            # Gather the text content of the remaining first element
            if extra_content:
                body_content = extra_content.text.replace("\n", " - ")
                logger.debug(f"Final body content: {body_content}")
            else:
                logger.debug("No nested content elements found.")
                body_content = ""
        except:
            body_content = ""

        return date, body_content

    def register_for_event(self, event_date: str, time_range: str):
        """Registers for the event."""

        self.display_all_events()
        event = self._find_event(event_date, time_range)

        join_button = WebDriverWait(event, self.wait_time).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    ".//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'join')]",
                )
            )
        )

        logger.debug("Join button found.")
        join_button.click()
        logger.info("Clicked join button.")

        time.sleep(30)
        logger.info("Successfully registered for the event.")

    def close(self):
        """Closes the browser."""
        logger.info("Closing the web driver.")
        self.driver.quit()


# Example usage:
# interactor = WebsiteInteractor()
# interactor.log_in("https://example.com/login", "user@example.com", "securepassword123")
# access_date = interactor.determine_access_date("https://example.com/event")
# interactor.register_for_event("https://example.com/event")
# interactor.close()

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


class Website:
    def __init__(self):
        logger.info("Initializing the web driver.")
        
        options = Options()
        options.headless = True


        service = ChromeService(executable_path='/usr/bin/chromedriver')
        self.driver = webdriver.Chrome(service=service, options=options)
        self.logged_in = False
        self.wait = WebDriverWait(self.driver, timeout=30)

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

    def determine_access_date(self, event_url: str, registration_time: datetime = None):
        """Determines the access date for the event."""
        logger.info(f"Determining access date for event: {event_url}")
        event_domain = urlparse(event_url).netloc.lower()
        logger.debug(f"Event domain parsed: {event_domain}")

        if self.website_domain != event_domain:
            logger.info("Event domain does not match the website domain.")
            return None

        if registration_time is None:
            registration_time = self.default_registration_time
        logger.debug(f"Using registration time: {registration_time}")

        self.driver.get(event_url)
        logger.debug(f"Navigated to event URL: {event_url}")
        access_date_element = self.wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//h6[contains(text(), 'not joinable')]")
            )
        )
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
            # extract additional information from the page
            # Use XPath to select all sibling elements that come after the header in the body.
            content_elements = self.driver.find_elements(By.XPATH, "//header/following-sibling::*")

            # Dive until we're out of the nested elements
            for _ in range(10):
                if len(content_elements) == 1:
                    content_elements = content_elements[0].find_elements(By.XPATH, "./*")
                else:
                    break

            # Gather the text content of the remaining first element
            body_content = content_elements[0].text.replace("\n", " - ")
        except:
            body_content = ""

        return date, body_content

    def register_for_event(self, event_url: str):
        """Registers for the event."""
        logger.info(f"Registering for the event: {event_url}")
        self.driver.get(event_url)
        logger.debug(f"Navigated to event URL: {event_url}")
        join_button = self.wait.until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Join')]"))
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

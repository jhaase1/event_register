from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
import selenium.webdriver.support.expected_conditions as EC
import re
from datetime import datetime
import time
import json
from urllib.parse import urlparse


class Website:
    def __init__(self):
        print("Initializing the web driver.")
        self.driver = webdriver.Chrome()
        self.logged_in = False
        self.wait = WebDriverWait(self.driver, timeout=30)

    def login(self, website_file="website_token.json"):
        """Logs into the website using the provided credentials."""
        if self.logged_in:
            print("Already logged in.")
            return
        
        print("Logging into the website.")
        with open(website_file, "r") as file:
            website_info = json.load(file)
        print("Website information loaded from file.")

        self.default_registration_time = website_info.get(
            "default_registration_time", None
        )

        login_url = website_info["login_url"]
        self.website_domain = urlparse(login_url).netloc.lower()
        print(f"Website domain parsed: {self.website_domain}")

        self.driver.get(login_url)
        print(f"Navigated to login URL: {login_url}")
        self.wait.until(EC.element_to_be_clickable((By.NAME, "email"))).send_keys(
            website_info["email"]
        )
        print("Entered email.")
        self.wait.until(EC.element_to_be_clickable((By.NAME, "password"))).send_keys(
            website_info["password"]
        )
        print("Entered password.")
        login_button = self.wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(text(), 'Login')]")
            )
        )
        login_button.click()
        print("Clicked login button.")
        self.wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//button[contains(text(), 'Join')]")
            )
        )

        print("Successfully logged into the website.")
        self.logged_in = True

    def determine_access_date(self, event_url: str, registration_time: datetime = None):
        """Determines the access date for the event."""
        print(f"Determining access date for event: {event_url}")
        event_domain = urlparse(event_url).netloc.lower()
        print(f"Event domain parsed: {event_domain}")

        if self.website_domain != event_domain:
            print("Event domain does not match the website domain.")
            return None

        if registration_time is None:
            registration_time = self.default_registration_time
        print(f"Using registration time: {registration_time}")

        self.driver.get(event_url)
        print(f"Navigated to event URL: {event_url}")
        access_date_element = self.wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//h6[contains(text(), 'not joinable')]")
            )
        )
        print("Access date element found.")
        text = access_date_element.text

        date_pattern = r"\b[A-Z][a-z]{2} \d{1,2}\b"
        match = re.search(date_pattern, text)

        if match:
            date_str = match.group(0)
            date = datetime.strptime(date_str, "%b %d")
            print(f"Extracted date string: {date_str}")

            if registration_time:
                registration_time = datetime.strptime(registration_time, "%H:%M:%S").time()

                date = date.replace(
                    hour=registration_time.hour,
                    minute=registration_time.minute,
                    second=registration_time.second,
                )
                print(f"Registration time set: {registration_time}")

            now = datetime.now()

            # Adjust the year if the date has already passed this year
            if date.replace(year=now.year) < now:
                date = date.replace(year=now.year + 1)
            else:
                date = date.replace(year=now.year)

            print(f"Extracted date: {date}")
        else:
            print("No date found in the text.")
            date = None

        return date

    def register_for_event(self, event_url: str):
        """Registers for the event."""
        print(f"Registering for the event: {event_url}")
        self.driver.get(event_url)
        print(f"Navigated to event URL: {event_url}")
        join_button = self.wait.until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Join')]"))
        )
        print("Join button found.")
        join_button.click()
        print("Clicked join button.")

        time.sleep(30)
        print("Successfully registered for the event.")

    def close(self):
        """Closes the browser."""
        print("Closing the web driver.")
        self.driver.quit()


# Example usage:
# interactor = WebsiteInteractor()
# interactor.log_in("https://example.com/login", "user@example.com", "securepassword123")
# access_date = interactor.determine_access_date("https://example.com/event")
# interactor.register_for_event("https://example.com/event")
# interactor.close()

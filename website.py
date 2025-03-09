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
        self.driver = webdriver.Chrome()
        self.wait = WebDriverWait(self.driver, timeout=30)

    def login(self, website_file="website_token.json"):
        """Logs into the website using the provided credentials."""
        with open(website_file, "r") as file:
            website_info = json.load(file)

        login_url = website_info["login_url"]
        self.website_domain = urlparse(login_url).netloc.lower()

        self.driver.get(login_url)
        self.wait.until(EC.element_to_be_clickable((By.NAME, "email"))).send_keys(
            website_info["email"]
        )
        self.wait.until(EC.element_to_be_clickable((By.NAME, "password"))).send_keys(
            website_info["password"]
        )
        login_button = self.wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(text(), 'Login')]")
            )
        )
        login_button.click()
        self.wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//button[contains(text(), 'Join')]")
            )
        )

    def determine_access_date(self, event_url: str, access_time: datetime = None):
        """Determines the access date for the event."""
        self.driver.get(event_url)
        access_date_element = self.wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//h6[contains(text(), 'not joinable')]")
            )
        )
        text = access_date_element.text

        date_pattern = r"\b[A-Z][a-z]{2} \d{1,2}\b"
        match = re.search(date_pattern, text)

        if match:
            date_str = match.group(0)
            date = datetime.strptime(date_str, "%b %d")

            if access_time:
                access_time = datetime.strptime(access_time, "%H:%M:%S").time()

                date = date.replace(
                    hour=access_time.hour,
                    minute=access_time.minute,
                    second=access_time.second,
                )

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
        event_domain = urlparse(event_url).netloc.lower()

        assert self.website_domain == event_domain, "Login URL and event URL must be from the same domain."

        self.driver.get(event_url)
        join_button = self.wait.until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Join')]"))
        )
        join_button.click()

        time.sleep(30)

    def close(self):
        """Closes the browser."""
        self.driver.quit()

# Example usage:
# interactor = WebsiteInteractor()
# interactor.log_in("https://example.com/login", "user@example.com", "securepassword123")
# access_date = interactor.determine_access_date("https://example.com/event")
# interactor.register_for_event("https://example.com/event")
# interactor.close()

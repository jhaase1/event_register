from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException

import website


class FakeElement:
    def __init__(self, text="", href=None):
        self.text = text
        self._href = href

    def get_attribute(self, name):
        assert name == "href"
        return self._href


class FakeEvent:
    """Stands in for an event-card element found on the events list page."""

    def __init__(self, register_label="register", dropin_text=None, href="https://app.courtreserve.com/Online/Events/Details/19992/ABC"):
        self._register_btn = FakeElement(text=register_label, href=href)
        self._dropin = [FakeElement(text=dropin_text)] if dropin_text is not None else []

    def find_element(self, by, selector):
        if selector == website.REGISTER_BTN:
            return self._register_btn
        raise NoSuchElementException(selector)

    def find_elements(self, by, selector):
        if selector == website.DROPIN_MSG:
            return self._dropin
        return []


class FakeDetailsDriver:
    """Stands in for the driver once navigated to an event's details page."""

    def __init__(self, restriction_text=None):
        self.gotten_urls = []
        self._restriction_text = restriction_text

    def get(self, url):
        self.gotten_urls.append(url)

    def find_elements(self, by, selector):
        if selector == website.RATING_NAMES and self._restriction_text is not None:
            return [FakeElement(text=self._restriction_text)]
        return []


class FakeWebDriverWait:
    def __init__(self, target, _timeout):
        self.target = target

    def until(self, locator):
        # EC.presence_of_element_located is monkeypatched to identity below,
        # so `locator` here is just the raw (by, selector) tuple; we only
        # need `until` to not raise.
        return True


def _make_site(monkeypatch, event, restriction_text=None, skill_level="Intermediate"):
    monkeypatch.setattr(website.EC, "presence_of_element_located", lambda locator: locator)

    site = website.Website.__new__(website.Website)
    site.driver = FakeDetailsDriver(restriction_text=restriction_text)
    site.wait = FakeWebDriverWait(site.driver, 5)
    site.wait_time = 5
    site.skill_level = skill_level
    site.default_registration_time = "20:00:00"
    site.display_all_events = lambda: None
    site._find_event = lambda *_args, **_kwargs: event
    return site


def test_event_actionable_now_with_no_restriction_returns_now(monkeypatch):
    event = FakeEvent(register_label="Register")
    site = _make_site(monkeypatch, event, restriction_text=None)

    date, summary = site.determine_access_date("SEP 1", "10:00am - 12:00pm")

    assert date is not None
    assert "Skill Level Restriction" not in summary


def test_matching_skill_restriction_still_actionable(monkeypatch):
    event = FakeEvent(register_label="Register")
    site = _make_site(monkeypatch, event, restriction_text="Intermediate, Advanced", skill_level="Intermediate")

    date, summary = site.determine_access_date("SEP 1", "10:00am - 12:00pm")

    assert date is not None
    assert "Skill Level Restriction: Intermediate, Advanced" in summary


def test_mismatched_skill_restriction_blocks_registration(monkeypatch):
    event = FakeEvent(register_label="Register")
    site = _make_site(monkeypatch, event, restriction_text="Beginner", skill_level="Intermediate")

    date, summary = site.determine_access_date("SEP 1", "10:00am - 12:00pm")

    assert date is None
    assert "Skill Level Restriction: Beginner" in summary


def test_no_skill_level_configured_assumes_eligible(monkeypatch):
    event = FakeEvent(register_label="Register")
    site = _make_site(monkeypatch, event, restriction_text="Beginner", skill_level=None)

    date, summary = site.determine_access_date("SEP 1", "10:00am - 12:00pm")

    assert date is not None


def test_not_yet_open_event_with_matching_restriction_computes_dropin_date(monkeypatch):
    event = FakeEvent(register_label="Details", dropin_text="Registration opens in 23 h and 49 min")
    site = _make_site(monkeypatch, event, restriction_text="Intermediate", skill_level="Intermediate")

    date, summary = site.determine_access_date("SEP 2", "11:00am - 1:00pm")

    assert date is not None
    assert date.hour == 20 and date.minute == 0 and date.second == 0

from selenium.common.exceptions import ElementClickInterceptedException
from selenium.webdriver.common.by import By

import website


class FakeElement:
    def __init__(self, raise_on_click=None):
        self._raise_on_click = raise_on_click
        self.click_calls = 0

    def click(self):
        self.click_calls += 1
        if self._raise_on_click is not None:
            exc, self._raise_on_click = self._raise_on_click, None
            raise exc


class FakeEvent:
    def __init__(self, element_map):
        self._element_map = element_map

    def resolve_condition(self, condition):
        by, xpath = condition
        assert by == By.XPATH
        if xpath in self._element_map:
            return self._element_map[xpath]
        raise TimeoutError(f"Element not found for xpath: {xpath}")


class FakeWebDriverWait:
    def __init__(self, target, _timeout):
        self.target = target

    def until(self, condition):
        return self.target.resolve_condition(condition)


class FakeDriver:
    def __init__(self):
        self.scrolled_elements = []
        self.js_clicked_elements = []

    def get(self, _url):
        pass

    def execute_script(self, script, *args):
        if "scrollIntoView" in script:
            self.scrolled_elements.append(args[0])
            return None
        if script.strip() == "arguments[0].click();":
            self.js_clicked_elements.append(args[0])
            args[0].click_calls += 1
            return None
        return None


def _make_site(driver, event):
    site = website.Website.__new__(website.Website)
    site.driver = driver
    site.wait_time = 5
    site.user_tag = "default"
    site.display_all_events = lambda: None
    site._find_event = lambda *_args, **_kwargs: event
    return site


def test_register_for_event_falls_back_to_js_click_when_intercepted(monkeypatch):
    join_button = FakeElement(
        raise_on_click=ElementClickInterceptedException("intercepted")
    )
    event = FakeEvent(
        {
            ".//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'join')]": join_button,
        }
    )
    driver = FakeDriver()

    monkeypatch.setattr(website, "WebDriverWait", FakeWebDriverWait)
    monkeypatch.setattr(website.EC, "element_to_be_clickable", lambda locator: locator)
    monkeypatch.setattr(website.time, "sleep", lambda *_args, **_kwargs: None)

    site = _make_site(driver, event)

    site.register_for_event("MON, MAY 5", "9:00am - 10:00am", event_url=None)

    assert driver.scrolled_elements == [join_button]
    assert driver.js_clicked_elements == [join_button]
    assert join_button.click_calls == 2


def test_register_for_event_uses_native_click_when_not_intercepted(monkeypatch):
    join_button = FakeElement()
    event = FakeEvent(
        {
            ".//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'join')]": join_button,
        }
    )
    driver = FakeDriver()

    monkeypatch.setattr(website, "WebDriverWait", FakeWebDriverWait)
    monkeypatch.setattr(website.EC, "element_to_be_clickable", lambda locator: locator)
    monkeypatch.setattr(website.time, "sleep", lambda *_args, **_kwargs: None)

    site = _make_site(driver, event)

    site.register_for_event("MON, MAY 5", "9:00am - 10:00am", event_url=None)

    assert driver.scrolled_elements == [join_button]
    assert driver.js_clicked_elements == []
    assert join_button.click_calls == 1

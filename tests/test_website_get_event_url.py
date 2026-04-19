from selenium.webdriver.common.by import By

import website


class FakeElement:
    def __init__(self, on_click=None):
        self._on_click = on_click
        self.clicked = False

    def click(self):
        self.clicked = True
        if self._on_click:
            self._on_click()


class FakeEvent:
    def __init__(self, element_map):
        self._element_map = element_map

    def resolve_condition(self, condition):
        by, xpath = condition
        assert by == By.XPATH
        if xpath in self._element_map:
            return self._element_map[xpath]
        raise TimeoutError(f"Element not found for xpath: {xpath}")


class FakeDriver:
    def __init__(self, element_map):
        self._element_map = element_map
        self.intercepted_clipboard = None
        self.clipboard_interceptor_installed = False

    def resolve_condition(self, condition):
        if callable(condition):
            result = condition(self)
            if result:
                return result
            raise TimeoutError("Condition callable returned falsy value")

        by, xpath = condition
        assert by == By.XPATH
        if xpath in self._element_map:
            return self._element_map[xpath]
        raise TimeoutError(f"Element not found for xpath: {xpath}")

    def execute_script(self, script):
        if "window.__interceptedClipboard = null" in script:
            self.intercepted_clipboard = None
            if "window.__clipboardInterceptorInstalled = true" in script:
                self.clipboard_interceptor_installed = True
            return None

        if script.strip() == "return window.__interceptedClipboard;":
            return self.intercepted_clipboard

        return None


class FakeWebDriverWait:
    def __init__(self, target, _timeout):
        self.target = target

    def until(self, condition):
        return self.target.resolve_condition(condition)


def test_get_event_url_selects_this_event_only_then_copy(monkeypatch):
    copied_url = "https://example.com/events/abc123"

    share_button = FakeElement()
    scope_option = FakeElement()

    driver = FakeDriver(
        {
            "//label[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'this event only')]": scope_option,
        }
    )

    copy_link_button = FakeElement(on_click=lambda: setattr(driver, "intercepted_clipboard", copied_url))
    driver._element_map[
        "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'copy link')]"
    ] = copy_link_button

    event = FakeEvent(
        {
            ".//button[contains(@aria-label, 'Share event')]": share_button,
        }
    )

    monkeypatch.setattr(website, "WebDriverWait", FakeWebDriverWait)
    monkeypatch.setattr(website.EC, "element_to_be_clickable", lambda locator: locator)
    monkeypatch.setattr(website.EC, "presence_of_element_located", lambda locator: locator)

    site = website.Website.__new__(website.Website)
    site.wait_time = 5
    site.driver = driver
    site.wait = FakeWebDriverWait(driver, site.wait_time)
    site.display_all_events = lambda: None
    site._find_event = lambda *_args, **_kwargs: event

    event_url = site.get_event_url("MON, MAY 5", "9:00am - 10:00am")

    assert share_button.clicked is True
    assert scope_option.clicked is True
    assert copy_link_button.clicked is True
    assert driver.clipboard_interceptor_installed is True
    assert event_url == copied_url


def test_get_event_url_returns_none_when_copy_link_missing(monkeypatch):
    share_button = FakeElement()
    event = FakeEvent(
        {
            ".//button[contains(@aria-label, 'Share event')]": share_button,
        }
    )
    driver = FakeDriver({})

    monkeypatch.setattr(website, "WebDriverWait", FakeWebDriverWait)
    monkeypatch.setattr(website.EC, "element_to_be_clickable", lambda locator: locator)
    monkeypatch.setattr(website.EC, "presence_of_element_located", lambda locator: locator)

    site = website.Website.__new__(website.Website)
    site.wait_time = 5
    site.driver = driver
    site.wait = FakeWebDriverWait(driver, site.wait_time)
    site.display_all_events = lambda: None
    site._find_event = lambda *_args, **_kwargs: event

    event_url = site.get_event_url("MON, MAY 5", "9:00am - 10:00am")

    assert share_button.clicked is True
    assert event_url is None

from selenium.common.exceptions import ElementClickInterceptedException

import website


class FakeElement:
    def __init__(self, text="", href=None, raise_on_click=None):
        self.text = text
        self._href = href
        self._raise_on_click = raise_on_click
        self.click_calls = 0

    def get_attribute(self, name):
        assert name == "href"
        return self._href

    def click(self):
        self.click_calls += 1
        if self._raise_on_click is not None:
            exc, self._raise_on_click = self._raise_on_click, None
            raise exc


class FakeEvent:
    def __init__(self, register_btn):
        self._register_btn = register_btn

    def find_element(self, by, selector):
        assert selector == website.REGISTER_BTN
        return self._register_btn


class FakeWebDriverWait:
    def __init__(self, target, _timeout):
        self.target = target

    def until(self, condition):
        result = condition(self.target)
        if not result:
            raise TimeoutError("condition never became truthy")
        return result


class FakeDriver:
    def __init__(self, ineligible_text=None, save_btn=None):
        self.js_clicked_elements = []
        self.gotten_urls = []
        self._ineligible_text = ineligible_text
        self._save_btn = save_btn

    def get(self, url):
        self.gotten_urls.append(url)

    def find_elements(self, by, selector):
        if selector == website.INELIGIBLE_XPATH and self._ineligible_text is not None:
            return [FakeElement(text=self._ineligible_text)]
        if selector == website.SAVE_BTN and self._save_btn is not None:
            return [self._save_btn]
        return []

    def execute_script(self, script, *args):
        if script.strip() == "arguments[0].click();":
            self.js_clicked_elements.append(args[0])
            args[0].click_calls += 1
            return None
        return None


def _make_site(driver, event):
    site = website.Website.__new__(website.Website)
    site.driver = driver
    site.wait_time = 5
    site.wait = FakeWebDriverWait(driver, site.wait_time)
    site.user_tag = "default"
    site.display_all_events = lambda: None
    site._find_event = lambda *_args, **_kwargs: event
    return site


def test_register_for_event_falls_back_to_js_click_when_intercepted(monkeypatch):
    save_btn = FakeElement(raise_on_click=ElementClickInterceptedException("intercepted"))
    register_btn = FakeElement(text="Register", href="https://app.courtreserve.com/Online/Events/SignUpToEvent/19992?eventId=1")
    driver = FakeDriver(save_btn=save_btn)
    monkeypatch.setattr(website.time, "sleep", lambda *_args, **_kwargs: None)

    site = _make_site(driver, FakeEvent(register_btn))

    site.register_for_event("MON, MAY 5", "9:00am - 10:00am", event_url=None)

    assert driver.gotten_urls == ["https://app.courtreserve.com/Online/Events/SignUpToEvent/19992?eventId=1"]
    assert driver.js_clicked_elements == [save_btn]
    assert save_btn.click_calls == 2


def test_register_for_event_uses_native_click_when_not_intercepted(monkeypatch):
    save_btn = FakeElement()
    register_btn = FakeElement(text="Register", href="https://app.courtreserve.com/Online/Events/SignUpToEvent/19992?eventId=1")
    driver = FakeDriver(save_btn=save_btn)
    monkeypatch.setattr(website.time, "sleep", lambda *_args, **_kwargs: None)

    site = _make_site(driver, FakeEvent(register_btn))

    site.register_for_event("MON, MAY 5", "9:00am - 10:00am", event_url=None)

    assert driver.js_clicked_elements == []
    assert save_btn.click_calls == 1


def test_register_for_event_navigates_directly_to_event_url_when_given(monkeypatch):
    save_btn = FakeElement()
    register_btn = FakeElement(text="Register", href="https://app.courtreserve.com/Online/Events/SignUpToEvent/19992?eventId=1")
    driver = FakeDriver(save_btn=save_btn)
    monkeypatch.setattr(website.time, "sleep", lambda *_args, **_kwargs: None)

    site = website.Website.__new__(website.Website)
    site.driver = driver
    site.wait_time = 5
    site.wait = FakeWebDriverWait(driver, site.wait_time)
    site.user_tag = "default"

    def fake_presence(locator):
        return lambda _driver: register_btn

    monkeypatch.setattr(website.EC, "presence_of_element_located", fake_presence)

    event_url = "https://app.courtreserve.com/Online/Events/Details/19992/ABC123"
    site.register_for_event("MON, MAY 5", "9:00am - 10:00am", event_url=event_url)

    assert driver.gotten_urls == [
        event_url,
        "https://app.courtreserve.com/Online/Events/SignUpToEvent/19992?eventId=1",
    ]
    assert save_btn.click_calls == 1


def test_register_for_event_raises_when_ineligible(monkeypatch):
    register_btn = FakeElement(text="Register", href="https://app.courtreserve.com/Online/Events/SignUpToEvent/19992?eventId=1")
    driver = FakeDriver(ineligible_text="You are not eligible. See restrictions below.")
    monkeypatch.setattr(website.time, "sleep", lambda *_args, **_kwargs: None)

    site = _make_site(driver, FakeEvent(register_btn))

    try:
        site.register_for_event("MON, MAY 5", "9:00am - 10:00am", event_url=None)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "not eligible" in str(e).lower()

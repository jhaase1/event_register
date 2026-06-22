import website
from selenium.common.exceptions import TimeoutException


class _FakeElement:
    pass


class _FakeDriver:
    def __init__(self, counts):
        self.counts = counts
        self.index = 0
        self.scroll_calls = 0

    def execute_script(self, script):
        if script == "window.scrollTo(0, document.body.scrollHeight);":
            self.scroll_calls += 1
            if self.index < len(self.counts) - 1:
                self.index += 1

    def find_elements(self, by, value):
        return [_FakeElement() for _ in range(self.counts[self.index])]


class _FakeWait:
    def __init__(self, driver):
        self.driver = driver

    def until(self, condition):
        result = condition(self.driver)
        if result:
            return result
        raise TimeoutException("Timed out waiting for more events")


def test_display_all_events_uses_scroll_mode_by_default(monkeypatch):
    site = website.Website.__new__(website.Website)
    calls = []

    monkeypatch.setattr(site, "_go_to_events_page", lambda: calls.append("go"))
    monkeypatch.setattr(site, "_display_all_events_by_scrolling", lambda: calls.append("scroll"))
    monkeypatch.setattr(site, "_display_all_events_by_button", lambda: calls.append("button"))

    site.display_all_events()

    assert calls == ["go", "scroll"]


def test_display_all_events_can_switch_back_to_button_mode(monkeypatch):
    site = website.Website.__new__(website.Website)
    site.event_loading_mode = website.EventLoadingMode.BUTTON
    calls = []

    monkeypatch.setattr(site, "_go_to_events_page", lambda: calls.append("go"))
    monkeypatch.setattr(site, "_display_all_events_by_scrolling", lambda: calls.append("scroll"))
    monkeypatch.setattr(site, "_display_all_events_by_button", lambda: calls.append("button"))

    site.display_all_events()

    assert calls == ["go", "button"]


def test_display_all_events_by_scrolling_stops_when_count_stalls(monkeypatch):
    site = website.Website.__new__(website.Website)
    site.driver = _FakeDriver([2, 4, 4, 4])
    site.wait = _FakeWait(site.driver)

    site._display_all_events_by_scrolling()

    assert site.driver.scroll_calls == 3
import website


class _FakeElement:
    def __init__(self, text=""):
        self.text = text


class _FakeDriver:
    def __init__(self, states):
        """states: list of dicts like
        {"date_box_count": int, "indicator": bool, "loaded_text": str or None}
        """
        self.states = states
        self.index = 0
        self.window_scroll_calls = 0
        self.indicator_scroll_calls = 0

    def execute_script(self, script, *args):
        if script == "window.scrollTo(0, document.body.scrollHeight);":
            self.window_scroll_calls += 1
            if self.index < len(self.states) - 1:
                self.index += 1
        elif "scrollIntoView" in script:
            self.indicator_scroll_calls += 1

    def find_elements(self, by, value):
        state = self.states[self.index]
        if value == website.DATE_BOX:
            return [_FakeElement() for _ in range(state.get("date_box_count", 0))]
        if value == website.LOAD_MORE_INDICATOR_XPATH:
            return [_FakeElement()] if state.get("indicator") else []
        if value == website.LOADED_RANGE_XPATH:
            text = state.get("loaded_text")
            return [_FakeElement(text)] if text is not None else []
        return []


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
    """Legacy signal: no 'load more' indicator on the page at all, DATE_BOX count stops growing."""
    site = website.Website.__new__(website.Website)
    site.driver = _FakeDriver(
        [
            {"date_box_count": 2},
            {"date_box_count": 4},
            {"date_box_count": 4},
            {"date_box_count": 4},
        ]
    )

    site._display_all_events_by_scrolling()

    assert site.driver.window_scroll_calls == 3
    assert site.driver.indicator_scroll_calls == 0


def test_display_all_events_by_scrolling_continues_via_indicator_when_count_stalls(monkeypatch):
    """New signal: DATE_BOX count is flat the whole time, but the loaded-range text keeps
    changing and then the indicator disappears - scrolling should keep going on that alone."""
    site = website.Website.__new__(website.Website)
    site.driver = _FakeDriver(
        [
            {"date_box_count": 4, "indicator": True, "loaded_text": "Loaded: Jul 14 - Jul 20"},
            {"date_box_count": 4, "indicator": True, "loaded_text": "Loaded: Jul 14 - Jul 27"},
            {"date_box_count": 4, "indicator": False, "loaded_text": None},
        ]
    )

    site._display_all_events_by_scrolling()

    assert site.driver.window_scroll_calls == 2
    assert site.driver.indicator_scroll_calls == 2
    assert site.driver.index == 2


def test_display_all_events_by_scrolling_stops_when_both_signals_stall(monkeypatch):
    site = website.Website.__new__(website.Website)
    site.wait_time = 1
    site.driver = _FakeDriver(
        [
            {"date_box_count": 4, "indicator": True, "loaded_text": "Loaded: Jul 14 - Jul 20"},
            {"date_box_count": 4, "indicator": True, "loaded_text": "Loaded: Jul 14 - Jul 20"},
        ]
    )

    site._display_all_events_by_scrolling()

    assert site.driver.window_scroll_calls == 2

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


def _make_site(states, wait_time=None):
    """Builds a Website with a fake driver and a fake _scroll_down that advances
    the fake driver's state on each call, standing in for a real wheel scroll."""
    site = website.Website.__new__(website.Website)
    site.driver = _FakeDriver(states)
    if wait_time is not None:
        site.wait_time = wait_time

    scroll_calls = {"count": 0}

    def fake_scroll_down(amount=1200):
        scroll_calls["count"] += 1
        if site.driver.index < len(site.driver.states) - 1:
            site.driver.index += 1

    site._scroll_down = fake_scroll_down
    return site, scroll_calls


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


def test_display_all_events_by_scrolling_uses_real_wheel_scroll(monkeypatch):
    """window.scrollTo/scrollIntoView never fire a real wheel event, so scrolling must
    go through _scroll_down (ActionChains) rather than driver.execute_script directly."""
    site, scroll_calls = _make_site([{"date_box_count": 2}, {"date_box_count": 2}])

    def fail_if_called(*args, **kwargs):
        raise AssertionError("execute_script must not be used to trigger scrolling")

    site.driver.execute_script = fail_if_called

    site._display_all_events_by_scrolling()

    assert scroll_calls["count"] >= 1


def test_display_all_events_by_scrolling_stops_when_count_stalls(monkeypatch):
    """Legacy signal: no 'load more' indicator on the page at all, DATE_BOX count stops growing."""
    site, scroll_calls = _make_site(
        [
            {"date_box_count": 2},
            {"date_box_count": 4},
            {"date_box_count": 4},
            {"date_box_count": 4},
        ]
    )

    site._display_all_events_by_scrolling()

    assert scroll_calls["count"] == 3


def test_display_all_events_by_scrolling_continues_via_indicator_when_count_stalls(monkeypatch):
    """New signal: DATE_BOX count is flat the whole time, but the loaded-range text keeps
    changing and then the indicator disappears - scrolling should keep going on that alone."""
    site, scroll_calls = _make_site(
        [
            {"date_box_count": 4, "indicator": True, "loaded_text": "Loaded: Jul 14 - Jul 20"},
            {"date_box_count": 4, "indicator": True, "loaded_text": "Loaded: Jul 14 - Jul 27"},
            {"date_box_count": 4, "indicator": False, "loaded_text": None},
        ]
    )

    site._display_all_events_by_scrolling()

    assert scroll_calls["count"] == 2
    assert site.driver.index == 2


def test_display_all_events_by_scrolling_stops_when_both_signals_stall(monkeypatch):
    site, scroll_calls = _make_site(
        [
            {"date_box_count": 4, "indicator": True, "loaded_text": "Loaded: Jul 14 - Jul 20"},
            {"date_box_count": 4, "indicator": True, "loaded_text": "Loaded: Jul 14 - Jul 20"},
        ],
        wait_time=1,
    )

    site._display_all_events_by_scrolling()

    assert scroll_calls["count"] == 2

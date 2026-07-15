import website


class _FakeElement:
    pass


class _FakeDriver:
    def __init__(self, states):
        """states: list of dicts like {"date_box_count": int, "indicator": bool}"""
        self.states = states
        self.index = 0

    def find_elements(self, by, value):
        state = self.states[self.index]
        if value == website.DATE_BOX:
            return [_FakeElement() for _ in range(state.get("date_box_count", 0))]
        if value == website.LOAD_MORE_INDICATOR_XPATH:
            return [_FakeElement()] if state.get("indicator") else []
        return []


def _make_site(states, wait_time=None):
    """Builds a Website with a fake driver and a fake _scroll_down that advances
    the fake driver's state on each call, standing in for a real wheel scroll."""
    site = website.Website.__new__(website.Website)
    site.driver = _FakeDriver(states)
    if wait_time is not None:
        site.wait_time = wait_time

    scroll_calls = {"count": 0}

    def fake_scroll_down(amount=1200, indicator_element=None):
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


class _FakeActionChains:
    calls = []

    def __init__(self, driver):
        self.driver = driver

    def scroll_by_amount(self, delta_x, delta_y):
        self.calls.append((delta_x, delta_y))
        return self

    def perform(self):
        pass


def test_scroll_down_jumps_to_bottom_then_dispatches_wheel_scroll(monkeypatch):
    """window.scrollTo alone never fires a real wheel event, so the JS jump must be
    followed by an actual ActionChains wheel scroll, not stand in for it."""
    site = website.Website.__new__(website.Website)
    js_calls = []
    site.driver = type("_Driver", (), {"execute_script": lambda self, script, *args: js_calls.append((script, args))})()

    _FakeActionChains.calls = []
    monkeypatch.setattr(website, "ActionChains", _FakeActionChains)

    site._scroll_down()

    assert js_calls == [("window.scrollTo(0, document.body.scrollHeight);", ())]
    assert _FakeActionChains.calls == [(0, 1200)]


def test_scroll_down_scrolls_indicator_into_view_when_present(monkeypatch):
    site = website.Website.__new__(website.Website)
    js_calls = []
    site.driver = type("_Driver", (), {"execute_script": lambda self, script, *args: js_calls.append((script, args))})()

    _FakeActionChains.calls = []
    monkeypatch.setattr(website, "ActionChains", _FakeActionChains)

    fake_indicator = object()
    site._scroll_down(indicator_element=fake_indicator)

    assert js_calls == [("arguments[0].scrollIntoView({block: 'center'});", (fake_indicator,))]
    assert _FakeActionChains.calls == [(0, 1200)]


def test_display_all_events_by_scrolling_stops_when_count_stalls(monkeypatch):
    """No 'load more' indicator on the page at all: DATE_BOX count stops growing for
    three consecutive scrolls (max_stalled_rounds) before we give up."""
    site, scroll_calls = _make_site(
        [
            {"date_box_count": 2},
            {"date_box_count": 4},
            {"date_box_count": 4},
            {"date_box_count": 4},
        ]
    )

    site._display_all_events_by_scrolling()

    assert scroll_calls["count"] == 4


def test_display_all_events_by_scrolling_ignores_indicator_text_and_relies_on_count(monkeypatch):
    """Regression test: the indicator's loaded-date-range text used to keep advancing every
    scroll even when no new events appeared, which masked a stall indefinitely. Count must be
    the thing that stalls things out even while the indicator is still present and 'active'."""
    site, scroll_calls = _make_site(
        [{"date_box_count": 17, "indicator": True}] * 5,
        wait_time=1,
    )

    site._display_all_events_by_scrolling()

    assert scroll_calls["count"] == 3


def test_display_all_events_by_scrolling_stops_when_indicator_disappears(monkeypatch):
    """DATE_BOX count is flat the whole time, but the indicator disappearing is still an
    immediate, authoritative 'done' signal."""
    site, scroll_calls = _make_site(
        [
            {"date_box_count": 4, "indicator": True},
            {"date_box_count": 4, "indicator": True},
            {"date_box_count": 4, "indicator": False},
        ]
    )

    site._display_all_events_by_scrolling()

    assert scroll_calls["count"] == 2
    assert site.driver.index == 2

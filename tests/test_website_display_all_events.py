import website


class _FakeElement:
    def __init__(self, text=""):
        self.text = text


class _FakeDriver:
    def __init__(self, states):
        """states: list of dicts like {"card_count": int, "total": int}"""
        self.states = states
        self.index = 0

    def find_elements(self, by, value):
        state = self.states[self.index]
        if value == website.EVENT_CARD:
            return [_FakeElement() for _ in range(state.get("card_count", 0))]
        return []

    def find_element(self, by, value):
        if value == website.EVENTS_COUNT_SPAN:
            state = self.states[self.index]
            total = state.get("total")
            if total is None:
                raise website.NoSuchElementException("no total in this state")
            return _FakeElement(text=f"{total} Events Found")
        raise website.NoSuchElementException(value)


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


def test_display_all_events_calls_go_then_scroll(monkeypatch):
    site = website.Website.__new__(website.Website)
    calls = []

    monkeypatch.setattr(site, "_go_to_events_page", lambda: calls.append("go"))
    monkeypatch.setattr(site, "_display_all_events_by_scrolling", lambda: calls.append("scroll"))

    site.display_all_events()

    assert calls == ["go", "scroll"]


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


def test_display_all_events_by_scrolling_stops_when_count_matches_total(monkeypatch):
    """The '<N> Events Found' total is authoritative: stop as soon as the loaded
    card count reaches it, without needing to stall out first."""
    site, scroll_calls = _make_site(
        [
            {"card_count": 20, "total": 20},
        ]
    )

    site._display_all_events_by_scrolling()

    assert scroll_calls["count"] == 0


def test_display_all_events_by_scrolling_scrolls_until_total_reached(monkeypatch):
    site, scroll_calls = _make_site(
        [
            {"card_count": 20, "total": 45},
            {"card_count": 40, "total": 45},
            {"card_count": 45, "total": 45},
        ]
    )

    site._display_all_events_by_scrolling()

    assert scroll_calls["count"] == 2


def test_display_all_events_by_scrolling_stops_when_count_stalls_without_total(monkeypatch):
    """No total available at all: card count stops growing for three consecutive
    scrolls (max_stalled_rounds) before we give up."""
    site, scroll_calls = _make_site(
        [
            {"card_count": 2},
            {"card_count": 4},
            {"card_count": 4},
            {"card_count": 4},
        ],
        wait_time=1,
    )

    site._display_all_events_by_scrolling()

    assert scroll_calls["count"] == 4

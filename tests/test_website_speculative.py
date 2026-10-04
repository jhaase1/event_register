from datetime import datetime, timedelta

import pytest
from selenium.common.exceptions import NoSuchElementException

import website


class FakeElement:
    def __init__(self, text="", href=None):
        self.text = text
        self._href = href

    def get_attribute(self, name):
        assert name == "href"
        return self._href


class FakeCard:
    def __init__(self, date_time=None, name=None, category=None, register_label=None, dropin=None, href=None):
        self._fields = {}
        if date_time is not None:
            self._fields[website.DATE_TIME_SECTION] = FakeElement(date_time)
        if name is not None:
            self._fields[website.EVENT_NAME] = FakeElement(name)
        if category is not None:
            self._fields[website.CATEGORY_NAME] = FakeElement(category)
        if register_label is not None:
            self._fields[website.REGISTER_BTN] = FakeElement(register_label, href=href)
        if dropin is not None:
            self._fields[website.DROPIN_MSG] = FakeElement(dropin)

    def find_element(self, by, selector):
        if selector in self._fields:
            return self._fields[selector]
        raise NoSuchElementException(selector)

    def find_elements(self, by, selector):
        return [self._fields[selector]] if selector in self._fields else []


class FakeListDriver:
    def __init__(self, cards):
        self.cards = cards

    def find_elements(self, by, selector):
        assert selector == website.EVENT_CARD
        return self.cards


def _site_with_cards(cards):
    site = website.Website.__new__(website.Website)
    site.driver = FakeListDriver(cards)
    return site


# --- scan_listed_events -----------------------------------------------------


def test_scan_listed_events_parses_every_card():
    cards = [
        FakeCard(
            date_time="Tue, Oct 13th, 9a - 11a",
            name="Intermediate Drop-in",
            category="Open Play",
            register_label="Details",
            dropin="Registration opens in 1 day and 23 h",
        ),
        FakeCard(
            date_time="Wed, Oct 7th, 1p - 2:30p",
            name="Advanced Drop-in",
            category="Open Play",
            register_label="Register",
        ),
    ]

    scanned = _site_with_cards(cards).scan_listed_events()

    assert scanned == [
        {
            "month_day": (10, 13),
            "time": (9, 0, 11, 0),
            "name": "Intermediate Drop-in",
            "category": "Open Play",
            "actionable": False,
            "opens_in": timedelta(days=1, hours=23),
        },
        {
            "month_day": (10, 7),
            "time": (13, 0, 14, 30),
            "name": "Advanced Drop-in",
            "category": "Open Play",
            "actionable": True,
            "opens_in": None,
        },
    ]


def test_scan_listed_events_skips_cards_without_parseable_date_time():
    cards = [
        FakeCard(name="No date section"),
        FakeCard(date_time="TBD", name="Unparseable"),
        FakeCard(date_time="Tue, Oct 13th, 9a - 11a"),
    ]

    scanned = _site_with_cards(cards).scan_listed_events()

    assert len(scanned) == 1
    assert scanned[0]["name"] is None
    assert scanned[0]["actionable"] is False


# --- find_event_url ---------------------------------------------------------


class _Event:
    def __init__(self, href, has_button=True):
        self._btn = FakeElement("Register", href=href) if has_button else None

    def find_elements(self, by, selector):
        assert selector == website.REGISTER_BTN
        return [self._btn] if self._btn else []


def _polling_site(monkeypatch, results):
    """results: sequence returned by successive _find_event calls."""
    site = website.Website.__new__(website.Website)
    calls = {"display": 0, "find": 0, "sleeps": []}
    remaining = list(results)

    def display():
        calls["display"] += 1

    def find(event_date, time_range, timeout=None):
        calls["find"] += 1
        return remaining.pop(0) if remaining else None

    site.display_all_events = display
    site._find_event = find
    monkeypatch.setattr(website.time, "sleep", lambda s: calls["sleeps"].append(s))
    return site, calls


def test_find_event_url_returns_href_once_posted(monkeypatch):
    site, calls = _polling_site(monkeypatch, [None, None, _Event("https://x/Details/1")])

    url = site.find_event_url("Tue, Oct 13", "9a - 11a", until=datetime.now() + timedelta(minutes=5), poll_seconds=7)

    assert url == "https://x/Details/1"
    assert calls["display"] == 3
    assert calls["sleeps"] == [7, 7]


def test_find_event_url_keeps_polling_when_card_has_no_link_or_button(monkeypatch):
    site, calls = _polling_site(
        monkeypatch,
        [_Event(None), _Event("ignored", has_button=False), _Event("https://x/Details/2")],
    )

    url = site.find_event_url("Tue, Oct 13", "9a - 11a", until=datetime.now() + timedelta(minutes=5))

    assert url == "https://x/Details/2"
    assert calls["find"] == 3


def test_find_event_miss_logs_at_debug_when_polling(monkeypatch, caplog):
    site = website.Website.__new__(website.Website)
    site.driver = FakeListDriver([])

    class _NeverWait:
        def __init__(self, *_a, **_k):
            pass

        def until(self, _fn):
            raise website.TimeoutException()

    monkeypatch.setattr(website, "WebDriverWait", _NeverWait)
    caplog.set_level("DEBUG", logger="website")

    assert site._find_event("Tue, Oct 13", "9a - 11a", timeout=2) is None
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_find_event_url_tries_once_even_when_deadline_passed(monkeypatch):
    site, calls = _polling_site(monkeypatch, [])

    url = site.find_event_url("Tue, Oct 13", "9a - 11a", until=datetime.now() - timedelta(seconds=1))

    assert url is None
    assert calls["find"] == 1


def test_find_event_url_gives_up_at_deadline(monkeypatch):
    site, calls = _polling_site(monkeypatch, [])
    clock = {"now": datetime(2026, 10, 6, 14, 0, 0)}

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(website, "datetime", FakeDatetime)
    monkeypatch.setattr(website.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + timedelta(seconds=s)))

    url = site.find_event_url(
        "Tue, Oct 13", "9a - 11a", until=datetime(2026, 10, 6, 14, 1, 0), poll_seconds=20
    )

    assert url is None
    assert calls["find"] == 4  # t = 0, 20, 40, 60


# --- EventNotFound ----------------------------------------------------------


def test_determine_access_date_raises_event_not_found():
    site = website.Website.__new__(website.Website)
    site.default_registration_time = "14:00:00"
    site.display_all_events = lambda: None
    site._find_event = lambda *_a, **_k: None

    with pytest.raises(website.EventNotFound):
        site.determine_access_date("Tue, Oct 13", "9a - 11a")


def test_register_for_event_without_url_raises_event_not_found():
    site = website.Website.__new__(website.Website)
    site.display_all_events = lambda: None
    site._find_event = lambda *_a, **_k: None

    with pytest.raises(website.EventNotFound):
        site.register_for_event("Tue, Oct 13", "9a - 11a", event_url=None)


# --- registration clock parsing ----------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [("20:00:00", (20, 0, 0)), ("20:00", (20, 0, 0)), (" 08:30 ", (8, 30, 0)), ("8pm", None), (None, None)],
)
def test_parse_clock(text, expected):
    parsed = website.parse_clock(text)
    assert (parsed.hour, parsed.minute, parsed.second) == expected if expected else parsed is None


class _CountdownCard:
    def __init__(self, dropin):
        self._btn = FakeElement("Details", href=None)
        self._dropin = FakeElement(dropin)

    def find_element(self, by, selector):
        if selector == website.REGISTER_BTN:
            return self._btn
        raise NoSuchElementException(selector)

    def find_elements(self, by, selector):
        return [self._dropin] if selector == website.DROPIN_MSG else []


def _countdown_site(registration_clock):
    site = website.Website.__new__(website.Website)
    site.default_registration_time = registration_clock
    site.skill_level = None
    site.display_all_events = lambda: None
    site._find_event = lambda *_a, **_k: _CountdownCard("Registration opens in 2 days and 5 h")
    site._card_summary = lambda _e: "summary"
    return site


def test_determine_access_date_accepts_clock_without_seconds():
    when, _summary = _countdown_site("20:00").determine_access_date("Tue, Oct 13", "9a - 11a")
    assert (when.hour, when.minute, when.second) == (20, 0, 0)


def test_determine_access_date_ignores_malformed_clock_instead_of_raising():
    when, _summary = _countdown_site("8pm").determine_access_date("Tue, Oct 13", "9a - 11a")
    assert when is not None

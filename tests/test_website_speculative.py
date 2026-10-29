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


OPENS = datetime(2026, 10, 26, 20, 0, 0)
POLLS = [OPENS - timedelta(seconds=10), OPENS + timedelta(seconds=1)]


def _polling_site(monkeypatch, results, start=None, load_seconds=0, visible_before_scroll=True):
    """results: one entry per poll: what the lookup finds (None for a miss), or
    an exception the page load raises. Uses a fake clock that sleep() and each
    page load (load_seconds) advance."""
    site = website.Website.__new__(website.Website)
    clock = {"now": start or OPENS - timedelta(minutes=1)}
    calls = {"polled_at": [], "loads": [], "scrolls": 0}
    remaining = list(results)
    current = {"event": None, "scrolled": False}

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    def go_to_events_page(attempts=3, wait_time=None):
        calls["polled_at"].append(clock["now"])
        calls["loads"].append((attempts, wait_time))
        clock["now"] += timedelta(seconds=load_seconds)
        result = remaining.pop(0) if remaining else None
        if isinstance(result, Exception):
            raise result
        current.update(event=result, scrolled=False)

    def scroll():
        calls["scrolls"] += 1
        current["scrolled"] = True

    def find(event_date, time_range, timeout=None):
        if visible_before_scroll or current["scrolled"]:
            return current["event"]
        return None

    site._go_to_events_page = go_to_events_page
    site._display_all_events_by_scrolling = scroll
    site._find_event = find
    monkeypatch.setattr(website, "datetime", FakeDatetime)
    monkeypatch.setattr(
        website.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + timedelta(seconds=s))
    )
    return site, calls


def test_find_event_url_polls_before_then_after_opening(monkeypatch):
    site, calls = _polling_site(monkeypatch, [None, _Event("https://x/Details/1")])

    url = site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS)

    assert url == "https://x/Details/1"
    assert calls["polled_at"] == POLLS


def test_find_event_url_stops_at_first_sighting(monkeypatch):
    site, calls = _polling_site(monkeypatch, [_Event("https://x/Details/1")])

    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS) == "https://x/Details/1"
    assert calls["polled_at"] == [POLLS[0]]


def test_find_event_url_returns_none_after_last_poll(monkeypatch):
    site, calls = _polling_site(monkeypatch, [])

    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS) is None
    assert calls["polled_at"] == POLLS


def test_find_event_url_honours_any_configured_schedule_in_order(monkeypatch):
    site, calls = _polling_site(monkeypatch, [])
    offsets = [5, -10, 1, -2, 30]
    times = [OPENS + timedelta(seconds=s) for s in offsets]

    site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=times)

    assert calls["polled_at"] == sorted(times)


def test_find_event_url_polls_past_times_immediately(monkeypatch):
    late = OPENS + timedelta(seconds=30)
    site, calls = _polling_site(monkeypatch, [], start=late)

    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS) is None
    assert calls["polled_at"] == [late, late]


def test_find_event_url_treats_card_without_link_or_button_as_not_posted(monkeypatch):
    site, calls = _polling_site(
        monkeypatch,
        [_Event(None), _Event("ignored", has_button=False), _Event("https://x/Details/2")],
    )
    times = POLLS + [OPENS + timedelta(seconds=5)]

    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=times) == "https://x/Details/2"
    assert len(calls["polled_at"]) == 3


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


# --- slow page loads while polling (Oct 26 failure) ---------------------------


def test_find_event_url_page_load_timeout_is_just_a_missed_poll(monkeypatch):
    site, calls = _polling_site(
        monkeypatch,
        [website.TimeoutException("events list didn't render"), _Event("https://x/Details/3")],
    )

    url = site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS)

    assert url == "https://x/Details/3"
    assert calls["polled_at"] == POLLS


def test_find_event_url_repeated_timeouts_return_none_instead_of_raising(monkeypatch):
    site, calls = _polling_site(
        monkeypatch,
        [website.TimeoutException("slow"), website.TimeoutException("slow")],
    )

    # None -> "not posted": the row stays speculative instead of being marked failed.
    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS) is None
    assert len(calls["polled_at"]) == 2


class _LoadDriver:
    def __init__(self):
        self.gets = 0

    def get(self, url):
        self.gets += 1


class _FlakyWait:
    def __init__(self, failures):
        self.failures = failures

    def until(self, _condition):
        if self.failures > 0:
            self.failures -= 1
            raise website.TimeoutException("slow")
        return True


def _events_page_site(failures):
    site = website.Website.__new__(website.Website)
    site.events_url = "https://x/Events"
    site.driver = _LoadDriver()
    site.wait = _FlakyWait(failures)
    return site


def test_events_page_reloads_after_a_slow_render():
    site = _events_page_site(failures=2)
    site._go_to_events_page()
    assert site.driver.gets == 3


def test_events_page_gives_up_after_three_slow_renders():
    site = _events_page_site(failures=3)
    with pytest.raises(website.TimeoutException):
        site._go_to_events_page()
    assert site.driver.gets == 3


# --- round-1 review: quick looks, overruns, dead sessions --------------------


def test_each_poll_is_one_page_load_and_only_the_last_gets_the_full_wait(monkeypatch):
    site, calls = _polling_site(monkeypatch, [])
    times = POLLS + [OPENS + timedelta(seconds=5)]

    site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=times, page_wait=7)

    # None = the default (full) wait: nothing comes after the last poll, so a
    # slow render is worth waiting out.
    assert calls["loads"] == [(1, 7), (1, 7), (1, None)]


def test_poll_skips_full_scroll_when_card_is_among_first_rendered(monkeypatch):
    site, calls = _polling_site(monkeypatch, [_Event("https://x/Details/1")])

    site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS)

    assert calls["scrolls"] == 0


def test_poll_scrolls_for_cards_further_down(monkeypatch):
    site, calls = _polling_site(
        monkeypatch, [_Event("https://x/Details/1")], visible_before_scroll=False
    )

    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS) == "https://x/Details/1"
    assert calls["scrolls"] == 1


def test_slow_poll_overrunning_the_next_time_runs_the_next_poll_right_away(monkeypatch):
    # The -10 s load takes 15 s, finishing at +5 s, past the +1 s slot.
    site, calls = _polling_site(monkeypatch, [], load_seconds=15)

    site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS)

    assert calls["polled_at"] == [POLLS[0], OPENS + timedelta(seconds=5)]


def test_dead_browser_session_stops_polling(monkeypatch):
    site, calls = _polling_site(monkeypatch, [website.InvalidSessionIdException("session deleted")])

    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS) is None
    assert len(calls["polled_at"]) == 1


def test_other_webdriver_errors_are_a_missed_poll(monkeypatch):
    site, calls = _polling_site(
        monkeypatch, [website.WebDriverException("net::ERR_CONNECTION_RESET"), _Event("https://x/Details/4")]
    )

    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS) == "https://x/Details/4"


class _FlakyGetDriver(_LoadDriver):
    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    def get(self, url):
        super().get(url)
        if self.failures > 0:
            self.failures -= 1
            raise website.TimeoutException("page load timed out")


def test_events_page_retries_when_the_page_load_itself_times_out():
    site = _events_page_site(failures=0)
    site.driver = _FlakyGetDriver(failures=1)
    site._go_to_events_page()
    assert site.driver.gets == 2


def test_events_page_uses_a_short_wait_when_asked(monkeypatch):
    site = _events_page_site(failures=0)
    used = []

    class RecordingWait:
        def __init__(self, driver, timeout):
            used.append(timeout)

        def until(self, _condition):
            return True

    monkeypatch.setattr(website, "WebDriverWait", RecordingWait)
    site._go_to_events_page(attempts=1, wait_time=8)
    assert used == [8]


# --- round-2 review -----------------------------------------------------------


def test_list_seen_flag_false_when_no_poll_rendered_the_list(monkeypatch):
    site, _calls = _polling_site(
        monkeypatch, [website.TimeoutException("slow"), website.TimeoutException("slow")]
    )
    site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS)
    assert site.list_seen_while_polling is False


def test_list_seen_flag_true_when_a_poll_rendered_the_list(monkeypatch):
    site, _calls = _polling_site(monkeypatch, [website.TimeoutException("slow"), None])
    site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS)
    assert site.list_seen_while_polling is True


def test_real_poll_path_treats_render_timeouts_as_misses(monkeypatch):
    # Runs the real _poll_for_event/_go_to_events_page with a fake driver whose
    # list never renders, rather than faking those methods.
    site = _events_page_site(failures=0)
    clock = {"now": OPENS - timedelta(minutes=1)}
    waits = []

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    class NeverRenders:
        def __init__(self, driver, timeout, **_kwargs):
            waits.append(timeout)

        def until(self, _condition):
            raise website.TimeoutException("list never rendered")

    site.wait = NeverRenders(None, 30)
    waits.clear()
    monkeypatch.setattr(website, "WebDriverWait", NeverRenders)
    monkeypatch.setattr(website, "datetime", FakeDatetime)
    monkeypatch.setattr(website.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + timedelta(seconds=s)))

    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS, page_wait=10) is None
    assert site.driver.gets == 2  # one load per poll, no extra retries
    assert waits == [10]  # short wait for the first poll; the last uses site.wait
    assert site.list_seen_while_polling is False


def test_find_event_ignores_stale_cards_while_scanning(monkeypatch):
    seen = {}

    class RecordingWait:
        def __init__(self, driver, timeout, ignored_exceptions=None, **_kwargs):
            seen["ignored"] = ignored_exceptions

        def until(self, _condition):
            raise website.TimeoutException()

    monkeypatch.setattr(website, "WebDriverWait", RecordingWait)
    site = website.Website.__new__(website.Website)
    site.driver = FakeListDriver([])

    site._find_event("Mon, Nov 2nd", "9:30a - 11a", timeout=1)

    assert website.StaleElementReferenceException in seen["ignored"]


def test_get_with_retry_retries_one_page_load_timeout():
    site = website.Website.__new__(website.Website)
    site.driver = _FlakyGetDriver(failures=1)
    site._get_with_retry("https://x/signup")
    assert site.driver.gets == 2


def test_get_with_retry_gives_up_after_two_timeouts():
    site = website.Website.__new__(website.Website)
    site.driver = _FlakyGetDriver(failures=2)
    with pytest.raises(website.TimeoutException):
        site._get_with_retry("https://x/signup")


# --- round-3 review -----------------------------------------------------------


class _StaleOnceEvent:
    def __init__(self, href, stale_reads):
        self._btn = FakeElement("Register", href=href)
        self.stale_reads = stale_reads

    def find_elements(self, by, selector):
        if self.stale_reads > 0:
            self.stale_reads -= 1
            raise website.StaleElementReferenceException("re-rendered")
        return [self._btn]


def test_stale_card_after_match_is_looked_up_again(monkeypatch):
    site, _calls = _polling_site(monkeypatch, [_StaleOnceEvent("https://x/Details/5", stale_reads=1)])

    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS) == "https://x/Details/5"


def test_lookup_that_never_completes_does_not_count_as_seeing_the_list(monkeypatch):
    site, _calls = _polling_site(
        monkeypatch,
        [_StaleOnceEvent("x", stale_reads=99), _StaleOnceEvent("x", stale_reads=99)],
    )

    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS) is None
    assert site.list_seen_while_polling is False


def test_login_loads_its_page_with_retry(monkeypatch, tmp_path):
    import json as _json

    token = tmp_path / "default.json"
    token.write_text(_json.dumps({
        "login_url": "https://x/login", "events_url": "https://x/events",
        "email": "a@b.c", "password": "pw",
    }))
    monkeypatch.setattr(website, "get_website_token_file", lambda tag: str(token))
    site = website.Website.__new__(website.Website)
    site.logged_in = False
    loaded = []

    def fake_get_with_retry(url, attempts=2):
        loaded.append(url)
        raise website.TimeoutException("login page timed out twice")

    site._get_with_retry = fake_get_with_retry

    with pytest.raises(website.TimeoutException):
        site.login("default")
    assert loaded == ["https://x/login"]

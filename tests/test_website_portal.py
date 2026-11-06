"""website.py against a fake of the booking site's redesigned portal (Nov 2026)."""

import os
from datetime import date, datetime, timedelta

import pytest

import website
from fake_portal import DateRow, FakePortal, Series

TODAY = date.today()
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def row(day, time_text, **kwargs):
    return DateRow(day.month, day.day, WEEKDAYS[day.weekday()], time_text, **kwargs)


def req(day):
    """How a user types a date: 'Wed, Nov 18th'."""
    return f"{WEEKDAYS[day.weekday()]}, {day:%b} {day.day}"


D1 = TODAY + timedelta(days=6)  # open now
D2 = D1 + timedelta(days=7)     # opens in ~5 days
D3 = D2 + timedelta(days=7)     # opens in ~12 days


def standard_series():
    return [
        Series(101, [
            row(D1, "11a - 1p", slots="8 of 28 spots remaining"),
            row(D2, "11a - 1p", state="not_open", opens_in="Single date registration opens in 4 days and 23 h"),
            row(D3, "11a - 1p", state="not_open", opens_in="Single date registration opens in 11 days and 23 h"),
        ], restriction="Intermediate, Advanced", details_code="SERIES101"),
        Series(202, [
            row(D1, "8a - 9:30a", state="full"),
        ], details_code="SERIES202"),
        Series(303, [
            row(D1, "4p - 6p"),
        ], name="$3 Novice Pickleball", category="Novice Open Play", restriction="Beginner",
            details_code="SERIES303"),
    ]


def make_site(series=None, skill="Intermediate", clock="20:00:00"):
    fake = FakePortal(series if series is not None else standard_series())
    site = website.Website.__new__(website.Website)
    site.driver = fake
    site.wait_time = 1
    site.skill_level = skill
    site.default_registration_time = clock
    site.events_url = fake.events_url
    site.user_tag = "default"
    return site, fake


# --- events list ------------------------------------------------------------


def test_scan_lists_one_entry_per_series_with_its_next_date():
    site, _fake = make_site()
    site.display_all_events()

    scanned = site.scan_listed_events()

    assert [(s["month_day"], s["time"], s["actionable"]) for s in scanned] == [
        ((D1.month, D1.day), (11, 0, 13, 0), True),
        ((D1.month, D1.day), (8, 0, 9, 30), True),   # full but open (waitlist)
        ((D1.month, D1.day), (16, 0, 18, 0), True),
    ]
    assert all(s["opens_in"] is None for s in scanned)
    assert scanned[0]["name"] == "$3 Intermediate Pickleball"
    assert scanned[0]["category"] == "Intermediate Open Play"


def test_scan_marks_not_yet_open_series_as_not_actionable():
    site, _fake = make_site([Series(1, [row(D2, "8a - 9:30a", state="not_open")], details_code="S1")])
    site.display_all_events()
    assert site.scan_listed_events()[0]["actionable"] is False


def test_total_events_count_reads_results_header():
    site, _fake = make_site()
    site.display_all_events()
    assert site._total_events_count() == 3


def test_list_that_never_renders_times_out_with_a_readable_message():
    site, fake = make_site()
    fake.render_list = False

    with pytest.raises(website.TimeoutException) as error:
        site._go_to_events_page(attempts=2)

    assert "the events list" in str(error.value)
    assert fake.events_url in str(error.value)
    assert fake.gets.count(fake.events_url) == 2


# --- determine_access_date --------------------------------------------------


def test_open_date_is_actionable_now_with_restriction_in_summary():
    site, _fake = make_site()

    when, summary = site.determine_access_date(req(D1), "11a - 1p")

    assert abs((when - datetime.now()).total_seconds()) < 5
    assert "Skill Level Restriction: Intermediate, Advanced" in summary
    assert "$3 Intermediate Pickleball" in summary


def test_later_date_in_series_uses_its_own_countdown_snapped_to_clock():
    site, _fake = make_site()

    when, _summary = site.determine_access_date(req(D2), "11a - 1p")

    expected_day = (datetime.now() + timedelta(days=4, hours=23)).date()
    assert (when.date(), when.hour, when.minute, when.second) == (expected_day, 20, 0, 0)


def test_lookup_records_every_series_date_with_its_countdown():
    site, _fake = make_site()

    site.determine_access_date(req(D1), "11a - 1p")

    rows = site.last_details_rows
    assert [(r["month_day"], r["opens_in"]) for r in rows] == [
        ((D1.month, D1.day), None),
        ((D2.month, D2.day), timedelta(days=4, hours=23)),
        ((D3.month, D3.day), timedelta(days=11, hours=23)),
    ]
    assert rows[0]["actionable"] is True and rows[1]["actionable"] is False


def test_full_session_raises_event_full():
    site, _fake = make_site()
    with pytest.raises(website.EventFull):
        site.determine_access_date(req(D1), "8a - 9:30a")


def test_restriction_excluding_skill_level_raises():
    site, _fake = make_site()
    with pytest.raises(website.SkillLevelIneligible) as error:
        site.determine_access_date(req(D1), "4p - 6p")
    assert str(error.value) == "That's a Beginner skill level session, current settings list you as Intermediate."


def test_no_skill_level_configured_assumes_eligible():
    site, _fake = make_site(skill=None)
    when, _summary = site.determine_access_date(req(D1), "4p - 6p")
    assert when is not None


@pytest.mark.parametrize("requested_day, time_range", [
    (D1, "1p - 3p"),                      # no series at that time
    (D1 + timedelta(days=1), "11a - 1p"),  # wrong weekday
    (D3 + timedelta(days=7), "11a - 1p"),  # beyond the series' listed dates
])
def test_dates_not_in_any_listed_series_raise_event_not_found(requested_day, time_range):
    site, _fake = make_site()
    with pytest.raises(website.EventNotFound):
        site.determine_access_date(req(requested_day), time_range)


def test_date_without_weekday_and_with_long_times_is_matched():
    site, _fake = make_site()
    when, _summary = site.determine_access_date(f"{D2:%b} {D2.day}", "11:00am - 1:00pm")
    assert when.hour == 20


def test_clock_without_seconds_is_accepted():
    site, _fake = make_site(clock="20:00")
    when, _summary = site.determine_access_date(req(D2), "11a - 1p")
    assert (when.hour, when.minute) == (20, 0)


def test_malformed_clock_is_ignored_instead_of_raising():
    site, _fake = make_site(clock="8pm")
    when, _summary = site.determine_access_date(req(D2), "11a - 1p")
    assert when is not None


def test_picks_the_series_that_actually_has_the_date():
    # Two series at the same weekday/time; only the second lists D2.
    series = [
        Series(1, [row(D1, "11a - 1p")], details_code="ONLYD1"),
        Series(2, [row(D1, "11a - 1p"), row(D2, "11a - 1p", state="not_open")], details_code="BOTH"),
    ]
    site, _fake = make_site(series)

    url = site.get_event_url(req(D2), "11a - 1p")

    assert "BOTH" in url


# --- get_event_url ----------------------------------------------------------


def test_get_event_url_returns_series_details_url():
    site, _fake = make_site()
    assert "SERIES101" in site.get_event_url(req(D1), "11a - 1p")


def test_get_event_url_returns_none_when_not_found():
    site, _fake = make_site()
    assert site.get_event_url(req(D1), "1p - 3p") is None


# --- register_for_event -----------------------------------------------------


def _details_url(code):
    return f"https://x/Online/Events/Details/1/{code}?resId=1"


def test_register_finalizes_on_the_signup_page_and_verifies():
    series = standard_series()
    site, fake = make_site(series)

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[0].registered is True
    assert fake.gets[0] == _details_url("SERIES101")
    assert any("SignUpToEvent" in url for url in fake.gets)
    # Re-checked the details page afterwards.
    assert fake.gets[-1].startswith("https://x/Online/Events/Details/1/SERIES101")


def test_register_raises_when_finalize_does_not_take_effect():
    series = standard_series()
    series[0].on_register = "nothing"
    site, _fake = make_site(series)

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert "still shows 'register'" in str(error.value)
    assert series[0].rows[0].registered is False


def test_register_refuses_to_finalize_a_different_date():
    series = standard_series()
    series[0].on_register = "wrong_date"
    site, _fake = make_site(series)

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert "not finalizing" in str(error.value)
    assert all(not r.registered for r in series[0].rows)


def test_register_reports_ineligible_message():
    series = standard_series()
    series[0].on_register = "ineligible"
    site, _fake = make_site(series)

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert "not eligible" in str(error.value)


def test_register_full_session_raises_event_full():
    site, _fake = make_site()
    with pytest.raises(website.EventFull):
        site.register_for_event(req(D1), "8a - 9:30a", _details_url("SERIES202"))


def test_register_when_already_registered_does_nothing():
    series = standard_series()
    series[0].rows[0].registered = True
    site, fake = make_site(series)

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert not any("SignUpToEvent" in url for url in fake.gets)


def test_register_selects_the_requested_date_when_header_shows_another():
    series = standard_series()
    series[0].rows[1].state = "open"  # D2 is open too; header shows D1 by default
    site, _fake = make_site(series)

    site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[1].registered is True
    assert series[0].rows[0].registered is False


def test_register_refuses_a_date_not_open_yet():
    series = standard_series()
    series[0].selected = 1
    site, _fake = make_site(series)

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert "may not be open" in str(error.value)


def test_register_checks_skill_restriction_before_clicking():
    series = standard_series()
    site, fake = make_site(series)

    with pytest.raises(website.SkillLevelIneligible):
        site.register_for_event(req(D1), "4p - 6p", _details_url("SERIES303"))
    assert not any("SignUpToEvent" in url for url in fake.gets)


def test_register_without_url_finds_the_series_first():
    series = standard_series()
    site, _fake = make_site(series)

    site.register_for_event(req(D1), "11a - 1p", None)

    assert series[0].rows[0].registered is True


def test_register_without_url_raises_event_not_found():
    site, _fake = make_site()
    with pytest.raises(website.EventNotFound):
        site.register_for_event(req(D1), "1p - 3p", None)


# --- find_event_url polling ---------------------------------------------------

OPENS = datetime(2026, 10, 26, 20, 0, 0)
POLLS = [OPENS - timedelta(seconds=10), OPENS + timedelta(seconds=1)]


def _polling_site(monkeypatch, results, start=None, load_seconds=0):
    """results: one per poll - a URL (found), None (not found), or an
    exception the lookup raises. Uses a fake clock."""
    site = website.Website.__new__(website.Website)
    clock = {"now": start or OPENS - timedelta(minutes=1)}
    calls = {"polled_at": [], "waits": []}
    remaining = list(results)

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    def find_occurrence(event_date, time_range, list_wait=None, attempts=3):
        calls["polled_at"].append(clock["now"])
        calls["waits"].append((attempts, list_wait))
        clock["now"] += timedelta(seconds=load_seconds)
        result = remaining.pop(0) if remaining else None
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise website.EventNotFound("not listed")
        return result, {}, {}

    site._find_occurrence = find_occurrence
    monkeypatch.setattr(website, "datetime", FakeDatetime)
    monkeypatch.setattr(website.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + timedelta(seconds=s)))
    return site, calls


def test_polls_before_then_after_opening(monkeypatch):
    site, calls = _polling_site(monkeypatch, [None, "https://x/Details/1"])
    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS) == "https://x/Details/1"
    assert calls["polled_at"] == POLLS


def test_stops_at_first_sighting(monkeypatch):
    site, calls = _polling_site(monkeypatch, ["https://x/Details/1"])
    site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS)
    assert calls["polled_at"] == [POLLS[0]]


def test_returns_none_after_last_poll(monkeypatch):
    site, calls = _polling_site(monkeypatch, [])
    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS) is None
    assert calls["polled_at"] == POLLS


def test_any_configured_schedule_is_polled_in_order(monkeypatch):
    site, calls = _polling_site(monkeypatch, [])
    times = [OPENS + timedelta(seconds=s) for s in (5, -10, 1, -2, 30)]
    site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=times)
    assert calls["polled_at"] == sorted(times)


def test_past_times_are_polled_immediately(monkeypatch):
    late = OPENS + timedelta(seconds=30)
    site, calls = _polling_site(monkeypatch, [], start=late)
    site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS)
    assert calls["polled_at"] == [late, late]


def test_each_poll_is_one_list_load_and_only_the_last_gets_the_full_wait(monkeypatch):
    site, calls = _polling_site(monkeypatch, [])
    times = POLLS + [OPENS + timedelta(seconds=5)]
    site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=times, page_wait=7)
    assert calls["waits"] == [(1, 7), (1, 7), (1, None)]


def test_slow_poll_overrunning_the_next_time_runs_the_next_right_away(monkeypatch):
    site, calls = _polling_site(monkeypatch, [], load_seconds=15)
    site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS)
    assert calls["polled_at"] == [POLLS[0], OPENS + timedelta(seconds=5)]


def test_page_load_timeout_is_just_a_missed_poll(monkeypatch):
    site, calls = _polling_site(monkeypatch, [website.TimeoutException("slow"), "https://x/Details/3"])
    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS) == "https://x/Details/3"


def test_other_webdriver_errors_are_a_missed_poll(monkeypatch):
    site, calls = _polling_site(
        monkeypatch, [website.WebDriverException("net::ERR_CONNECTION_RESET"), "https://x/Details/4"]
    )
    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS) == "https://x/Details/4"


def test_dead_browser_session_stops_polling(monkeypatch):
    site, calls = _polling_site(monkeypatch, [website.InvalidSessionIdException("gone")])
    assert site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS) is None
    assert len(calls["polled_at"]) == 1


def test_list_seen_flag_false_when_no_poll_saw_the_list(monkeypatch):
    site, _calls = _polling_site(monkeypatch, [website.TimeoutException("slow"), website.TimeoutException("slow")])
    site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS)
    assert site.list_seen_while_polling is False


def test_list_seen_flag_true_when_a_poll_saw_the_list(monkeypatch):
    site, _calls = _polling_site(monkeypatch, [website.TimeoutException("slow"), None])
    site.find_event_url("Mon, Nov 2nd", "9:30a - 11a", poll_times=POLLS)
    assert site.list_seen_while_polling is True


def test_real_poll_path_finds_a_series_posted_between_polls(monkeypatch):
    late_series = Series(9, [row(D2, "9:30a - 11a")], details_code="LATE")
    site, fake = make_site([])
    clock = {"now": OPENS - timedelta(minutes=1)}

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    def sleep(seconds):
        clock["now"] += timedelta(seconds=seconds)
        if clock["now"] >= OPENS:
            fake.series[late_series.card_id] = late_series  # posted at opening

    monkeypatch.setattr(website, "datetime", FakeDatetime)
    monkeypatch.setattr(website.time, "sleep", sleep)
    # resolve_year needs a real "today" relative to D2.
    monkeypatch.setattr(website, "resolve_year", lambda m, d, today: date(D2.year, m, d))

    url = site.find_event_url(req(D2), "9:30a - 11a", poll_times=POLLS)

    assert "LATE" in url
    assert site.list_seen_while_polling is True


def test_real_poll_path_treats_a_list_that_never_renders_as_misses(monkeypatch):
    site, fake = make_site([])
    fake.render_list = False
    monkeypatch.setattr(website.time, "sleep", lambda s: None)
    times = [datetime.now() - timedelta(seconds=2), datetime.now() - timedelta(seconds=1)]

    assert site.find_event_url(req(D2), "9:30a - 11a", poll_times=times, page_wait=1) is None
    assert fake.gets.count(fake.events_url) == 2  # one load per poll
    assert site.list_seen_while_polling is False


# --- diagnostics ----------------------------------------------------------------


def test_capture_failure_saves_screenshot_and_html(tmp_path, monkeypatch):
    monkeypatch.setattr(website, "FAILURE_DIR", str(tmp_path / "failures"))
    site, _fake = make_site()
    site.display_all_events()

    paths = site.capture_failure("register default")

    assert len(paths) == 2
    assert paths[0].endswith(".png") and paths[1].endswith(".html")
    html = open(paths[1], encoding="utf-8").read()
    assert "events found" in html and "url=" in html


def test_capture_failure_prunes_old_snapshots(tmp_path, monkeypatch):
    folder = tmp_path / "failures"
    folder.mkdir()
    old = folder / "old.png"
    old.write_bytes(b"x")
    stale = datetime.now().timestamp() - (website.FAILURE_RETENTION_DAYS + 1) * 86400
    os.utime(old, (stale, stale))
    monkeypatch.setattr(website, "FAILURE_DIR", str(folder))
    site, _fake = make_site()

    site.capture_failure("x")

    assert not old.exists()


def test_capture_failure_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(website, "FAILURE_DIR", str(tmp_path / "failures"))
    site = website.Website.__new__(website.Website)

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("driver is gone")

    site.driver = Broken()
    assert site.capture_failure("x") == []
    assert list((tmp_path / "failures").iterdir()) == []


# --- parsing and small helpers ------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("Tue\n10\nNov", (11, 10)), ("SAT 14 NOV", (11, 14)), ("Mon\n1\nDec", (12, 1)), ("", None), ("Tue", None),
])
def test_parse_row_month_day(text, expected):
    assert website._parse_row_month_day(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("20:00:00", (20, 0, 0)), ("20:00", (20, 0, 0)), (" 08:30 ", (8, 30, 0)), ("8pm", None), (None, None),
])
def test_parse_clock(text, expected):
    parsed = website.parse_clock(text)
    assert ((parsed.hour, parsed.minute, parsed.second) if parsed else None) == expected


@pytest.mark.parametrize("text, expected", [
    ("Single date registration opens in 4 days and 23 h", timedelta(days=4, hours=23)),
    ("Registration opens in 1 day and 23 h", timedelta(days=1, hours=23)),
    ("opens in 23 h and 49 min", timedelta(hours=23, minutes=49)),
    ("opens in 45 sec", timedelta(seconds=45)),
    ("no countdown", None),
])
def test_parse_opens_in(text, expected):
    assert website._parse_opens_in(text) == expected


class _FlakyGetDriver:
    def __init__(self, failures):
        self.failures = failures
        self.gets = 0

    def get(self, url):
        self.gets += 1
        if self.failures > 0:
            self.failures -= 1
            raise website.TimeoutException("page load timed out")


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


def test_chrome_is_quit_if_setup_after_launch_fails(monkeypatch):
    class FakeChrome:
        quit_called = False

        def __init__(self, *a, **k):
            pass

        def set_page_load_timeout(self, seconds):
            raise RuntimeError("driver rejected timeout")

        def quit(self):
            FakeChrome.quit_called = True

    monkeypatch.setattr(website.webdriver, "Chrome", FakeChrome)
    with pytest.raises(RuntimeError):
        website.Website(headless=False)
    assert FakeChrome.quit_called


def test_login_loads_its_page_with_retry(monkeypatch, tmp_path):
    import json

    token = tmp_path / "default.json"
    token.write_text(json.dumps({
        "login_url": "https://x/login", "events_url": "https://x/events", "email": "a@b.c", "password": "pw",
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

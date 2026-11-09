"""website.py against a fake of the booking site's redesigned portal (Nov 2026)."""

import os
from datetime import date, datetime, timedelta

import pytest

import website
from fake_portal import DateRow, FakePortal, Series

TODAY = date.today()


@pytest.fixture(autouse=True)
def failure_dir(tmp_path, monkeypatch):
    """Failure snapshots (taken before a re-check) go to a temp folder, never logs/."""
    folder = tmp_path / "failures"
    monkeypatch.setattr(website, "FAILURE_DIR", str(folder))
    return folder


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


def test_prefers_the_series_the_user_can_register_for_in_a_shared_slot():
    series = [
        Series(1, [row(D1, "11a - 1p")], restriction="Advanced", details_code="ADVANCED"),
        Series(2, [row(D1, "11a - 1p")], restriction="Intermediate", details_code="INTERMEDIATE"),
    ]
    site, _fake = make_site(series)

    when, summary = site.determine_access_date(req(D1), "11a - 1p")

    assert when is not None
    assert "Skill Level Restriction: Intermediate" in summary
    assert "INTERMEDIATE" in site.last_details_url


def test_prefers_a_series_with_room_over_a_full_one_in_a_shared_slot():
    series = [
        Series(1, [row(D1, "11a - 1p", state="full")], details_code="FULLONE"),
        Series(2, [row(D1, "11a - 1p")], details_code="ROOMY"),
    ]
    site, _fake = make_site(series)

    assert "ROOMY" in site.get_event_url(req(D1), "11a - 1p")


def test_falls_back_to_the_ineligible_series_when_none_fits():
    series = [
        Series(1, [row(D1, "11a - 1p")], restriction="Advanced", details_code="ADV1"),
        Series(2, [row(D1, "11a - 1p")], restriction="Beginner", details_code="BEG2"),
    ]
    site, fake = make_site(series)

    with pytest.raises(website.SkillLevelIneligible) as error:
        site.determine_access_date(req(D1), "11a - 1p")

    assert "Advanced" in str(error.value)  # the first match, re-opened
    assert "ADV1" in fake.current_url


def test_eligible_series_rendered_only_after_scrolling_is_preferred():
    series = [
        Series(1, [row(D1, "11a - 1p")], restriction="Advanced", details_code="ADVANCED"),
        Series(2, [row(D1, "11a - 1p")], restriction="Intermediate", details_code="INTERMEDIATE"),
    ]
    site, fake = make_site(series)
    fake.lazy_cards = 1  # only the Advanced card renders until scrolled

    assert "INTERMEDIATE" in site.get_event_url(req(D1), "11a - 1p")


def test_right_series_rendered_only_after_scrolling_is_found():
    # The rendered card is a same-slot series that doesn't list D2.
    series = [
        Series(1, [row(D1, "11a - 1p")], details_code="ONLYD1"),
        Series(2, [row(D1, "11a - 1p"), row(D2, "11a - 1p", state="not_open")], details_code="BOTH"),
    ]
    site, fake = make_site(series)
    fake.lazy_cards = 1

    assert "BOTH" in site.get_event_url(req(D2), "11a - 1p")


def test_fully_rendered_list_is_not_reloaded_after_the_last_candidate():
    series = [Series(1, [row(D1, "11a - 1p")], details_code="ONLYD1")]
    site, fake = make_site(series)

    with pytest.raises(website.EventNotFound):
        site.determine_access_date(req(D2), "11a - 1p")

    assert fake.gets.count(fake.events_url) == 1


@pytest.mark.parametrize("order", ["full_first", "restricted_first"])
def test_full_eligible_series_beats_a_restricted_one_when_none_fits(order):
    full = Series(1, [row(D1, "11a - 1p", state="full")], restriction="Intermediate", details_code="FULLINT")
    restricted = Series(2, [row(D1, "11a - 1p")], restriction="Advanced", details_code="ADVANCED")
    site, _fake = make_site([full, restricted] if order == "full_first" else [restricted, full])

    with pytest.raises(website.EventFull):
        site.determine_access_date(req(D1), "11a - 1p")


def test_a_card_rendered_only_after_scrolling_is_still_opened():
    # The match is the second candidate, reached after reloading the list,
    # and the reloaded list only renders the first card until it's scrolled.
    series = [
        Series(1, [row(D1, "11a - 1p")], details_code="ONLYD1"),
        Series(2, [row(D1, "11a - 1p"), row(D2, "11a - 1p", state="not_open")], details_code="BOTH"),
    ]
    site, fake = make_site(series)
    real_get = fake.get

    def get(url):
        real_get(url)
        if fake.gets.count(fake.events_url) > 1:
            fake.lazy_cards = 1

    fake.get = get

    assert "BOTH" in site.get_event_url(req(D2), "11a - 1p")


def test_a_card_missing_from_the_reloaded_list_is_skipped_not_a_timeout():
    series = [
        Series(1, [row(D1, "11a - 1p")], details_code="ONLYD1"),
        Series(2, [row(D1, "11a - 1p"), row(D2, "11a - 1p", state="not_open")], details_code="GONE"),
    ]
    site, fake = make_site(series)
    real_get = fake.get

    def get(url):
        real_get(url)
        if fake.gets.count(fake.events_url) > 1:
            fake.series.pop("2", None)  # withdrawn between list loads

    fake.get = get

    with pytest.raises(website.EventNotFound) as error:
        site.determine_access_date(req(D2), "11a - 1p")

    assert "1 not on the reloaded list" in str(error.value)


def test_details_wait_requires_date_rows_not_just_the_list_container():
    site, fake = make_site()
    fake.get(_details_url("SERIES101"))
    original = fake._details_page

    def rows_not_rendered(s):
        children = original(s)
        for child in children:
            if child.testid == "events-details-dates-list":
                child.children = []
        return children

    fake._details_page = rows_not_rendered

    with pytest.raises(website.TimeoutException) as error:
        site._wait_for_details_page()

    assert "dates" in str(error.value)


def test_details_wait_tolerates_a_header_without_button_or_message():
    site, fake = make_site()
    fake.get(_details_url("SERIES101"))
    original = fake._details_page

    def without_button(s):
        return [c for c in original(s) if "register-btn" not in (c.testid or "")]

    fake._details_page = without_button

    site._wait_for_details_page()  # no exception: the label check reports it


def test_polling_passes_its_short_wait_to_the_details_page(monkeypatch):
    site, _fake = make_site()
    waits = []
    original = site._wait_for_details_page

    def recording(timeout=None):
        waits.append(timeout)
        original(timeout)

    monkeypatch.setattr(site, "_wait_for_details_page", recording)

    site._find_occurrence(req(D1), "11a - 1p", list_wait=2, attempts=1)

    assert waits == [2]


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


def test_register_selects_the_requested_date_when_a_row_click_selects_it():
    series = standard_series()
    series[0].rows[1].state = "open"  # D2 is open too; header shows D1 by default
    series[0].row_click_selects = True  # opt-in: not confirmed on the live site
    site, _fake = make_site(series)

    site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[1].registered is True
    assert series[0].rows[0].registered is False


def test_register_stops_when_a_row_click_does_not_select_the_date():
    series = standard_series()
    series[0].rows[1].state = "open"
    site, fake = make_site(series)

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert "selecting that date didn't work" in str(error.value)
    assert not isinstance(error.value, website.RegistrationUnverified)
    assert not any("SignUpToEvent" in url for url in fake.gets)  # Register never clicked
    assert all(not r.registered for r in series[0].rows)


def test_header_resets_to_its_default_date_on_every_load():
    series = standard_series()
    series[0].row_click_selects = True
    site, fake = make_site(series)
    fake.get(_details_url("SERIES101"))
    site._select_date(req(D2), "11a - 1p")
    assert site._header_month_day() == (D2.month, D2.day)

    fake.get(_details_url("SERIES101"))

    assert site._header_month_day() == (D1.month, D1.day)


def _header_resets_after_signup(finalize_navigates):
    series = standard_series()
    series[0].rows[1].state = "open"
    series[0].default_row = 1  # the page opens on D2...
    series[0].finalize_navigates = finalize_navigates
    site, fake = make_site(series)
    real_get = fake.get

    def get(url):
        real_get(url)
        if "Details" in url and any("SignUpToEvent" in u for u in fake.gets):
            series[0].selected = 0  # ...but shows D1 after the reload

    fake.get = get
    return site, series


def test_unverified_message_says_the_signup_page_was_left():
    site, series = _header_resets_after_signup(finalize_navigates=True)
    series[0].registered_markup = False

    with pytest.raises(website.RegistrationUnverified) as error:
        site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert "the browser left the signup page" in str(error.value)


def test_unfinished_signup_that_cannot_be_checked_is_a_plain_failure():
    site, series = _header_resets_after_signup(finalize_navigates=False)
    series[0].registered_markup = False

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert not isinstance(error.value, website.RegistrationUnverified)
    assert "still showed Finalize Registration after 1 s" in str(error.value)


def test_post_finalize_wait_is_capped(monkeypatch):
    series = standard_series()
    site, _fake = make_site(series)
    site.wait_time = 30
    waits = []
    real_wait = site._wait

    def recording(condition, what, timeout=None):
        if what == "the signup to complete":
            waits.append(timeout)
            raise website.TimeoutException("still on signup")
        return real_wait(condition, what, timeout=timeout)

    monkeypatch.setattr(site, "_wait", recording)

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert waits == [website.FINALIZE_SETTLE_SECONDS]


def test_verification_that_cannot_reselect_the_date_is_unverified_not_failed():
    series = standard_series()
    series[0].rows[1].state = "open"
    series[0].default_row = 1  # the page opens on D2...
    series[0].finalize_navigates = True
    series[0].registered_markup = False  # no row tag to fall back on
    site, fake = make_site(series)
    real_get = fake.get

    def get(url):
        real_get(url)
        if "Details" in url and any("SignUpToEvent" in u for u in fake.gets):
            series[0].selected = 0  # ...but shows D1 after the reload

    fake.get = get

    with pytest.raises(website.RegistrationUnverified) as error:
        site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[1].registered is True
    message = str(error.value)
    assert "Finalize Registration was clicked for" in message
    assert "couldn't be checked" in message and f"{D1:%b} {D1.day}" in message


@pytest.mark.parametrize("navigates", [False, True])
def test_finalize_is_verified_by_reload_whether_or_not_the_site_leaves_the_signup_page(navigates):
    series = standard_series()
    series[0].finalize_navigates = navigates
    site, fake = make_site(series)

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[0].registered is True
    assert fake.gets[-1] == _details_url("SERIES101")


def test_register_refuses_a_date_not_open_yet():
    series = standard_series()
    series[0].default_row = 1
    site, fake = make_site(series)

    with pytest.raises(website.RegistrationNotOpen) as error:
        site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert "may not be open" in str(error.value)
    assert error.value.opens_in == timedelta(days=4, hours=23)
    assert fake.gets == [_details_url("SERIES101")]  # far off: no reloading


class _FakeTime:
    """Stands in for website.py's `time` module only (WebDriverWait keeps the real one)."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def time(self):
        import time
        return time.time()


def _opening_soon_site(monkeypatch, opens_in_text, opens_after_loads):
    series = standard_series()
    series[0].default_row = 1
    target = series[0].rows[1]
    target.opens_in = opens_in_text
    target.opens_after_loads = opens_after_loads
    clock = _FakeTime()
    monkeypatch.setattr(website, "time", clock)
    site, fake = make_site(series)
    return site, fake, series, clock.sleeps


def test_register_reloads_until_a_date_opening_shortly_opens(monkeypatch):
    # The first load counts too: opens on the 3rd load, i.e. after 2 reloads.
    site, _fake, series, sleeps = _opening_soon_site(
        monkeypatch, "Single date registration opens in 1 min and 30 sec", opens_after_loads=3
    )

    site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[1].registered is True
    assert sleeps == [website.OPENING_RELOAD_SECONDS] * 2


def test_register_gives_up_on_a_date_that_does_not_open_in_time(monkeypatch):
    site, _fake, series, sleeps = _opening_soon_site(
        monkeypatch, "Single date registration opens in 45 sec", opens_after_loads=1000
    )

    with pytest.raises(website.RegistrationNotOpen) as error:
        site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert error.value.opens_in == timedelta(seconds=45)
    assert sum(sleeps) == website.OPENING_WAIT.total_seconds()
    assert not series[0].rows[1].registered


def test_failed_reloads_while_waiting_for_the_opening_are_retried(monkeypatch):
    site, fake, series, _sleeps = _opening_soon_site(
        monkeypatch, "Single date registration opens in 30 sec", opens_after_loads=4
    )
    real_get = fake.get
    loads = {"n": 0}

    def flaky_get(url):
        if "Details" in url:
            loads["n"] += 1
            if loads["n"] in (2, 3):
                raise website.WebDriverException("net::ERR_CONNECTION_RESET")
        real_get(url)

    fake.get = flaky_get

    site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[1].registered is True


def test_reloads_that_keep_failing_end_in_not_open_not_a_timeout(monkeypatch):
    site, fake, series, sleeps = _opening_soon_site(
        monkeypatch, "Single date registration opens in 30 sec", opens_after_loads=1000
    )
    real_get = fake.get

    def get(url):
        if "Details" in url and fake.gets:
            raise website.TimeoutException("page load timed out")
        real_get(url)

    fake.get = get

    with pytest.raises(website.RegistrationNotOpen):
        site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert sum(sleeps) == website.OPENING_WAIT.total_seconds()


def test_reload_pause_follows_a_countdown_shorter_than_the_interval(monkeypatch):
    site, _fake, series, sleeps = _opening_soon_site(
        monkeypatch, "Single date registration opens in 2 sec", opens_after_loads=2
    )

    site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert sleeps == [2]


def test_zero_countdown_counts_as_a_real_countdown(monkeypatch):
    site, fake, series, sleeps = _opening_soon_site(
        monkeypatch, "Single date registration opens in 30 sec", opens_after_loads=3
    )
    real_get = fake.get

    def get(url):
        real_get(url)
        if sum("Details" in u for u in fake.gets) >= 2:  # from the first reload on
            series[0].rows[1].opens_in = "Single date registration opens in 0 sec"

    fake.get = get

    site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[1].registered is True
    assert sleeps == [5, 1]  # the second pause follows the 0 s countdown


def test_register_does_not_wait_for_a_countdown_beyond_a_few_minutes(monkeypatch):
    site, _fake, _series, sleeps = _opening_soon_site(
        monkeypatch, "Single date registration opens in 10 min", opens_after_loads=2
    )

    with pytest.raises(website.RegistrationNotOpen):
        site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert sleeps == []


def test_register_falls_back_to_js_click_when_the_click_is_intercepted():
    series = standard_series()
    series[0].register_click_error = website.ElementClickInterceptedException("overlay")
    site, _fake = make_site(series)

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[0].registered is True


def test_register_does_not_hide_other_click_errors_behind_a_js_click():
    series = standard_series()
    series[0].register_click_error = website.WebDriverException("session crashed")
    site, fake = make_site(series)

    with pytest.raises(website.WebDriverException):
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert not any("SignUpToEvent" in url for url in fake.gets)


def test_not_eligible_text_on_the_details_page_does_not_abort_the_signup():
    series = standard_series()
    series[0].description = "Players not eligible for the ladder may still drop in."
    site, _fake = make_site(series)

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[0].registered is True


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


# --- round 3: button labels, signup page timing, eligibility text -------------


def test_open_date_without_a_register_button_is_a_failure_not_not_open():
    series = standard_series()
    site, fake = make_site(series)
    original = fake._details_page

    def without_button(s):
        return [c for c in original(s) if "register-btn" not in (c.testid or "")]

    fake._details_page = without_button

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert not isinstance(error.value, website.RegistrationNotOpen)
    assert "no registration countdown" in str(error.value)


def test_unrecognized_label_after_finalize_is_unverified():
    series = standard_series()
    plain_label = series[0].row_label
    series[0].row_label = lambda r: "Manage" if r.registered else plain_label(r)
    series[0].registered_markup = False
    site, _fake = make_site(series)

    with pytest.raises(website.RegistrationUnverified) as error:
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert "'manage'" in str(error.value)


def test_register_label_after_finalize_is_a_definite_failure():
    series = standard_series()
    series[0].on_register = "nothing"
    site, _fake = make_site(series)

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert not isinstance(error.value, website.RegistrationUnverified)


def test_finalize_waits_for_the_signup_date_rendering_after_the_button():
    series = standard_series()
    series[0].signup_date_delay_renders = 6
    site, _fake = make_site(series)

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[0].registered is True


def test_finalize_spinner_hiding_the_button_is_not_taken_as_done():
    # The button vanishes for a second while the request runs; leaving the
    # page then would abandon the registration.
    series = standard_series()
    series[0].finalize_spinner_seconds = 1.0
    series[0].finalize_navigates = True
    site, _fake = make_site(series)
    site.wait_time = 5

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[0].registered is True


def test_not_eligible_inside_a_script_does_not_block_the_signup():
    series = standard_series()
    series[0].signup_script_text = "var messages = {ineligible: 'You are not eligible'};"
    site, _fake = make_site(series)

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[0].registered is True


def test_hidden_not_eligible_text_does_not_block_the_signup():
    series = standard_series()
    site, fake = make_site(series)
    original = fake._signup_page

    def with_hidden_message(s):
        return original(s) + [website_hidden_message()]

    fake._signup_page = with_hidden_message

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[0].registered is True


def website_hidden_message():
    from fake_portal import El
    return El(text="You are not eligible to register for this event.", displayed=False)


def test_enabled_finalize_button_wins_over_stray_not_eligible_text():
    series = standard_series()
    site, fake = make_site(series)
    fake.ineligible = True  # visible text on every page

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[0].registered is True


# --- registered markup seen on the live site (Nov 7 2026) ---------------------


def test_registration_is_confirmed_by_edit_registration_and_withdraw_buttons():
    # After a real registration the site shows Edit Registration + Withdraw
    # and no Register button; this was misreported as "no button" on Nov 7.
    series = standard_series()
    site, fake = make_site(series)

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[0].registered is True
    assert site._details_button_label(req(D1), "11a - 1p") == "edit registration"


def test_registered_row_tag_confirms_even_when_header_shows_another_date():
    site, series = _header_resets_after_signup(finalize_navigates=True)

    site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[1].registered is True


def test_already_registered_date_is_not_registered_again():
    series = standard_series()
    series[0].rows[1].state = "open"
    series[0].rows[1].registered = True  # header (D1) is not the registered date
    site, fake = make_site(series)

    site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert not any("SignUpToEvent" in url for url in fake.gets)


def test_details_rows_report_the_registered_tag():
    series = standard_series()
    series[0].rows[2].registered = True
    site, fake = make_site(series)
    fake.get(_details_url("SERIES101"))

    assert [r["registered"] for r in site._details_rows()] == [False, False, True]


# --- round 5: same-slot registrations, tag text, late markup -----------------


def test_a_series_the_user_is_registered_in_wins_even_when_full():
    # The user holds D1 in the first series, which has since filled up; the
    # second series in the slot has room. Registering there would book the
    # same time twice.
    mine = Series(1, [row(D1, "11a - 1p", state="full")], details_code="MINE")
    mine.rows[0].registered = True
    other = Series(2, [row(D1, "11a - 1p")], details_code="OTHER")
    site, fake = make_site([mine, other])

    url = site.get_event_url(req(D1), "11a - 1p")
    assert "MINE" in url

    site.register_for_event(req(D1), "11a - 1p", url)
    site.register_for_event(req(D1), "11a - 1p", None)

    assert not any("SignUpToEvent" in u for u in fake.gets)
    assert other.rows[0].registered is False


def test_a_row_tag_with_other_text_is_not_a_registration():
    series = standard_series()
    series[0].rows[2].registered = True
    series[0].registered_tag_text = "Waitlisted"
    site, fake = make_site(series)
    fake.get(_details_url("SERIES101"))

    assert [r["registered"] for r in site._details_rows()] == [False, False, False]


def test_registered_tag_text_is_matched_ignoring_case_and_spaces():
    series = standard_series()
    series[0].rows[2].registered = True
    series[0].registered_tag_text = "  REGISTERED "
    site, fake = make_site(series)
    fake.get(_details_url("SERIES101"))

    assert site._details_rows()[2]["registered"] is True


def test_withdraw_button_alone_is_not_taken_as_registered():
    # Without Edit Registration (and without the row tag) the header date may
    # be a waitlist spot, which isn't a registration.
    series = standard_series()
    series[0].rows[0].registered = True
    series[0].registered_buttons = ("withdraw",)
    series[0].registered_tag_text = "Waitlisted"
    site, fake = make_site(series)
    fake.get(_details_url("SERIES101"))
    assert site._details_button_label(req(D1), "11a - 1p") == ""

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert "No Register button" in str(error.value)
    assert not any("SignUpToEvent" in u for u in fake.gets)


def test_registration_markup_arriving_after_the_page_loads_is_waited_for():
    series = standard_series()
    series[0].registered_markup_delay_finds = 40
    site, fake = make_site(series)
    site.wait_time = 5
    labels_after_signup = []
    real_label = site._details_button_label

    def recording_label(*args):
        label = real_label(*args)
        if any("SignUpToEvent" in u for u in fake.gets):
            labels_after_signup.append(label)
        return label

    site._details_button_label = recording_label

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[0].registered is True
    # The verification really did see Register first.
    assert labels_after_signup[:1] == ["register"]
    assert fake.gets[-1] == _details_url("SERIES101")  # no extra reload


def test_registration_markup_that_never_arrives_is_still_a_failure():
    series = standard_series()
    series[0].registered_markup_delay_finds = 10 ** 9
    site, _fake = make_site(series)

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert not isinstance(error.value, website.RegistrationUnverified)
    assert "still shows 'register'" in str(error.value)


def _header_registered_on_d1(row_click_selects):
    series = standard_series()
    series[0].rows[0].registered = True  # header (D1) shows Edit Registration / Withdraw
    series[0].rows[1].state = "open"
    series[0].row_click_selects = row_click_selects
    return series


def test_registered_header_date_is_not_success_for_another_date_that_cannot_be_selected():
    series = _header_registered_on_d1(row_click_selects=False)
    site, fake = make_site(series)

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert "selecting that date didn't work" in str(error.value)
    assert not isinstance(error.value, website.RegistrationUnverified)
    assert not any("SignUpToEvent" in u for u in fake.gets)
    assert series[0].rows[1].registered is False


def test_registered_header_date_does_not_stop_registering_another_date():
    series = _header_registered_on_d1(row_click_selects=True)
    site, fake = make_site(series)
    tag_checks = []
    real_row_registered = site._row_registered

    def recording(event_date, time_range):
        result = real_row_registered(event_date, time_range)
        tag_checks.append((event_date, result))
        return result

    site._row_registered = recording

    site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[1].registered is True
    assert any("SignUpToEvent" in u for u in fake.gets)
    # Verified by D2's own row tag on the reloaded page (whose header is D1).
    assert tag_checks[-1] == (req(D2), True)
    assert site._header_month_day() == (D1.month, D1.day)


# --- round 6: already registered, late tags, stale buttons -------------------


def test_already_registered_full_date_is_not_reported_full():
    series = standard_series()
    series[1].rows[0].registered = True  # SERIES202: D1 8a is full
    site, _fake = make_site(series)

    with pytest.raises(website.AlreadyRegistered):
        site.determine_access_date(req(D1), "8a - 9:30a")


def test_already_registered_date_is_not_reported_skill_ineligible():
    series = standard_series()
    series[2].rows[0].registered = True  # SERIES303: Beginner only
    site, _fake = make_site(series)

    with pytest.raises(website.AlreadyRegistered):
        site.determine_access_date(req(D1), "4p - 6p")


def test_late_registered_tag_on_a_full_series_is_waited_for_before_moving_on():
    mine = Series(1, [row(D1, "11a - 1p", state="full")], details_code="MINE")
    mine.rows[0].registered = True
    # Hidden from the two reads of the first look (rows, then observations).
    mine.registered_tag_hidden_reads = 2
    other = Series(2, [row(D1, "11a - 1p")], details_code="OTHER")
    site, fake = make_site([mine, other])
    waits = _spy_appears_within(site)

    with pytest.raises(website.AlreadyRegistered):
        site.determine_access_date(req(D1), "11a - 1p")

    assert waits == [True]
    assert "MINE" in fake.current_url
    assert not any("OTHER" in u for u in fake.gets)


@pytest.mark.parametrize("path", ["poll", "register_without_url"])
def test_searches_close_to_the_opening_never_wait_for_late_tags(path):
    restricted = Series(1, [row(D1, "11a - 1p")], restriction="Advanced", details_code="ADVANCED")
    fitting = Series(2, [row(D1, "11a - 1p")], details_code="FITS")
    site, _fake = make_site([restricted, fitting])
    waits = _spy_appears_within(site)

    if path == "poll":
        assert "FITS" in site._poll_for_event(req(D1), "11a - 1p", page_wait=1)
    else:
        site.register_for_event(req(D1), "11a - 1p", None)
        assert fitting.rows[0].registered is True

    assert waits == []


def test_a_fitting_first_series_costs_no_wait_for_late_tags(monkeypatch):
    site, _fake = make_site()
    waits = []
    monkeypatch.setattr(site, "_appears_within", lambda *a: waits.append(a) or False)

    site._find_occurrence(req(D1), "11a - 1p")

    assert waits == []


def _open_then_registered():
    first = Series(1, [row(D1, "11a - 1p")], restriction="Intermediate", details_code="FIRSTOPEN")
    later = Series(2, [row(D1, "11a - 1p")], restriction="Advanced", details_code="LATERMINE")
    return first, later


def test_a_later_series_the_user_is_registered_in_beats_an_earlier_open_one():
    first, later = _open_then_registered()
    later.rows[0].registered = True
    site, fake = make_site([first, later])

    with pytest.raises(website.AlreadyRegistered):
        site.determine_access_date(req(D1), "11a - 1p")
    assert "LATERMINE" in fake.current_url


def test_get_event_url_right_before_the_opening_does_not_check_later_series():
    first, later = _open_then_registered()
    later.rows[0].registered = True
    site, fake = make_site([first, later])

    assert "FIRSTOPEN" in site.get_event_url(req(D1), "11a - 1p")
    assert not any("LATERMINE" in u for u in fake.gets)


def test_a_failing_later_series_check_still_returns_the_open_series():
    first, later = _open_then_registered()
    site, fake = make_site([first, later])
    real_open = site._open_details_from_card

    def open_details(card_id, wait_time=None):
        if card_id == later.card_id:
            raise website.TimeoutException("details page load timed out")
        return real_open(card_id, wait_time=wait_time)

    site._open_details_from_card = open_details

    when, summary = site.determine_access_date(req(D1), "11a - 1p")

    assert when is not None
    assert "Skill Level Restriction: Intermediate" in summary
    assert "FIRSTOPEN" in fake.current_url and "FIRSTOPEN" in site.last_details_url


def test_polling_does_not_look_past_an_open_series_for_a_registration():
    first, later = _open_then_registered()
    later.rows[0].registered = True
    site, fake = make_site([first, later])

    assert "FIRSTOPEN" in site._poll_for_event(req(D1), "11a - 1p", page_wait=1)
    assert not any("LATERMINE" in u for u in fake.gets)


def test_open_series_is_shown_again_after_checking_later_ones():
    first, later = _open_then_registered()  # not registered in either
    site, fake = make_site([first, later])

    when, summary = site.determine_access_date(req(D1), "11a - 1p")

    assert when is not None
    assert "FIRSTOPEN" in fake.current_url and "FIRSTOPEN" in site.last_details_url
    assert "Skill Level Restriction: Intermediate" in summary
    assert any("LATERMINE" in u for u in fake.gets)


def test_edit_registration_for_another_header_date_is_not_the_requested_dates():
    series = standard_series()
    series[0].rows[0].registered = True  # header D1: Edit Registration / Withdraw
    site, fake = make_site(series)
    fake.get(_details_url("SERIES101"))

    assert site._details_button_label(req(D1), "11a - 1p") == "edit registration"
    assert site._details_button_label(req(D2), "11a - 1p") == ""


def test_edit_registration_for_another_header_date_counts_for_nothing_without_any_tag():
    # No row carries the tag, so only the header date tells the button's
    # date apart from the requested one.
    series = standard_series()
    series[0].rows[0].registered = True
    series[0].registered_tag_text = "Confirmed"  # not the "Registered" tag
    site, fake = make_site(series)
    fake.get(_details_url("SERIES101"))

    assert site._details_button_label(req(D2), "11a - 1p") == ""


def test_edit_registration_is_not_trusted_when_only_another_row_is_tagged():
    # The header shows D2 with an Edit Registration button, but only D1's row
    # is tagged Registered: the button is likely D1's, left over.
    from fake_portal import El

    series = standard_series()
    series[0].rows[0].registered = True
    series[0].rows[1].state = "open"
    series[0].default_row = 1
    site, fake = make_site(series)
    original = fake._details_page

    def stale_buttons(s):
        return [
            El(testid="events-details-edit-registration-btn edit-registration-btn", text="Edit Registration",
               tag="button")
            if "register-btn" in (c.testid or "") else c
            for c in original(s)
        ]

    fake._details_page = stale_buttons
    fake.get(_details_url("SERIES101"))

    assert site._details_button_label(req(D2), "11a - 1p") == ""
    assert site._registered_markup_appears(req(D2), "11a - 1p") is False
    series[0].rows[1].registered = True  # now D2's row is tagged too
    assert site._details_button_label(req(D2), "11a - 1p") == "edit registration"


def test_late_tag_is_waited_for_when_the_header_shows_another_date():
    # The reloaded header is D1, row clicks don't select D2, and D2's tag
    # renders a moment late: still a success, not "couldn't be checked".
    site, series = _header_resets_after_signup(finalize_navigates=True)
    # Hidden from the first check's read of all three rows' tags.
    series[0].registered_tag_hidden_reads = 3
    waits = _spy_appears_within(site)

    site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[1].registered is True
    assert waits == [True]


def test_a_registered_tag_that_is_not_displayed_still_counts():
    series = standard_series()
    series[0].rows[1].state = "open"
    series[0].rows[1].registered = True
    series[0].registered_tag_displayed = False
    site, fake = make_site(series)
    fake.get(_details_url("SERIES101"))

    assert [r["registered"] for r in site._details_rows()] == [False, True, False]

    site.register_for_event(req(D2), "11a - 1p", _details_url("SERIES101"))
    assert not any("SignUpToEvent" in u for u in fake.gets)


def _spy_appears_within(site):
    """Records each _appears_within result while still running it."""
    results = []
    real = site._appears_within

    def spy(condition, what):
        results.append(real(condition, what))
        return results[-1]

    site._appears_within = spy
    return results


# --- round 7: failures that registered anyway, stale tags --------------------


def test_a_failed_signup_step_that_registered_anyway_is_a_success():
    series = standard_series()
    site, _fake = make_site(series)

    def finalize(event_date):
        series[0].rows[0].registered = True  # went through...
        raise website.RegistrationError("Cannot register for event: try again")  # ...but said otherwise

    site._finalize_signup = finalize

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[0].registered is True


def test_a_verification_that_broke_on_a_registered_date_is_a_success():
    series = standard_series()
    site, _fake = make_site(series)

    def verify(*_args):
        raise website.TimeoutException("details reload timed out")

    site._verify_registered = verify

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[0].registered is True


def test_the_original_failure_is_reported_when_the_recheck_cannot_load():
    series = standard_series()
    site, fake = make_site(series)
    real_get = site._get_with_retry

    def finalize(event_date):
        raise website.RegistrationError("signup failed")

    def get(url, attempts=2):
        if any("SignUpToEvent" in u for u in fake.gets):
            raise website.TimeoutException("reload timed out")
        real_get(url, attempts)

    site._finalize_signup = finalize
    site._get_with_retry = get
    # Register opens the signup page before finalize is reached.
    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert str(error.value) == "signup failed"


class _StaleTag:
    def get_attribute(self, name):
        raise website.StaleElementReferenceException("re-rendered")

    @property
    def text(self):
        raise website.StaleElementReferenceException("re-rendered")


@pytest.mark.parametrize("stale_reads, expected", [(1, True), (2, False)])
def test_a_stale_registered_tag_is_found_again_once(stale_reads, expected):
    series = standard_series()
    series[0].rows[0].registered = True
    site, fake = make_site(series)
    fake.get(_details_url("SERIES101"))
    real_find = fake.find_elements
    left = {"stale": stale_reads}

    def find_elements(by, selector):
        if selector == website.DETAILS_ROW_REGISTERED(0) and left["stale"]:
            left["stale"] -= 1
            return [_StaleTag()]
        return real_find(by, selector)

    fake.find_elements = find_elements

    assert site._has_registered_tag(0) is expected


# --- round 8: snapshots before the re-check, time-less rows ------------------


def _finalize_failing(site, series, registers=False):
    def finalize(event_date):
        if registers:
            series[0].rows[0].registered = True
        raise website.RegistrationError("signup failed")

    site._finalize_signup = finalize


def test_the_failing_signup_page_is_saved_before_the_recheck(failure_dir):
    series = standard_series()
    site, _fake = make_site(series)
    _finalize_failing(site, series)

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    paths = error.value.failure_snapshots
    assert [os.path.splitext(p)[1] for p in paths] == [".png", ".html"]
    assert "register_before_recheck" in paths[1]
    html = open(paths[1], encoding="utf-8").read()
    assert "SignUpToEvent" in html and "('signup'" in html  # not the reloaded details page


def test_a_recheck_that_finds_the_registration_leaves_no_snapshot(failure_dir):
    series = standard_series()
    site, _fake = make_site(series)
    _finalize_failing(site, series, registers=True)

    site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert series[0].rows[0].registered is True
    assert not failure_dir.exists() or list(failure_dir.iterdir()) == []


def test_any_error_in_the_recheck_reports_the_original_failure():
    series = standard_series()
    site, fake = make_site(series)
    _finalize_failing(site, series, registers=True)
    real_wait = site._wait_for_details_page

    def wait(timeout=None):
        if any("SignUpToEvent" in u for u in fake.gets):
            raise ValueError("unexpected page shape")
        real_wait(timeout)

    site._wait_for_details_page = wait

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert str(error.value) == "signup failed"


def test_a_verification_failure_is_not_rechecked_with_another_reload():
    series = standard_series()
    series[0].on_register = "nothing"
    site, fake = make_site(series)

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert "still shows 'register'" in str(error.value)
    after_signup = fake.gets[next(i for i, u in enumerate(fake.gets) if "SignUpToEvent" in u):]
    assert sum("Details" in u for u in after_signup) == 1  # verification's own reload only
    assert not hasattr(error.value, "failure_snapshots")


def _same_day_untimed_series():
    # D1 has the requested 11a session and a second session whose time can't
    # be read; only the second is tagged Registered.
    series = standard_series()
    series[0].rows.insert(1, row(D1, "time to be announced"))
    series[0].rows[1].registered = True
    return series


def test_an_untimed_tagged_row_on_the_same_day_is_not_the_requested_session():
    series = _same_day_untimed_series()
    site, fake = make_site(series)
    fake.get(_details_url("SERIES101"))
    assert site._row_registered(req(D1), "11a - 1p") is False

    _finalize_failing(site, series)
    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert any("SignUpToEvent" in u for u in fake.gets)  # the pre-click check didn't stop it
    assert str(error.value) == "signup failed"  # nor did the re-check


def test_a_tagged_row_for_another_date_is_no_success():
    series = standard_series()
    series[0].rows[1].registered = True  # D2, not the requested D1
    site, _fake = make_site(series)
    _finalize_failing(site, series)

    with pytest.raises(website.RegistrationError) as error:
        site.register_for_event(req(D1), "11a - 1p", _details_url("SERIES101"))

    assert str(error.value) == "signup failed"


def test_an_untimed_row_still_counts_when_it_is_the_dates_only_row():
    series = standard_series()
    series[0].rows[0].time_text = "time to be announced"
    series[0].rows[0].registered = True
    site, fake = make_site(series)
    fake.get(_details_url("SERIES101"))

    assert site._row_registered(req(D1), "11a - 1p") is True


def test_a_failed_reload_of_the_open_series_still_uses_its_restriction():
    first, later = _open_then_registered()  # registered in neither
    site, fake = make_site([first, later])
    real_get = site._get_with_retry

    def get(url, attempts=2):
        if "FIRSTOPEN" in url and any("LATERMINE" in u for u in fake.gets):
            raise website.TimeoutException("reload timed out")
        real_get(url, attempts)

    site._get_with_retry = get

    when, summary = site.determine_access_date(req(D1), "11a - 1p")

    assert when is not None  # not SkillLevelIneligible from the Advanced page left showing
    assert "LATERMINE" in fake.current_url
    assert "Skill Level Restriction: Intermediate" in summary
    assert "FIRSTOPEN" in site.last_details_url


# --- round 9: finding vs. registered rules, single restriction read ----------


def _untimed_session_series():
    # On D2 the requested 11a session's time is unreadable, and a second
    # session (4p) shares the date.
    series = standard_series()
    series[0].rows[1] = row(D2, "time to be announced", state="not_open",
                            opens_in="Single date registration opens in 4 days and 23 h")
    series[0].rows.insert(2, row(D2, "4p - 6p", state="not_open",
                                 opens_in="Single date registration opens in 2 days"))
    return series


def test_an_untimed_session_among_several_is_still_found():
    series = _untimed_session_series()
    site, _fake = make_site(series)

    assert "SERIES101" in site.get_event_url(req(D2), "11a - 1p")
    when, _summary = site.determine_access_date(req(D2), "11a - 1p")
    expected_day = (datetime.now() + timedelta(days=4, hours=23)).date()
    assert when.date() == expected_day  # the untimed row's countdown, not the 4p one's


def test_an_untimed_session_among_several_does_not_count_as_registered():
    series = _untimed_session_series()
    series[0].rows[1].registered = True
    site, fake = make_site(series)

    when, _summary = site.determine_access_date(req(D2), "11a - 1p")  # no AlreadyRegistered

    assert when is not None
    assert site._row_registered(req(D2), "11a - 1p") is False


def test_countdown_comes_from_the_exact_session_when_there_is_one():
    series = standard_series()
    # An untimed D2 row listed before the exact one, with another countdown.
    series[0].rows.insert(1, row(D2, "time to be announced", state="not_open",
                                 opens_in="Single date registration opens in 9 days"))
    site, fake = make_site(series)
    fake.get(_details_url("SERIES101"))

    assert site._selected_opens_in(req(D2), "11a - 1p") == timedelta(days=4, hours=23)


def test_the_restriction_is_read_once_per_series(monkeypatch):
    restricted = Series(1, [row(D1, "11a - 1p")], restriction="Advanced", details_code="ADVANCED")
    fitting = Series(2, [row(D1, "11a - 1p")], restriction="Intermediate", details_code="FITS")
    site, _fake = make_site([restricted, fitting])
    reads = []
    real_read = site._details_restriction

    def read():
        reads.append(site.driver.current_url)
        return real_read()

    monkeypatch.setattr(site, "_details_restriction", read)

    assert "FITS" in site._find_occurrence(req(D1), "11a - 1p")[0]
    assert [("ADVANCED" in u, "FITS" in u) for u in reads] == [(True, False), (False, True)]


def test_a_failing_read_of_a_registered_later_series_returns_the_open_one(monkeypatch):
    first, later = _open_then_registered()
    later.rows[0].registered = True
    site, fake = make_site([first, later])
    real_rows = site._rows_as_observations

    def rows_as_observations(card):
        if "LATERMINE" in fake.current_url:
            raise website.TimeoutException("page went away")
        return real_rows(card)

    monkeypatch.setattr(site, "_rows_as_observations", rows_as_observations)

    when, summary = site.determine_access_date(req(D1), "11a - 1p")  # no AlreadyRegistered, no timeout

    assert when is not None
    assert "Skill Level Restriction: Intermediate" in summary
    assert "FIRSTOPEN" in site.last_details_url


def test_same_second_failure_captures_never_share_a_file(failure_dir, monkeypatch):
    frozen = datetime(2026, 11, 8, 20, 0, 0, 123456)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(website, "datetime", FrozenDatetime)
    site, _fake = make_site()
    site.display_all_events()

    first = site.capture_failure("register_before_recheck_default")
    second = site.capture_failure("register_before_recheck_default")

    assert len(set(first + second)) == 4
    assert all(os.path.exists(p) for p in first + second)
    assert "20261108-200000-123" in first[0]

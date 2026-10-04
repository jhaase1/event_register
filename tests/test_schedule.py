from datetime import date, datetime, timedelta

import pytest

import schedule

# Sunday 2026-10-04, mid-afternoon.
NOW = datetime(2026, 10, 4, 15, 0, 0)
TUE_SLOT = ("09:00", "11:00")


def _obs(day, slot=TUE_SLOT, name="Intermediate Drop-in", lead_days=None, lead_source=None):
    return {
        "event_day": day,
        "start_hm": slot[0],
        "end_hm": slot[1],
        "name": name,
        "category": "Open Play",
        "lead_days": lead_days,
        "lead_source": lead_source,
    }


def _weekly(last_day, weeks, **kwargs):
    """Observations of the same slot on `last_day` and the `weeks - 1` weeks before it."""
    return [_obs(last_day - timedelta(weeks=k), **kwargs) for k in range(weeks)]


def _predict(observations, requested_day, slot=TUE_SLOT, **overrides):
    kwargs = dict(
        lookback_weeks=4,
        min_matches=3,
        max_weeks_ahead=3,
        registration_clock="14:00:00",
        lead_override=None,
    )
    kwargs.update(overrides)
    return schedule.predict(observations, requested_day, slot, NOW, **kwargs)


# --- resolve_year -----------------------------------------------------------


def test_resolve_year_picks_this_year_for_nearby_dates():
    assert schedule.resolve_year(10, 13, NOW.date()) == date(2026, 10, 13)


def test_resolve_year_rolls_forward_across_new_year():
    assert schedule.resolve_year(1, 5, date(2026, 12, 20)) == date(2027, 1, 5)


def test_resolve_year_rolls_back_across_new_year():
    assert schedule.resolve_year(12, 29, date(2027, 1, 3)) == date(2026, 12, 29)


def test_resolve_year_skips_impossible_leap_day():
    assert schedule.resolve_year(2, 29, date(2027, 2, 20)) == date(2028, 2, 29)


# --- parse_request ----------------------------------------------------------


def test_parse_request_normalizes_user_text():
    day, slot = schedule.parse_request("Tue, Oct 13th", "9a - 11a", NOW.date())
    assert day == date(2026, 10, 13)
    assert slot == ("09:00", "11:00")


def test_parse_request_handles_pm_and_minutes():
    day, slot = schedule.parse_request("WED, OCT 14", "1:00pm - 2:30pm", NOW.date())
    assert day == date(2026, 10, 14)
    assert slot == ("13:00", "14:30")


def test_parse_request_returns_none_when_unparseable():
    assert schedule.parse_request("someday", "9a - 11a", NOW.date()) is None


# --- to_observation ---------------------------------------------------------


def _card(**overrides):
    card = {
        "month_day": (10, 13),
        "time": (9, 0, 11, 0),
        "name": "Intermediate Drop-in",
        "category": "Open Play",
        "actionable": False,
        "opens_in": None,
    }
    card.update(overrides)
    return card


def test_to_observation_learns_exact_lead_from_countdown():
    # Opens in 2 days -> Oct 6; event Oct 13 -> 7 day lead.
    obs = schedule.to_observation(_card(opens_in=timedelta(days=2, hours=1)), NOW)
    assert obs["event_day"] == date(2026, 10, 13)
    assert (obs["start_hm"], obs["end_hm"]) == ("09:00", "11:00")
    assert obs["lead_days"] == 7
    assert obs["lead_source"] == "countdown"


def test_to_observation_estimates_lead_from_first_sighting_when_already_open():
    obs = schedule.to_observation(_card(actionable=True), NOW)
    assert obs["lead_days"] == 9
    assert obs["lead_source"] == "first_seen"


def test_to_observation_ignores_first_sighting_when_untrusted():
    # On the very first scan an open card may have been open for days.
    obs = schedule.to_observation(_card(actionable=True), NOW, trust_first_seen=False)
    assert obs["lead_days"] is None
    assert obs["lead_source"] is None


def test_to_observation_countdown_lead_kept_when_first_seen_untrusted():
    obs = schedule.to_observation(_card(opens_in=timedelta(days=2, hours=1)), NOW, trust_first_seen=False)
    assert (obs["lead_days"], obs["lead_source"]) == (7, "countdown")


def test_to_observation_without_countdown_or_open_has_no_lead():
    obs = schedule.to_observation(_card(), NOW)
    assert obs["lead_days"] is None
    assert obs["lead_source"] is None


def test_to_observation_skips_cards_without_date_or_time():
    assert schedule.to_observation(_card(month_day=None), NOW) is None
    assert schedule.to_observation(_card(time=None), NOW) is None


def test_to_observation_blank_name_is_empty_string():
    assert schedule.to_observation(_card(name=None), NOW)["name"] == ""


# --- predict: accepts -------------------------------------------------------


def test_predict_accepts_consistent_weekly_slot_with_countdown_lead():
    history = _weekly(date(2026, 9, 29), 3, lead_days=7, lead_source="countdown")
    result = _predict(history, date(2026, 10, 13))

    assert isinstance(result, schedule.Prediction)
    assert result.event_day == date(2026, 10, 13)
    assert result.lead_days == 7
    assert result.registration_time == datetime(2026, 10, 6, 14, 0, 0)
    assert result.matched_days == [date(2026, 9, 15), date(2026, 9, 22), date(2026, 9, 29)]
    assert result.names == ["Intermediate Drop-in"]


def test_predict_counts_listed_future_weeks_as_evidence():
    # Bootstrapping: only Oct 6 is in the past-ish; Oct 13 is listed but not
    # yet happened. Requesting Oct 20 still has 3 matching weeks.
    history = _weekly(date(2026, 10, 13), 3, lead_days=7, lead_source="countdown")
    result = _predict(history, date(2026, 10, 20))

    assert isinstance(result, schedule.Prediction)
    assert result.registration_time == datetime(2026, 10, 13, 14, 0, 0)


def test_predict_lead_override_wins_over_observed():
    history = _weekly(date(2026, 9, 29), 3, lead_days=7, lead_source="countdown")
    result = _predict(history, date(2026, 10, 13), lead_override=3)

    assert result.lead_days == 3
    assert result.registration_time == datetime(2026, 10, 10, 14, 0, 0)


def test_predict_prefers_slot_countdown_lead_over_other_slots():
    history = _weekly(date(2026, 9, 29), 3, lead_days=7, lead_source="countdown")
    history.append(_obs(date(2026, 10, 1), slot=("18:00", "20:00"), lead_days=2, lead_source="countdown"))
    assert _predict(history, date(2026, 10, 13)).lead_days == 7


def test_predict_falls_back_to_any_slot_countdown_lead():
    history = _weekly(date(2026, 9, 29), 3)
    history.append(_obs(date(2026, 10, 1), slot=("18:00", "20:00"), lead_days=5, lead_source="countdown"))
    assert _predict(history, date(2026, 10, 13)).lead_days == 5


def test_predict_uses_largest_first_seen_lead_when_no_countdown():
    # First sightings happen after opening, so they underestimate the lead;
    # the largest one is closest to the truth.
    history = [
        _obs(date(2026, 9, 15), lead_days=6, lead_source="first_seen"),
        _obs(date(2026, 9, 22), lead_days=7, lead_source="first_seen"),
        _obs(date(2026, 9, 29), lead_days=6, lead_source="first_seen"),
    ]
    assert _predict(history, date(2026, 10, 13)).lead_days == 7


def test_predict_countdown_lead_beats_first_seen_lead():
    history = _weekly(date(2026, 9, 29), 3, lead_days=9, lead_source="first_seen")
    history.append(_obs(date(2026, 10, 1), slot=("18:00", "20:00"), lead_days=7, lead_source="countdown"))
    assert _predict(history, date(2026, 10, 13)).lead_days == 7


def test_predict_lists_every_event_name_seen_in_slot():
    history = [
        _obs(date(2026, 9, 15), name="Advanced Drop-in", lead_days=7, lead_source="countdown"),
        _obs(date(2026, 9, 22), lead_days=7, lead_source="countdown"),
        _obs(date(2026, 9, 29), lead_days=7, lead_source="countdown"),
    ]
    assert _predict(history, date(2026, 10, 13)).names == ["Advanced Drop-in", "Intermediate Drop-in"]


def test_predict_same_day_counted_once_even_with_two_names():
    history = _weekly(date(2026, 9, 29), 2, lead_days=7, lead_source="countdown")
    history.append(_obs(date(2026, 9, 29), name="Advanced Drop-in", lead_days=7, lead_source="countdown"))
    result = _predict(history, date(2026, 10, 13))
    assert isinstance(result, schedule.Rejection)


# --- predict: rejects -------------------------------------------------------


def test_predict_rejects_too_few_matching_weeks():
    history = _weekly(date(2026, 9, 29), 2, lead_days=7, lead_source="countdown")
    result = _predict(history, date(2026, 10, 13))
    assert isinstance(result, schedule.Rejection)
    assert "2" in result.reason


def test_predict_ignores_other_times_on_the_same_weekday():
    history = _weekly(date(2026, 9, 29), 3, slot=("09:00", "10:30"), lead_days=7, lead_source="countdown")
    assert isinstance(_predict(history, date(2026, 10, 13)), schedule.Rejection)


def test_predict_ignores_same_time_on_other_weekdays():
    history = _weekly(date(2026, 9, 30), 3, lead_days=7, lead_source="countdown")
    assert isinstance(_predict(history, date(2026, 10, 13)), schedule.Rejection)


def test_predict_ignores_matches_older_than_lookback():
    history = _weekly(date(2026, 8, 25), 4, lead_days=7, lead_source="countdown")
    assert isinstance(_predict(history, date(2026, 10, 13)), schedule.Rejection)


def test_predict_rejects_past_and_today():
    history = _weekly(date(2026, 9, 29), 3, lead_days=7, lead_source="countdown")
    assert isinstance(_predict(history, date(2026, 10, 4)), schedule.Rejection)
    assert isinstance(_predict(history, date(2026, 9, 29)), schedule.Rejection)


def test_predict_rejects_too_far_ahead():
    history = _weekly(date(2026, 9, 29), 3, lead_days=7, lead_source="countdown")
    result = _predict(history, date(2026, 11, 3))
    assert isinstance(result, schedule.Rejection)


def test_predict_rejects_when_a_later_occurrence_is_already_listed():
    # Oct 13 isn't listed but Oct 20 already is -> Oct 13 was skipped on purpose.
    history = _weekly(date(2026, 9, 29), 3, lead_days=7, lead_source="countdown")
    history.append(_obs(date(2026, 10, 20), lead_days=7, lead_source="countdown"))
    result = _predict(history, date(2026, 10, 13))
    assert isinstance(result, schedule.Rejection)
    assert "Oct 20" in result.reason


def test_predict_rejects_when_lead_unknown():
    history = _weekly(date(2026, 9, 29), 3)
    result = _predict(history, date(2026, 10, 13))
    assert isinstance(result, schedule.Rejection)


def test_predict_rejects_when_no_registration_clock():
    history = _weekly(date(2026, 9, 29), 3, lead_days=7, lead_source="countdown")
    assert isinstance(_predict(history, date(2026, 10, 13), registration_clock=None), schedule.Rejection)


def test_predict_accepts_registration_clock_without_seconds():
    history = _weekly(date(2026, 9, 29), 3, lead_days=7, lead_source="countdown")
    result = _predict(history, date(2026, 10, 13), registration_clock="20:00")
    assert result.registration_time == datetime(2026, 10, 6, 20, 0, 0)


def test_predict_rejects_malformed_registration_clock_instead_of_raising():
    history = _weekly(date(2026, 9, 29), 3, lead_days=7, lead_source="countdown")
    assert isinstance(_predict(history, date(2026, 10, 13), registration_clock="8pm"), schedule.Rejection)


def test_predict_ignores_non_numeric_lead_override():
    history = _weekly(date(2026, 9, 29), 3, lead_days=7, lead_source="countdown")
    result = _predict(history, date(2026, 10, 13), lead_override="abc")
    assert result.lead_days == 7


def test_predict_rejects_when_registration_should_already_be_open():
    # 7-day lead for Oct 6 means it opened Sep 29; it should be listed already.
    history = _weekly(date(2026, 9, 29), 3, lead_days=7, lead_source="countdown")
    result = _predict(history, date(2026, 10, 6), min_matches=3)
    assert isinstance(result, schedule.Rejection)


# --- later_occurrence_listed ------------------------------------------------


@pytest.mark.parametrize(
    "listed_day, expected",
    [
        (date(2026, 10, 20), True),
        (date(2026, 10, 13), False),
        (date(2026, 10, 6), False),
        (date(2026, 10, 21), False),  # different weekday
    ],
)
def test_later_occurrence_listed(listed_day, expected):
    observations = [_obs(listed_day)]
    assert schedule.later_occurrence_listed(observations, date(2026, 10, 13), TUE_SLOT) is expected

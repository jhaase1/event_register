"""Recurring-schedule inference for speculative pre-registration.

Pure logic (no browser, no database) so the matching rules are cheap to test.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from website import _parse_month_day, _parse_time_range
from logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class Prediction:
    event_day: date
    registration_time: datetime
    lead_days: int
    matched_days: list = field(default_factory=list)
    names: list = field(default_factory=list)


@dataclass
class Rejection:
    reason: str


def resolve_year(month, day, today):
    """Site and user dates carry no year; pick the one closest to today."""
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue  # Feb 29 outside a leap year
    if not candidates:
        # Only Feb 29 can get here; the nearest leap year may be further out.
        year = today.year + 1
        while True:
            try:
                return date(year, month, day)
            except ValueError:
                year += 1
    return min(candidates, key=lambda d: abs((d - today).days))


def hm_pair(time_tuple):
    """(9, 0, 11, 0) -> ('09:00', '11:00')."""
    h1, m1, h2, m2 = time_tuple
    return f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"


def parse_request(event_date, time_range, today):
    """Parses a user's requested date/time into (date, (start_hm, end_hm)), or None."""
    month_day = _parse_month_day(event_date or "")
    time_tuple = _parse_time_range(time_range or "")
    if not month_day or not time_tuple:
        return None
    return resolve_year(*month_day, today), hm_pair(time_tuple)


def to_observation(card, now, trust_first_seen=True):
    """Converts a scanned event card into an observed_events row.

    lead_days is the gap between registration opening and the event:
    exact when a countdown is shown, and only an estimate ('first_seen')
    when the card is already open the first time we see it - in that case
    it opened at or before now, so the estimate can only be too small.

    trust_first_seen should be False when there was no recent earlier scan:
    an open card on the very first scan may have been open for days.
    """
    if not card.get("month_day") or not card.get("time"):
        return None

    event_day = resolve_year(*card["month_day"], now.date())
    start_hm, end_hm = hm_pair(card["time"])

    lead_days, lead_source = None, None
    if card.get("opens_in") is not None:
        opens_at = now + card["opens_in"]
        lead_days, lead_source = (event_day - opens_at.date()).days, "countdown"
    elif card.get("actionable") and trust_first_seen:
        lead_days, lead_source = (event_day - now.date()).days, "first_seen"

    return {
        "event_day": event_day,
        "start_hm": start_hm,
        "end_hm": end_hm,
        "name": card.get("name") or "",
        "category": card.get("category") or "",
        "lead_days": lead_days,
        "lead_source": lead_source,
    }


def _same_slot(obs, slot, weekday):
    return (obs["start_hm"], obs["end_hm"]) == tuple(slot) and obs["event_day"].weekday() == weekday


def later_occurrence_listed(observations, event_day, slot):
    """True when the same weekly slot is already seen on a later date.

    Sessions are posted in date order, so a later week being up while this
    one isn't means this one was skipped (holiday, cancellation).
    """
    return any(
        _same_slot(obs, slot, event_day.weekday()) and obs["event_day"] > event_day
        for obs in observations
    )


def _parse_clock(text):
    """'20:00:00' or '20:00' -> time; None if missing or malformed."""
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(text).strip(), fmt).time()
        except (TypeError, ValueError):
            continue
    return None


def _estimate_lead(observations, slot_obs, lead_override):
    if lead_override is not None:
        try:
            return int(lead_override)
        except (TypeError, ValueError):
            logger.warning(f"Ignoring non-numeric registration_lead_days: {lead_override!r}")

    for pool in (slot_obs, observations):
        countdown = [o["lead_days"] for o in pool if o.get("lead_source") == "countdown" and o.get("lead_days") is not None]
        if countdown:
            return Counter(countdown).most_common(1)[0][0]

    for pool in (slot_obs, observations):
        first_seen = [o["lead_days"] for o in pool if o.get("lead_source") == "first_seen" and o.get("lead_days") is not None]
        if first_seen:
            return max(first_seen)

    return None


def predict(
    observations,
    requested_day,
    slot,
    now,
    *,
    lookback_weeks,
    min_matches,
    max_weeks_ahead,
    registration_clock,
    lead_override=None,
):
    """Decides whether an unlisted request fits the recent weekly schedule.

    Returns a Prediction with the expected registration-open time, or a
    Rejection whose reason is phrased for the user.
    """
    today = now.date()
    slot_label = f"{requested_day:%A}s {slot[0]}-{slot[1]}"

    if requested_day <= today:
        return Rejection("That date isn't in the future.")

    if requested_day > today + timedelta(weeks=max_weeks_ahead):
        return Rejection(
            f"I only plan ahead up to {max_weeks_ahead} weeks for sessions that aren't posted yet."
        )

    if later_occurrence_listed(observations, requested_day, slot):
        later = min(
            o["event_day"] for o in observations
            if _same_slot(o, slot, requested_day.weekday()) and o["event_day"] > requested_day
        )
        return Rejection(
            f"The same slot is already posted for {later:%b} {later.day}, so this week looks skipped."
        )

    window_start = today - timedelta(weeks=lookback_weeks)
    slot_obs = [
        o for o in observations
        if _same_slot(o, slot, requested_day.weekday()) and window_start <= o["event_day"] < requested_day
    ]
    matched_days = sorted({o["event_day"] for o in slot_obs})
    if len(matched_days) < min_matches:
        return Rejection(
            f"I've only seen {slot_label} {len(matched_days)} time(s) recently "
            f"and need {min_matches} to treat it as a regular session."
        )

    lead_days = _estimate_lead(observations, slot_obs, lead_override)
    if lead_days is None:
        return Rejection("I don't know yet how far ahead registration opens for these sessions.")

    clock = _parse_clock(registration_clock) if registration_clock else None
    if clock is None:
        return Rejection("No valid default registration time is configured, so I can't tell when it opens.")

    registration_time = datetime.combine(requested_day - timedelta(days=lead_days), clock)
    if registration_time <= now:
        return Rejection(
            "Based on past weeks registration should already be open, but the session isn't posted."
        )

    names = sorted({o["name"] for o in slot_obs if o["name"]})
    logger.info(
        f"Predicted {requested_day} {slot}: opens {registration_time} "
        f"(lead {lead_days}d, matched {matched_days})"
    )
    return Prediction(
        event_day=requested_day,
        registration_time=registration_time,
        lead_days=lead_days,
        matched_days=matched_days,
        names=names,
    )

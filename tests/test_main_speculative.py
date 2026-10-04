import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

import main
from events import Events

TODAY = date.today()
REQUESTED_DAY = TODAY + timedelta(days=14)
SLOT_TEXT = "9a - 11a"


def _date_text(day):
    return f"{day:%a}, {day:%b} {day.day}"


def _card(day, actionable=False, opens_in=None, name="Intermediate Drop-in"):
    return {
        "month_day": (day.month, day.day),
        "time": (9, 0, 11, 0),
        "name": name,
        "category": "Open Play",
        "actionable": actionable,
        "opens_in": opens_in,
    }


def _obs(day, lead_days=7, lead_source="countdown"):
    return {
        "event_day": day,
        "start_hm": "09:00",
        "end_hm": "11:00",
        "name": "Intermediate Drop-in",
        "category": "Open Play",
        "lead_days": lead_days,
        "lead_source": lead_source,
    }


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "events.db")
    monkeypatch.setattr(main, "Events", lambda: Events(db_name=path))
    return path


def _open(db_path):
    return Events(db_name=db_path)


class FakeEmailClient:
    def __init__(self, emails=()):
        self._emails = list(emails)
        self.user = "robin.pickleball.scheduler@gmail.com"
        self.replies = []
        self.notifications = []
        self.marked_read_ids = []
        self.archived_ids = []

    def authenticate_email(self):
        return None

    def read_new_emails(self):
        return self._emails

    def is_sender_authorized(self, _sender):
        return True

    def mark_email_as_read(self, email):
        self.marked_read_ids.append(email.id)

    def archive_email(self, email):
        self.archived_ids.append(email.id)

    def reply_to_email(self, email, reply_plaintext, reply_html=None, subject=None, user_tag=None):
        self.replies.append(SimpleNamespace(email=email, body=reply_plaintext, subject=subject, user_tag=user_tag))

    def send_notification(self, subject, body, user_tag=None):
        self.notifications.append((subject, body))

    @staticmethod
    def extract_email_address(addresses):
        return addresses if isinstance(addresses, list) else [addresses]


class FakeWebsite:
    """Configurable stand-in; class attributes are set per test."""

    cards = []
    outcome = None  # callable(event_date, time_range) -> (registration_time, info) or raises
    instances = []
    failing_logins = set()

    def __init__(self, headless=True):
        self.default_registration_time = "14:00:00"
        self.displayed = 0
        self.lookups = []
        FakeWebsite.instances.append(self)

    def login(self, user_tag=None):
        if user_tag in FakeWebsite.failing_logins:
            raise RuntimeError(f"login failed for {user_tag}")
        self.user_tag = user_tag

    def display_all_events(self):
        self.displayed += 1

    def scan_listed_events(self):
        return list(FakeWebsite.cards)

    def determine_access_date(self, event_date, time_range):
        self.lookups.append((event_date, time_range))
        return FakeWebsite.outcome(event_date, time_range)

    def close(self):
        pass


@pytest.fixture
def fake_site(monkeypatch):
    FakeWebsite.cards = []
    FakeWebsite.outcome = None
    FakeWebsite.instances = []
    FakeWebsite.failing_logins = set()
    monkeypatch.setattr(main, "Website", FakeWebsite)
    return FakeWebsite


@pytest.fixture(autouse=True)
def isolated_lock(tmp_path, monkeypatch):
    path = str(tmp_path / "refresh.lock")
    monkeypatch.setattr(main, "REFRESH_LOCK_FILE", path)
    return path


def _not_found(*_args):
    raise main.EventNotFound("not listed")


def _make_email(body):
    return SimpleNamespace(
        To=["robin.pickleball.scheduler@gmail.com"],
        From=["alice@example.com"],
        Cc=[],
        subject="Please register",
        body=body,
        id="msg-1",
        thread_id="thread-1",
        message_id="<msg-1@example.com>",
    )


def _wire_email_flow(monkeypatch, email):
    client = FakeEmailClient(emails=[email])
    monkeypatch.setattr(main, "EmailClient", lambda: client)
    monkeypatch.setattr(main, "extract_user_tag", lambda *_a, **_k: "default")
    monkeypatch.setattr(main, "validate_user_tag", lambda tag: tag)
    monkeypatch.setattr(main, "is_sender_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(main, "load_user_config", lambda _tag: {})
    monkeypatch.setattr(
        main, "extract_user_intent", lambda _e: ("add", (_date_text(REQUESTED_DAY), SLOT_TEXT))
    )
    return client


# --- add path ---------------------------------------------------------------


def test_unlisted_event_matching_weekly_pattern_is_scheduled_speculatively(monkeypatch, db_path, fake_site):
    seed = _open(db_path)
    seed.upsert_observations(
        "default",
        [_obs(REQUESTED_DAY - timedelta(days=21)), _obs(REQUESTED_DAY - timedelta(days=14))],
        seen_at=datetime.now(),
    )
    seed.close()
    # The following week is listed right now; the add path records it on the way.
    fake_site.cards = [_card(REQUESTED_DAY - timedelta(days=7), opens_in=timedelta(hours=1))]
    fake_site.outcome = _not_found
    email = _make_email(f"{_date_text(REQUESTED_DAY)} {SLOT_TEXT}")
    client = _wire_email_flow(monkeypatch, email)

    main.check_for_new_event(headless=True)

    check = _open(db_path)
    rows = check.get_speculative_events()
    assert len(rows) == 1
    row = rows[0]
    assert row["expected_event_day"] == REQUESTED_DAY
    expected_open = datetime.combine(REQUESTED_DAY - timedelta(days=7), datetime.min.time()).replace(hour=14)
    assert row["registration_time"] == expected_open
    assert row["request_ref"] == {
        "from": ["alice@example.com"],
        "thread_id": "thread-1",
        "message_id": "<msg-1@example.com>",
        "subject": "Please register",
    }
    # Opportunistic recording of what was listed.
    assert REQUESTED_DAY - timedelta(days=7) in [
        o["event_day"] for o in check.get_observations("default", since=TODAY)
    ]
    check.close()

    assert len(client.replies) == 1
    assert "isn't posted" in client.replies[0].body
    assert client.notifications == []
    assert client.archived_ids == ["msg-1"]


def test_unlisted_event_without_pattern_gets_polite_rejection(monkeypatch, db_path, fake_site):
    fake_site.outcome = _not_found
    email = _make_email(f"{_date_text(REQUESTED_DAY)} {SLOT_TEXT}")
    client = _wire_email_flow(monkeypatch, email)

    main.check_for_new_event(headless=True)

    check = _open(db_path)
    assert check.get_speculative_events() == []
    check.close()
    assert len(client.replies) == 1
    assert "couldn't find" in client.replies[0].body
    assert client.notifications == []
    assert client.archived_ids == ["msg-1"]


def test_unlisted_event_with_speculative_disabled(monkeypatch, db_path, fake_site):
    monkeypatch.setitem(main.APP_CONFIG, "speculative_enabled", False)
    seed = _open(db_path)
    seed.upsert_observations(
        "default",
        [_obs(REQUESTED_DAY - timedelta(days=k)) for k in (7, 14, 21)],
        seen_at=datetime.now(),
    )
    seed.close()
    fake_site.outcome = _not_found
    client = _wire_email_flow(monkeypatch, _make_email("x"))

    main.check_for_new_event(headless=True)

    check = _open(db_path)
    assert check.get_speculative_events() == []
    check.close()
    assert "couldn't find" in client.replies[0].body


# --- refresh / confirmation step --------------------------------------------


REQUEST_REF = {
    "from": ["alice@example.com"],
    "thread_id": "thread-1",
    "message_id": "<msg-1@example.com>",
    "subject": "Please register",
}


def _seed_speculative(db_path, registration_time=None, expected_day=REQUESTED_DAY, last_checked=None):
    if registration_time is None:
        registration_time = datetime.combine(expected_day - timedelta(days=7), datetime.min.time()).replace(hour=14)
    db = _open(db_path)
    db.insert_event(
        event_date=_date_text(expected_day),
        time_range=SLOT_TEXT,
        registration_time=registration_time,
        user_tag="default",
        additional_info="Speculative",
        status="speculative",
        expected_event_day=expected_day,
        request_ref=REQUEST_REF,
    )
    if last_checked is not None:
        db.mark_checked(_date_text(expected_day), SLOT_TEXT, "default", when=last_checked)
    db.close()


@pytest.fixture
def refresh_env(monkeypatch, db_path, fake_site):
    client = FakeEmailClient()
    monkeypatch.setattr(main, "EmailClient", lambda: client)
    monkeypatch.setattr(main, "list_user_tags", lambda: ["default"])
    env = SimpleNamespace(
        client=client, registrations=[], statuses_at_register=[], register_success=True,
        db_path=db_path, site=fake_site,
    )

    def fake_register(event_info, headless=True, results=None, results_lock=None):
        env.registrations.append(event_info)
        db = _open(db_path)
        db.cursor.execute(
            "SELECT status FROM events WHERE event_date = ? AND time_range = ?",
            (event_info["event_date"], event_info["time_range"]),
        )
        env.statuses_at_register.append(db.cursor.fetchone()[0])
        db.close()
        result = {"user_tag": event_info["user_tag"], "event": "x", "success": env.register_success}
        if not env.register_success:
            result.update(error="signup timed out", traceback="tb")
        results.append(result)

    monkeypatch.setattr(main, "register_for_single_event", fake_register)
    return env


def test_refresh_snapshots_stale_users(refresh_env):
    refresh_env.site.cards = [_card(TODAY + timedelta(days=7), opens_in=timedelta(hours=3))]

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    assert [o["event_day"] for o in db.get_observations("default", since=TODAY)] == [TODAY + timedelta(days=7)]
    assert db.get_last_snapshot("default") is not None
    db.close()
    assert len(refresh_env.site.instances) == 1


def test_first_ever_snapshot_does_not_trust_open_cards_as_first_sightings(refresh_env):
    refresh_env.site.cards = [_card(TODAY + timedelta(days=3), actionable=True)]

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    obs = db.get_observations("default", since=TODAY)[0]
    db.close()
    assert (obs["lead_days"], obs["lead_source"]) == (None, None)


def test_snapshot_after_recent_scan_records_first_sighting_lead(refresh_env):
    db = _open(refresh_env.db_path)
    db.record_snapshot("default", datetime.now() - timedelta(hours=7))
    db.close()
    refresh_env.site.cards = [_card(TODAY + timedelta(days=3), actionable=True)]

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    obs = db.get_observations("default", since=TODAY)[0]
    db.close()
    assert (obs["lead_days"], obs["lead_source"]) == (3, "first_seen")


def test_snapshot_after_long_gap_does_not_trust_first_sightings(refresh_env):
    db = _open(refresh_env.db_path)
    db.record_snapshot("default", datetime.now() - timedelta(days=3))
    db.close()
    refresh_env.site.cards = [_card(TODAY + timedelta(days=3), actionable=True)]

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    obs = db.get_observations("default", since=TODAY)[0]
    db.close()
    assert obs["lead_days"] is None


def test_refresh_skips_everything_when_nothing_is_due(refresh_env):
    db = _open(refresh_env.db_path)
    db.record_snapshot("default", datetime.now())
    db.close()
    _seed_speculative(refresh_env.db_path, last_checked=datetime.now())

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert refresh_env.site.instances == []


def test_refresh_confirms_posted_event_with_future_registration(refresh_env):
    _seed_speculative(refresh_env.db_path)
    real_time = datetime.combine(REQUESTED_DAY - timedelta(days=7), datetime.min.time()).replace(hour=14)
    refresh_env.site.outcome = lambda *_a: (real_time, "Intermediate Drop-in - $10")

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    assert db.get_speculative_events() == []
    row = db.list_all_events("default")[0]
    assert row[3] == "Intermediate Drop-in - $10"
    assert row[4] == "confirmed"
    db.close()
    assert len(refresh_env.client.replies) == 1
    reply = refresh_env.client.replies[0]
    assert reply.email.thread_id == "thread-1"
    assert reply.email.From == ["alice@example.com"]
    assert "posted" in reply.body
    assert refresh_env.registrations == []


def test_refresh_registers_immediately_when_already_open(refresh_env):
    # Prediction missed: the card appeared already open after the predicted time.
    _seed_speculative(refresh_env.db_path, registration_time=datetime.now() - timedelta(hours=2))
    refresh_env.site.outcome = lambda *_a: (datetime.now(), "Intermediate Drop-in")

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert len(refresh_env.registrations) == 1
    info = refresh_env.registrations[0]
    assert info["event_date"] == _date_text(REQUESTED_DAY)
    assert info["user_tag"] == "default"
    db = _open(refresh_env.db_path)
    assert db.get_speculative_events() == []
    db.close()
    assert any("registered" in r.body for r in refresh_env.client.replies)
    assert refresh_env.client.notifications == []


def test_refresh_drops_ineligible_event_politely(refresh_env):
    _seed_speculative(refresh_env.db_path)

    def ineligible(*_a):
        raise main.SkillLevelIneligible("That's a Beginner skill level session, current settings list you as Intermediate.")

    refresh_env.site.outcome = ineligible

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    assert db.list_all_events("default") == []
    db.close()
    assert "Beginner" in refresh_env.client.replies[0].body
    assert refresh_env.client.notifications == []


def test_refresh_expires_when_later_week_is_listed(refresh_env):
    _seed_speculative(refresh_env.db_path)
    refresh_env.site.cards = [_card(REQUESTED_DAY + timedelta(days=7), opens_in=timedelta(days=1))]
    refresh_env.site.outcome = _not_found

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    assert db.list_all_events("default") == []
    db.close()
    assert "never posted" in refresh_env.client.replies[0].body
    assert refresh_env.client.notifications == []


def test_refresh_expires_when_event_day_arrives(refresh_env):
    _seed_speculative(
        refresh_env.db_path,
        expected_day=TODAY,
        registration_time=datetime.now() - timedelta(days=1),
    )
    refresh_env.site.outcome = _not_found

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    assert db.list_all_events("default") == []
    db.close()
    assert "never posted" in refresh_env.client.replies[0].body


def test_refresh_keeps_waiting_when_not_posted_yet(refresh_env):
    _seed_speculative(refresh_env.db_path)
    refresh_env.site.outcome = _not_found

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    rows = db.get_speculative_events()
    assert len(rows) == 1
    assert rows[0]["last_checked"] is not None
    db.close()
    assert refresh_env.client.replies == []


def test_refresh_polls_every_run_after_predicted_time_passes(refresh_env):
    db = _open(refresh_env.db_path)
    db.record_snapshot("default", datetime.now())
    db.close()
    _seed_speculative(
        refresh_env.db_path,
        registration_time=datetime.now() - timedelta(minutes=30),
        last_checked=datetime.now() - timedelta(minutes=15),
    )
    refresh_env.site.outcome = _not_found

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert len(refresh_env.site.instances) == 1
    assert refresh_env.site.instances[0].lookups == [(_date_text(REQUESTED_DAY), SLOT_TEXT)]


# --- registration time ------------------------------------------------------


class FakeRegisterSite:
    url = None
    registered = []

    def __init__(self, headless=True):
        pass

    def login(self, user_tag=None):
        pass

    def get_event_url(self, event_date, time_range):
        raise AssertionError("speculative rows must poll with find_event_url")

    def find_event_url(self, event_date, time_range, until, poll_seconds=10):
        self.until = until
        return FakeRegisterSite.url

    def register_for_event(self, event_date, time_range, event_url):
        FakeRegisterSite.registered.append(event_url)

    def close(self):
        pass


def _speculative_info():
    return {
        "event_date": _date_text(REQUESTED_DAY),
        "time_range": SLOT_TEXT,
        "registration_time": datetime.now(),
        "user_tag": "default",
        "status": "speculative",
        "request_ref": REQUEST_REF,
    }


def test_speculative_registration_not_posted_is_flagged_not_failed(monkeypatch):
    FakeRegisterSite.url = None
    FakeRegisterSite.registered = []
    monkeypatch.setattr(main, "Website", FakeRegisterSite)
    monkeypatch.setattr(main, "dwell_until", lambda *_a, **_k: None)
    results = []

    main.register_for_single_event(_speculative_info(), results=results)

    assert FakeRegisterSite.registered == []
    assert results[0]["success"] is False
    assert results[0]["speculative_not_found"] is True


def test_speculative_registration_posted_registers(monkeypatch):
    FakeRegisterSite.url = "https://x/Details/1"
    FakeRegisterSite.registered = []
    monkeypatch.setattr(main, "Website", FakeRegisterSite)
    monkeypatch.setattr(main, "dwell_until", lambda *_a, **_k: None)
    results = []

    main.register_for_single_event(_speculative_info(), results=results)

    assert FakeRegisterSite.registered == ["https://x/Details/1"]
    assert results[0]["success"] is True


def _wire_registration(monkeypatch, db_path, result):
    client = FakeEmailClient()
    monkeypatch.setattr(main, "EmailClient", lambda: client)
    monkeypatch.setattr(main, "is_within_offset", lambda *_a, **_k: True)
    monkeypatch.setattr(main, "dwell_until", lambda *_a, **_k: None)

    def fake_register(event_info, headless=True, results=None, results_lock=None):
        results.append(dict(result, user_tag=event_info["user_tag"], event=f"{event_info['event_date']} {event_info['time_range']}",
                            event_date=event_info["event_date"], time_range=event_info["time_range"],
                            status=event_info.get("status"), request_ref=event_info.get("request_ref")))

    monkeypatch.setattr(main, "register_for_single_event", fake_register)
    return client


def test_next_event_speculative_not_posted_keeps_row_and_replies_to_requester(monkeypatch, db_path):
    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    client = _wire_registration(monkeypatch, db_path, {"success": False, "speculative_not_found": True, "error": "not posted"})

    main.register_for_next_event(headless=True)

    db = _open(db_path)
    assert len(db.get_speculative_events()) == 1
    db.close()
    assert client.notifications == []
    assert len(client.replies) == 1
    assert client.replies[0].email.thread_id == "thread-1"
    assert "keep checking" in client.replies[0].body


def test_next_event_speculative_success_marks_confirmed(monkeypatch, db_path):
    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    client = _wire_registration(monkeypatch, db_path, {"success": True})

    main.register_for_next_event(headless=True)

    db = _open(db_path)
    assert db.get_speculative_events() == []
    assert db.list_all_events("default")[0][4] == "confirmed"
    db.close()
    assert client.notifications == []


# --- review fixes: registration timing ----------------------------------------


def test_refresh_registers_this_run_when_opening_before_next_run(refresh_env):
    # register_for_next_event already ran; 14:00 would be missed by the next run.
    _seed_speculative(refresh_env.db_path)
    opens = (datetime.now() + timedelta(minutes=9)).replace(microsecond=0)
    refresh_env.site.outcome = lambda *_a: (opens, "Intermediate Drop-in")

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert len(refresh_env.registrations) == 1
    assert refresh_env.registrations[0]["registration_time"] == opens
    db = _open(refresh_env.db_path)
    row = db.list_all_events("default")[0]
    db.close()
    assert row[4] == "confirmed"
    assert row[2] == opens.strftime("%Y-%m-%d %H:%M:%S")
    assert any("registered" in r.body for r in refresh_env.client.replies)


def test_refresh_inline_registration_waits_for_actual_open_time(refresh_env):
    _seed_speculative(refresh_env.db_path)
    opens = (datetime.now() + timedelta(seconds=40)).replace(microsecond=0)
    refresh_env.site.outcome = lambda *_a: (opens, "info")

    main.refresh_schedule_and_confirm_speculative(headless=True)

    # Must dwell until the real opening, not click immediately.
    assert refresh_env.registrations[0]["registration_time"] == opens


def test_refresh_only_confirms_when_opening_after_next_run(refresh_env):
    _seed_speculative(refresh_env.db_path)
    opens = (datetime.now() + timedelta(minutes=40)).replace(microsecond=0)
    refresh_env.site.outcome = lambda *_a: (opens, "info")

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert refresh_env.registrations == []
    db = _open(refresh_env.db_path)
    assert db.list_all_events("default")[0][4] == "confirmed"
    db.close()


def test_refresh_claims_row_while_registering_inline(refresh_env):
    _seed_speculative(refresh_env.db_path, registration_time=datetime.now() - timedelta(hours=1))
    refresh_env.site.outcome = lambda *_a: (datetime.now(), "info")

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert refresh_env.statuses_at_register == ["in_progress"]


def test_refresh_inline_registration_failure_marks_failed_and_tells_everyone(refresh_env):
    _seed_speculative(refresh_env.db_path, registration_time=datetime.now() - timedelta(hours=1))
    refresh_env.site.outcome = lambda *_a: (datetime.now(), "info")
    refresh_env.register_success = False

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    assert db.list_all_events("default")[0][4] == "failed"
    assert db.get_speculative_events() == []
    db.close()
    assert any("didn't go through" in r.body for r in refresh_env.client.replies)
    assert len(refresh_env.client.notifications) == 1


def test_failed_row_is_not_retried_by_refresh_or_scheduler(refresh_env):
    _seed_speculative(refresh_env.db_path, registration_time=datetime.now() + timedelta(minutes=5))
    db = _open(refresh_env.db_path)
    db.set_status(_date_text(REQUESTED_DAY), SLOT_TEXT, "default", "failed")
    db.record_snapshot("default", datetime.now())
    assert db.get_next_event_after() == []
    db.close()

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert refresh_env.site.instances == []


# --- review fixes: expiry -------------------------------------------------------


def test_refresh_keeps_row_on_event_day_before_registration_opens(refresh_env):
    # Lead of 0 days: posted and opened on the event day itself.
    _seed_speculative(
        refresh_env.db_path,
        expected_day=TODAY,
        registration_time=datetime.now() + timedelta(hours=3),
    )
    refresh_env.site.outcome = _not_found

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    assert len(db.get_speculative_events()) == 1
    db.close()
    assert refresh_env.client.replies == []


def test_refresh_posted_without_registration_time_keeps_waiting(refresh_env):
    _seed_speculative(refresh_env.db_path)
    refresh_env.site.outcome = lambda *_a: (None, "info")

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    rows = db.get_speculative_events()
    db.close()
    assert rows[0]["last_checked"] is not None
    assert refresh_env.client.replies == []


def test_refresh_posted_without_registration_time_dropped_on_event_day(refresh_env):
    _seed_speculative(
        refresh_env.db_path, expected_day=TODAY, registration_time=datetime.now() - timedelta(days=1)
    )
    refresh_env.site.outcome = lambda *_a: (None, "info")

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    assert db.list_all_events("default") == []
    db.close()
    assert "couldn't tell when registration opens" in refresh_env.client.replies[0].body


# --- review fixes: one event per registration time ---------------------------


def test_confirm_replaces_other_event_at_same_registration_time(refresh_env):
    real_time = datetime.combine(REQUESTED_DAY - timedelta(days=7), datetime.min.time()).replace(hour=20)
    db = _open(refresh_env.db_path)
    db.insert_event("Some other day", "1p - 3p", real_time, "default")
    db.close()
    _seed_speculative(refresh_env.db_path)
    refresh_env.site.outcome = lambda *_a: (real_time, "info")

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    rows = db.list_all_events("default")
    db.close()
    assert [(r[0], r[4]) for r in rows] == [(_date_text(REQUESTED_DAY), "confirmed")]


# --- review fixes: overlap, polling and backoff --------------------------------


def test_refresh_skips_when_another_run_holds_the_lock(refresh_env, isolated_lock):
    open(isolated_lock, "w").close()
    _seed_speculative(refresh_env.db_path)

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert refresh_env.site.instances == []
    assert os.path.exists(isolated_lock)  # not ours to remove


def test_refresh_removes_stale_lock_and_runs(refresh_env, isolated_lock):
    open(isolated_lock, "w").close()
    old = datetime.now().timestamp() - 2 * 3600
    os.utime(isolated_lock, (old, old))

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert len(refresh_env.site.instances) == 1
    assert not os.path.exists(isolated_lock)


def test_refresh_releases_lock_after_running(refresh_env, isolated_lock):
    main.refresh_schedule_and_confirm_speculative(headless=True)
    assert not os.path.exists(isolated_lock)


def test_refresh_ignores_freshly_claimed_rows(refresh_env):
    _seed_speculative(refresh_env.db_path, registration_time=datetime.now() - timedelta(minutes=1))
    db = _open(refresh_env.db_path)
    db.set_status(_date_text(REQUESTED_DAY), SLOT_TEXT, "default", "in_progress")
    db.record_snapshot("default", datetime.now())
    db.close()

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert refresh_env.site.instances == []
    db = _open(refresh_env.db_path)
    assert len(db.get_speculative_events(status="in_progress")) == 1
    db.close()


def test_refresh_releases_stale_claims(refresh_env):
    _seed_speculative(refresh_env.db_path)
    db = _open(refresh_env.db_path)
    db.set_status(
        _date_text(REQUESTED_DAY), SLOT_TEXT, "default", "in_progress",
        when=datetime.now() - timedelta(hours=2),
    )
    db.close()
    refresh_env.site.outcome = _not_found

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    assert len(db.get_speculative_events()) == 1
    assert db.get_speculative_events(status="in_progress") == []
    db.close()


def test_refresh_stops_polling_every_run_after_fast_poll_window(refresh_env):
    db = _open(refresh_env.db_path)
    db.record_snapshot("default", datetime.now())
    db.close()
    _seed_speculative(
        refresh_env.db_path,
        registration_time=datetime.now() - timedelta(hours=3),
        last_checked=datetime.now() - timedelta(minutes=15),
    )

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert refresh_env.site.instances == []


def test_failed_snapshot_login_backs_off(refresh_env):
    refresh_env.site.failing_logins = {"default"}

    main.refresh_schedule_and_confirm_speculative(headless=True)
    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert len(refresh_env.site.instances) == 1
    db = _open(refresh_env.db_path)
    assert db.get_last_snapshot("default") is None
    assert db.get_last_snapshot_attempt("default") is not None
    db.close()


def test_refresh_continues_after_a_user_fails(refresh_env, monkeypatch):
    monkeypatch.setattr(main, "list_user_tags", lambda: ["alice", "default"])
    refresh_env.site.failing_logins = {"alice"}
    refresh_env.site.cards = [_card(TODAY + timedelta(days=7), opens_in=timedelta(hours=3))]

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    assert db.get_last_snapshot("default") is not None
    assert db.get_last_snapshot("alice") is None
    db.close()


def test_refresh_continues_after_a_row_fails(refresh_env):
    _seed_speculative(refresh_env.db_path)
    _seed_speculative(refresh_env.db_path, expected_day=REQUESTED_DAY + timedelta(days=1))
    calls = []

    def outcome(event_date, time_range):
        calls.append(event_date)
        if len(calls) == 1:
            raise RuntimeError("page blew up")
        raise main.EventNotFound("not listed")

    refresh_env.site.outcome = outcome

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert len(calls) == 2


# --- review fixes: scheduler path ----------------------------------------------


def test_next_event_claims_speculative_rows_before_registering(monkeypatch, db_path):
    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    _wire_registration(monkeypatch, db_path, {"success": True})
    seen = []
    original = main.register_for_single_event

    def spying(event_info, headless=True, results=None, results_lock=None):
        db = _open(db_path)
        seen.append(db.get_speculative_events(status="in_progress"))
        db.close()
        original(event_info, headless, results, results_lock)

    monkeypatch.setattr(main, "register_for_single_event", spying)

    main.register_for_next_event(headless=True)

    assert len(seen[0]) == 1


def test_next_event_speculative_hard_failure_marks_failed(monkeypatch, db_path):
    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    client = _wire_registration(
        monkeypatch, db_path, {"success": False, "error": "signup timed out", "traceback": "tb"}
    )

    main.register_for_next_event(headless=True)

    db = _open(db_path)
    assert db.list_all_events("default")[0][4] == "failed"
    db.close()
    assert len(client.notifications) == 1
    assert "didn't go through" in client.replies[0].body


def test_one_failed_followup_does_not_block_the_rest(monkeypatch, db_path):
    when = datetime.now() + timedelta(minutes=5)
    _seed_speculative(db_path, registration_time=when)
    db = _open(db_path)
    db.insert_event(
        _date_text(REQUESTED_DAY), SLOT_TEXT, when, "alice", status="speculative",
        expected_event_day=REQUESTED_DAY, request_ref=dict(REQUEST_REF, thread_id="thread-alice"),
    )
    db.close()
    client = _wire_registration(monkeypatch, db_path, {"success": False, "speculative_not_found": True, "error": "x"})
    original_reply = client.reply_to_email

    def flaky_reply(email, body, **kwargs):
        if email.thread_id == "thread-alice":
            raise RuntimeError("gmail down")
        original_reply(email, body, **kwargs)

    client.reply_to_email = flaky_reply

    main.register_for_next_event(headless=True)

    assert [r.email.thread_id for r in client.replies] == ["thread-1"]
    db = _open(db_path)
    assert len(db.get_speculative_events()) == 2  # both released back to speculative
    db.close()


# --- review fixes: email path ----------------------------------------------------


def test_unlisted_request_error_still_replies_and_archives(monkeypatch, db_path, fake_site):
    fake_site.outcome = _not_found
    client = _wire_email_flow(monkeypatch, _make_email("x"))

    def boom(*_a, **_k):
        raise ValueError("bad config")

    monkeypatch.setattr(main, "_handle_unlisted_request", boom)

    main.check_for_new_event(headless=True)

    assert "couldn't find" in client.replies[0].body
    assert client.archived_ids == ["msg-1"]


@pytest.mark.parametrize("enabled, expected", [(True, 1), (False, 0)])
def test_webmaster_notification_for_unlisted_request_is_optional(monkeypatch, db_path, fake_site, enabled, expected):
    monkeypatch.setitem(main.APP_CONFIG, "notify_webmaster_on_unlisted_request", enabled)
    fake_site.outcome = _not_found
    client = _wire_email_flow(monkeypatch, _make_email("x"))

    main.check_for_new_event(headless=True)

    assert len(client.notifications) == expected
    assert len(client.replies) == 1


def test_report_shows_status_column(monkeypatch, db_path, fake_site):
    _seed_speculative(db_path)
    client = _wire_email_flow(monkeypatch, _make_email("report"))
    monkeypatch.setattr(main, "extract_user_intent", lambda _e: ("report", None))

    main.check_for_new_event(headless=True)

    body = client.replies[0].body
    assert "status" in body
    assert "speculative" in body
    assert "default" not in body

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

    poll_times = None

    def find_event_url(self, event_date, time_range, poll_times):
        FakeRegisterSite.poll_times = poll_times
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


@pytest.mark.parametrize("offsets", [[-10, 1], [-30, -5, 0, 1, 3, 10]])
def test_speculative_registration_polls_at_configured_offsets_from_opening(monkeypatch, offsets):
    monkeypatch.setitem(main.APP_CONFIG, "speculative_poll_offsets_seconds", offsets)
    FakeRegisterSite.url = None
    FakeRegisterSite.registered = []
    monkeypatch.setattr(main, "Website", FakeRegisterSite)
    monkeypatch.setattr(main, "dwell_until", lambda *_a, **_k: None)
    info = _speculative_info()
    opens = datetime(2026, 10, 26, 20, 0, 0)
    info["registration_time"] = opens

    main.register_for_single_event(info, results=[])

    assert FakeRegisterSite.poll_times == [opens + timedelta(seconds=s) for s in offsets]



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


def test_confirmed_booking_wins_slot_over_speculative_and_requester_is_told(refresh_env):
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
    assert [(r[0], r[4]) for r in rows] == [("Some other day", "confirmed")]
    assert len(refresh_env.client.replies) == 1
    assert "Some other day 1p - 3p is already set to register" in refresh_env.client.replies[0].body


def test_confirming_speculative_displaces_other_speculative_and_tells_its_requester(refresh_env):
    real_time = datetime.combine(REQUESTED_DAY - timedelta(days=7), datetime.min.time()).replace(hour=20)
    db = _open(refresh_env.db_path)
    # Another speculative request already sitting at the real opening time.
    db.insert_event(
        "Other day", "1p - 3p", real_time, "default", status="speculative",
        expected_event_day=REQUESTED_DAY + timedelta(days=1),
        request_ref=dict(REQUEST_REF, thread_id="thread-other"),
    )
    db.record_snapshot("default", datetime.now())
    db.mark_checked("Other day", "1p - 3p", "default", when=datetime.now())
    db.close()
    _seed_speculative(refresh_env.db_path)
    refresh_env.site.outcome = lambda *_a: (real_time, "info")

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    rows = db.list_all_events("default")
    db.close()
    assert [(r[0], r[4]) for r in rows] == [(_date_text(REQUESTED_DAY), "confirmed")]
    threads = {r.email.thread_id: r.body for r in refresh_env.client.replies}
    assert "was replaced by" in threads["thread-other"]
    assert "now posted" in threads["thread-1"]


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


# --- round-2 review fixes: claims ---------------------------------------------


def test_scheduler_skips_rows_another_run_claimed_first(monkeypatch, db_path):
    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    _wire_registration(monkeypatch, db_path, {"success": True})
    calls = []
    monkeypatch.setattr(main, "register_for_single_event", lambda *a, **k: calls.append(a))
    # Another process wins the claim between selection and claiming.
    monkeypatch.setattr(Events, "claim", lambda self, *a, **k: False)

    main.register_for_next_event(headless=True)

    assert calls == []


def test_scheduler_prefers_confirmed_over_speculative_for_same_user_and_time(monkeypatch, db_path):
    when = datetime.now() + timedelta(minutes=5)
    _seed_speculative(db_path, registration_time=when)
    db = _open(db_path)
    db.insert_event("Booked day", "1p - 3p", when, "default")
    db.close()
    _wire_registration(monkeypatch, db_path, {"success": True})
    registered = []
    original = main.register_for_single_event

    def spy(event_info, headless=True, results=None, results_lock=None):
        registered.append(event_info["event_date"])
        original(event_info, headless, results, results_lock)

    monkeypatch.setattr(main, "register_for_single_event", spy)

    main.register_for_next_event(headless=True)

    assert registered == ["Booked day"]
    db = _open(db_path)
    assert len(db.get_speculative_events()) == 1  # left for refresh to settle
    db.close()


def test_scheduler_followup_email_failure_still_finishes_bookkeeping(monkeypatch, db_path):
    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    _wire_registration(monkeypatch, db_path, {"success": False, "speculative_not_found": True, "error": "x"})

    def broken_client():
        raise RuntimeError("gmail auth failed")

    monkeypatch.setattr(main, "EmailClient", broken_client)
    cleaned = []
    monkeypatch.setattr(Events, "remove_old_events", lambda self, n_days: cleaned.append(n_days))

    main.register_for_next_event(headless=True)

    db = _open(db_path)
    assert len(db.get_speculative_events()) == 1
    db.close()
    assert cleaned


def test_refresh_skips_row_whose_status_changed_after_due_list_was_read(refresh_env, monkeypatch):
    monkeypatch.setattr(main, "list_user_tags", lambda: ["default"])
    second_day = REQUESTED_DAY + timedelta(days=1)
    _seed_speculative(refresh_env.db_path)
    _seed_speculative(refresh_env.db_path, expected_day=second_day)
    looked_up = []

    def outcome(event_date, time_range):
        looked_up.append(event_date)
        # While checking the first row, another run claims the second one.
        db = _open(refresh_env.db_path)
        db.claim(_date_text(second_day), SLOT_TEXT, "default")
        db.close()
        raise main.EventNotFound("not listed")

    refresh_env.site.outcome = outcome

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert looked_up == [_date_text(REQUESTED_DAY)]


def test_refresh_inline_claim_lost_to_another_run_does_not_register(refresh_env, monkeypatch):
    _seed_speculative(refresh_env.db_path, registration_time=datetime.now() - timedelta(hours=1))
    refresh_env.site.outcome = lambda *_a: (datetime.now(), "info")
    monkeypatch.setattr(Events, "claim", lambda self, *a, **k: False)

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert refresh_env.registrations == []


def test_released_claim_with_future_predicted_time_is_rechecked_right_away(refresh_env):
    _seed_speculative(refresh_env.db_path, registration_time=datetime.now() + timedelta(days=3))
    db = _open(refresh_env.db_path)
    db.record_snapshot("default", datetime.now())
    db.set_status(
        _date_text(REQUESTED_DAY), SLOT_TEXT, "default", "in_progress",
        when=datetime.now() - timedelta(hours=2),
    )
    db.close()
    refresh_env.site.outcome = lambda *_a: (datetime.now(), "info")

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert len(refresh_env.registrations) == 1


# --- round-2 review fixes: refresh concurrency and resilience ----------------


def test_refresh_registers_without_waiting_for_other_users_scans(refresh_env, monkeypatch):
    import threading

    monkeypatch.setattr(main, "list_user_tags", lambda: ["alice", "default"])
    db = _open(refresh_env.db_path)
    db.insert_event(
        "Alice day", SLOT_TEXT, datetime.now() - timedelta(hours=1), "alice", status="speculative",
        expected_event_day=REQUESTED_DAY, request_ref=dict(REQUEST_REF, thread_id="thread-alice"),
    )
    db.close()
    _seed_speculative(refresh_env.db_path, registration_time=datetime.now() - timedelta(hours=1))
    default_scanned = threading.Event()
    waited = []

    def outcome(event_date, time_range):
        if event_date != "Alice day":
            default_scanned.set()
        return datetime.now(), "info"

    def slow_register(event_info, headless=True, results=None, results_lock=None):
        if event_info["user_tag"] == "alice":
            # Sequential processing would deadlock here until the timeout.
            waited.append(default_scanned.wait(timeout=5))
        results.append({"user_tag": event_info["user_tag"], "event": "x", "success": True})

    refresh_env.site.outcome = outcome
    monkeypatch.setattr(main, "register_for_single_event", slow_register)

    main.refresh_schedule_and_confirm_speculative(headless=True)

    assert waited == [True]
    db = _open(refresh_env.db_path)
    assert db.get_speculative_events() == []
    assert db.get_speculative_events(status="in_progress") == []
    db.close()


def test_refresh_email_client_failure_still_records_snapshots(refresh_env, monkeypatch):
    _seed_speculative(refresh_env.db_path)
    refresh_env.site.outcome = _not_found
    refresh_env.site.cards = [_card(TODAY + timedelta(days=7), opens_in=timedelta(hours=3))]

    def broken_client():
        raise RuntimeError("gmail auth failed")

    monkeypatch.setattr(main, "EmailClient", broken_client)

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    assert db.get_last_snapshot("default") is not None
    db.close()


def test_lock_is_not_removed_after_another_run_took_it_over(isolated_lock):
    with main._exclusive_lock(isolated_lock, timedelta(minutes=60)) as acquired:
        assert acquired
        # Simulate another run treating our lock as stale and taking it over.
        with open(isolated_lock, "w") as f:
            f.write("someone-else")
    assert os.path.exists(isolated_lock)


def test_lock_open_error_counts_as_not_acquired(isolated_lock, monkeypatch):
    def denied(*_a, **_k):
        raise PermissionError("mid-delete")

    monkeypatch.setattr(main.os, "open", denied)
    with main._exclusive_lock(isolated_lock, timedelta(minutes=60)) as acquired:
        assert acquired is False


# --- round-2 review fixes: email add path --------------------------------------


def _wire_add(monkeypatch, outcome_time, event_date=None):
    event_date = event_date or _date_text(REQUESTED_DAY)
    client = FakeEmailClient(emails=[_make_email("add")])
    monkeypatch.setattr(main, "EmailClient", lambda: client)
    monkeypatch.setattr(main, "extract_user_tag", lambda *_a, **_k: "default")
    monkeypatch.setattr(main, "validate_user_tag", lambda tag: tag)
    monkeypatch.setattr(main, "is_sender_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(main, "extract_user_intent", lambda _e: ("add", (event_date, SLOT_TEXT)))
    FakeWebsite.outcome = lambda *_a: (outcome_time, "info")
    registered = []

    def fake_register(event_info, headless=True, results=None, results_lock=None):
        registered.append(event_info)
        results.append({"user_tag": event_info["user_tag"], "event": "x", "success": True})

    monkeypatch.setattr(main, "register_for_single_event", fake_register)
    return client, registered


def test_add_for_event_being_registered_right_now_is_left_alone(monkeypatch, db_path, fake_site):
    _seed_speculative(db_path)
    db = _open(db_path)
    db.claim(_date_text(REQUESTED_DAY), SLOT_TEXT, "default")
    db.close()
    client, registered = _wire_add(monkeypatch, datetime.now())

    main.check_for_new_event(headless=True)

    assert registered == []
    assert "already registering" in client.replies[0].body
    db = _open(db_path)
    assert db.get_status(_date_text(REQUESTED_DAY), SLOT_TEXT, "default") == "in_progress"
    db.close()


def test_add_for_open_event_replaces_its_speculative_row(monkeypatch, db_path, fake_site):
    _seed_speculative(db_path)
    client, registered = _wire_add(monkeypatch, datetime.now())

    main.check_for_new_event(headless=True)

    assert len(registered) == 1
    db = _open(db_path)
    assert db.get_status(_date_text(REQUESTED_DAY), SLOT_TEXT, "default") == "confirmed"
    db.close()


def test_add_displaces_speculative_row_at_same_time_and_tells_its_requester(monkeypatch, db_path, fake_site):
    when = (datetime.now() + timedelta(days=2)).replace(microsecond=0)
    _seed_speculative(db_path, registration_time=when)
    client, registered = _wire_add(monkeypatch, when, event_date="Some other day")

    main.check_for_new_event(headless=True)

    db = _open(db_path)
    rows = db.list_all_events("default")
    db.close()
    assert [(r[0], r[4]) for r in rows] == [("Some other day", "confirmed")]
    assert any("was replaced by Some other day" in r.body for r in client.replies)


def test_add_never_displaces_a_row_being_registered(monkeypatch, db_path, fake_site):
    when = (datetime.now() + timedelta(days=2)).replace(microsecond=0)
    _seed_speculative(db_path, registration_time=when)
    db = _open(db_path)
    db.claim(_date_text(REQUESTED_DAY), SLOT_TEXT, "default")
    db.close()
    _wire_add(monkeypatch, when, event_date="Some other day")

    main.check_for_new_event(headless=True)

    db = _open(db_path)
    assert db.get_status(_date_text(REQUESTED_DAY), SLOT_TEXT, "default") == "in_progress"
    db.close()


def test_webmaster_not_notified_when_speculative_request_is_accepted(monkeypatch, db_path, fake_site):
    monkeypatch.setitem(main.APP_CONFIG, "notify_webmaster_on_unlisted_request", True)
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

    assert "isn't posted" in client.replies[0].body
    assert client.notifications == []


# --- round-1 polling review: config, waits, run order -------------------------


def _load_config_with(tmp_path, offsets):
    import json as _json

    path = tmp_path / "app_config.json"
    path.write_text(_json.dumps({"speculative_poll_offsets_seconds": offsets}))
    return main.load_app_config(str(path))


@pytest.mark.parametrize("offsets", [[-10, 1], [-30, -2.5, 0, 1, 5]])
def test_valid_poll_offsets_are_kept(tmp_path, offsets):
    assert _load_config_with(tmp_path, offsets)["speculative_poll_offsets_seconds"] == offsets


@pytest.mark.parametrize("offsets", [[], ["-10", 1], 1, None, [True, 1], "[-10, 1]"])
def test_invalid_poll_offsets_fall_back_to_default(tmp_path, offsets):
    assert _load_config_with(tmp_path, offsets)["speculative_poll_offsets_seconds"] == main.DEFAULT_APP_CONFIG["speculative_poll_offsets_seconds"]


def test_missing_poll_offsets_key_uses_default(tmp_path):
    path = tmp_path / "app_config.json"
    path.write_text('{"speculative_find_grace_minutes": 5}')
    assert main.load_app_config(str(path))["speculative_poll_offsets_seconds"] == main.DEFAULT_APP_CONFIG["speculative_poll_offsets_seconds"]


def _dwell_spy(monkeypatch):
    dwells = []
    monkeypatch.setattr(main, "dwell_until", lambda *a, **k: dwells.append((a, k)))
    return dwells


def test_speculative_hit_after_opening_registers_without_extra_delay(monkeypatch):
    FakeRegisterSite.url = "https://x/Details/1"
    FakeRegisterSite.registered = []
    monkeypatch.setattr(main, "Website", FakeRegisterSite)
    dwells = _dwell_spy(monkeypatch)
    info = _speculative_info()
    info["registration_time"] = datetime.now() - timedelta(seconds=2)

    main.register_for_single_event(info, results=[])

    assert dwells == []
    assert FakeRegisterSite.registered == ["https://x/Details/1"]


def test_speculative_hit_before_opening_still_waits_for_opening(monkeypatch):
    FakeRegisterSite.url = "https://x/Details/1"
    FakeRegisterSite.registered = []
    monkeypatch.setattr(main, "Website", FakeRegisterSite)
    dwells = _dwell_spy(monkeypatch)
    info = _speculative_info()
    info["registration_time"] = datetime.now() + timedelta(seconds=30)

    main.register_for_single_event(info, results=[])

    assert len(dwells) == 1
    assert dwells[0][0][0] == info["registration_time"]


def _run_with_spies(monkeypatch, due):
    calls = []
    monkeypatch.setattr(main, "_scheduled_registration_due", lambda: due)
    monkeypatch.setattr(main, "check_for_new_event", lambda headless=True, pending_open=None: calls.append("email"))
    monkeypatch.setattr(main, "register_for_next_event", lambda headless=True: calls.append("scheduler"))
    monkeypatch.setattr(main, "register_open_events", lambda pending, headless=True: calls.append("open"))
    monkeypatch.setattr(
        main, "refresh_schedule_and_confirm_speculative", lambda headless=True: calls.append("refresh")
    )
    main.run(headless=True)
    return calls


def test_run_holds_before_reading_email_when_a_registration_is_due(monkeypatch):
    assert _run_with_spies(monkeypatch, due=True) == ["scheduler", "email", "scheduler", "open", "refresh"]


def test_run_reads_email_first_when_nothing_is_due(monkeypatch):
    assert _run_with_spies(monkeypatch, due=False) == ["email", "scheduler", "open", "refresh"]


def test_scheduled_registration_due_checks_the_hold_window(db_path):
    db = _open(db_path)
    db.insert_event("Soon", "9a - 11a", datetime.now() + timedelta(minutes=5), "default")
    db.close()
    assert main._scheduled_registration_due() is True


def test_scheduled_registration_not_due_when_far_off(db_path):
    db = _open(db_path)
    db.insert_event("Later", "9a - 11a", datetime.now() + timedelta(hours=3), "default")
    db.close()
    assert main._scheduled_registration_due() is False


# --- round-2 polling review ---------------------------------------------------


@pytest.mark.parametrize("offsets", [[-30], [-10, -1]])
def test_all_negative_poll_offsets_fall_back_to_default(tmp_path, offsets):
    assert _load_config_with(tmp_path, offsets)["speculative_poll_offsets_seconds"] == main.DEFAULT_APP_CONFIG["speculative_poll_offsets_seconds"]


def test_zero_offset_counts_as_at_opening(tmp_path):
    assert _load_config_with(tmp_path, [-10, 0])["speculative_poll_offsets_seconds"] == [-10, 0]


@pytest.mark.parametrize("list_seen, expected", [(True, False), (False, True)])
def test_not_found_result_flags_when_the_list_never_loaded(monkeypatch, list_seen, expected):
    FakeRegisterSite.url = None
    FakeRegisterSite.registered = []
    FakeRegisterSite.list_seen_while_polling = list_seen
    monkeypatch.setattr(main, "Website", FakeRegisterSite)
    monkeypatch.setattr(main, "dwell_until", lambda *_a, **_k: None)
    results = []

    main.register_for_single_event(_speculative_info(), results=results)

    assert results[0]["list_unavailable"] is expected
    del FakeRegisterSite.list_seen_while_polling


@pytest.mark.parametrize(
    "list_unavailable, phrase",
    [(False, "wasn't posted"), (True, "couldn't load the events list")],
)
def test_not_posted_followup_wording(monkeypatch, db_path, list_unavailable, phrase):
    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    client = _wire_registration(
        monkeypatch, db_path,
        {"success": False, "speculative_not_found": True, "list_unavailable": list_unavailable, "error": "x"},
    )

    main.register_for_next_event(headless=True)

    assert phrase in client.replies[0].body
    assert "keep checking" in client.replies[0].body


# --- round-3 polling review: login failures ------------------------------------


class _LoginTimesOutSite(FakeRegisterSite):
    def login(self, user_tag=None):
        raise main.WebDriverException("login page timed out")


def test_speculative_login_timeout_leaves_row_retryable(monkeypatch):
    monkeypatch.setattr(main, "Website", _LoginTimesOutSite)
    results = []

    main.register_for_single_event(_speculative_info(), results=results)

    assert results[0]["success"] is False
    assert results[0]["speculative_not_found"] is True
    assert results[0]["list_unavailable"] is True


def test_confirmed_login_timeout_is_still_a_real_failure(monkeypatch):
    monkeypatch.setattr(main, "Website", _LoginTimesOutSite)
    info = dict(_speculative_info(), status="confirmed")
    results = []

    main.register_for_single_event(info, results=results)

    assert results[0]["success"] is False
    assert not results[0].get("speculative_not_found")


def test_speculative_login_timeout_end_to_end_keeps_row_speculative(monkeypatch, db_path):
    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    client = FakeEmailClient()
    monkeypatch.setattr(main, "EmailClient", lambda: client)
    monkeypatch.setattr(main, "is_within_offset", lambda *_a, **_k: True)
    monkeypatch.setattr(main, "dwell_until", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "Website", _LoginTimesOutSite)

    main.register_for_next_event(headless=True)

    db = _open(db_path)
    assert len(db.get_speculative_events()) == 1
    db.close()
    assert len(client.notifications) == 1  # owner alerted; row still retryable
    assert "couldn't load the events list" in client.replies[0].body


def test_speculative_login_failure_still_alerts_the_owner(monkeypatch, db_path):
    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    client = FakeEmailClient()
    monkeypatch.setattr(main, "EmailClient", lambda: client)
    monkeypatch.setattr(main, "is_within_offset", lambda *_a, **_k: True)
    monkeypatch.setattr(main, "dwell_until", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "Website", _LoginTimesOutSite)

    main.register_for_next_event(headless=True)

    assert len(client.notifications) == 1
    assert client.notifications[0][0] == "Event registration failed"
    db = _open(db_path)
    assert len(db.get_speculative_events()) == 1  # still retryable
    db.close()


def test_plain_not_posted_does_not_alert_the_owner(monkeypatch, db_path):
    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    client = _wire_registration(
        monkeypatch, db_path, {"success": False, "speculative_not_found": True, "error": "x"}
    )

    main.register_for_next_event(headless=True)

    assert client.notifications == []


def test_default_poll_schedule_starts_before_opening_and_keeps_looking_for_a_minute():
    offsets = main.DEFAULT_APP_CONFIG["speculative_poll_offsets_seconds"]
    assert offsets == [-10, 1, 3, 6, 10, 20, 40, 60]
    assert min(offsets) < 0 <= max(offsets)
    assert main._valid_poll_offsets(offsets) == offsets


# --- Site redesign: inbox isolation, full sessions, diagnostics ------


class _SnapshottingWebsite(FakeWebsite):
    snapshots_taken = []
    closed = False

    def close(self):
        self.closed = True

    def capture_failure(self, label):
        _SnapshottingWebsite.snapshots_taken.append(label)
        return [f"logs/failures/{label}.png"]


def _two_emails():
    first = _make_email("first")
    second = SimpleNamespace(**dict(vars(_make_email("second")), id="msg-2", thread_id="thread-2"))
    return first, second


def _wire_first_email_browser_error(monkeypatch, fake_site):
    _SnapshottingWebsite.snapshots_taken = []
    first, second = _two_emails()
    client = FakeEmailClient(emails=[first, second])
    monkeypatch.setattr(main, "EmailClient", lambda: client)
    monkeypatch.setattr(main, "Website", _SnapshottingWebsite)
    monkeypatch.setattr(main, "extract_user_tag", lambda *_a, **_k: "default")
    monkeypatch.setattr(main, "validate_user_tag", lambda tag: tag)
    monkeypatch.setattr(main, "is_sender_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(main, "load_user_config", lambda _tag: {})
    monkeypatch.setattr(
        main, "extract_user_intent",
        lambda e: ("add", ("Wed, Nov 11th", "11a - 1p") if e.id == "msg-1" else (_date_text(REQUESTED_DAY), SLOT_TEXT)),
    )
    later = datetime.combine(REQUESTED_DAY - timedelta(days=7), datetime.min.time()).replace(hour=20)

    def outcome(event_date, time_range):
        if event_date == "Wed, Nov 11th":
            raise main.WebDriverException("Timed out after 30s waiting for the events list")
        return later, "info"

    fake_site.outcome = outcome  # FakeWebsite reads its own class attribute
    return client


def test_browser_error_leaves_the_email_for_the_next_run(monkeypatch, db_path, fake_site):
    client = _wire_first_email_browser_error(monkeypatch, fake_site)

    main.check_for_new_event(headless=True)

    # The failing email stays unread and unanswered; the next one is handled.
    assert client.archived_ids == ["msg-2"]
    assert client.marked_read_ids == ["msg-2"]
    assert [r.email.id for r in client.replies] == ["msg-2"]
    assert client.notifications == []
    db = _open(db_path)
    assert db.get_email_failures("msg-1") == 1
    db.close()


def test_browser_error_gets_a_fresh_browser_for_the_next_email(monkeypatch, db_path, fake_site):
    _wire_first_email_browser_error(monkeypatch, fake_site)

    main.check_for_new_event(headless=True)

    assert len(fake_site.instances) == 2
    assert fake_site.instances[0].closed


def test_one_failing_email_does_not_block_the_rest_of_the_inbox(monkeypatch, db_path, fake_site):
    client = _wire_first_email_browser_error(monkeypatch, fake_site)
    db = _open(db_path)
    for _ in range(main.EMAIL_FAILURE_LIMIT - 1):
        db.bump_email_failures("msg-1")
    db.close()

    main.check_for_new_event(headless=True)

    # On the last allowed attempt both emails are handled and taken out of the inbox.
    assert client.archived_ids == ["msg-1", "msg-2"]
    assert client.marked_read_ids == ["msg-1", "msg-2"]
    bodies = {r.email.id: r.body for r in client.replies}
    assert "Something went wrong" in bodies["msg-1"]
    assert "I determined I need to register" in bodies["msg-2"]
    # The webmaster is told, with the snapshot.
    assert len(client.notifications) == 1
    assert "logs/failures/email_default.png" in client.notifications[0][1]
    assert _SnapshottingWebsite.snapshots_taken == ["email_default"]
    db = _open(db_path)
    assert db.get_email_failures("msg-1") == 0
    db.close()


def test_successfully_handled_email_clears_its_failure_count(monkeypatch, db_path, fake_site):
    db = _open(db_path)
    db.bump_email_failures("msg-1")
    db.close()
    fake_site.outcome = lambda *_a: (datetime.now() + timedelta(days=2), "info")
    _wire_email_flow(monkeypatch, _make_email("x"))

    main.check_for_new_event(headless=True)

    db = _open(db_path)
    assert db.get_email_failures("msg-1") == 0
    db.close()


def test_non_browser_error_after_authorization_is_reported_at_once(monkeypatch, db_path, fake_site):
    def broken(*_a):
        raise KeyError("bad page data")

    fake_site.outcome = broken
    client = _wire_email_flow(monkeypatch, _make_email("x"))

    main.check_for_new_event(headless=True)

    assert "Something went wrong" in client.replies[0].body
    assert len(client.notifications) == 1
    assert client.archived_ids == ["msg-1"]


@pytest.mark.parametrize("stage", ["contacts", "extract_tag", "validate_tag", "sender_allowed"])
def test_errors_before_authorization_never_reply(monkeypatch, db_path, fake_site, stage):
    client = _wire_email_flow(monkeypatch, _make_email("x"))

    def boom(*_a, **_k):
        raise RuntimeError(f"{stage} lookup failed")

    if stage == "contacts":
        client.is_sender_authorized = boom
    else:
        target = {"extract_tag": "extract_user_tag", "validate_tag": "validate_user_tag",
                  "sender_allowed": "is_sender_allowed"}[stage]
        monkeypatch.setattr(main, target, boom)

    # Retried (left unread) until the limit, then silently archived.
    for run in range(1, main.EMAIL_FAILURE_LIMIT + 1):
        main.check_for_new_event(headless=True)
        if run < main.EMAIL_FAILURE_LIMIT:
            assert client.archived_ids == [] and client.notifications == []

    assert client.replies == []
    assert client.marked_read_ids == ["msg-1"]
    assert client.archived_ids == ["msg-1"]
    assert len(client.notifications) == 1
    assert "before the sender was authorized" in client.notifications[0][1]


def test_full_session_gets_a_polite_reply_without_webmaster_mail(monkeypatch, db_path, fake_site):
    def full(*_a):
        raise main.EventFull("full")

    fake_site.outcome = full
    client = _wire_email_flow(monkeypatch, _make_email("x"))

    main.check_for_new_event(headless=True)

    assert "already full" in client.replies[0].body
    assert client.notifications == []
    assert client.archived_ids == ["msg-1"]


def test_refresh_drops_speculative_row_that_turned_out_full(refresh_env):
    _seed_speculative(refresh_env.db_path)

    def full(*_a):
        raise main.EventFull("full")

    refresh_env.site.outcome = full

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    assert db.list_all_events("default") == []
    db.close()
    assert "already full" in refresh_env.client.replies[0].body


def test_series_dates_seen_on_details_page_are_recorded_with_their_countdowns(monkeypatch, db_path, fake_site):
    later_week = REQUESTED_DAY + timedelta(days=7)

    class WithRows(FakeWebsite):
        last_details_rows = [
            {"month_day": (later_week.month, later_week.day), "time": (9, 0, 11, 0), "name": "Drop-in",
             "category": "Open Play", "actionable": False, "opens_in": timedelta(days=13, hours=23)},
        ]

    monkeypatch.setattr(main, "Website", WithRows)
    fake_site.outcome = lambda *_a: (datetime.now() + timedelta(days=2), "info")
    _wire_email_flow(monkeypatch, _make_email("x"))

    main.check_for_new_event(headless=True)

    db = _open(db_path)
    obs = db.get_observations("default", since=TODAY)
    db.close()
    assert [(o["event_day"], o["lead_source"]) for o in obs] == [(later_week, "countdown")]


def test_registration_failure_snapshot_is_in_the_failure_email(monkeypatch, db_path):
    class FailingSite(FakeRegisterSite):
        def find_event_url(self, *a, **k):
            return "https://x/Details/1"

        def register_for_event(self, *a, **k):
            raise main.WebDriverException("Timed out after 30s waiting for confirmation")

        def capture_failure(self, label):
            return [f"logs/failures/{label}.png", f"logs/failures/{label}.html"]

    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    client = FakeEmailClient()
    monkeypatch.setattr(main, "EmailClient", lambda: client)
    monkeypatch.setattr(main, "is_within_offset", lambda *_a, **_k: True)
    monkeypatch.setattr(main, "dwell_until", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "Website", FailingSite)

    main.register_for_next_event(headless=True)

    body = client.notifications[0][1]
    assert "logs/failures/register_default.png" in body
    assert "waiting for confirmation" in body


@pytest.mark.parametrize("outcome, decision", [
    (_not_found, "wait"),
    (lambda *_a: (datetime.combine(REQUESTED_DAY - timedelta(days=7), datetime.min.time()).replace(hour=20), "i"),
     "confirm"),
])
def test_every_speculative_check_logs_one_decision_line(refresh_env, caplog, outcome, decision):
    _seed_speculative(refresh_env.db_path)
    refresh_env.site.outcome = outcome
    caplog.set_level("INFO", logger="__main__")
    caplog.set_level("INFO", logger="main")

    main.refresh_schedule_and_confirm_speculative(headless=True)

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Speculative check")]
    assert len(lines) == 1
    assert f": {decision} - " in lines[0]
    assert "predicted opening" in lines[0]


# --- review round: list reload, unverified and not-open registrations --------


class _ListReloadingSite(FakeWebsite):
    """Like the real site: the lookup leaves the browser on a details page."""

    def determine_access_date(self, event_date, time_range):
        self.on_list = False
        return super().determine_access_date(event_date, time_range)

    def display_all_events(self):
        super().display_all_events()
        self.on_list = True

    def scan_listed_events(self):
        return super().scan_listed_events() if self.on_list else []


def _seed_pattern(db_path):
    seed = _open(db_path)
    seed.upsert_observations(
        "default",
        [_obs(REQUESTED_DAY - timedelta(days=21)), _obs(REQUESTED_DAY - timedelta(days=14))],
        seen_at=datetime.now(),
    )
    seed.close()


def test_unlisted_request_reloads_the_list_before_recording_it(monkeypatch, db_path, fake_site):
    _seed_pattern(db_path)
    monkeypatch.setattr(main, "Website", _ListReloadingSite)
    fake_site.cards = [_card(REQUESTED_DAY - timedelta(days=7), opens_in=timedelta(hours=1))]
    fake_site.outcome = _not_found
    _wire_email_flow(monkeypatch, _make_email("x"))

    main.check_for_new_event(headless=True)

    db = _open(db_path)
    seen = [o["event_day"] for o in db.get_observations("default", since=TODAY)]
    db.close()
    assert REQUESTED_DAY - timedelta(days=7) in seen


def test_unlisted_request_still_scheduled_when_the_list_reload_fails(monkeypatch, db_path, fake_site):
    _seed_pattern(db_path)

    class ReloadFails(FakeWebsite):
        def display_all_events(self):
            raise main.WebDriverException("list timed out")

    monkeypatch.setattr(main, "Website", ReloadFails)
    fake_site.outcome = _not_found
    client = _wire_email_flow(monkeypatch, _make_email("x"))

    main.check_for_new_event(headless=True)

    db = _open(db_path)
    assert len(db.get_speculative_events()) == 1
    db.close()
    assert "isn't posted" in client.replies[0].body


class _RaisingRegisterSite(FakeRegisterSite):
    error = None

    def find_event_url(self, *a, **k):
        return "https://x/Details/1"

    def get_event_url(self, *a, **k):
        return "https://x/Details/1"

    def register_for_event(self, *a, **k):
        raise _RaisingRegisterSite.error

    def capture_failure(self, label):
        return []


def _raising_site(monkeypatch, error):
    _RaisingRegisterSite.error = error
    monkeypatch.setattr(main, "Website", _RaisingRegisterSite)
    monkeypatch.setattr(main, "dwell_until", lambda *_a, **_k: None)


def test_unverified_registration_is_flagged_in_the_result(monkeypatch):
    _raising_site(monkeypatch, main.RegistrationUnverified("couldn't be checked"))
    results = []

    main.register_for_single_event(dict(_speculative_info(), status="confirmed"), results=results)

    assert results[0]["success"] is False
    assert results[0]["unverified"] is True


def test_speculative_row_not_open_yet_goes_back_to_speculative(monkeypatch):
    _raising_site(monkeypatch, main.RegistrationNotOpen("not open", opens_in=timedelta(hours=2)))
    results = []

    main.register_for_single_event(_speculative_info(), results=results)

    assert results[0]["speculative_not_found"] is True
    assert results[0]["not_open"] is True


def test_confirmed_row_not_open_is_still_a_failure(monkeypatch):
    _raising_site(monkeypatch, main.RegistrationNotOpen("not open"))
    results = []

    main.register_for_single_event(dict(_speculative_info(), status="confirmed"), results=results)

    assert results[0]["success"] is False
    assert not results[0].get("speculative_not_found")
    assert results[0]["not_open"] is True


def test_scheduler_not_open_speculative_row_stays_speculative_with_its_own_wording(monkeypatch, db_path):
    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    client = _wire_registration(
        monkeypatch, db_path,
        {"success": False, "speculative_not_found": True, "not_open": True, "error": "not open"},
    )

    main.register_for_next_event(headless=True)

    db = _open(db_path)
    assert len(db.get_speculative_events()) == 1
    db.close()
    assert "registration wasn't open yet" in client.replies[0].body
    assert client.notifications == []


def test_scheduler_unverified_speculative_row_is_confirmed_with_a_warning(monkeypatch, db_path):
    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    client = _wire_registration(
        monkeypatch, db_path,
        {"success": False, "unverified": True, "error": "couldn't be checked", "traceback": "tb"},
    )

    main.register_for_next_event(headless=True)

    db = _open(db_path)
    row = db.list_all_events("default")[0]
    db.close()
    assert row[4] == "confirmed"
    assert row[3] == f"Speculative - {main.UNVERIFIED_INFO}"
    assert "couldn't confirm" in client.replies[0].body
    assert "didn't go through" not in client.replies[0].body
    assert len(client.notifications) == 1


def test_refresh_inline_unverified_registration_is_confirmed_with_a_warning(refresh_env, monkeypatch):
    _seed_speculative(refresh_env.db_path, registration_time=datetime.now() - timedelta(hours=1))
    refresh_env.site.outcome = lambda *_a: (datetime.now(), "info")

    def unverified(event_info, headless=True, results=None, results_lock=None):
        results.append({"user_tag": event_info["user_tag"], "event": "x", "success": False,
                        "unverified": True, "error": "couldn't be checked", "traceback": "tb"})

    monkeypatch.setattr(main, "register_for_single_event", unverified)

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    row = db.list_all_events("default")[0]
    db.close()
    assert row[4] == "confirmed"
    assert row[3] == f"info - {main.UNVERIFIED_INFO}"
    assert any("couldn't confirm" in r.body for r in refresh_env.client.replies)
    assert len(refresh_env.client.notifications) == 1


def test_scheduler_unverified_confirmed_row_gets_the_warning_without_a_reply(monkeypatch, db_path):
    db = _open(db_path)
    db.insert_event(_date_text(REQUESTED_DAY), SLOT_TEXT, datetime.now() + timedelta(minutes=5), "default",
                    additional_info="$3 Intermediate Pickleball")
    db.close()
    client = _wire_registration(
        monkeypatch, db_path,
        {"success": False, "unverified": True, "error": "couldn't be checked", "traceback": "tb"},
    )

    main.register_for_next_event(headless=True)

    db = _open(db_path)
    row = db.list_all_events("default")[0]
    db.close()
    assert row[4] == "confirmed"
    assert row[3] == f"$3 Intermediate Pickleball - {main.UNVERIFIED_INFO}"
    assert client.replies == []
    assert len(client.notifications) == 1


def test_refresh_inline_registration_not_open_goes_back_to_speculative(refresh_env, monkeypatch):
    _seed_speculative(refresh_env.db_path, registration_time=datetime.now() - timedelta(hours=1))
    refresh_env.site.outcome = lambda *_a: (datetime.now(), "info")

    def not_open(event_info, headless=True, results=None, results_lock=None):
        results.append({"user_tag": event_info["user_tag"], "event": "x", "success": False,
                        "not_open": True, "unverified": False, "error": "not open", "traceback": "tb"})

    monkeypatch.setattr(main, "register_for_single_event", not_open)

    main.refresh_schedule_and_confirm_speculative(headless=True)

    db = _open(refresh_env.db_path)
    rows = db.get_speculative_events()
    db.close()
    assert len(rows) == 1 and rows[0]["last_checked"] is None  # due again next run
    assert any("registration wasn't open yet" in r.body for r in refresh_env.client.replies)
    assert refresh_env.client.notifications == []


def test_scheduler_cleanup_prunes_old_email_failure_counts(monkeypatch, db_path):
    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    db = _open(db_path)
    db.bump_email_failures("old", when=datetime.now() - timedelta(days=main.EMAIL_FAILURE_RETENTION_DAYS + 1))
    db.bump_email_failures("recent")
    db.close()
    _wire_registration(monkeypatch, db_path, {"success": True})

    main.register_for_next_event(headless=True)

    db = _open(db_path)
    assert db.get_email_failures("old") == 0
    assert db.get_email_failures("recent") == 1
    db.close()


# --- round 3: not-open cap, rescheduling ---------------------------------------

_NOT_OPEN = {"success": False, "speculative_not_found": True, "not_open": True, "error": "not open"}


def test_repeated_not_open_is_capped_with_a_single_not_open_followup(monkeypatch, db_path):
    _seed_speculative(db_path, registration_time=datetime.now() + timedelta(minutes=5))
    client = _wire_registration(monkeypatch, db_path, _NOT_OPEN)

    for _ in range(main.NOT_OPEN_LIMIT):
        main.register_for_next_event(headless=True)

    bodies = [r.body for r in client.replies]
    assert sum("wasn't open yet" in b for b in bodies) == 1
    assert "didn't go through" in bodies[-1]
    db = _open(db_path)
    assert db.list_all_events("default")[0][4] == "failed"
    db.close()
    assert len(client.notifications) == 1


def _refresh_not_open(refresh_env, monkeypatch, prior_not_open):
    _seed_speculative(refresh_env.db_path, registration_time=datetime.now() - timedelta(hours=1))
    db = _open(refresh_env.db_path)
    for _ in range(prior_not_open):
        db.bump_not_open(_date_text(REQUESTED_DAY), SLOT_TEXT, "default")
    db.close()
    refresh_env.site.outcome = lambda *_a: (datetime.now(), "info")

    def not_open(event_info, headless=True, results=None, results_lock=None):
        results.append({"user_tag": event_info["user_tag"], "event": "x", "success": False,
                        "not_open": True, "unverified": False, "error": "not open", "traceback": "tb"})

    monkeypatch.setattr(main, "register_for_single_event", not_open)
    main.refresh_schedule_and_confirm_speculative(headless=True)


def test_refresh_second_not_open_sends_no_second_followup(refresh_env, monkeypatch):
    _refresh_not_open(refresh_env, monkeypatch, prior_not_open=1)

    db = _open(refresh_env.db_path)
    assert len(db.get_speculative_events()) == 1
    db.close()
    assert refresh_env.client.replies == []


def test_refresh_not_open_at_the_limit_fails_the_row(refresh_env, monkeypatch):
    _refresh_not_open(refresh_env, monkeypatch, prior_not_open=main.NOT_OPEN_LIMIT - 1)

    db = _open(refresh_env.db_path)
    assert db.list_all_events("default")[0][4] == "failed"
    db.close()
    assert any("several times" in r.body for r in refresh_env.client.replies)
    assert len(refresh_env.client.notifications) == 1


def test_scheduler_reschedules_a_confirmed_row_that_is_not_open_yet(monkeypatch, db_path):
    db = _open(db_path)
    db.insert_event(_date_text(REQUESTED_DAY), SLOT_TEXT, datetime.now() + timedelta(minutes=5), "default")
    db.close()
    later = (datetime.now() + timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    client = _wire_registration(
        monkeypatch, db_path,
        {"success": False, "not_open": True, "reopens_at": later, "error": "not open", "traceback": "tb"},
    )

    main.register_for_next_event(headless=True)

    db = _open(db_path)
    row = db.list_all_events("default")[0]
    db.close()
    assert row[4] == "confirmed"
    assert row[2] == later.strftime("%Y-%m-%d %H:%M:%S")
    assert client.notifications == []


def test_confirmed_row_not_open_without_countdown_is_still_a_failure(monkeypatch, db_path):
    db = _open(db_path)
    when = datetime.now() + timedelta(minutes=5)
    db.insert_event(_date_text(REQUESTED_DAY), SLOT_TEXT, when, "default")
    db.close()
    client = _wire_registration(
        monkeypatch, db_path,
        {"success": False, "not_open": True, "reopens_at": None, "error": "not open", "traceback": "tb"},
    )

    main.register_for_next_event(headless=True)

    assert len(client.notifications) == 1


def test_not_open_result_carries_the_opening_on_the_users_clock(monkeypatch):
    class NotOpenSite(_RaisingRegisterSite):
        def login(self, user_tag=None):
            self.default_registration_time = "20:00:00"

    _RaisingRegisterSite.error = main.RegistrationNotOpen("not open", opens_in=timedelta(days=2, hours=1))
    monkeypatch.setattr(main, "Website", NotOpenSite)
    monkeypatch.setattr(main, "dwell_until", lambda *_a, **_k: None)
    results = []

    main.register_for_single_event(dict(_speculative_info(), status="confirmed"), results=results)

    expected_day = (datetime.now() + timedelta(days=2, hours=1)).date()
    assert results[0]["reopens_at"] == datetime.combine(expected_day, datetime.min.time()).replace(hour=20)

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import main
from events import Events


def _make_email(message_id="add-1"):
    return SimpleNamespace(
        To=["robin.pickleball.scheduler@gmail.com"],
        From=["sender@example.com"],
        Cc=[],
        subject="",
        body="Wed, Sep 2nd, 1p - 2:30p",
        id=message_id,
        thread_id=f"thread-{message_id}",
        message_id=f"<{message_id}@example.com>",
    )


class FakeEmailClient:
    def __init__(self, emails):
        self._emails = emails
        self.user = "robin.pickleball.scheduler@gmail.com"
        self.replies = []
        self.notifications = []
        self.archived_ids = []

    def authenticate_email(self):
        return None

    def read_new_emails(self):
        return self._emails

    def is_sender_authorized(self, _sender):
        return True

    def mark_email_as_read(self, email):
        pass

    def archive_email(self, email):
        self.archived_ids.append(email.id)

    def reply_to_email(self, email, reply_plaintext, reply_html=None, subject=None, user_tag=None):
        self.replies.append(SimpleNamespace(body=reply_plaintext, subject=subject))

    def send_notification(self, subject, body, user_tag=None):
        self.notifications.append((subject, body))

    @staticmethod
    def extract_email_address(addresses):
        return addresses if isinstance(addresses, list) else [addresses]


class FakeWebsite:
    registration_time = None

    def __init__(self, headless=True):
        pass

    def login(self, user_tag=None):
        pass

    def determine_access_date(self, event_date, time_range):
        return FakeWebsite.registration_time, "$3 Intermediate Pickleball"

    def close(self):
        pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    db_path = str(tmp_path / "events.db")
    client = FakeEmailClient([_make_email()])
    monkeypatch.setattr(main, "EmailClient", lambda: client)
    monkeypatch.setattr(main, "Events", lambda: Events(db_name=db_path))
    monkeypatch.setattr(main, "Website", FakeWebsite)
    monkeypatch.setattr(main, "extract_user_tag", lambda *_a, **_k: "default")
    monkeypatch.setattr(main, "validate_user_tag", lambda tag: tag)
    monkeypatch.setattr(main, "is_sender_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(
        main, "extract_user_intent", lambda _e: ("add", ("Wed, Sep 2nd", "1p - 2:30p"))
    )

    state = SimpleNamespace(
        client=client, db_path=db_path, registrations=[], success=True, archived_at_register=[]
    )

    def fake_register(event_info, headless=True, results=None, results_lock=None):
        state.registrations.append(event_info)
        state.archived_at_register.append(list(client.archived_ids))
        result = {"user_tag": event_info["user_tag"], "event": "x", "success": state.success}
        if not state.success:
            result.update(error="signup timed out", traceback="tb")
        results.append(result)

    monkeypatch.setattr(main, "register_for_single_event", fake_register)
    return state


def _rows(db_path):
    db = Events(db_name=db_path)
    rows = db.list_all_events("default")
    db.close()
    return rows


def test_already_open_event_is_registered_immediately(env):
    # determine_access_date returns "now" for an event that's already open.
    FakeWebsite.registration_time = datetime.now()

    main.check_for_new_event(headless=True)

    assert len(env.registrations) == 1
    info = env.registrations[0]
    assert (info["event_date"], info["time_range"], info["user_tag"]) == ("Wed, Sep 2nd", "1p - 2:30p", "default")
    assert info["registration_time"].microsecond == 0
    assert "registered you right away" in env.client.replies[0].body
    assert "$3 Intermediate Pickleball" in env.client.replies[0].body
    assert env.client.notifications == []
    assert env.client.archived_ids == ["add-1"]
    # Recorded for the report, with a timestamp the scheduler can parse.
    rows = _rows(env.db_path)
    assert len(rows) == 1
    datetime.strptime(rows[0][2], "%Y-%m-%d %H:%M:%S")


def test_already_open_registration_failure_tells_user_and_webmaster(env):
    FakeWebsite.registration_time = datetime.now()
    env.success = False

    main.check_for_new_event(headless=True)

    assert "didn't go through" in env.client.replies[0].body
    assert len(env.client.notifications) == 1
    assert env.client.notifications[0][0] == "Event registration failed"
    assert _rows(env.db_path) == []
    assert env.client.archived_ids == ["add-1"]


def test_future_registration_time_is_still_scheduled_not_registered(env):
    FakeWebsite.registration_time = (datetime.now() + timedelta(days=2)).replace(microsecond=0)

    main.check_for_new_event(headless=True)

    assert env.registrations == []
    assert "I determined I need to register at" in env.client.replies[0].body
    assert len(_rows(env.db_path)) == 1


def test_already_open_row_is_not_picked_up_by_scheduler_again(env):
    FakeWebsite.registration_time = datetime.now()

    main.check_for_new_event(headless=True)

    db = Events(db_name=env.db_path)
    assert db.get_next_event_after() == []
    db.close()


def test_open_event_is_queued_for_caller_and_email_handled_first(env):
    FakeWebsite.registration_time = datetime.now()
    pending = []

    main.check_for_new_event(headless=True, pending_open=pending)

    # Nothing registered during email processing, but the email is done with.
    assert env.registrations == []
    assert env.client.replies == []
    assert env.client.archived_ids == ["add-1"]
    assert len(pending) == 1

    main.register_open_events(pending, headless=True)

    assert len(env.registrations) == 1
    assert "registered you right away" in env.client.replies[0].body
    assert len(_rows(env.db_path)) == 1


def test_email_is_archived_before_registration_starts(env):
    FakeWebsite.registration_time = datetime.now()

    main.check_for_new_event(headless=True)

    assert env.archived_at_register == [["add-1"]]


def test_time_seconds_away_is_treated_as_open_and_waited_for(env):
    opens = (datetime.now() + timedelta(seconds=30)).replace(microsecond=0)
    FakeWebsite.registration_time = opens

    main.check_for_new_event(headless=True)

    assert len(env.registrations) == 1
    # Waits for the real opening rather than clicking now.
    assert env.registrations[0]["registration_time"] == opens


def test_time_beyond_margin_is_scheduled_normally(env):
    FakeWebsite.registration_time = (datetime.now() + timedelta(minutes=2)).replace(microsecond=0)

    main.check_for_new_event(headless=True)

    assert env.registrations == []
    assert "I determined I need to register at" in env.client.replies[0].body


def test_reply_failure_after_registration_does_not_crash(env):
    FakeWebsite.registration_time = datetime.now()

    def broken_reply(*_a, **_k):
        raise RuntimeError("gmail down")

    env.client.reply_to_email = broken_reply

    main.check_for_new_event(headless=True)

    assert len(env.registrations) == 1
    assert len(_rows(env.db_path)) == 1


def test_register_open_events_with_nothing_pending_does_nothing(env):
    main.register_open_events([], headless=True)
    assert env.registrations == []


def test_run_registers_open_events_after_the_scheduler(monkeypatch):
    calls = []

    def fake_check(headless=True, pending_open=None):
        calls.append("check")
        pending_open.append({"queued": True})

    monkeypatch.setattr(main, "check_for_new_event", fake_check)
    monkeypatch.setattr(main, "register_for_next_event", lambda headless=True: calls.append("scheduler"))
    monkeypatch.setattr(
        main, "register_open_events", lambda pending, headless=True: calls.append(("open", len(pending)))
    )

    main.run(headless=True)

    assert calls == ["check", "scheduler", ("open", 1)]


def test_run_still_registers_open_events_when_earlier_steps_fail(monkeypatch):
    calls = []

    def failing_check(headless=True, pending_open=None):
        pending_open.append({"queued": True})
        raise RuntimeError("a later email blew up")

    def failing_scheduler(headless=True):
        raise RuntimeError("scheduler blew up")

    monkeypatch.setattr(main, "check_for_new_event", failing_check)
    monkeypatch.setattr(main, "register_for_next_event", failing_scheduler)
    monkeypatch.setattr(
        main, "register_open_events", lambda pending, headless=True: calls.append(len(pending))
    )

    main.run(headless=True)

    assert calls == [1]

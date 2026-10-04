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

    state = SimpleNamespace(client=client, db_path=db_path, registrations=[], success=True)

    def fake_register(event_info, headless=True, results=None, results_lock=None):
        state.registrations.append(event_info)
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

from types import SimpleNamespace

import main


def _make_email(message_id, subject="Report", body="Body"):
    return SimpleNamespace(
        To=["robin.pickleball.scheduler@gmail.com"],
        From=["sender@example.com"],
        Cc=[],
        subject=subject,
        body=body,
        id=message_id,
        thread_id=f"thread-{message_id}",
        message_id=f"<{message_id}@example.com>",
    )


class FakeEvents:
    def __init__(self):
        self.closed = False

    def list_all_events(self, user_tag):
        return []

    def close(self):
        self.closed = True


class FakeEmailClient:
    def __init__(self, emails, sender_authorized=True):
        self._emails = emails
        self._sender_authorized = sender_authorized
        self.user = "robin.pickleball.scheduler@gmail.com"

        self.replied_ids = []
        self.marked_read_ids = []
        self.archived_ids = []
        self.deleted_ids = []

    def authenticate_email(self):
        return None

    def read_new_emails(self):
        return self._emails

    def is_sender_authorized(self, _sender):
        return self._sender_authorized

    def mark_email_as_read(self, email):
        self.marked_read_ids.append(email.id)

    def archive_email(self, email):
        self.archived_ids.append(email.id)

    def delete_email(self, email):
        self.deleted_ids.append(email.id)

    def reply_to_email(self, email, reply_plaintext, reply_html=None, subject=None, user_tag=None):
        self.replied_ids.append(email.id)

    @staticmethod
    def extract_email_address(addresses):
        if isinstance(addresses, list):
            return addresses
        return [addresses]


def test_deferred_reports_are_marked_and_archived_per_report_email(monkeypatch):
    emails = [_make_email("report-1"), _make_email("report-2")]
    fake_client = FakeEmailClient(emails=emails, sender_authorized=True)

    monkeypatch.setattr(main, "EmailClient", lambda: fake_client)
    monkeypatch.setattr(main, "Events", FakeEvents)
    monkeypatch.setattr(main, "extract_user_tag", lambda *_args, **_kwargs: "default")
    monkeypatch.setattr(main, "validate_user_tag", lambda user_tag: user_tag)
    monkeypatch.setattr(main, "is_sender_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(main, "extract_user_intent", lambda _email: ("report", None))

    main.check_for_new_event(headless=True)

    assert fake_client.replied_ids == ["report-1", "report-2"]
    assert fake_client.marked_read_ids == ["report-1", "report-2"]
    assert fake_client.archived_ids == ["report-1", "report-2"]


def test_unauthorized_sender_is_archived_not_deleted(monkeypatch):
    emails = [_make_email("unauth-1", subject="Hello")]
    fake_client = FakeEmailClient(emails=emails, sender_authorized=False)

    monkeypatch.setattr(main, "EmailClient", lambda: fake_client)
    monkeypatch.setattr(main, "Events", FakeEvents)

    main.check_for_new_event(headless=True)

    assert fake_client.marked_read_ids == ["unauth-1"]
    assert fake_client.archived_ids == ["unauth-1"]
    assert fake_client.deleted_ids == []


def test_user_tag_extraction_failure_is_archived_not_deleted(monkeypatch):
    emails = [_make_email("bad-tag-1")]
    fake_client = FakeEmailClient(emails=emails, sender_authorized=True)

    monkeypatch.setattr(main, "EmailClient", lambda: fake_client)
    monkeypatch.setattr(main, "Events", FakeEvents)
    monkeypatch.setattr(main, "extract_user_tag", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad tag")))

    main.check_for_new_event(headless=True)

    assert fake_client.marked_read_ids == ["bad-tag-1"]
    assert fake_client.archived_ids == ["bad-tag-1"]
    assert fake_client.deleted_ids == []


def test_validate_user_tag_failure_is_archived_not_deleted(monkeypatch):
    emails = [_make_email("invalid-user-1")]
    fake_client = FakeEmailClient(emails=emails, sender_authorized=True)

    monkeypatch.setattr(main, "EmailClient", lambda: fake_client)
    monkeypatch.setattr(main, "Events", FakeEvents)
    monkeypatch.setattr(main, "extract_user_tag", lambda *_args, **_kwargs: "missing-user")
    monkeypatch.setattr(
        main,
        "validate_user_tag",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )

    main.check_for_new_event(headless=True)

    assert fake_client.marked_read_ids == ["invalid-user-1"]
    assert fake_client.archived_ids == ["invalid-user-1"]
    assert fake_client.deleted_ids == []

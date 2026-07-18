import base64
import os
from types import SimpleNamespace

import pytest

import main
import email_client


def test_format_failure_body_includes_fields_and_timestamp():
    ctx = {
        "user_tag": "default",
        "event": "2026-07-20 10:00-11:00",
        "error": "Something went wrong",
        "traceback": "Traceback line1\nline2",
    }

    body = main._format_failure_body(ctx, headless_flag=False)

    assert "Timestamp:" in body
    assert "Environment:" in body
    assert "user_tag: default" in body
    assert "event: 2026-07-20 10:00-11:00" in body
    assert "error: Something went wrong" in body
    assert "traceback:" in body
    assert "line1" in body


def test_tail_logs_returns_tail(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    f1 = logs_dir / "2026-01-01.txt"
    f1.write_text("line1\nline2\n")

    f2 = logs_dir / "2026-07-18.txt"
    content = "\n".join([f"log {i}" for i in range(1, 101)])
    f2.write_text(content)
    # ensure f2 has the newest modification time
    os.utime(f2, None)

    client = email_client.EmailClient.__new__(email_client.EmailClient)
    tail = email_client.EmailClient._tail_logs(client, n_lines=5, logs_dir=str(logs_dir))
    assert "log 100" in tail
    assert tail.count("log") >= 5


def test_send_notification_for_failure_sends_to_webmaster_and_self(monkeypatch, tmp_path):
    recorder = []

    class FakeMessages:
        def __init__(self, recorder):
            self.recorder = recorder

        def send(self, userId, body):
            self.recorder.append(body.get("raw"))

            class Exec:
                def execute(self):
                    return {"id": "ok"}

            return Exec()

    class FakeUsers:
        def __init__(self, recorder):
            self.recorder = recorder

        def messages(self):
            return FakeMessages(self.recorder)

    class FakeService:
        def __init__(self, recorder):
            self.recorder = recorder

        def users(self):
            return FakeUsers(self.recorder)

    monkeypatch.setattr(email_client, "build", lambda *a, **k: FakeService(recorder))

    # construct client without running __init__ (avoid real auth)
    client = email_client.EmailClient.__new__(email_client.EmailClient)
    client.creds = True
    client.user = "me@example.com"
    client.webmaster = "wm@example.com"
    # make tail logs return something predictable
    client._tail_logs = lambda *a, **k: "recent-log-line"

    # send a failure notification
    client.send_notification(subject="Event registration failed", body="Something bad happened", user_tag="default")

    # two messages should have been recorded (self + webmaster)
    assert len(recorder) == 2

    decoded = [base64.urlsafe_b64decode(r).decode("utf-8", errors="replace") for r in recorder]
    # first should go to self
    assert any("To: me@example.com" in d or "To: me@example.com" in d for d in decoded)
    # second should go to webmaster
    assert any("To: wm@example.com" in d or "To: wm@example.com" in d for d in decoded)


def test_send_notification_for_success_only_sends_to_self(monkeypatch):
    recorder = []

    class FakeMessages:
        def __init__(self, recorder):
            self.recorder = recorder

        def send(self, userId, body):
            self.recorder.append(body.get("raw"))

            class Exec:
                def execute(self):
                    return {"id": "ok"}

            return Exec()

    class FakeUsers:
        def __init__(self, recorder):
            self.recorder = recorder

        def messages(self):
            return FakeMessages(self.recorder)

    class FakeService:
        def __init__(self, recorder):
            self.recorder = recorder

        def users(self):
            return FakeUsers(self.recorder)

    monkeypatch.setattr(email_client, "build", lambda *a, **k: FakeService(recorder))

    client = email_client.EmailClient.__new__(email_client.EmailClient)
    client.creds = True
    client.user = "me@example.com"
    client.webmaster = "wm@example.com"
    client._tail_logs = lambda *a, **k: "recent-log-line"

    client.send_notification(subject="All good", body="Everything succeeded", user_tag="default")

    # only one message should be recorded (self)
    assert len(recorder) == 1

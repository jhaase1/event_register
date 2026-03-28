import os
from types import SimpleNamespace

import email_client


class DummyCreds:
    valid = True
    expired = False
    refresh_token = None

    @staticmethod
    def to_json():
        return '{"access_token": "new-token"}'


class DummyFlow:
    def run_local_server(self, port=0):
        return DummyCreds()


def test_authenticate_email_recovers_from_corrupt_token(monkeypatch, tmp_path):
    token_file = tmp_path / "email_token.json"
    token_file.write_text("", encoding="utf-8")

    removed = {"called": False}

    def fake_remove(path):
        removed["called"] = True
        os.unlink(path)

    monkeypatch.setattr(email_client.os, "remove", fake_remove)
    monkeypatch.setattr(
        email_client.Credentials,
        "from_authorized_user_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("corrupt token")),
    )
    monkeypatch.setattr(
        email_client.InstalledAppFlow,
        "from_client_secrets_file",
        lambda *_args, **_kwargs: DummyFlow(),
    )

    client = email_client.EmailClient.__new__(email_client.EmailClient)
    client.creds = None

    client.authenticate_email(token_file=str(token_file))

    assert removed["called"] is True
    assert token_file.exists()
    assert "new-token" in token_file.read_text(encoding="utf-8")

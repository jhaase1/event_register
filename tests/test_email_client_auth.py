import os
from types import SimpleNamespace

import pytest

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
    monkeypatch.setattr(email_client.sys.stdin, "isatty", lambda: True)

    client = email_client.EmailClient.__new__(email_client.EmailClient)
    client.creds = None

    client.authenticate_email(token_file=str(token_file))

    assert removed["called"] is True
    assert token_file.exists()
    assert "new-token" in token_file.read_text(encoding="utf-8")


def test_authenticate_email_fails_fast_when_noninteractive(monkeypatch, tmp_path):
    token_file = tmp_path / "email_token.json"

    monkeypatch.setattr(
        email_client.InstalledAppFlow,
        "from_client_secrets_file",
        lambda *_args, **_kwargs: DummyFlow(),
    )
    monkeypatch.setattr(email_client.sys.stdin, "isatty", lambda: False)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("run_local_server must not be called when non-interactive")

    monkeypatch.setattr(DummyFlow, "run_local_server", fail_if_called)

    client = email_client.EmailClient.__new__(email_client.EmailClient)
    client.creds = None

    with pytest.raises(RuntimeError, match="non-interactive"):
        client.authenticate_email(token_file=str(token_file))

    assert not token_file.exists()

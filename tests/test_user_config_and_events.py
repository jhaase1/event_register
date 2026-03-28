import os
import sqlite3

import pytest

from events import Events
from user_config import (
    extract_user_tag,
    get_website_token_file,
    is_sender_allowed,
    load_user_config,
    validate_user_tag,
)


def _simulate_email_auth_flow(to_addresses, sender_email, system_email):
    """Mimics the authorization sequence used by main email processing."""
    try:
        user_tag = extract_user_tag(to_addresses, system_email=system_email)
    except ValueError:
        return False

    try:
        user_tag = validate_user_tag(user_tag)
    except (ValueError, FileNotFoundError):
        return False

    return is_sender_allowed(sender_email, user_tag)


def test_get_website_token_file_valid_tags():
    assert get_website_token_file("default") == os.path.join("user_tokens", "default.json")
    assert get_website_token_file("alice") == os.path.join("user_tokens", "alice.json")
    assert get_website_token_file("Bob") == os.path.join("user_tokens", "bob.json")


@pytest.mark.parametrize(
    "tag",
    ["../etc/passwd", "..\\..\\secrets", "user/../admin", "user;rm -rf"],
)
def test_get_website_token_file_rejects_path_traversal(tag):
    with pytest.raises(ValueError):
        get_website_token_file(tag)


def test_load_user_config_default_exists():
    config = load_user_config("default")
    assert config is not None
    assert "email" in config


def test_load_user_config_nonexistent_returns_none():
    assert load_user_config("nonexistent_user_12345") is None


@pytest.mark.parametrize(
    "email, expected",
    [
        ("system@gmail.com", "default"),
        ("system+alice@gmail.com", "alice"),
        ("system+BOB@gmail.com", "bob"),
        ("system+user-1@gmail.com", "user-1"),
        ("system+user_2@gmail.com", "user_2"),
    ],
)
def test_extract_user_tag_single_address(email, expected):
    assert extract_user_tag(email) == expected


def test_extract_user_tag_list_requires_system_email():
    with pytest.raises(ValueError):
        extract_user_tag(["other@example.com", "myapp+alice@gmail.com"], system_email=None)


def test_extract_user_tag_list_filters_by_system_email():
    result = extract_user_tag(
        ["other@example.com", "myapp+alice@gmail.com", "someone@yahoo.com"],
        system_email="myapp@gmail.com",
    )
    assert result == "alice"


def test_validate_user_tag_default():
    assert validate_user_tag("default") == "default"


def test_validate_user_tag_case_normalization():
    assert validate_user_tag("DEFAULT") == "default"


def test_validate_user_tag_invalid_format_raises():
    with pytest.raises(ValueError):
        validate_user_tag("../invalid")


def test_validate_user_tag_nonexistent_raises():
    with pytest.raises(FileNotFoundError):
        validate_user_tag("nonexistent_user_xyz")


def test_is_sender_allowed_authorized_sender_from_list(monkeypatch):
    monkeypatch.setattr(
        "user_config.load_user_config",
        lambda _: {
            "email": "owner@example.com",
            "authorized_senders": ["friend@example.com"],
        },
    )
    assert is_sender_allowed("friend@example.com", "default") is True


def test_is_sender_allowed_authorized_sender_by_owner_email(monkeypatch):
    monkeypatch.setattr(
        "user_config.load_user_config",
        lambda _: {
            "email": "owner@example.com",
            "authorized_senders": [],
        },
    )
    assert is_sender_allowed("owner@example.com", "default") is True


def test_is_sender_allowed_case_insensitive(monkeypatch):
    monkeypatch.setattr(
        "user_config.load_user_config",
        lambda _: {
            "email": "owner@example.com",
            "authorized_senders": ["JohnRHaase@gmail.com"],
        },
    )
    assert is_sender_allowed("JOHNRHAASE@GMAIL.COM", "default") is True


def test_is_sender_allowed_unauthorized_sender(monkeypatch):
    monkeypatch.setattr(
        "user_config.load_user_config",
        lambda _: {
            "email": "owner@example.com",
            "authorized_senders": ["friend@example.com"],
        },
    )
    assert is_sender_allowed("attacker@evil.com", "default") is False


def test_is_sender_allowed_missing_user_config_fails_closed(monkeypatch):
    monkeypatch.setattr("user_config.load_user_config", lambda _: None)
    assert is_sender_allowed("anyone@example.com", "nonexistent_user") is False


def test_email_auth_flow_authorized_default_user():
    config = load_user_config("default")
    assert config is not None

    system_email = config["email"]
    assert _simulate_email_auth_flow(
        to_addresses=[system_email],
        sender_email=system_email,
        system_email=system_email,
    ) is True


def test_email_auth_flow_rejects_unauthorized_sender():
    config = load_user_config("default")
    assert config is not None

    system_email = config["email"]
    assert _simulate_email_auth_flow(
        to_addresses=[system_email],
        sender_email="attacker@evil.com",
        system_email=system_email,
    ) is False


def test_email_auth_flow_rejects_nonexistent_plus_tag_user():
    config = load_user_config("default")
    assert config is not None

    system_email = config["email"]
    local, domain = system_email.split("@", 1)
    plus_address = f"{local}+fakeuser@{domain}"

    assert _simulate_email_auth_flow(
        to_addresses=[plus_address],
        sender_email=system_email,
        system_email=system_email,
    ) is False


def test_events_create_table_migrates_old_schema(tmp_path):
    db_path = tmp_path / "test_migration.db"

    # Build legacy schema (without user_tag).
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE events (
            event_spec TEXT PRIMARY KEY,
            event_date TEXT NOT NULL,
            time_range TEXT NOT NULL,
            registration_time TIMESTAMP NOT NULL,
            additional_info TEXT
        )
        """
    )

    test_events = [
        (
            "MON, JAN 1 9:00am-10:00am",
            "MON, JAN 1",
            "9:00am-10:00am",
            "2026-01-01 08:00:00",
            "Event 1",
        ),
        (
            "TUE, JAN 2 2:00pm-3:00pm",
            "TUE, JAN 2",
            "2:00pm-3:00pm",
            "2026-01-02 13:00:00",
            "Event 2",
        ),
        (
            "WED, JAN 3 4:00pm-5:00pm",
            "WED, JAN 3",
            "4:00pm-5:00pm",
            "2026-01-03 15:00:00",
            "",
        ),
    ]

    cursor.executemany(
        """
        INSERT INTO events (event_spec, event_date, time_range, registration_time, additional_info)
        VALUES (?, ?, ?, ?, ?)
        """,
        test_events,
    )
    conn.commit()
    conn.close()

    events_db = Events(db_name=str(db_path))

    # Schema includes user_tag post migration.
    events_db.cursor.execute("PRAGMA table_info(events)")
    columns = [col[1] for col in events_db.cursor.fetchall()]
    assert "user_tag" in columns
    assert "event_spec" in columns
    assert "event_date" in columns
    assert "time_range" in columns
    assert "registration_time" in columns
    assert "additional_info" in columns

    # Existing records were migrated with default user_tag.
    events_db.cursor.execute("SELECT event_spec, user_tag FROM events")
    rows = events_db.cursor.fetchall()
    assert len(rows) == 3
    assert all(user_tag == "default" for _, user_tag in rows)

    # Composite PK is in place.
    events_db.cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
    )
    create_sql = events_db.cursor.fetchone()[0]
    assert "PRIMARY KEY (event_spec, user_tag)" in create_sql

    # Same event can be registered by different users.
    events_db.insert_event(
        event_date="MON, JAN 1",
        time_range="9:00am-10:00am",
        registration_time="2026-01-01 08:00:00",
        user_tag="alice",
        additional_info="Alice registration",
    )

    default_events = events_db.list_all_events(user_tag="default")
    alice_events = events_db.list_all_events(user_tag="alice")
    assert len(default_events) == 3
    assert len(alice_events) == 1

    # Legacy table cleanup completed.
    events_db.cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='events_old'"
    )
    assert events_db.cursor.fetchone() is None

    events_db.close()

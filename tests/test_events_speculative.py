import sqlite3
from datetime import date, datetime, timedelta

import pytest

from events import Events


@pytest.fixture
def db(tmp_path):
    events = Events(db_name=str(tmp_path / "events.db"))
    yield events
    events.close()


REQUEST_REF = {
    "from": ["alice@example.com"],
    "thread_id": "thread-1",
    "message_id": "<m1@example.com>",
    "subject": "Tue, Oct 13 9a - 11a",
}


def _insert_speculative(db, user_tag="default", registration_time=datetime(2026, 10, 6, 14, 0, 0)):
    db.insert_event(
        event_date="Tue, Oct 13",
        time_range="9a - 11a",
        registration_time=registration_time,
        user_tag=user_tag,
        additional_info="Speculative",
        status="speculative",
        expected_event_day=date(2026, 10, 13),
        request_ref=REQUEST_REF,
    )


def test_old_schema_gains_new_columns_without_losing_rows(tmp_path):
    db_path = tmp_path / "events.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE events (
            event_spec TEXT NOT NULL,
            user_tag TEXT NOT NULL,
            event_date TEXT NOT NULL,
            time_range TEXT NOT NULL,
            registration_time TIMESTAMP NOT NULL,
            additional_info TEXT,
            PRIMARY KEY (event_spec, user_tag)
        )
        """
    )
    conn.execute(
        "INSERT INTO events VALUES ('MON, JAN 1 9a - 10a', 'default', 'MON, JAN 1', '9a - 10a', '2026-01-01 08:00:00', '')"
    )
    conn.commit()
    conn.close()

    events = Events(db_name=str(db_path))
    events.cursor.execute("PRAGMA table_info(events)")
    columns = [col[1] for col in events.cursor.fetchall()]
    for column in ("status", "last_checked", "expected_event_day", "request_ref"):
        assert column in columns

    rows = events.list_all_events(user_tag="default")
    assert len(rows) == 1
    assert rows[0][4] == "confirmed"
    events.close()


def test_insert_defaults_to_confirmed(db):
    db.insert_event("Tue, Oct 13", "9a - 11a", datetime(2026, 10, 6, 14, 0, 0), "default")
    assert db.get_speculative_events() == []
    assert db.list_all_events("default")[0][4] == "confirmed"


def test_speculative_round_trip(db):
    _insert_speculative(db)

    rows = db.get_speculative_events()
    assert len(rows) == 1
    row = rows[0]
    assert row["event_date"] == "Tue, Oct 13"
    assert row["time_range"] == "9a - 11a"
    assert row["registration_time"] == datetime(2026, 10, 6, 14, 0, 0)
    assert row["user_tag"] == "default"
    assert row["expected_event_day"] == date(2026, 10, 13)
    assert row["last_checked"] is None
    assert row["request_ref"] == REQUEST_REF


def test_get_speculative_events_filters_by_user(db):
    _insert_speculative(db, user_tag="default")
    _insert_speculative(db, user_tag="alice")
    assert [r["user_tag"] for r in db.get_speculative_events(user_tag="alice")] == ["alice"]
    assert len(db.get_speculative_events()) == 2


def test_get_next_event_after_reports_status_and_request_ref(db):
    _insert_speculative(db)
    events = db.get_next_event_after(datetime(2026, 10, 4))
    assert events[0]["status"] == "speculative"
    assert events[0]["request_ref"] == REQUEST_REF


def test_confirm_event_updates_time_info_and_status(db):
    _insert_speculative(db)
    db.confirm_event("Tue, Oct 13", "9a - 11a", "default", datetime(2026, 10, 6, 20, 0, 0), "Real summary")

    assert db.get_speculative_events() == []
    row = db.list_all_events("default")[0]
    assert row[2] == "2026-10-06 20:00:00"
    assert row[3] == "Real summary"
    assert row[4] == "confirmed"


def test_mark_checked_records_timestamp(db):
    _insert_speculative(db)
    when = datetime(2026, 10, 5, 9, 30, 0)
    db.mark_checked("Tue, Oct 13", "9a - 11a", "default", when=when)
    assert db.get_speculative_events()[0]["last_checked"] == when


def test_registration_time_with_microseconds_is_stored_without_them(db):
    db.insert_event("Tue, Oct 13", "9a - 11a", datetime(2026, 10, 6, 14, 0, 0, 123456), "default")
    assert db.get_next_event_after(datetime(2026, 10, 4))[0]["registration_time"] == datetime(2026, 10, 6, 14, 0, 0)


def test_list_all_events_keeps_user_tag_last(db):
    _insert_speculative(db)
    row = db.list_all_events("default")[0]
    assert row[-1] == "default"
    assert row[4] == "speculative"


# --- observed events --------------------------------------------------------


def _obs(day, lead_days=None, lead_source=None, name="Drop-in", start="09:00", end="11:00"):
    return {
        "event_day": day,
        "start_hm": start,
        "end_hm": end,
        "name": name,
        "category": "Open Play",
        "lead_days": lead_days,
        "lead_source": lead_source,
    }


def test_upsert_observations_round_trip(db):
    seen = datetime(2026, 10, 4, 12, 0, 0)
    db.upsert_observations("default", [_obs(date(2026, 10, 13), 7, "countdown")], seen_at=seen)

    rows = db.get_observations("default", since=date(2026, 10, 1))
    assert rows == [_obs(date(2026, 10, 13), 7, "countdown")]


def test_upsert_keeps_first_seen_and_first_sighting_lead(db):
    day = date(2026, 10, 13)
    db.upsert_observations("default", [_obs(day, 9, "first_seen")], seen_at=datetime(2026, 10, 4, 12))
    # A later sighting of an already-open card must not shrink the lead.
    db.upsert_observations("default", [_obs(day, 8, "first_seen")], seen_at=datetime(2026, 10, 5, 12))

    db.cursor.execute("SELECT first_seen, last_seen, lead_days FROM observed_events")
    first_seen, last_seen, lead_days = db.cursor.fetchone()
    assert first_seen.startswith("2026-10-04")
    assert last_seen.startswith("2026-10-05")
    assert lead_days == 9


def test_upsert_countdown_lead_overrides_first_seen_lead(db):
    day = date(2026, 10, 13)
    db.upsert_observations("default", [_obs(day, 9, "first_seen")], seen_at=datetime(2026, 10, 4, 12))
    db.upsert_observations("default", [_obs(day, 7, "countdown")], seen_at=datetime(2026, 10, 5, 12))
    row = db.get_observations("default", since=day)[0]
    assert (row["lead_days"], row["lead_source"]) == (7, "countdown")


def test_upsert_does_not_replace_countdown_lead_with_missing_lead(db):
    day = date(2026, 10, 13)
    db.upsert_observations("default", [_obs(day, 7, "countdown")], seen_at=datetime(2026, 10, 4, 12))
    db.upsert_observations("default", [_obs(day)], seen_at=datetime(2026, 10, 5, 12))
    row = db.get_observations("default", since=day)[0]
    assert (row["lead_days"], row["lead_source"]) == (7, "countdown")


def test_observations_are_isolated_per_user_and_filtered_by_date(db):
    seen = datetime(2026, 10, 4, 12)
    db.upsert_observations("default", [_obs(date(2026, 9, 1)), _obs(date(2026, 10, 13))], seen_at=seen)
    db.upsert_observations("alice", [_obs(date(2026, 10, 14))], seen_at=seen)

    assert [r["event_day"] for r in db.get_observations("default", since=date(2026, 9, 15))] == [date(2026, 10, 13)]
    assert [r["event_day"] for r in db.get_observations("alice", since=date(2026, 9, 15))] == [date(2026, 10, 14)]


def test_remove_old_observations(db):
    today = datetime.now().date()
    seen = datetime.now()
    db.upsert_observations(
        "default",
        [_obs(today - timedelta(days=60)), _obs(today - timedelta(days=10))],
        seen_at=seen,
    )
    db.remove_old_observations(n_days=42)
    assert [r["event_day"] for r in db.get_observations("default", since=today - timedelta(days=365))] == [
        today - timedelta(days=10)
    ]


def test_snapshot_bookkeeping(db):
    assert db.get_last_snapshot("default") is None
    when = datetime(2026, 10, 4, 12, 0, 0)
    db.record_snapshot("default", when)
    assert db.get_last_snapshot("default") == when
    assert db.get_last_snapshot("alice") is None

import json
import sqlite3
from datetime import date, datetime, timedelta
from logging_config import get_logger

logger = get_logger(__name__)

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# Columns added after the user_tag migration; ALTER TABLE ADD COLUMN is enough
# for these since they're nullable or have a default.
EVENT_COLUMN_ADDITIONS = {
    "status": "TEXT NOT NULL DEFAULT 'confirmed'",
    "last_checked": "TIMESTAMP",
    "expected_event_day": "DATE",
    "request_ref": "TEXT",
}


def _to_db_timestamp(value):
    """Stores datetimes without microseconds so they parse back with TIMESTAMP_FORMAT."""
    if isinstance(value, datetime):
        return value.strftime(TIMESTAMP_FORMAT)
    return value


def _from_db_timestamp(value):
    return datetime.strptime(value, TIMESTAMP_FORMAT) if value else None


# Statuses the scheduler acts on. 'in_progress' rows are claimed by a run that
# is registering them right now; 'failed' rows had a real registration error.
SCHEDULABLE_STATUSES = ("confirmed", "speculative")


class Events:
    def __init__(self, db_name="events.db"):
        self.conn = sqlite3.connect(db_name)
        self.cursor = self.conn.cursor()
        self._create_table()

    def _create_table(self):
        """Create table if it doesn't exist, with migration support."""
        # Check if table exists and needs migration
        self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='events'")
        table_exists = self.cursor.fetchone() is not None

        if table_exists:
            self.cursor.execute("PRAGMA table_info(events)")
            columns = [column[1] for column in self.cursor.fetchall()]
            if "user_tag" not in columns:
                logger.info("Migrating database to include user_tag column...")
                try:
                    self.cursor.execute("ALTER TABLE events RENAME TO events_old")
                    self.cursor.execute(
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
                    self.cursor.execute(
                        """
                        INSERT INTO events (event_spec, user_tag, event_date, time_range, registration_time, additional_info)
                        SELECT event_spec, 'default', event_date, time_range, registration_time, additional_info FROM events_old
                    """
                    )
                    self.cursor.execute("DROP TABLE events_old")
                    self.conn.commit()
                    logger.info("Migration complete.")
                except Exception as e:
                    self.conn.rollback()
                    logger.error(f"Migration failed, rolled back: {e}", exc_info=True)
                    raise
        else:
            self.cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
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

        self._add_missing_event_columns()
        self._create_schedule_tables()

    def _add_missing_event_columns(self):
        self.cursor.execute("PRAGMA table_info(events)")
        columns = {column[1] for column in self.cursor.fetchall()}
        for name, definition in EVENT_COLUMN_ADDITIONS.items():
            if name not in columns:
                logger.info(f"Adding column '{name}' to events table.")
                self.cursor.execute(f"ALTER TABLE events ADD COLUMN {name} {definition}")
        self.conn.commit()

    def _create_schedule_tables(self):
        """Tables backing speculative pre-registration: what the site has listed
        over time, and when each user's listing was last scanned."""
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS observed_events (
                user_tag TEXT NOT NULL,
                event_day DATE NOT NULL,
                start_hm TEXT NOT NULL,
                end_hm TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                category TEXT,
                lead_days INTEGER,
                lead_source TEXT,
                first_seen TIMESTAMP NOT NULL,
                last_seen TIMESTAMP NOT NULL,
                PRIMARY KEY (user_tag, event_day, start_hm, end_hm, name)
            )
        """
        )
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                user_tag TEXT PRIMARY KEY,
                last_snapshot TIMESTAMP,
                last_attempt TIMESTAMP
            )
        """
        )
        self.conn.commit()

    def create_spec(self, event_date, time_range):
        """Creates a unique event specification."""
        return f"{event_date} {time_range}"

    def insert_event(
        self,
        event_date,
        time_range,
        registration_time,
        user_tag,
        additional_info="",
        status="confirmed",
        expected_event_day=None,
        request_ref=None,
    ):
        self.cursor.execute(
            """
            INSERT OR REPLACE INTO events (
                event_spec, user_tag, event_date, time_range, registration_time, additional_info,
                status, expected_event_day, request_ref
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                self.create_spec(event_date, time_range),
                user_tag,
                event_date,
                time_range,
                _to_db_timestamp(registration_time),
                additional_info,
                status,
                expected_event_day.isoformat() if expected_event_day else None,
                json.dumps(request_ref) if request_ref is not None else None,
            ),
        )
        self.conn.commit()
        logger.info(f"Upserted {status} event {event_date} {time_range} for user '{user_tag}'.")

    def get_events_by_date(self, registration_time, user_tag):
        self.cursor.execute(
            """
            SELECT event_date, time_range FROM events WHERE registration_time = ? AND user_tag = ?
        """,
            (_to_db_timestamp(registration_time), user_tag),
        )
        rows = self.cursor.fetchall()
        return [(row[0], row[1]) for row in rows]

    def get_next_event_after(self, timestamp=None):

        if timestamp is None:
            timestamp = datetime.now()

        """Finds all events at the next registration time after the provided timestamp."""
        # First, find the earliest registration time
        self.cursor.execute(
            """
            SELECT MIN(registration_time) FROM events
            WHERE registration_time > ? AND status IN (?, ?)
        """,
            (_to_db_timestamp(timestamp), *SCHEDULABLE_STATUSES),
        )
        row = self.cursor.fetchone()
        if not row or not row[0]:
            return []

        next_registration_time = row[0]

        # Then, get all events at that time
        self.cursor.execute(
            """
            SELECT event_date, time_range, registration_time, user_tag, status, request_ref FROM events
            WHERE registration_time = ? AND status IN (?, ?)
            ORDER BY user_tag ASC
        """,
            (next_registration_time, *SCHEDULABLE_STATUSES),
        )
        rows = self.cursor.fetchall()

        events = []
        for row in rows:
            event_date = row[0]
            time_range = row[1]
            registration_time = _from_db_timestamp(row[2])
            user_tag = row[3]
            events.append({
                "event_date": event_date,
                "time_range": time_range,
                "registration_time": registration_time,
                "user_tag": user_tag,
                "status": row[4],
                "request_ref": json.loads(row[5]) if row[5] else None,
            })

        return events

    def remove_event(self, event_date, time_range, user_tag):
        """Removes a row based on the event_spec and user_tag."""
        logger.info(f"Removing event: {event_date, time_range} for user {user_tag}")
        event_spec = self.create_spec(event_date, time_range)
        logger.debug(f"Event spec to remove: {event_spec}")

        self.cursor.execute(
            """
            DELETE FROM events WHERE event_spec = ? AND user_tag = ?
        """,
            (event_spec, user_tag),
        )
        self.conn.commit()

    def remove_old_events(self, n_days):
        """Removes events with a registration_time older than n_days days ago."""
        cutoff = datetime.now() - timedelta(days=n_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        self.cursor.execute(
            """
            DELETE FROM events WHERE registration_time < ?
            """,
            (cutoff_str,),
        )
        self.conn.commit()

    def list_all_events(self, user_tag):
        """Returns all rows for a specific user, ordered by descending registration_time.

        Args:
            user_tag: Required. The user tag to filter events by.

        Returns:
            list: List of (event_date, time_range, registration_time,
                additional_info, status, user_tag) tuples for the specified user.

        Raises:
            ValueError: If user_tag is None or empty.
        """
        if not user_tag:
            raise ValueError("user_tag is required to list events (cannot list across all users)")

        self.cursor.execute(
            """
            SELECT event_date, time_range, registration_time, additional_info, status, user_tag FROM events
            WHERE user_tag = ?
            ORDER BY registration_time DESC
            """,
            (user_tag,)
        )
        rows = self.cursor.fetchall()
        return rows

    def get_speculative_events(self, user_tag=None, status="speculative"):
        """Returns rows with the given status (optionally for one user) as dicts."""
        query = """
            SELECT event_date, time_range, registration_time, user_tag, additional_info,
                   expected_event_day, last_checked, request_ref
            FROM events WHERE status = ?
        """
        params = (status,)
        if user_tag:
            query += " AND user_tag = ?"
            params += (user_tag,)
        query += " ORDER BY registration_time ASC"
        self.cursor.execute(query, params)
        return [
            {
                "event_date": row[0],
                "time_range": row[1],
                "registration_time": _from_db_timestamp(row[2]),
                "user_tag": row[3],
                "additional_info": row[4],
                "expected_event_day": date.fromisoformat(row[5]) if row[5] else None,
                "last_checked": _from_db_timestamp(row[6]),
                "request_ref": json.loads(row[7]) if row[7] else None,
            }
            for row in self.cursor.fetchall()
        ]

    def confirm_event(self, event_date, time_range, user_tag, registration_time, additional_info=None):
        """Promotes a speculative row once the real event card has been seen."""
        self.cursor.execute(
            """
            UPDATE events SET status = 'confirmed', registration_time = ?,
                additional_info = COALESCE(?, additional_info)
            WHERE event_spec = ? AND user_tag = ?
            """,
            (
                _to_db_timestamp(registration_time),
                additional_info,
                self.create_spec(event_date, time_range),
                user_tag,
            ),
        )
        self.conn.commit()
        logger.info(f"Confirmed event {event_date} {time_range} for user '{user_tag}' at {registration_time}.")

    def set_status(self, event_date, time_range, user_tag, status, when=None):
        """Changes a row's status, stamping last_checked (the claim time for 'in_progress')."""
        self.cursor.execute(
            "UPDATE events SET status = ?, last_checked = ? WHERE event_spec = ? AND user_tag = ?",
            (
                status,
                _to_db_timestamp(when or datetime.now()),
                self.create_spec(event_date, time_range),
                user_tag,
            ),
        )
        self.conn.commit()
        logger.info(f"Set {event_date} {time_range} for user '{user_tag}' to {status}.")

    def mark_checked(self, event_date, time_range, user_tag, when=None):
        self.cursor.execute(
            "UPDATE events SET last_checked = ? WHERE event_spec = ? AND user_tag = ?",
            (
                _to_db_timestamp(when or datetime.now()),
                self.create_spec(event_date, time_range),
                user_tag,
            ),
        )
        self.conn.commit()

    def upsert_observations(self, user_tag, observations, seen_at):
        """Records scanned event cards.

        A countdown-derived lead is exact and always wins. A 'first_seen'
        lead is only meaningful on the first sighting, so later sightings
        never overwrite the lead.
        """
        seen = _to_db_timestamp(seen_at)
        for obs in observations:
            self.cursor.execute(
                """
                INSERT INTO observed_events (
                    user_tag, event_day, start_hm, end_hm, name, category,
                    lead_days, lead_source, first_seen, last_seen
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_tag, event_day, start_hm, end_hm, name) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    category = excluded.category,
                    lead_days = CASE WHEN excluded.lead_source = 'countdown'
                        THEN excluded.lead_days ELSE observed_events.lead_days END,
                    lead_source = CASE WHEN excluded.lead_source = 'countdown'
                        THEN excluded.lead_source ELSE observed_events.lead_source END
                """,
                (
                    user_tag,
                    obs["event_day"].isoformat(),
                    obs["start_hm"],
                    obs["end_hm"],
                    obs.get("name") or "",
                    obs.get("category"),
                    obs.get("lead_days"),
                    obs.get("lead_source"),
                    seen,
                    seen,
                ),
            )
        self.conn.commit()
        logger.debug(f"Recorded {len(observations)} observed event(s) for user '{user_tag}'.")

    def get_observations(self, user_tag, since):
        """Returns observations for a user with event_day on or after `since`."""
        self.cursor.execute(
            """
            SELECT event_day, start_hm, end_hm, name, category, lead_days, lead_source
            FROM observed_events WHERE user_tag = ? AND event_day >= ?
            ORDER BY event_day ASC, start_hm ASC
            """,
            (user_tag, since.isoformat()),
        )
        return [
            {
                "event_day": date.fromisoformat(row[0]),
                "start_hm": row[1],
                "end_hm": row[2],
                "name": row[3],
                "category": row[4],
                "lead_days": row[5],
                "lead_source": row[6],
            }
            for row in self.cursor.fetchall()
        ]

    def remove_old_observations(self, n_days):
        cutoff = (datetime.now() - timedelta(days=n_days)).date().isoformat()
        self.cursor.execute("DELETE FROM observed_events WHERE event_day < ?", (cutoff,))
        self.conn.commit()

    def record_snapshot(self, user_tag, when):
        """Records a successful scan of the user's listing."""
        when = _to_db_timestamp(when)
        self.cursor.execute(
            """
            INSERT INTO snapshots (user_tag, last_snapshot, last_attempt) VALUES (?, ?, ?)
            ON CONFLICT (user_tag) DO UPDATE SET
                last_snapshot = excluded.last_snapshot, last_attempt = excluded.last_attempt
            """,
            (user_tag, when, when),
        )
        self.conn.commit()

    def record_snapshot_attempt(self, user_tag, when):
        """Records that a scan was attempted, so a failing login backs off."""
        self.cursor.execute(
            """
            INSERT INTO snapshots (user_tag, last_attempt) VALUES (?, ?)
            ON CONFLICT (user_tag) DO UPDATE SET last_attempt = excluded.last_attempt
            """,
            (user_tag, _to_db_timestamp(when)),
        )
        self.conn.commit()

    def get_last_snapshot(self, user_tag):
        self.cursor.execute("SELECT last_snapshot FROM snapshots WHERE user_tag = ?", (user_tag,))
        row = self.cursor.fetchone()
        return _from_db_timestamp(row[0]) if row else None

    def get_last_snapshot_attempt(self, user_tag):
        self.cursor.execute("SELECT last_attempt FROM snapshots WHERE user_tag = ?", (user_tag,))
        row = self.cursor.fetchone()
        return _from_db_timestamp(row[0]) if row else None

    def close(self):
        self.conn.close()

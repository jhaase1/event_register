import sqlite3
from datetime import datetime, timedelta
from logging_config import get_logger

logger = get_logger(__name__)

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

    def create_spec(self, event_date, time_range):
        """Creates a unique event specification."""
        return f"{event_date} {time_range}"
    
    def insert_event(self, event_date, time_range, registration_time, user_tag, additional_info=""):
        self.cursor.execute(
            """
            INSERT OR REPLACE INTO events (event_spec, user_tag, event_date, time_range, registration_time, additional_info)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (self.create_spec(event_date, time_range), user_tag, event_date, time_range, registration_time, additional_info),
        )
        self.conn.commit()
        logger.info(f"Upserted event {event_date} {time_range} for user '{user_tag}'.")

    def get_events_by_date(self, registration_time, user_tag):
        self.cursor.execute(
            """
            SELECT event_date, time_range FROM events WHERE registration_time = ? AND user_tag = ?
        """,
            (registration_time, user_tag),
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
            WHERE registration_time > ?
        """,
            (timestamp,),
        )
        row = self.cursor.fetchone()
        if not row or not row[0]:
            return []
        
        next_registration_time = row[0]
        
        # Then, get all events at that time
        self.cursor.execute(
            """
            SELECT event_date, time_range, registration_time, user_tag FROM events 
            WHERE registration_time = ?
            ORDER BY user_tag ASC
        """,
            (next_registration_time,),
        )
        rows = self.cursor.fetchall()
        
        events = []
        for row in rows:
            event_date = row[0]
            time_range = row[1]
            registration_time = datetime.strptime(row[2], "%Y-%m-%d %H:%M:%S")
            user_tag = row[3]
            events.append({
                "event_date": event_date,
                "time_range": time_range,
                "registration_time": registration_time,
                "user_tag": user_tag
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
            list: List of event tuples for the specified user.
            
        Raises:
            ValueError: If user_tag is None or empty.
        """
        if not user_tag:
            raise ValueError("user_tag is required to list events (cannot list across all users)")
        
        self.cursor.execute(
            """
            SELECT event_date, time_range, registration_time, additional_info, user_tag FROM events 
            WHERE user_tag = ?
            ORDER BY registration_time DESC
            """,
            (user_tag,)
        )
        rows = self.cursor.fetchall()
        return rows

    def close(self):
        self.conn.close()

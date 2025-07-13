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
        """Create table if it doesn't exist."""
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_spec TEXT PRIMARY KEY,
                event_date TEXT NOT NULL,
                time_range TEXT NOT NULL,
                registration_time TIMESTAMP NOT NULL,
                additional_info TEXT
            )
        """
        )

    def create_spec(self, event_date, time_range):
        """Creates a unique event specification."""
        return f"{event_date} {time_range}"
    
    def insert_event(self, event_date, time_range, registration_time, additional_info=""):
        try:
            self.cursor.execute(
                """
                INSERT INTO events (event_spec, event_date, time_range, registration_time, additional_info)
                VALUES (?, ?, ?, ?, ?)
            """,
                (self.create_spec(event_date, time_range), event_date, time_range, registration_time, additional_info),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            print(f"Event with spec {event_date, time_range} already exists.")

    def get_events_by_date(self, registration_time):
        self.cursor.execute(
            """
            SELECT event_date, time_range FROM events WHERE registration_time = ?
        """,
            (registration_time,),
        )
        rows = self.cursor.fetchall()
        return [(row[0], row[1]) for row in rows]

    def get_next_event_after(self, timestamp=None):

        if timestamp is None:
            timestamp = datetime.now()

        """Finds the next event time after the provided timestamp."""
        self.cursor.execute(
            """
            SELECT event_date, time_range, registration_time FROM events 
            WHERE registration_time > ? 
            ORDER BY registration_time ASC 
            LIMIT 1
        """,
            (timestamp,),
        )
        row = self.cursor.fetchone()
        if row:
            event_date = row[0]
            time_range = row[1]
            registration_time = datetime.strptime(row[2], "%Y-%m-%d %H:%M:%S")

            return {"event_date": event_date, "time_range": time_range, "registration_time": registration_time}
        return None

    def remove_event(self, event_date, time_range):
        """Removes a row based on the event_spec."""
        logger.info(f"Removing event: {event_date, time_range}")
        event_spec = self.create_spec(event_date, time_range)
        logger.debug(f"Event spec to remove: {event_spec}")
        
        self.cursor.execute(
            """
            DELETE FROM events WHERE event_spec = ?
        """,
            (event_spec,),
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

    def list_all_events(self):
        """Returns all rows ordered by descending registration_time."""
        self.cursor.execute(
            """
            SELECT event_date, time_range, registration_time, additional_info FROM events 
            ORDER BY registration_time DESC
            """
        )
        rows = self.cursor.fetchall()
        return rows

    def close(self):
        self.conn.close()

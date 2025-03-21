import sqlite3
from datetime import datetime


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
                event_url TEXT PRIMARY KEY,
                registration_time TIMESTAMP NOT NULL
            )
        """
        )

    def insert_event(self, event_url, registration_time):
        try:
            self.cursor.execute(
                """
                INSERT INTO events (event_url, registration_time)
                VALUES (?, ?)
            """,
                (event_url, registration_time),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            print(f"Event with URL {event_url} already exists.")

    def get_event_urls_by_date(self, registration_time):
        self.cursor.execute(
            """
            SELECT event_url FROM events WHERE registration_time = ?
        """,
            (registration_time,),
        )
        rows = self.cursor.fetchall()
        return [row[0] for row in rows]

    def get_next_event_after(self, timestamp=None):

        if timestamp is None:
            timestamp = datetime.now()

        """Finds the next event time after the provided timestamp."""
        self.cursor.execute(
            """
            SELECT event_url, registration_time FROM events 
            WHERE registration_time > ? 
            ORDER BY registration_time ASC 
            LIMIT 1
        """,
            (timestamp,),
        )
        row = self.cursor.fetchone()
        if row:
            event_url = row[0]
            registration_time = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")

            return {"event_url": event_url, "registration_time": registration_time}
        return None

    def remove_event(self, event_url):
        """Removes a row based on the event_url."""
        self.cursor.execute(
            """
            DELETE FROM events WHERE event_url = ?
        """,
            (event_url,),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

"""Manual check for speculative pre-registration against a test database.

Read-only with respect to production: copies events.db to a separate test
database, migrates the copy, and records a live snapshot of each user's events
list into it. Never registers for anything and never sends email.

Usage:
    python live_snapshot_check.py                    # fresh copy + live snapshot for all users
    python live_snapshot_check.py --no-live          # migration check only
    python live_snapshot_check.py --keep             # reuse the existing test DB (accumulate snapshots)
    python live_snapshot_check.py --user default --predict "Tue, Oct 13" "9a - 11a"
"""

import argparse
import os
import sqlite3
from datetime import datetime, timedelta

from tabulate import tabulate

import schedule
from events import Events
from main import APP_CONFIG, _record_listing
from user_config import list_user_tags, load_user_config

PRODUCTION_DB = "events.db"
TEST_DB = "test_events.db"


def copy_production_db(source, target):
    """Uses SQLite's backup API so the copy is consistent even if source is in use."""
    if os.path.exists(target):
        os.remove(target)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    src.backup(dst)
    src.close()
    dst.close()


def table_snapshot(db_path):
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = [r[1] for r in conn.execute("PRAGMA table_info(events)")] if "events" in tables else []
    rows = (
        conn.execute(
            "SELECT event_spec, user_tag, event_date, time_range, registration_time, additional_info "
            "FROM events ORDER BY event_spec, user_tag"
        ).fetchall()
        if "events" in tables
        else []
    )
    conn.close()
    return tables, columns, rows


def check_migration(db_path):
    print(f"\n== Migration check on {db_path} ==")
    tables_before, columns_before, rows_before = table_snapshot(db_path)
    print(f"Before: tables={sorted(tables_before)} columns={columns_before} rows={len(rows_before)}")

    Events(db_name=db_path).close()

    tables_after, columns_after, rows_after = table_snapshot(db_path)
    print(f"After:  tables={sorted(tables_after)} columns={columns_after} rows={len(rows_after)}")

    problems = []
    if rows_before != rows_after:
        problems.append("existing event rows changed")
    for column in ("status", "last_checked", "expected_event_day", "request_ref"):
        if column not in columns_after:
            problems.append(f"missing column {column}")
    for table in ("observed_events", "snapshots"):
        if table not in tables_after:
            problems.append(f"missing table {table}")

    conn = sqlite3.connect(db_path)
    statuses = conn.execute("SELECT status, COUNT(*) FROM events GROUP BY status").fetchall()
    conn.close()
    print(f"Status counts: {statuses}")

    if problems:
        print("MIGRATION PROBLEMS: " + "; ".join(problems))
        return False
    print("Migration OK: all existing rows preserved and marked confirmed.")
    return True


def live_snapshot(db_path, user_tags, headless):
    # Imported here so --no-live works without Selenium/Chrome available.
    from website import Website

    events = Events(db_name=db_path)
    for tag in user_tags:
        print(f"\n== Live snapshot for user '{tag}' ==")
        website = None
        try:
            website = Website(headless=headless)
            website.login(user_tag=tag)
            website.display_all_events()
            previous = events.get_last_snapshot(tag)
            # Same code path the cron job uses, so this exercises the real logic.
            cards, observations = _record_listing(events, website, tag, datetime.now())

            print(
                f"Previous snapshot: {previous} | reported total on page: {website._total_events_count()} | "
                f"observations recorded: {len(observations)}"
            )
            print(
                tabulate(
                    [
                        (
                            f"{o['event_day']:%a %Y-%m-%d}",
                            f"{o['start_hm']}-{o['end_hm']}",
                            o["name"],
                            o["category"],
                            "open" if c["actionable"] else "",
                            c["opens_in"] or "",
                            o["lead_days"],
                            o["lead_source"] or "",
                        )
                        for c, o in zip(cards, observations)
                    ],
                    headers=["day", "time", "name", "category", "state", "opens in", "lead", "lead src"],
                )
            )
        except Exception as e:
            print(f"Snapshot failed for '{tag}': {e!r}")
        finally:
            if website is not None:
                website.close()
    events.close()


def print_history(db_path, user_tag):
    events = Events(db_name=db_path)
    since = datetime.now().date() - timedelta(weeks=APP_CONFIG["speculative_lookback_weeks"])
    observations = events.get_observations(user_tag, since=since)
    last = events.get_last_snapshot(user_tag)
    events.close()

    print(f"\n== Stored history for '{user_tag}' (since {since}, last snapshot {last}) ==")
    slots = {}
    for o in observations:
        key = (o["event_day"].strftime("%a"), o["start_hm"], o["end_hm"])
        slots.setdefault(key, []).append(o["event_day"])
    print(
        tabulate(
            [
                (day, f"{start}-{end}", len(days), ", ".join(f"{d:%m-%d}" for d in sorted(days)))
                for (day, start, end), days in sorted(slots.items())
            ],
            headers=["weekday", "time", "weeks seen", "dates"],
        )
    )


def run_prediction(db_path, user_tag, event_date, time_range):
    print(f"\n== Prediction for '{user_tag}': {event_date} {time_range} ==")
    now = datetime.now()
    parsed = schedule.parse_request(event_date, time_range, now.date())
    if not parsed:
        print("Could not parse the requested date/time.")
        return
    requested_day, slot = parsed

    events = Events(db_name=db_path)
    observations = events.get_observations(
        user_tag, since=now.date() - timedelta(weeks=APP_CONFIG["speculative_lookback_weeks"])
    )
    events.close()

    user_config = load_user_config(user_tag) or {}
    result = schedule.predict(
        observations,
        requested_day,
        slot,
        now,
        lookback_weeks=APP_CONFIG["speculative_lookback_weeks"],
        min_matches=APP_CONFIG["speculative_min_matches"],
        max_weeks_ahead=APP_CONFIG["speculative_max_weeks_ahead"],
        registration_clock=user_config.get("default_registration_time"),
        lead_override=user_config.get("registration_lead_days"),
    )
    print(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=TEST_DB, help=f"test database path (default {TEST_DB})")
    parser.add_argument("--source", default=PRODUCTION_DB, help="database to copy from")
    parser.add_argument("--keep", action="store_true", help="reuse the existing test DB instead of re-copying")
    parser.add_argument("--no-live", action="store_true", help="skip the live website snapshot")
    parser.add_argument("--headless", action="store_true", help="run Chrome headless")
    parser.add_argument("--user", action="append", help="user tag(s) to snapshot (default: all)")
    parser.add_argument("--predict", nargs=2, metavar=("EVENT_DATE", "TIME_RANGE"))
    args = parser.parse_args()

    if os.path.abspath(args.db) == os.path.abspath(PRODUCTION_DB):
        parser.error("refusing to use the production database as the test database")

    if not args.keep or not os.path.exists(args.db):
        if os.path.exists(args.source):
            print(f"Copying {args.source} -> {args.db}")
            copy_production_db(args.source, args.db)
        else:
            print(f"{args.source} not found; starting {args.db} empty")
        if not check_migration(args.db):
            raise SystemExit(1)

    user_tags = args.user or list_user_tags()
    if not args.no_live:
        live_snapshot(args.db, user_tags, args.headless)

    for tag in user_tags:
        print_history(args.db, tag)

    if args.predict:
        run_prediction(args.db, user_tags[0], *args.predict)


if __name__ == "__main__":
    main()

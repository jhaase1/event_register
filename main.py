import textile
from tabulate import tabulate
import json
from events import Events
from website import Website, SkillLevelIneligible
from dwell import dwell_until, is_within_offset
from email_client import EmailClient
from user_intent import extract_user_intent
from user_config import extract_user_tag, validate_user_tag, is_sender_allowed
from logging_config import get_logger
import os
import random
import threading
import concurrent.futures
import traceback
from datetime import datetime, timedelta, timezone
import platform
import sys

APP_CONFIG_FILE = "app_config.json"
DEFAULT_APP_CONFIG = {
    "hold_buffer_minutes": 10,
    "login_buffer_minutes": 1,
    "min_delay_seconds": 4,
    "max_delay_seconds": 6,
    "cleanup_days": 8,
}

if os.name == "nt":
    headless = False
elif os.name == "posix":
    headless = True

logger = get_logger(__name__)


def load_app_config(config_file=APP_CONFIG_FILE):
    """Loads runtime settings for the application."""
    config = DEFAULT_APP_CONFIG.copy()

    if not os.path.exists(config_file):
        logger.warning(
            f"App config file not found: {config_file}. Using built-in defaults."
        )
        return config

    with open(config_file, "r") as file:
        loaded_config = json.load(file)

    config.update(loaded_config)
    return config


APP_CONFIG = load_app_config()
HOLD_BUFFER = APP_CONFIG["hold_buffer_minutes"]  # minutes
LOGIN_BUFFER = APP_CONFIG["login_buffer_minutes"]  # minutes
MIN_DELAY = APP_CONFIG["min_delay_seconds"]  # seconds
MAX_DELAY = APP_CONFIG["max_delay_seconds"]  # seconds

cleanup_days = APP_CONFIG["cleanup_days"]  # days to keep events in the database

# A registration time this close is treated as already open: it would slip
# into the past before register_for_next_event looks for future times.
ALREADY_OPEN_MARGIN = timedelta(minutes=1)


def register_for_single_event(
    event_info, headless=True, results=None, results_lock=None
):
    """Register for a single event (used for concurrent registrations)."""
    event_date = event_info["event_date"]
    time_range = event_info["time_range"]
    registration_time = event_info["registration_time"]
    user_tag = event_info["user_tag"]

    def _record_result(result):
        if results is not None:
            if results_lock:
                with results_lock:
                    results.append(result)
            else:
                results.append(result)

    logger.info(
        f"Registering event for user '{user_tag}': {event_date} {time_range} at {registration_time}"
    )

    website = None
    try:
        website = Website(headless=headless)
        website.login(user_tag=user_tag)
        event_url = website.get_event_url(event_date, time_range)

        delay = random.uniform(MIN_DELAY, MAX_DELAY)
        logger.info(
            f"Waiting until registration time for user '{user_tag}' (delay: {delay:.2f}s)"
        )
        dwell_until(registration_time, offset_seconds=-delay)

        logger.info(
            f"Registering for event (user '{user_tag}'): {event_date} {time_range}"
        )
        website.register_for_event(
            event_date=event_date, time_range=time_range, event_url=event_url
        )

        logger.info(
            f"Successfully registered user '{user_tag}' for {event_date} {time_range}"
        )
        _record_result(
            {
                "user_tag": user_tag,
                "event": f"{event_date} {time_range}",
                "success": True,
            }
        )
    except Exception as e:
        logger.error(
            f"Error registering user '{user_tag}' for {event_date} {time_range}: {e}",
            exc_info=True,
        )
        tb = traceback.format_exc()
        _record_result(
            {
                "user_tag": user_tag,
                "event": f"{event_date} {time_range}",
                "success": False,
                "error": str(e),
                "traceback": tb,
                "registration_time": registration_time,
            }
        )
    finally:
        if website is not None:
            try:
                logger.info(f"Closing website for user '{user_tag}'")
                website.close()
            except Exception as close_error:
                logger.warning(
                    f"Failed to close website for user '{user_tag}': {close_error}"
                )


def register_for_next_event(headless=True):
    logger.info("Starting registration process for the next event(s).")
    # Connect to the database
    events = Events()
    next_events = events.get_next_event_after()

    if not next_events:
        logger.info("No upcoming events.")
        events.close()
        return

    # All events share the same registration time
    registration_time = next_events[0]["registration_time"]
    logger.info(
        f"Found {len(next_events)} event(s) at registration time: {registration_time}"
    )

    for event in next_events:
        logger.info(
            f"  - User '{event['user_tag']}': {event['event_date']} {event['time_range']}"
        )

    if is_within_offset(registration_time, offset_minutes=HOLD_BUFFER):
        logger.info("Holding until registration time.")
        dwell_until(registration_time, offset_minutes=HOLD_BUFFER)
    else:
        logger.info("Registration time is too far away.")
        events.close()
        return

    logger.info("Logging in to website(s).")
    dwell_until(registration_time, offset_minutes=LOGIN_BUFFER)

    # Register events (concurrent if multiple, sequential if single)
    results = []
    max_workers = min(len(next_events), 4)
    if len(next_events) > 1:
        logger.info(
            f"Submitting {len(next_events)} events to thread pool (max_workers={max_workers})."
        )
        results_lock = threading.Lock()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    register_for_single_event, event, headless, results, results_lock
                )
                for event in next_events
            ]
            concurrent.futures.wait(futures)
    else:
        # Single event, no threading needed
        register_for_single_event(next_events[0], headless=headless, results=results)

    # Report results and notify on failures
    succeeded = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    if results:
        logger.info(
            f"Registration complete: {len(succeeded)} succeeded, {len(failed)} failed."
        )

    _notify_registration_failures(failed, headless)

    logger.info("Removing old events from the database.")
    events.remove_old_events(n_days=cleanup_days)
    events.close()


def _notify_registration_failures(failed, headless):
    if not failed:
        return
    try:
        notifier = EmailClient()
        for f in failed:
            logger.error(
                f"FAILED: user '{f['user_tag']}' for {f['event']}: {f['error']}"
            )
            ctx = {
                "user_tag": f.get("user_tag"),
                "event": f.get("event"),
                "registration_time": f.get("registration_time"),
                "error": f.get("error"),
                "traceback": f.get("traceback"),
            }
            notifier.send_notification(
                subject="Event registration failed",
                body=_format_failure_body(ctx, headless_flag=headless),
                user_tag=f["user_tag"],
            )
    except Exception as e:
        logger.error(f"Failed to send failure notifications: {e}", exc_info=True)


def _attempt_open_registration(item, headless):
    """Registers for one queued already-open event. Returns its results list."""
    # Never earlier than the real opening; register_for_single_event then
    # waits for it the same way the scheduler does.
    registration_time = max(item["registration_time"], datetime.now()).replace(microsecond=0)
    item["registration_time"] = registration_time
    results = []
    register_for_single_event(
        {
            "event_date": item["event_date"],
            "time_range": item["time_range"],
            "registration_time": registration_time,
            "user_tag": item["user_tag"],
        },
        headless=headless,
        results=results,
    )
    return results


def register_open_events(pending, headless=True):
    """Registers events that were already open (or about to open) when emailed.

    check_for_new_event only queues these. They run after
    register_for_next_event so that these ad-hoc registrations, each a minute
    or two of browser work, never push a scheduled registration past its hold
    window.
    """
    if not pending:
        return
    logger.info(f"Registering {len(pending)} already-open event(s) from email.")

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(pending), 4)) as executor:
        all_results = list(
            executor.map(lambda item: _attempt_open_registration(item, headless), pending)
        )

    # Database and Gmail work stays on this thread: the sqlite connection
    # can't be shared across threads.
    events = Events()
    try:
        email_client = EmailClient()
    except Exception:
        logger.exception("Could not create email client for already-open replies")
        email_client = None

    failed = []
    for item, results in zip(pending, all_results):
        event_date, time_range, user_tag = item["event_date"], item["time_range"], item["user_tag"]
        additional_info = item["additional_info"]

        if any(r["success"] for r in results):
            # Recorded so the report shows it; its time is already past, so
            # the scheduler won't try it again.
            events.insert_event(
                event_date=event_date,
                time_range=time_range,
                registration_time=item["registration_time"],
                user_tag=user_tag,
                additional_info=additional_info,
            )
            reply = "Registration was already open, so I registered you right away."
            subject = f"Event Registration Confirmation: {event_date} {time_range}"
        else:
            failed.extend(r for r in results if not r["success"])
            reply = (
                "Registration was already open, but my attempt to register didn't go through. "
                "Please check the website."
            )
            subject = f"Event Registration: {event_date} {time_range}"

        if additional_info:
            reply += f"\n\nAdditional info: {additional_info}"

        if email_client is None:
            continue
        try:
            email_client.reply_to_email(
                item["email"],
                reply_plaintext=reply,
                reply_html=textile.textile(reply),
                subject=subject,
                user_tag=user_tag,
            )
        except Exception:
            logger.exception(f"Failed to reply about {event_date} {time_range} for '{user_tag}'")

    events.close()
    _notify_registration_failures(failed, headless)


def check_for_new_event(headless=True, pending_open=None):
    """Processes command emails.

    Events that are already open are appended to `pending_open` for the caller
    to register later (see register_open_events). Appending as we go keeps
    them even if a later email raises. Without a list, they're registered at
    the end of this call.
    """
    register_open_now = pending_open is None
    if register_open_now:
        pending_open = []
    logger.info("Checking for new events via email.")
    email_client = EmailClient()
    email_client.authenticate_email()
    new_emails = email_client.read_new_emails()

    if not new_emails:
        logger.info("No new emails found.")
        return

    websites = {}  # Per-user Website instances keyed by user_tag
    events = Events()
    deferred_reports = []  # Store report requests until all other emails are processed

    for email in new_emails:
        # LAYER 1: Global authorization - sender must be in Google Contacts
        # This is a first-pass filter to reject unknown senders before any processing
        if email_client.is_sender_authorized(email.From):
            logger.info(f"Authorized sender (in contacts): {email.From}")
        else:
            logger.info(f"Unauthorized sender (not in contacts): {email.From}")
            email_client.mark_email_as_read(email)
            email_client.archive_email(email)

            continue

        # Extract user tag from the To address (filter by system email to avoid mismatches)
        try:
            user_tag = extract_user_tag(email.To, system_email=email_client.user)
        except ValueError as e:
            # Missing system_email or other extraction error - treat as security event
            logger.error(f"Failed to extract user tag: {e}")
            email_client.mark_email_as_read(email)
            email_client.archive_email(email)
            continue

        logger.info(f"Processing email for user tag: {user_tag}")

        # Validate user tag exists and is properly configured
        try:
            user_tag = validate_user_tag(user_tag)
        except (ValueError, FileNotFoundError) as e:
            logger.warning(f"Invalid user tag '{user_tag}': {e}")
            # Silent archive to prevent user enumeration via response timing.
            email_client.mark_email_as_read(email)
            email_client.archive_email(email)
            continue

        # LAYER 2: User-specific authorization - sender must be authorized for this user_tag
        # Even if sender passed global check (is in contacts), they must be explicitly
        # authorized for the specific user account they're trying to access
        sender_email = email_client.extract_email_address(email.From)[0]
        if not is_sender_allowed(sender_email, user_tag):
            logger.warning(
                f"SECURITY: Unauthorized access attempt - sender '{sender_email}' "
                f"tried to access user tag '{user_tag}'"
            )
            # Silent failure - do NOT reply to prevent confirmation of valid tags
            email_client.mark_email_as_read(email)
            email_client.archive_email(email)
            continue

        action, event_details = extract_user_intent(email)
        event_date, time_range = event_details or (None, None)

        if action == "report":
            logger.info(
                f"Deferring report for user '{user_tag}' until all other emails are processed."
            )
            deferred_reports.append((email, user_tag))
            continue

        elif action == "add":
            logger.info(f"Adding event for user '{user_tag}': {event_date, time_range}")
            if user_tag not in websites:
                websites[user_tag] = Website(headless=headless)
            website = websites[user_tag]
            website.login(user_tag=user_tag)

            try:
                registration_time, additional_info = website.determine_access_date(
                    event_date, time_range
                )
            except SkillLevelIneligible as e:
                logger.info(
                    f"Event not eligible for user '{user_tag}' due to skill level: {e}"
                )
                email_client.reply_to_email(email, str(e), user_tag=user_tag)
                email_client.mark_email_as_read(email)
                email_client.archive_email(email)
                continue

            if registration_time is None:
                logger.info(
                    f"Could not determine the registration time for {event_date, time_range}."
                )
                reply = "I could not determine the registration time."
                if additional_info:
                    reply += f"\n\nI found this info on the page (check if you are in an eligible tier): {additional_info}"

                email_client.reply_to_email(email, reply, user_tag=user_tag)
                try:
                    notifier = EmailClient()
                    ctx = {
                        "user_tag": user_tag,
                        "event_date": event_date,
                        "time_range": time_range,
                        "reason": "could not determine registration time",
                        "additional_info": additional_info,
                    }
                    notifier.send_notification(
                        subject="Event registration failed",
                        body=_format_failure_body(ctx, headless_flag=headless),
                        user_tag=user_tag,
                    )
                except Exception:
                    logger.exception(
                        "Failed to send failure notification for undetermined registration time"
                    )
            elif registration_time <= datetime.now() + ALREADY_OPEN_MARGIN:
                # determine_access_date returns "now" when the event is already
                # open. Storing that (or a time seconds away, which slips into
                # the past while later emails are processed) would never
                # register: the scheduler only picks up future times. Queue it;
                # the email is marked read below, before any registration.
                logger.info(
                    f"Registration open or about to open for user '{user_tag}': "
                    f"{event_date} {time_range}; queued."
                )
                pending_open.append(
                    {
                        "email": email,
                        "user_tag": user_tag,
                        "event_date": event_date,
                        "time_range": time_range,
                        "additional_info": additional_info,
                        "registration_time": registration_time,
                    }
                )
            else:
                logger.debug(
                    f"Inserting {event_date, time_range} into database at {registration_time} for user '{user_tag}'"
                )
                old_events = events.get_events_by_date(
                    registration_time, user_tag=user_tag
                )
                if old_events:
                    logger.info(
                        f"Event already exists for this date and user: {old_events}. Removing old event."
                    )

                    for old_event in old_events:
                        events.remove_event(*old_event, user_tag=user_tag)
                events.insert_event(
                    event_date=event_date,
                    time_range=time_range,
                    registration_time=registration_time,
                    user_tag=user_tag,
                    additional_info=additional_info,
                )

                reply = f"I determined I need to register at {registration_time} and will do so."

                if additional_info:
                    reply += f"\n\nAdditional info: {additional_info}"

                reply_html = textile.textile(reply)

                email_client.reply_to_email(
                    email,
                    reply_plaintext=reply,
                    reply_html=reply_html,
                    subject=f"Event Registration Confirmation: {event_date} {time_range}",
                    user_tag=user_tag,
                )

                logger.info(
                    f"Inserted and emailed {event_date} {time_range} into database at {registration_time} for user '{user_tag}' with additional info: {additional_info}"
                )

        elif action == "remove":
            logger.info(
                f"Removing event for user '{user_tag}': {event_date, time_range}"
            )
            events.remove_event(event_date, time_range, user_tag=user_tag)
            email_client.reply_to_email(
                email,
                "I am not going to register for the event.",
                subject=f"Event Registration Cancellation: {event_date} {time_range}",
                user_tag=user_tag,
            )

        elif action is None:
            logger.info("Could not determine the action from the email.")
            email_client.reply_to_email(
                email, "I am not sure what you want me to do.", user_tag=user_tag
            )
            try:
                notifier = EmailClient()
                ctx = {
                    "user_tag": user_tag,
                    "email_from": email.From,
                    "email_subject": email.subject,
                    "reason": "could not determine action",
                    "email_body": email.body,
                }
                notifier.send_notification(
                    subject="Event registration failed",
                    body=_format_failure_body(ctx, headless_flag=headless),
                    user_tag=user_tag,
                )
            except Exception:
                logger.exception(
                    "Failed to send failure notification for unknown action"
                )

        email_client.mark_email_as_read(email)
        email_client.archive_email(email)

    # Process deferred report requests now that all add/remove actions are complete
    for report_email, report_user_tag in deferred_reports:
        logger.info(f"Reporting events for user '{report_user_tag}' (deferred).")
        event_list = events.list_all_events(user_tag=report_user_tag)
        # Omit user_tag column (last element) from each row for privacy
        event_list = [row[:-1] for row in event_list]

        headers = ["event date", "time range", "registration time", "additional info"]
        reply = tabulate(event_list, headers=headers)
        reply_html = tabulate(event_list, headers=headers, tablefmt="html")

        email_client.reply_to_email(
            report_email,
            reply_plaintext=reply,
            reply_html=reply_html,
            user_tag=report_user_tag,
        )

        email_client.mark_email_as_read(report_email)
        email_client.archive_email(report_email)

    logger.info("Closing website and database connections.")
    for tag, website in websites.items():
        try:
            website.close()
        except Exception as e:
            logger.error(f"Error closing website for user '{tag}': {e}")
    events.close()

    if register_open_now:
        register_open_events(pending_open, headless=headless)


def run(headless):
    """One cron cycle: read email, run scheduled registrations, then
    register anything that was already open when emailed."""
    pending_open = []
    try:
        check_for_new_event(headless=headless, pending_open=pending_open)
    except Exception as e:
        logger.error(f"An error occurred: {e}")

    try:
        register_for_next_event(headless=headless)
    except Exception as e:
        logger.error(f"Scheduled registration failed: {e}", exc_info=True)

    register_open_events(pending_open, headless=headless)


def _format_failure_body(context: dict, headless_flag: bool = True) -> str:
    """Build a generalized failure body from a context dictionary.

    Includes timestamp, environment info, and all provided context keys.
    """
    lines = []
    lines.append(f"Timestamp: {datetime.now(timezone.utc).isoformat().replace('+00:00','Z')}")
    lines.append("Environment:")
    lines.append(f"  Python: {platform.python_version()}")
    lines.append(f"  OS: {platform.system()} {platform.release()}")
    lines.append(f"  Headless: {headless_flag}")
    lines.append("")

    # Add context fields in a stable order
    for key in sorted(context.keys()):
        val = context.get(key)
        # Pretty-print large values (like tracebacks) with separation
        if isinstance(val, str) and "\n" in val:
            lines.append(f"{key}:")
            lines.append(val)
        else:
            lines.append(f"{key}: {val}")

    return "\n".join(lines)


if __name__ == "__main__":
    run(headless=headless)

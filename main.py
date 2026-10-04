import textile
from tabulate import tabulate
import json
from events import Events
from website import Website, SkillLevelIneligible, EventNotFound
from dwell import dwell_until, is_within_offset
from email_client import EmailClient
from user_intent import extract_user_intent
from user_config import (
    extract_user_tag,
    validate_user_tag,
    is_sender_allowed,
    load_user_config,
    list_user_tags,
)
import schedule
from logging_config import get_logger
import os
import random
import threading
import concurrent.futures
import contextlib
import time
import traceback
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import platform
import sys

APP_CONFIG_FILE = "app_config.json"
DEFAULT_APP_CONFIG = {
    "hold_buffer_minutes": 10,
    "login_buffer_minutes": 1,
    "min_delay_seconds": 4,
    "max_delay_seconds": 6,
    "cleanup_days": 8,
    "speculative_enabled": True,
    "speculative_lookback_weeks": 4,
    "speculative_min_matches": 3,
    "speculative_max_weeks_ahead": 3,
    "speculative_find_grace_minutes": 5,
    "speculative_recheck_hours": 6,
    "snapshot_interval_hours": 6,
    "observed_retention_days": 42,
    # How often cron runs main.py. A speculative session confirmed to open
    # sooner than the next run is registered in the current run.
    "cron_interval_minutes": 15,
    # After a speculative row's predicted time passes, recheck it every run for
    # this long, then fall back to speculative_recheck_hours.
    "speculative_fast_poll_hours": 2,
    # A row claimed for registration longer ago than this is assumed to belong
    # to a crashed run and is released back to speculative.
    "speculative_claim_stale_minutes": 60,
    "notify_webmaster_on_unlisted_request": False,
}

REFRESH_LOCK_FILE = "refresh.lock"

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
    status = event_info.get("status", "confirmed")

    def _record_result(result):
        result.update(
            event_date=event_date,
            time_range=time_range,
            status=status,
            request_ref=event_info.get("request_ref"),
        )
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
        if status == "speculative":
            # The session may only get posted the moment registration opens,
            # so keep looking for it a little past the predicted time.
            deadline = registration_time + timedelta(
                minutes=APP_CONFIG["speculative_find_grace_minutes"]
            )
            event_url = website.find_event_url(event_date, time_range, until=deadline)
            if not event_url:
                logger.info(
                    f"Speculative event for user '{user_tag}' not posted by {deadline}: {event_date} {time_range}"
                )
                _record_result(
                    {
                        "user_tag": user_tag,
                        "event": f"{event_date} {time_range}",
                        "success": False,
                        "speculative_not_found": True,
                        "error": "event not posted at the predicted time",
                        "registration_time": registration_time,
                    }
                )
                return
        else:
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
        # Claim speculative rows before holding so an overlapping run's
        # refresh step can't register them a second time.
        for event in next_events:
            if event.get("status") == "speculative":
                events.set_status(
                    event["event_date"], event["time_range"], event["user_tag"], "in_progress"
                )
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
    # A speculative session that hasn't been posted yet isn't a system failure;
    # the row stays speculative and the confirmation step keeps polling for it.
    not_posted = [r for r in results if r.get("speculative_not_found")]
    failed = [r for r in results if not r["success"] and not r.get("speculative_not_found")]
    if results:
        logger.info(
            f"Registration complete: {len(succeeded)} succeeded, {len(failed)} failed, "
            f"{len(not_posted)} speculative not posted yet."
        )

    for r in succeeded:
        if r.get("status") == "speculative":
            events.confirm_event(
                r["event_date"], r["time_range"], r["user_tag"], registration_time
            )
    for r in not_posted:
        # Back to speculative so the refresh step keeps polling for it.
        events.set_status(r["event_date"], r["time_range"], r["user_tag"], "speculative")
    speculative_failed = [r for r in failed if r.get("status") == "speculative"]
    for r in speculative_failed:
        # A real error, not a missing card: don't let refresh retry it blindly.
        events.set_status(r["event_date"], r["time_range"], r["user_tag"], "failed")

    followups = [
        (
            r,
            f"The {r['event']} session wasn't posted when I expected registration to open "
            f"({registration_time}). I'll keep checking every run and register as soon as it shows up.",
        )
        for r in not_posted
    ] + [
        (
            r,
            f"I tried to register for {r['event']} when registration opened, but it didn't go through. "
            "Please check the website.",
        )
        for r in speculative_failed
    ]
    if followups:
        notifier = EmailClient()
        for r, body in followups:
            try:
                _reply_to_request(notifier, r.get("request_ref"), body, r["user_tag"])
            except Exception as e:
                logger.error(f"Failed to send follow-up for {r['event']}: {e}", exc_info=True)

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


def _reply_to_request(email_client, request_ref, body, user_tag, subject=None):
    """Follows up in the thread of the email that asked for a speculative registration."""
    if not request_ref:
        email_client.send_notification(
            subject=subject or "Speculative registration update", body=body, user_tag=user_tag
        )
        return
    original = SimpleNamespace(
        id=request_ref.get("message_id"),
        From=request_ref.get("from") or [],
        subject=request_ref.get("subject") or "",
        message_id=request_ref.get("message_id"),
        thread_id=request_ref.get("thread_id"),
    )
    email_client.reply_to_email(original, body, subject=subject, user_tag=user_tag)


def _record_listing(events, website, user_tag, now):
    """Saves every card on the already-loaded events list as schedule history.

    Returns the scanned cards and their observations (empty on failure).
    """
    try:
        # A card that's already open is only a meaningful "first sighting" if a
        # recent earlier scan would have caught it before it was posted.
        last = events.get_last_snapshot(user_tag)
        trust_first_seen = last is not None and now - last <= timedelta(
            hours=2 * APP_CONFIG["snapshot_interval_hours"]
        )
        cards = website.scan_listed_events()
        observations = [
            schedule.to_observation(c, now, trust_first_seen=trust_first_seen) for c in cards
        ]
        cards = [c for c, o in zip(cards, observations) if o]
        observations = [o for o in observations if o]
        events.upsert_observations(user_tag, observations, seen_at=now)
        events.record_snapshot(user_tag, now)
        return cards, observations
    except Exception:
        logger.exception(f"Failed to record event listing for user '{user_tag}'")
        return [], []


def _clear_registration_slot(events, user_tag, registration_time, keep):
    """Enforces one event per registration time per user (the rule the add path
    already applies) before a row is inserted at or moved to that time."""
    for old_event in events.get_events_by_date(registration_time, user_tag=user_tag):
        if tuple(old_event) == tuple(keep):
            continue
        logger.info(f"Replacing event at the same registration time: {old_event}")
        events.remove_event(*old_event, user_tag=user_tag)


def _confirm_speculative(events, row, registration_time, additional_info):
    _clear_registration_slot(
        events, row["user_tag"], registration_time, keep=(row["event_date"], row["time_range"])
    )
    events.confirm_event(
        row["event_date"], row["time_range"], row["user_tag"], registration_time, additional_info
    )


def _handle_unlisted_request(events, website, email, user_tag, event_date, time_range):
    """Tries to accept a request for an event that isn't on the website yet.

    Returns the reply text for the user.
    """
    now = datetime.now()
    not_found = f"I couldn't find {event_date} {time_range} on the website."

    # determine_access_date left the full list loaded, so this scan is free.
    _record_listing(events, website, user_tag, now)

    if not APP_CONFIG["speculative_enabled"]:
        return not_found

    parsed = schedule.parse_request(event_date, time_range, now.date())
    if not parsed:
        return not_found
    requested_day, slot = parsed

    observations = events.get_observations(
        user_tag, since=now.date() - timedelta(weeks=APP_CONFIG["speculative_lookback_weeks"])
    )
    user_config = load_user_config(user_tag) or {}
    result = schedule.predict(
        observations,
        requested_day,
        slot,
        now,
        lookback_weeks=APP_CONFIG["speculative_lookback_weeks"],
        min_matches=APP_CONFIG["speculative_min_matches"],
        max_weeks_ahead=APP_CONFIG["speculative_max_weeks_ahead"],
        registration_clock=website.default_registration_time,
        lead_override=user_config.get("registration_lead_days"),
    )
    if isinstance(result, schedule.Rejection):
        logger.info(f"Speculative request rejected for user '{user_tag}': {result.reason}")
        return f"{not_found} It isn't posted yet and I can't schedule it ahead of time: {result.reason}"

    _clear_registration_slot(events, user_tag, result.registration_time, keep=(event_date, time_range))

    matched = ", ".join(f"{d:%b} {d.day}" for d in result.matched_days)
    events.insert_event(
        event_date=event_date,
        time_range=time_range,
        registration_time=result.registration_time,
        user_tag=user_tag,
        additional_info=f"Not posted yet; same slot seen {matched}",
        status="speculative",
        expected_event_day=requested_day,
        request_ref={
            "from": email.From,
            "thread_id": email.thread_id,
            "message_id": email.message_id,
            "subject": email.subject,
        },
    )

    reply = (
        f"That session isn't posted on the website yet, but the same {requested_day:%A} "
        f"{time_range} slot was listed on {matched}. I expect registration to open at "
        f"{result.registration_time} and will try to register then. If it opens at a "
        f"different time I'll register as soon as it shows up, and I'll email you once it's posted."
    )
    if result.names:
        reply += f"\n\nRecent sessions in that slot: {', '.join(result.names)}"
    return reply


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
            except EventNotFound:
                logger.info(
                    f"Event not listed for user '{user_tag}': {event_date} {time_range}; trying speculative."
                )
                try:
                    reply = _handle_unlisted_request(
                        events, website, email, user_tag, event_date, time_range
                    )
                except Exception:
                    # Never let one request (e.g. a bad config value) stall the
                    # whole inbox; it would be retried and fail every run.
                    logger.exception("Speculative handling failed; replying not found.")
                    reply = f"I couldn't find {event_date} {time_range} on the website."
                if APP_CONFIG["notify_webmaster_on_unlisted_request"]:
                    try:
                        EmailClient().send_notification(
                            subject="Event registration failed",
                            body=_format_failure_body(
                                {
                                    "user_tag": user_tag,
                                    "event_date": event_date,
                                    "time_range": time_range,
                                    "reason": "event not listed on the website",
                                    "reply": reply,
                                },
                                headless_flag=headless,
                            ),
                            user_tag=user_tag,
                        )
                    except Exception:
                        logger.exception("Failed to send webmaster notification for unlisted request")
                email_client.reply_to_email(
                    email,
                    reply,
                    subject=f"Event Registration: {event_date} {time_range}",
                    user_tag=user_tag,
                )
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

        headers = ["event date", "time range", "registration time", "additional info", "status"]
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


def _drop_speculative(events, email_client, row, message):
    logger.info(f"Dropping speculative event {row['event_date']} {row['time_range']} for '{row['user_tag']}'.")
    events.remove_event(row["event_date"], row["time_range"], user_tag=row["user_tag"])
    _reply_to_request(email_client, row["request_ref"], message, row["user_tag"])


def _register_speculative_now(events, email_client, row, registration_time, additional_info, headless):
    """Registers a speculative event in this run, waiting for its real opening time."""
    event_date, time_range, user_tag = row["event_date"], row["time_range"], row["user_tag"]
    label = f"{event_date} {time_range}"
    # Wait for the real opening; never click before it.
    registration_time = max(registration_time, datetime.now().replace(microsecond=0))

    events.set_status(event_date, time_range, user_tag, "in_progress")
    results = []
    register_for_single_event(
        {
            "event_date": event_date,
            "time_range": time_range,
            "registration_time": registration_time,
            "user_tag": user_tag,
            "status": "confirmed",
        },
        headless=headless,
        results=results,
    )

    if any(r["success"] for r in results):
        _confirm_speculative(events, row, registration_time, additional_info)
        _reply_to_request(
            email_client,
            row["request_ref"],
            f"The {label} session is posted and I registered you for it.",
            user_tag,
        )
        return

    events.set_status(event_date, time_range, user_tag, "failed")
    _reply_to_request(
        email_client,
        row["request_ref"],
        f"I tried to register for {label} when registration opened, but it didn't go through. "
        "Please check the website.",
        user_tag,
    )
    _notify_registration_failures([r for r in results if not r["success"]], headless)


def _check_speculative_row(events, website, email_client, row, now, headless):
    """Looks for one speculative event on the site and acts on what it finds."""
    event_date, time_range, user_tag = row["event_date"], row["time_range"], row["user_tag"]
    label = f"{event_date} {time_range}"
    expected_day = row["expected_event_day"]
    # Before its predicted opening, a missing card can still show up (even on
    # the event day itself when the lead is 0 days).
    opening_passed = now >= row["registration_time"]
    event_over = bool(expected_day and expected_day < now.date())
    event_day_reached = bool(expected_day and expected_day <= now.date())

    try:
        registration_time, additional_info = website.determine_access_date(event_date, time_range)
    except SkillLevelIneligible as e:
        logger.info(f"Speculative event {label} for '{user_tag}' is skill-ineligible: {e}")
        _drop_speculative(events, email_client, row, str(e))
        return
    except EventNotFound:
        parsed = schedule.parse_request(event_date, time_range, now.date())
        skipped = bool(
            expected_day
            and parsed
            and schedule.later_occurrence_listed(
                events.get_observations(user_tag, since=expected_day), expected_day, parsed[1]
            )
        )
        if skipped or event_over or (event_day_reached and opening_passed):
            _drop_speculative(
                events,
                email_client,
                row,
                f"The {label} session never posted on the website (it looks cancelled or "
                f"skipped this week), so I won't register for it.",
            )
        else:
            logger.info(f"Speculative event {label} for '{user_tag}' not posted yet.")
            events.mark_checked(event_date, time_range, user_tag, when=now)
        return

    if registration_time is None:
        if event_day_reached:
            _drop_speculative(
                events,
                email_client,
                row,
                f"The {label} session is posted, but I couldn't tell when registration opens, "
                "so I couldn't register for it. Please check the website.",
            )
        else:
            logger.warning(
                f"Speculative event {label} for '{user_tag}' is posted but its registration time "
                "couldn't be read; keeping the prediction."
            )
            events.mark_checked(event_date, time_range, user_tag, when=now)
        return

    # register_for_next_event already ran this cron cycle; anything opening
    # before the next run would be missed unless it's handled here.
    next_run = datetime.now() + timedelta(minutes=APP_CONFIG["cron_interval_minutes"])
    if registration_time <= next_run:
        _register_speculative_now(events, email_client, row, registration_time, additional_info, headless)
        return

    _confirm_speculative(events, row, registration_time, additional_info)
    reply = (
        f"The {label} session is now posted. Registration opens at {registration_time} "
        "and I'll register then."
    )
    if additional_info:
        reply += f"\n\nAdditional info: {additional_info}"
    _reply_to_request(email_client, row["request_ref"], reply, user_tag)


@contextlib.contextmanager
def _exclusive_lock(path, stale_after):
    """Non-blocking cross-process lock file. Yields True if acquired.

    A lock older than stale_after is assumed to be left by a crashed run.
    """
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) > stale_after.total_seconds():
            logger.warning(f"Removing stale lock file {path}")
            os.remove(path)
    except OSError:
        pass

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        yield False
        return

    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield True
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def refresh_schedule_and_confirm_speculative(headless=True):
    """Records schedule history and follows up on speculative registrations.

    Each user's listing is snapshotted every snapshot_interval_hours so the
    weekly pattern builds up even when no one is emailing. Speculative rows
    are rechecked every speculative_recheck_hours, and on every run for
    speculative_fast_poll_hours once their predicted registration time passes.
    Only one process runs this at a time; an overlapping run skips it.
    """
    stale_after = timedelta(minutes=APP_CONFIG["speculative_claim_stale_minutes"])
    with _exclusive_lock(REFRESH_LOCK_FILE, stale_after) as acquired:
        if not acquired:
            logger.info("Another run is refreshing speculative registrations; skipping.")
            return
        _refresh_schedule_and_confirm_speculative(headless)


def _refresh_schedule_and_confirm_speculative(headless):
    logger.info("Refreshing schedule history and checking speculative registrations.")
    now = datetime.now()
    events = Events()
    snapshot_interval = timedelta(hours=APP_CONFIG["snapshot_interval_hours"])
    recheck_interval = timedelta(hours=APP_CONFIG["speculative_recheck_hours"])
    fast_poll = timedelta(hours=APP_CONFIG["speculative_fast_poll_hours"])
    claim_stale = timedelta(minutes=APP_CONFIG["speculative_claim_stale_minutes"])

    # Release claims left behind by a run that died mid-registration.
    for row in events.get_speculative_events(status="in_progress"):
        if row["last_checked"] is None or now - row["last_checked"] >= claim_stale:
            logger.warning(
                f"Releasing stale claim on {row['event_date']} {row['time_range']} for '{row['user_tag']}'."
            )
            events.set_status(row["event_date"], row["time_range"], row["user_tag"], "speculative")

    due_rows = {}
    for row in events.get_speculative_events():
        since_opening = now - row["registration_time"]
        if (
            row["last_checked"] is None
            or timedelta(0) <= since_opening <= fast_poll
            or now - row["last_checked"] >= recheck_interval
        ):
            due_rows.setdefault(row["user_tag"], []).append(row)

    stale_users = set()
    for tag in list_user_tags():
        # Back off on attempts, not just successes, so a failing login isn't
        # retried every run.
        last = events.get_last_snapshot_attempt(tag)
        if last is None or now - last >= snapshot_interval:
            stale_users.add(tag)

    user_tags = sorted(stale_users | set(due_rows))
    if not user_tags:
        logger.info("No snapshots or speculative checks due.")
        events.close()
        return

    email_client = EmailClient() if due_rows else None

    for tag in user_tags:
        website = None
        try:
            if tag in stale_users:
                events.record_snapshot_attempt(tag, now)
            website = Website(headless=headless)
            website.login(user_tag=tag)
            website.display_all_events()
            _record_listing(events, website, tag, now)

            for row in due_rows.get(tag, []):
                try:
                    _check_speculative_row(events, website, email_client, row, now, headless)
                except Exception:
                    logger.exception(
                        f"Failed to check speculative event {row['event_date']} {row['time_range']} for '{tag}'"
                    )
        except Exception:
            logger.exception(f"Failed to refresh schedule for user '{tag}'")
        finally:
            if website is not None:
                try:
                    website.close()
                except Exception as close_error:
                    logger.warning(f"Failed to close website for user '{tag}': {close_error}")

    events.remove_old_observations(n_days=APP_CONFIG["observed_retention_days"])
    events.close()


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

    # Runs last so a slow scan never delays an imminent hold.
    try:
        refresh_schedule_and_confirm_speculative(headless=headless)
    except Exception as e:
        logger.error(f"Speculative refresh failed: {e}", exc_info=True)


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

import textile
from tabulate import tabulate
from events import Events
from website import Website
from dwell import dwell_until, is_within_offset
from email_client import EmailClient
from user_intent import extract_user_intent
from user_config import extract_user_tag, validate_user_tag, is_sender_allowed
from logging_config import get_logger
import os
import threading
import concurrent.futures

if os.name == "nt":
    headless = False
elif os.name == "posix":
    headless = True

logger = get_logger(__name__)

HOLD_BUFFER = 10  # minutes
LOGIN_BUFFER = 1  # minutes
DELAY = 0  # seconds

cleanup_days = 8  # days to keep events in the database


def register_for_single_event(event_info, headless=True, results=None, results_lock=None):
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

    logger.info(f"Registering event for user '{user_tag}': {event_date} {time_range} at {registration_time}")
    
    try:
        website = Website(headless=headless)
        website.login(user_tag=user_tag)
        event_url = website.get_event_url(event_date, time_range)
        
        logger.info(f"Waiting until exact registration time for user '{user_tag}'")
        dwell_until(registration_time, offset_seconds=-DELAY)
        
        logger.info(f"Registering for event (user '{user_tag}'): {event_date} {time_range}")
        website.register_for_event(event_date=event_date, time_range=time_range, event_url=event_url)
        
        logger.info(f"Closing website for user '{user_tag}'")
        website.close()
        
        logger.info(f"Successfully registered user '{user_tag}' for {event_date} {time_range}")
        _record_result({"user_tag": user_tag, "event": f"{event_date} {time_range}", "success": True})
    except Exception as e:
        logger.error(f"Error registering user '{user_tag}' for {event_date} {time_range}: {e}", exc_info=True)
        _record_result({"user_tag": user_tag, "event": f"{event_date} {time_range}", "success": False, "error": str(e)})


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
    logger.info(f"Found {len(next_events)} event(s) at registration time: {registration_time}")
    
    for event in next_events:
        logger.info(f"  - User '{event['user_tag']}': {event['event_date']} {event['time_range']}")

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
        logger.info(f"Submitting {len(next_events)} events to thread pool (max_workers={max_workers}).")
        results_lock = threading.Lock()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(register_for_single_event, event, headless, results, results_lock)
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
        logger.info(f"Registration complete: {len(succeeded)} succeeded, {len(failed)} failed.")
    if failed:
        try:
            notifier = EmailClient()
            for f in failed:
                logger.error(f"FAILED: user '{f['user_tag']}' for {f['event']}: {f['error']}")
                notifier.send_notification(
                    subject=f"Registration Failed: {f['event']}",
                    body=f"Failed to register user '{f['user_tag']}' for {f['event']}.\n\nError: {f['error']}",
                    user_tag=f["user_tag"],
                )
        except Exception as e:
            logger.error(f"Failed to send failure notifications: {e}", exc_info=True)

    logger.info("Removing old events from the database.")
    events.remove_old_events(n_days=cleanup_days)
    events.close()


def check_for_new_event(headless=True):
    logger.info("Checking for new events via email.")
    email_client = EmailClient()
    email_client.authenticate_email()
    new_emails = email_client.read_new_emails()

    if not new_emails:
        logger.info("No new emails found.")
        return

    websites = {}  # Per-user Website instances keyed by user_tag
    events = Events()

    for email in new_emails:
        # LAYER 1: Global authorization - sender must be in Google Contacts
        # This is a first-pass filter to reject unknown senders before any processing
        if email_client.is_sender_authorized(email.From):
            logger.info(f"Authorized sender (in contacts): {email.From}")
        else:
            logger.info(f"Unauthorized sender (not in contacts): {email.From}")
            email_client.mark_email_as_read(email)
            email_client.delete_email(email)

            continue
        
        # Extract user tag from the To address (filter by system email to avoid mismatches)
        try:
            user_tag = extract_user_tag(email.To, system_email=email_client.user)
        except ValueError as e:
            # Missing system_email or other extraction error - treat as security event
            logger.error(f"Failed to extract user tag: {e}")
            email_client.mark_email_as_read(email)
            email_client.delete_email(email)
            continue
            
        logger.info(f"Processing email for user tag: {user_tag}")

        # Validate user tag exists and is properly configured
        try:
            user_tag = validate_user_tag(user_tag)
        except (ValueError, FileNotFoundError) as e:
            logger.warning(f"Invalid user tag '{user_tag}': {e}")
            # Silent delete to prevent user enumeration via response timing
            # (Same behavior as unauthorized access)
            email_client.mark_email_as_read(email)
            email_client.delete_email(email)
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
            email_client.delete_email(email)
            continue

        action, event_details = extract_user_intent(email)
        event_date, time_range = event_details or (None, None)

        if action == "report":
            logger.info(f"Reporting events for user '{user_tag}'.")
            event_list = events.list_all_events(user_tag=user_tag)
            # Omit user_tag column (last element) from each row for privacy
            event_list = [row[:-1] for row in event_list]

            headers = ["event date", "time range", "registration time", "additional info"]
            reply = tabulate(event_list, headers=headers)
            reply_html = tabulate(event_list, headers=headers, tablefmt="html")

            email_client.reply_to_email(
                email, reply_plaintext=reply, reply_html=reply_html, user_tag=user_tag
            )

        elif action == "add":
            logger.info(f"Adding event for user '{user_tag}': {event_date, time_range}")
            if user_tag not in websites:
                websites[user_tag] = Website(headless=headless)
            website = websites[user_tag]
            website.login(user_tag=user_tag)
            registration_time, additional_info = website.determine_access_date(
                event_date, time_range
            )

            if registration_time is None:
                logger.info(
                    f"Could not determine the registration time for {event_date, time_range}."
                )
                reply = "I could not determine the registration time."
                if additional_info:
                    reply += f"\n\nI found this info on the page (check if you are in an eligible tier): {additional_info}"
                
                email_client.reply_to_email(
                    email, reply, user_tag=user_tag
                )
            else:
                logger.debug(
                    f"Inserting {event_date, time_range} into database at {registration_time} for user '{user_tag}'"
                )
                old_events = events.get_events_by_date(registration_time, user_tag=user_tag)
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
            logger.info(f"Removing event for user '{user_tag}': {event_date, time_range}")
            events.remove_event(event_date, time_range, user_tag=user_tag)
            email_client.reply_to_email(
                email, "I am not going to register for the event.",
                subject=f"Event Registration Cancellation: {event_date} {time_range}",
                user_tag=user_tag,
            )

        elif action is None:
            logger.info("Could not determine the action from the email.")
            email_client.reply_to_email(email, "I am not sure what you want me to do.", user_tag=user_tag)

        email_client.mark_email_as_read(email)
        email_client.archive_email(email)

    logger.info("Closing website and database connections.")
    for tag, website in websites.items():
        try:
            website.close()
        except Exception as e:
            logger.error(f"Error closing website for user '{tag}': {e}")
    events.close()


if __name__ == "__main__":

    try:
        check_for_new_event(headless=headless)
    except Exception as e:
        logger.error(f"An error occurred: {e}")

    register_for_next_event(headless=headless)

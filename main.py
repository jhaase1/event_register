import textile
from tabulate import tabulate
from events import Events
from website import Website
from dwell import dwell_until, is_within_offset
from email_client import EmailClient
from user_intent import extract_user_intent
from user_config import extract_user_tag, validate_user_tag
from logging_config import get_logger
import os
import threading

if os.name == "nt":
    headless = False
elif os.name == "posix":
    headless = True

logger = get_logger(__name__)

HOLD_BUFFER = 10  # minutes
LOGIN_BUFFER = 1  # minutes
DELAY = 0  # seconds

cleanup_days = 8  # days to keep events in the database


def register_for_single_event(event_info, headless=True):
    """Register for a single event (used for concurrent registrations)."""
    event_date = event_info["event_date"]
    time_range = event_info["time_range"]
    registration_time = event_info["registration_time"]
    user_tag = event_info["user_tag"]
    
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
    except Exception as e:
        logger.error(f"Error registering user '{user_tag}' for {event_date} {time_range}: {e}", exc_info=True)


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
    
    # If multiple events, spawn threads for concurrent registration
    if len(next_events) > 1:
        logger.info(f"Spawning {len(next_events)} threads for concurrent registration.")
        threads = []
        for event in next_events:
            thread = threading.Thread(target=register_for_single_event, args=(event, headless))
            threads.append(thread)
            thread.start()
        
        # Wait for all threads to complete
        for thread in threads:
            thread.join()
        logger.info("All concurrent registrations completed.")
    else:
        # Single event, no threading needed
        register_for_single_event(next_events[0], headless=headless)

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

    website = Website(headless=headless)
    events = Events()

    for email in new_emails:
        if email_client.is_sender_authorized(email.From):
            logger.info(f"Authorized sender: {email.From}")
        else:
            logger.info(f"Unauthorized sender: {email.From}")
            email_client.mark_email_as_read(email)
            email_client.delete_email(email)

            continue
        
        # Extract user tag from the To address
        user_tag = extract_user_tag(email.To)
        logger.info(f"Processing email for user tag: {user_tag}")

        # Validate user tag exists and is properly configured
        try:
            validate_user_tag(user_tag)
        except (ValueError, FileNotFoundError) as e:
            logger.warning(f"Invalid user tag '{user_tag}': {e}")
            email_client.reply_to_email(
                email, f"Sorry, the user '{user_tag}' is not registered in this system."
            )
            email_client.mark_email_as_read(email)
            email_client.archive_email(email)
            continue

        action, event_details = extract_user_intent(email)
        event_date, time_range = event_details or (None, None)

        if action == "report":
            logger.info(f"Reporting events for user '{user_tag}'.")
            event_list = events.list_all_events(user_tag=user_tag)

            headers = ["event date", "time range", "registration time", "additional info", "user tag"]
            reply = tabulate(event_list, headers=headers)
            reply_html = tabulate(event_list, headers=headers, tablefmt="html")

            email_client.reply_to_email(
                email, reply_plaintext=reply, reply_html=reply_html
            )

        elif action == "add":
            logger.info(f"Adding event for user '{user_tag}': {event_date, time_range}")
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
                    email, reply
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
                )

                logger.info(
                    f"Inserted and emailed {event_date} {time_range} into database at {registration_time} for user '{user_tag}' with additional info: {additional_info}"
                )

        elif action == "remove":
            logger.info(f"Removing event for user '{user_tag}': {event_date, time_range}")
            events.remove_event(event_date, time_range, user_tag=user_tag)
            email_client.reply_to_email(
                email, "I am not going to register for the event.",
                subject=f"Event Registration Cancellation: {event_date} {time_range}"
            )

        elif action is None:
            logger.info("Could not determine the action from the email.")
            email_client.reply_to_email(email, "I am not sure what you want me to do.")

        email_client.mark_email_as_read(email)
        email_client.archive_email(email)

    logger.info("Closing website and database connections.")
    website.close()
    events.close()


if __name__ == "__main__":

    try:
        check_for_new_event(headless=headless)
    except Exception as e:
        logger.error(f"An error occurred: {e}")

    register_for_next_event(headless=headless)

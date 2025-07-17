import textile
from tabulate import tabulate
from events import Events
from website import Website
from dwell import dwell_until, is_within_offset
from email_client import EmailClient
from user_intent import extract_user_intent
from logging_config import get_logger
import os

if os.name == "nt":
    headless = False
elif os.name == "posix":
    headless = True

logger = get_logger(__name__)

HOLD_BUFFER = 10  # minutes
LOGIN_BUFFER = 1  # minutes
DELAY = 0  # seconds

cleanup_days = 8  # days to keep events in the database


def register_for_next_event(headless=True):
    logger.info("Starting registration process for the next event.")
    # Connect to the database
    events = Events()
    next_event = events.get_next_event_after()

    if next_event:
        event_date = next_event["event_date"]
        time_range = next_event["time_range"]
        registration_time = next_event["registration_time"]
        logger.info(f"Next event found: {event_date} {time_range} at {registration_time}")
    else:
        logger.info("No upcoming events.")
        events.close()
        return

    if is_within_offset(registration_time, offset_minutes=HOLD_BUFFER):
        logger.info("Holding until registration time.")
        dwell_until(registration_time, offset_minutes=HOLD_BUFFER)
    else:
        logger.info("Registration time is too far away.")
        events.close()
        return

    website = Website(headless=headless)

    logger.info("Logging in to the website.")
    dwell_until(registration_time, offset_minutes=LOGIN_BUFFER)

    website.login()
    event_url = website.get_event_url(event_date, time_range)

    logger.info("Waiting until the exact registration time.")
    dwell_until(registration_time, offset_seconds=-DELAY)

    logger.info(f"Registering for the event: {event_date} {time_range}")
    website.register_for_event(event_date=event_date, time_range=time_range, event_url=event_url)

    logger.info("Closing website and database connections.")
    website.close()

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

        action, event_details = extract_user_intent(email)
        event_date, time_range = event_details or (None, None)

        if action == "report":
            logger.info("Reporting the event.")
            event_list = events.list_all_events()

            headers = ["event date", "time range", "registration time", "additional info"]
            reply = tabulate(event_list, headers=headers)
            reply_html = tabulate(event_list, headers=headers, tablefmt="html")

            email_client.reply_to_email(
                email, reply_plaintext=reply, reply_html=reply_html
            )

        elif action == "add":
            logger.info(f"Adding event: {event_date, time_range}")
            website.login()
            registration_time, additional_info = website.determine_access_date(
                event_date, time_range
            )

            if registration_time is None:
                logger.info(
                    f"Could not determine the registration time for {event_date, time_range}."
                )
                email_client.reply_to_email(
                    email, "I could not determine the registration time."
                )
            else:
                logger.debug(
                    f"Inserting {event_date, time_range} into database at {registration_time}"
                )
                old_events = events.get_events_by_date(registration_time)
                if old_events:
                    logger.info(
                        f"Event already exists for this date: {old_events}. Removing old event."
                    )

                    for old_event in old_events:
                        events.remove_event(*old_event)
                events.insert_event(
                    event_date=event_date,
                    time_range=time_range,
                    registration_time=registration_time,
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
                    f"Inserted and emailed {event_date} {time_range} into database at {registration_time} with additional info: {additional_info}"
                )

        elif action == "remove":
            logger.info(f"Removing event: {event_date, time_range}")
            events.remove_event(event_date, time_range)
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

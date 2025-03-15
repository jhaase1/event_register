from events import Events
from website import Website
from dwell import dwell_until, is_within_offset
from email_client import EmailClient
from user_intent import extract_user_intent
from logging_config import get_logger

logger = get_logger(__name__)

HOLD_BUFFER = 10 # minutes
LOGIN_BUFFER = 1 # minutes
DELAY = 5 # seconds

def register_for_next_event():
    logger.info("Starting registration process for the next event.")
    # Connect to the database
    events = Events()
    next_event = events.get_next_event_after()

    if next_event:
        event_url = next_event["event_url"]
        registration_time = next_event["registration_time"]
        logger.info(f"Next event found: {event_url} at {registration_time}")
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
    
    website = Website()

    logger.info("Logging in to the website.")
    dwell_until(registration_time, offset_minutes=LOGIN_BUFFER)
    
    website.login()

    logger.info("Waiting until the exact registration time.")
    dwell_until(registration_time, offset_seconds=-DELAY)

    logger.info(f"Registering for the event: {event_url}")
    website.register_for_event(event_url)

    logger.info("Closing website and database connections.")
    website.close()
    events.close()

def check_for_new_event():
    logger.info("Checking for new events via email.")
    email_client = EmailClient()
    email_client.authenticate_email()
    new_emails = email_client.read_new_emails()

    if not new_emails:
        logger.info("No new emails found.")
        return
    
    website = Website()
    events = Events()

    for email in new_emails:
        if email_client.is_sender_authorized(email.From):
            logger.info(f"Authorized sender: {email.From}")
        else:
            logger.info(f"Unauthorized sender: {email.From}")
            email_client.mark_email_as_read(email)
            email_client.delete_email(email)

            continue

        action, event_url = extract_user_intent(email)

        if action == "add":
            logger.info(f"Adding event: {event_url}")
            website.login()
            registration_time = website.determine_access_date(event_url)

            if registration_time is None:
                email_client.reply_to_email(email, "I could not determine the registration time.")
            else:
                events.insert_event(event_url=event_url, registration_time=registration_time)
                email_client.reply_to_email(email, f"I determined I need to register at {registration_time} and will do so.")
        
        if action == "remove":
            logger.info(f"Removing event: {event_url}")
            events.remove_event(event_url)
            email_client.reply_to_email(email, "I am not going to register for the event.")

        if action is None:
            logger.info("Could not determine the action from the email.")
            email_client.reply_to_email(email, "I am not sure what you want me to do.")
        
        email_client.mark_email_as_read(email)
        email_client.archive_email(email)

    logger.info("Closing website and database connections.")
    website.close()
    events.close()

if __name__ == "__main__":
    check_for_new_event()
    register_for_next_event()

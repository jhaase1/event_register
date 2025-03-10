from events import Events
from website import Website
from dwell import dwell_until, is_within_offset
from email_client import EmailClient
from user_intent import extract_user_intent

HOLD_BUFFER = 10 # minutes
LOGIN_BUFFER = 1 # minutes

def register_for_next_event():
    print("Starting registration process for the next event.")
    # Connect to the database
    events = Events()
    next_event = events.get_next_event_after()

    if next_event:
        event_url = next_event["event_url"]
        registration_time = next_event["registration_time"]
        print(f"Next event found: {event_url} at {registration_time}")
    else:
        print("No upcoming events.")
        events.close()
        return
    
    if is_within_offset(registration_time, offset_minutes=HOLD_BUFFER):
        print("Holding until registration time.")
        dwell_until(registration_time, offset_minutes=HOLD_BUFFER)
    else:
        print("Registration time is too far away.")
        events.close()
        return
    
    website = Website()

    print("Logging in to the website.")
    dwell_until(registration_time, offset_minutes=LOGIN_BUFFER)
    
    website.login()

    print("Waiting until the exact registration time.")
    dwell_until(registration_time)

    print(f"Registering for the event: {event_url}")
    website.register_for_event(event_url)

    print("Closing website and database connections.")
    website.close()
    events.close()

def check_for_new_event():
    print("Checking for new events via email.")
    email_client = EmailClient()
    email_client.authenticate_email()
    new_emails = email_client.read_new_emails()

    if not new_emails:
        print("No new emails found.")
        return
    
    website = Website()
    events = Events()

    for email in new_emails:
        if email_client.is_sender_authorized(email.From):
            print(f"Authorized sender: {email.From}")
        else:
            print(f"Unauthorized sender: {email.From}")
            email_client.mark_email_as_read(email)
            email_client.delete_email(email)

            continue

        action, event_url = extract_user_intent(email)

        if action == "add":
            print(f"Adding event: {event_url}")
            website.login()
            registration_time = website.determine_access_date(event_url)

            if registration_time is None:
                email_client.reply_to_email(email, "I could not determine the registration time.")
            else:
                events.insert_event(event_url=event_url, registration_time=registration_time)
                email_client.reply_to_email(email, f"I determined I need to register at {registration_time} and will do so.")
        
        if action == "remove":
            print(f"Removing event: {event_url}")
            events.remove_event(event_url)
            email_client.reply_to_email(email, "I am not going to register for the event.")

        if action is None:
            print("Could not determine the action from the email.")
            email_client.reply_to_email(email, "I am not sure what you want me to do.")
        
        email_client.mark_email_as_read(email)
        email_client.archive_email(email)

    print("Closing website and database connections.")
    website.close()
    events.close()

if __name__ == "__main__":
    check_for_new_event()
    register_for_next_event()

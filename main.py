from events import Events
from website import Website
from dwell import dwell_until, is_within_offset
from email_client import EmailClient

HOLD_BUFFER = 10 # minutes
LOGIN_BUFFER = 1 # minutes

def register_for_next_event():
    # Connect to the database
    events = Events()
    next_event = events.get_next_event_after()

    if next_event:
        event_url = next_event["event_url"]
        registration_time = next_event["registration_time"]
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

    dwell_until(registration_time, offset_minutes=LOGIN_BUFFER)
    
    website.login()

    dwell_until(registration_time)

    website.register_for_event(event_url)

    website.close()
    events.close()

def check_for_new_event():
    email_client = EmailClient()
    email_client.authenticate_email()
    new_emails = email_client.read_new_emails()

    if new_emails:
        for email in new_emails:
            if email_client.is_sender_authorized(email.sender_email):
                print(f"Authorized sender: {email.sender_email}")
                register_for_next_event()
            else:
                print(f"Unauthorized sender: {email.sender_email}")
    events = Events()
    next_event = events.get_next_event_after()

    if not next_event:
        print("No upcoming events.")
        events.close()
        return
    
    registration_time = next_event["registration_time"]
    if is_within_offset(registration_time, offset_minutes=HOLD_BUFFER):
        print("Holding until registration time.")
        dwell_until(registration_time, offset_minutes=HOLD_BUFFER)
    else:
        print("Registration time is too far away.")
        events.close()
        return
    
    website = Website()

    dwell_until(registration_time, offset_minutes=LOGIN_BUFFER)
    
    website.login()

    dwell_until(registration_time)

    website.register_for_event(event_url)

    website.close()
    events.close()

if __name__ == "__main__":
    register_for_next_event()

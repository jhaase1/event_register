import os.path
import base64
import re
from types import SimpleNamespace
from email.message import EmailMessage
import email

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError

from logging_config import get_logger

EMAIL_CAPTURE_PATTERN = re.compile(r"^(.*?)?(?:<(.+@.+)>)?$")

# Initialize logger
logger = get_logger(__name__)
logger.setLevel("DEBUG")  # Set logger to debug level for detailed output

class EmailClient:
    def __init__(self):
        logger.info("Initializing EmailClient...")
        self.creds = None
        self.authenticate_email()
        self.whoami()

    def authenticate_email(self, token_file="email_token.json"):
        """Shows basic usage of the Gmail API. Lists the user's Gmail labels."""
        logger.info("Authenticating email...")
        SCOPES = [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/contacts.readonly",
        ]
        logger.debug(f"Scopes: {SCOPES}")

        if os.path.exists(token_file):
            logger.info(f"Loading credentials from {token_file}...")
            try:
                self.creds = Credentials.from_authorized_user_file(token_file, SCOPES)
            except RefreshError:
                logger.warning("Token is invalid or revoked. Deleting token file and reauthenticating...")
                os.remove(token_file)
                self.creds = None

        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                try:
                    logger.info("Refreshing expired credentials...")
                    self.creds.refresh(Request())
                except RefreshError:
                    logger.warning("Failed to refresh credentials. Deleting token file and reauthenticating...")
                    os.remove(token_file)
                    self.creds = None
            if not self.creds:
                logger.info("Fetching new credentials...")
                flow = InstalledAppFlow.from_client_secrets_file(
                    "credentials.json", SCOPES
                )
                self.creds = flow.run_local_server(port=0)
            with open(token_file, "w") as token:
                token.write(self.creds.to_json())
                logger.debug(f"Credentials saved to {token_file}.")

    def whoami(self):
        """Returns the email address of the authenticated user."""
        logger.info("Fetching authenticated user's email address...")
        service = build('gmail', 'v1', credentials=self.creds)
        self.user = service.users().getProfile(userId='me').execute().get('emailAddress')
        logger.info(f"Authenticated as {self.user}")
        logger.debug(f"Authenticated user email: {self.user}")

    def is_sender_authorized(self, sender_email, auth_label="Scheduler"):
        """Checks if the sender is a contact with the label 'Scheduler'."""
        logger.info(f"Checking if sender {sender_email} is authorized...")
        logger.debug(f"Sender email: {sender_email}")
        if not self.creds:
            self.authenticate_email()

        if isinstance(sender_email, list):
            assert len(sender_email) == 1, "Only one sender email is allowed."
            sender_email = sender_email[0]

        try:
            service = build("people", "v1", credentials=self.creds)
            results = (
                service.people()
                .connections()
                .list(resourceName="people/me", personFields="emailAddresses,metadata")
                .execute()
            )
            connections = results.get("connections", [])

            for person in connections:
                email_addresses = person.get("emailAddresses", [])
                for email_object in email_addresses:
                    email = email_object.get("value", "")
                    if email.lower() == sender_email.lower():
                        logger.info(f"Sender {sender_email} is authorized.")
                        return True
            logger.info(f"Sender {sender_email} is not authorized.")
            return False

        except HttpError as error:
            logger.info(f"An error occurred: {error}")
            return False

    def read_new_emails(self, raw_email=False):
        """Reads new unread emails from the user's Gmail inbox."""
        logger.info("Reading new unread emails...")
        logger.debug(f"Raw email flag: {raw_email}")
        if not self.creds:
            self.authenticate_email()

        try:
            service = build("gmail", "v1", credentials=self.creds)
            results = (
                service.users()
                .messages()
                .list(userId="me", labelIds=["INBOX"], q="is:unread")
                .execute()
            )
            messages = results.get("messages", [])

            if not messages:
                logger.info("No new emails found.")
                return
           
            msgs = []

            for message in messages:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=message["id"], format="raw")
                    .execute()
                )

                if raw_email:
                    msgs.append(msg)
                    continue

                msg_str = base64.urlsafe_b64decode(msg["raw"]).decode("utf-8")
                email_message = email.message_from_string(msg_str)

                subject = email_message["Subject"]
                message_id = email_message["Message-ID"]

                address_fields = {
                    field: self.extract_email_address(
                        email_message.get(field, "")
                    ) for field in ["To", "From", "Cc"]
                }
                
                body = ""
                if email_message.is_multipart():
                    for part in email_message.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode("utf-8")
                            break
                else:
                    body = email_message.get_payload(decode=True).decode("utf-8")

                id = msg["id"]
                thread_id = msg["threadId"]

                msg_namespace = SimpleNamespace(
                    **address_fields,
                    subject=subject,
                    body=body,
                    id=id,
                    thread_id=thread_id,
                    message_id=message_id,
                )
                msgs.append(msg_namespace)

            logger.info(f"Found {len(msgs)} new emails.")
            logger.debug(f"Email details: {msgs}")
            return msgs

        except HttpError as error:
            logger.info(f"An error occurred: {error}")

    def mark_email_as_read(self, email):
        """Marks an email as read."""
        logger.info(f"Marking email with ID {email.id} as read...")
        logger.debug(f"Email ID: {email.id}")
        if not self.creds:
            self.authenticate_email()

        try:
            service = build("gmail", "v1", credentials=self.creds)
            service.users().messages().modify(
                userId="me", id=email.id, body={"removeLabelIds": ["UNREAD"]}
            ).execute()
            logger.info(f"Email with ID {email.id} marked as read.")

        except HttpError as error:
            logger.info(f"An error occurred: {error}")

    def archive_email(self, email):
        """Archives an email."""
        logger.info(f"Archiving email with ID {email.id}...")
        logger.debug(f"Email ID: {email.id}")
        if not self.creds:
            self.authenticate_email()

        try:
            service = build("gmail", "v1", credentials=self.creds)
            service.users().messages().modify(
                userId="me", id=email.id, body={"removeLabelIds": ["INBOX"]}
            ).execute()
            logger.info(f"Email with ID {email.id} archived.")

        except HttpError as error:
            logger.info(f"An error occurred: {error}")

    def delete_email(self, email):
        """Deletes an email."""
        logger.info(f"Deleting email with ID {email.id}...")
        logger.debug(f"Email ID: {email.id}")
        if not self.creds:
            self.authenticate_email()

        try:
            service = build("gmail", "v1", credentials=self.creds)
            service.users().messages().delete(userId="me", id=email.id).execute()
            logger.info(f"Email with ID {email.id} deleted.")

        except HttpError as error:
            logger.info(f"An error occurred: {error}")

    def reply_to_email(self, email, reply_plaintext, reply_html=None, subject=None):
        """Replies to an email."""
        logger.info(f"Replying to email with ID {email.id}...")
        logger.debug(f"Reply content: {reply_plaintext}")
        if not self.creds:
            self.authenticate_email()

        try:
            service = build("gmail", "v1", credentials=self.creds)

            message = EmailMessage()
            message.set_content(reply_plaintext)

            if reply_html:
                message.add_alternative(reply_html, subtype="html")


            me = self.user.lower()

            logger.debug(f"Replying to email as: {me}")

            message['To'] = ", ".join([address for address in email.To + email.From if address.lower() != me])
            message['From'] = me
            message['Cc'] = ", ".join([address for address in email.Cc if address.lower() != me])
            message['Subject'] = subject or email.subject
            message['References'] = email.message_id
            message['In-Reply-To'] = email.message_id

            logger.debug(f"Message headers: {message.items()}")

            encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()

            create_message = {"raw": encoded_message, "threadId": email.thread_id}

            # pylint: disable=E1101
            send_message = (
                service.users()
                .messages()
                .send(userId="me", body=create_message)
                .execute()
            )

            logger.info(f"Replied to email with ID {email.id}.")
            return send_message
        except HttpError as error:
            logger.info(f"An error occurred: {error}")

    @staticmethod
    def extract_email_address(emails):
        """Extracts the email address from the sender's email."""
        return re.findall(r'[\w\.-]+@[\w\.-]+\b', emails or '')
        

# Example usage:
# receiver = GmailEventReceiver()
# new_emails = receiver.read_new_emails()
# print(new_emails)

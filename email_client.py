import os.path
import base64
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


class EmailClient:
    def __init__(self):
        self.creds = None
        self.authenticate_email()

    def authenticate_email(self, token_file="email_token.json"):
        """Shows basic usage of the Gmail API. Lists the user's Gmail labels."""

        SCOPES = [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/contacts.readonly",
        ]

        if os.path.exists(token_file):
            self.creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    "credentials.json", SCOPES
                )
                self.creds = flow.run_local_server(port=0)
            with open(token_file, "w") as token:
                token.write(self.creds.to_json())

    def is_sender_authorized(self, sender_email, auth_label="Scheduler"):
        """Checks if the sender is a contact with the label 'Scheduler'."""
        if not self.creds:
            self.authenticate_email()

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
                        return True
            return False

        except HttpError as error:
            print(f"An error occurred: {error}")
            return False

    def read_new_emails(self):
        """Reads new unread emails from the user's Gmail inbox."""
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
                print("No new emails found.")
                return

            msgs = []

            for message in messages:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=message["id"])
                    .execute()
                )
                payload = msg["payload"]
                headers = payload.get("headers", [])

                sender = None
                subject = None
                for header in headers:
                    if header["name"] == "From":
                        sender = self.extract_email_address(header["value"])
                    if header["name"] == "Subject":
                        subject = header["value"]

                parts = payload.get("parts", [])
                body = ""
                for part in parts:
                    if part["mimeType"] == "text/plain":
                        body = base64.urlsafe_b64decode(part["body"]["data"]).decode(
                            "utf-8"
                        )

                if sender and self.is_sender_authorized(sender):
                    msgs.append(
                        {
                            "sender": sender,
                            "subject": subject,
                            "body": body,
                            "id": message["id"],
                        }
                    )

            return msgs

        except HttpError as error:
            print(f"An error occurred: {error}")

    def mark_email_as_read(self, email_id):
        """Marks an email as read."""
        if not self.creds:
            self.authenticate_email()

        try:
            service = build("gmail", "v1", credentials=self.creds)
            service.users().messages().modify(
                userId="me", id=email_id, body={"removeLabelIds": ["UNREAD"]}
            ).execute()
            print(f"Email with ID {email_id} marked as read.")

        except HttpError as error:
            print(f"An error occurred: {error}")

    def archive_email(self, email_id):
        """Archives an email."""
        if not self.creds:
            self.authenticate_email()

        try:
            service = build("gmail", "v1", credentials=self.creds)
            service.users().messages().modify(
                userId="me", id=email_id, body={"removeLabelIds": ["INBOX"]}
            ).execute()
            print(f"Email with ID {email_id} archived.")

        except HttpError as error:
            print(f"An error occurred: {error}")

    def delete_email(self, email_id):
        """Deletes an email."""
        if not self.creds:
            self.authenticate_email()

        try:
            service = build("gmail", "v1", credentials=self.creds)
            service.users().messages().delete(userId="me", id=email_id).execute()
            print(f"Email with ID {email_id} deleted.")

        except HttpError as error:
            print(f"An error occurred: {error}")

    @staticmethod
    def extract_email_address(sender):
        """Extracts the email address from the sender's email."""
        match = re.match("^.*<(.+@.+)>$", sender)
        if match:
            return match.group(1)
        return None


# Example usage:
# receiver = GmailEventReceiver()
# new_emails = receiver.read_new_emails()
# print(new_emails)

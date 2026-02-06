# Event Register

This project automates the process of registering for events on a specified website. It uses Selenium for web automation, SQLite for event storage, and Gmail API for email interactions. It supports **multiple users** through a single Gmail address using Gmail's plus-tag system (e.g., `base+user1@gmail.com`).

## Project Structure

- `main.py`: Main script to check for new events and register for them.
- `website.py`: Handles website interactions using Selenium.
- `user_intent.py`: Extracts user intent from emails.
- `events.py`: Manages event storage using SQLite (shared database with per-user isolation).
- `email_client.py`: Handles email interactions using Gmail API.
- `user_config.py`: User identification and validation utilities for multi-tenant support.
- `dwell.py`: Provides utility functions for time-based operations.
- `logging_config.py`: Centralized logging configuration.
- `user_tokens/`: Directory containing per-user website credential files.

## Multi-User Support

The system uses Gmail's **plus-tag** addressing to route emails to different user profiles:

- Emails sent to `base@gmail.com` → handled as the **default** user
- Emails sent to `base+alice@gmail.com` → handled as user **alice**
- Emails sent to `base+bob@gmail.com` → handled as user **bob**

All plus-tagged emails arrive in the same Gmail inbox. The system extracts the tag from the `To` address and routes it to the appropriate user's website credentials.

### Onboarding a New User

1. Create a JSON file in `user_tokens/` named after the user tag (e.g., `user_tokens/alice.json`):
    ```json
    {
        "login_url": "https://example.com/login",
        "events_url": "https://example.com/events",
        "email": "alice@example.com",
        "password": "alices-password",
        "default_registration_time": "14:00:00",
        "authorized_senders": ["alice@example.com", "delegate@example.com"]
    }
    ```
2. Authorized senders can now email `base+alice@gmail.com` to manage alice's events.

### Authorization Model

Every user (including the default user) must have explicit authorization configured:

- **`email`**: The website login email - this address is automatically authorized to send commands
- **`authorized_senders`**: Additional email addresses that can manage this user's events (e.g., delegates, family members)

If neither `email` nor `authorized_senders` is configured, all requests for that user will be denied (fail-closed security).

> **Security Notes:**
> - User token files contain plaintext credentials. The `user_tokens/` directory is gitignored by default.
> - User tags are case-insensitive (e.g., `Alice` and `alice` are treated the same).
> - Invalid or unauthorized requests are logged but do not reveal whether a user exists (prevents enumeration).
> - Never commit token files to version control.

## Setup

1. **Install Dependencies**:
    ```sh
    pip install selenium google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client tabulate textile
    ```

2. **Download WebDriver**:
    - Download the appropriate WebDriver for your browser (e.g., ChromeDriver for Chrome) and ensure it's in your system's PATH.

3. **Gmail API Setup**:
    - Follow the [Gmail API Python Quickstart](https://developers.google.com/gmail/api/quickstart/python) to enable the API and download `credentials.json`.

4. **Database Initialization**:
    - The SQLite database (`events.db`) will be created automatically when running the scripts.

5. **User Setup**:
    - Create `user_tokens/default.json` for the default user (see Onboarding above).
    - Create additional `user_tokens/<tag>.json` files for each plus-tagged user.

## Usage

1. **Check for New Events**:
    ```sh
    python main.py
    ```

2. **Register for Next Event**:
    - The script will automatically register for the next event based on the stored events in the database.

## Configuration

- **Email Authentication**:
    - The first run will prompt for Gmail authentication and save the token in `email_token.json`.

- **Website Credentials**:
    - Store per-user website login credentials in `user_tokens/<tag>.json`:
    ```json
    {
        "login_url": "https://example.com/login",
        "events_url": "https://example.com/events",
        "email": "user@example.com",
        "password": "securepassword123",
        "default_registration_time": "15:00:00"
    }
    ```

## Email Commands

Send an email to the system's Gmail address (with optional plus-tag for user routing):

- **Add event**: Include the event date and time range in the email body.
- **Remove event**: Include "stop", "cancel", or "remove" in the body along with the event details.
- **Report**: Use "report" in the subject to receive a list of scheduled events.

## Example

```python
# Example usage in main.py
if __name__ == "__main__":
    check_for_new_event()
    register_for_next_event()
```

## License

This project is licensed under the MIT License.

# Privacy policy
Privacy Policy

Last Updated: 2025-03-18

Welcome to Event Register. Your privacy is important to us. This Privacy Policy outlines the collection and use of your data in our app. Because this is a personal project I make no guarantees to data security. Use at your own risk. This app uses the GMail API and therefore requires you to authorize it's use.

If you have a concern please submit a GitHub issue. https://github.com/jhaase1/event_register/issues

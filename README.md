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

6. **Notebook Output Stripping (Recommended)**:
    - Install `nbstripout`:
    ```sh
    pip install --upgrade nbstripout
    ```
    - Enable the git filter for this repo (uses the committed `.gitattributes` rule):
    ```sh
    nbstripout --install --attributes .gitattributes
    ```
    - After this, notebook outputs are stripped from commits automatically while your local notebook can still keep outputs during your session.

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

- **Application Settings**:
    - Runtime timing and cleanup settings live in `app_config.json` at the repo root.
    - Current keys: `hold_buffer_minutes`, `login_buffer_minutes`, `min_delay_seconds`, `max_delay_seconds`, `cleanup_days`.
    - Speculative pre-registration keys (see below): `speculative_enabled`, `speculative_lookback_weeks`, `speculative_min_matches`, `speculative_max_weeks_ahead`, `speculative_find_grace_minutes`, `speculative_recheck_hours`, `snapshot_interval_hours`, `observed_retention_days`, `cron_interval_minutes`, `speculative_fast_poll_hours`, `speculative_claim_stale_minutes`.
    - `notify_webmaster_on_unlisted_request` (default `false`): also email the webmaster when a requested event isn't on the website.

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

### Speculative Pre-Registration

If you ask for a session that isn't on the website yet, the system checks whether the same weekday and time slot has been listed in recent weeks (at least `speculative_min_matches` times in the last `speculative_lookback_weeks` weeks). If so, it accepts the request as **speculative**:

- It predicts when registration opens from how far ahead past sessions opened. Set `"registration_lead_days"` in a user's token file to override the learned value.
- At the predicted time it keeps reloading the list for up to `speculative_find_grace_minutes` in case the session is posted the moment registration opens.
- It rechecks the website every `speculative_recheck_hours`, and on every run for `speculative_fast_poll_hours` after the predicted time. Once the session is posted, it confirms the registration time in your original email thread. If registration opens before the next cron run (`cron_interval_minutes`), it registers in the current run, waiting for the real opening time.
- If a later week of the same slot is posted first, or the session date arrives and registration time passes without it being posted, it assumes the session was cancelled, drops the request, and tells you.
- A registration attempt that fails with a real error marks the request `failed` and tells you and the webmaster. It is not retried automatically.

The schedule history is built by scanning each user's events list every `snapshot_interval_hours`. A failed scan also waits that long before it's retried. There is no backfill, so for the first few weeks matches come mostly from sessions currently listed. Token files must have lowercase names (for example `user_tokens/alice.json`) to be scanned.

The recheck step uses a `refresh.lock` file so overlapping cron runs don't run it twice; a lock older than `speculative_claim_stale_minutes` is treated as left over from a crashed run.

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

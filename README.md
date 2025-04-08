# Event Register

This project automates the process of registering for events on a specified website. It uses Selenium for web automation, SQLite for event storage, and Gmail API for email interactions.

## Project Structure

- `website.py`: Handles website interactions using Selenium.
- `user_intent.py`: Extracts user intent from emails.
- `main.py`: Main script to check for new events and register for them.
- `events.py`: Manages event storage using SQLite.
- `email_client.py`: Handles email interactions using Gmail API.
- `dwell.py`: Provides utility functions for time-based operations.

## Setup

1. **Install Dependencies**:
    ```sh
    pip install selenium google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client tabulate
    ```

2. **Download WebDriver**:
    - Download the appropriate WebDriver for your browser (e.g., ChromeDriver for Chrome) and ensure it's in your system's PATH.

3. **Gmail API Setup**:
    - Follow the [Gmail API Python Quickstart](https://developers.google.com/gmail/api/quickstart/python) to enable the API and download `credentials.json`.

4. **Database Initialization**:
    - The SQLite database (`events.db`) will be created automatically when running the scripts.

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
    - Store website login credentials in `website_token.json`:
    ```json
    {
        "login_url": "https://example.com/login",
        "email": "user@example.com",
        "password": "securepassword123",
        "default_registration_time": "15:00:00"
    }
    ```

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

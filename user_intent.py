import re
from logging_config import get_logger

logger = get_logger(__name__)
logger.setLevel("DEBUG")


def _strip_quoted_reply_text(body):
    """Return only the newest user-authored body content from reply threads."""
    if not body:
        return ""

    # Common Gmail reply delimiter.
    newest = re.split(r"\nOn .+ wrote:\n", body, maxsplit=1)[0]
    # Drop quoted lines that begin with '>'.
    lines = [line for line in newest.splitlines() if not line.lstrip().startswith(">")]
    return "\n".join(lines).strip()

def extract_user_intent(email, assumed_action="add"):
    """
    Extracts the user's intent from an email.

    Args:
        email: The email to extract the user's intent from.

    Returns:
        A tuple containing the action and the event URL.
    """
    logger.info("Extracting user intent from email.")
    logger.debug(f"Email subject: {email.subject}")
    logger.debug(f"Email body: {email.body}")
    action = assumed_action
    
    event_details = None

    subject_lower = email.subject.lower().strip()
    body_clean = _strip_quoted_reply_text(email.body)
    corpus = email.subject + "\n" + body_clean

    logger.debug(f"Corpus: {corpus}")

    # Guard against accidental event extraction from quoted history in reply threads.
    if subject_lower.startswith("re:"):
        command_terms = ("report", "stop", "cancel", "remove", "add", "register")
        if not any(term in corpus.lower() for term in command_terms):
            logger.info("Reply email without explicit command ignored.")
            return None, None

    if "report" in corpus.lower():
        logger.info("Detected action: report")
        return "report", None

    # Extract the event details from the email
    event_details = extract_event_details(corpus)

    if event_details:
        date, time_range = event_details
        logger.info(f"Found event date: {date}, time range: {time_range}")
    else:
        logger.info("No event details found in the email.")
        return None, None
    
    if "stop" in corpus.lower() or "cancel" in corpus.lower() or "remove" in corpus.lower():
        action = "remove"
        logger.info("Detected action: remove")
    else:
        logger.info("Detected action: add")

    return action, event_details

def extract_event_details(text):
    """
    Finds a date and a range of times in a given string.

    Args:
    text: The string to search for the date and time range in.

    Returns:
    A tuple containing the date and the time range if found, otherwise None.
    """
    # Regex pattern to match the date and time range format
    pattern = re.compile(r'(?P<date>\b([A-Z]{3},)?\s[A-Z]{3,9}\s\d{1,2}\b)\s*(?P<time_range>\d{1,2}:\d{2}(?:[ap]m)?\s-\s\d{1,2}:\d{2}(?:[ap]m)?)')
    match = pattern.search(text)

    if match:
        date = match.group('date').strip()
        time_range = match.group('time_range').strip()
        logger.debug(f"Extracted date: {date}, time range: {time_range}")
        return date, time_range

    return None
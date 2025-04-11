import re
from logging_config import get_logger

logger = get_logger(__name__)

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
    
    event_url = None

    corpus = email.subject + "\n" + email.body

    if corpus.lower().startswith("report"):
        logger.info("Detected action: report")
        return "report", None

    # Extract the URLs from the email
    urls = find_urls(email.subject + "\n" + email.body)
        
    # assume the first URL is the event URL
    if urls:
        event_url = urls[0]
        logger.info(f"Found event URL: {event_url}")
    else:
        logger.info("No URLs found in the email.")
        return None, None
        
    if "stop" in corpus.lower() or "cancel" in corpus.lower() or "remove" in corpus.lower():
        action = "remove"
        logger.info("Detected action: remove")
    else:
        logger.info("Detected action: add")

    return action, event_url

def find_urls(text):
    """
    Finds all URLs in a given string.

    Args:
    text: The string to search for URLs in.

    Returns:
    A list of URLs found in the string.
    """
    url_pattern = re.compile(r'https?://(?:www\.)?[a-zA-Z0-9./-]+')
    return url_pattern.findall(text)
import re

def extract_user_intent(email, assumed_action="add"):
    """
    Extracts the user's intent from an email.

    Args:
        email: The email to extract the user's intent from.

    Returns:
        A tuple containing the action and the event URL.
    """
    print("Extracting user intent from email.")
    action = assumed_action
    event_url = None

    corpus = email.subject + "\n" + email.body

    # Extract the URLs from the email
    urls = find_urls(email.subject + "\n" + email.body)

    # assume the first URL is the event URL
    if urls:
        event_url = urls[0]
        print(f"Found event URL: {event_url}")
    else:
        print("No URLs found in the email.")
        return None, None
    
    if "stop" in corpus.lower() or "cancel" in corpus.lower():
        action = "remove"
        print("Detected action: remove")
    else:
        print("Detected action: add")

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

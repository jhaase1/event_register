"""User configuration and identification utilities for multi-tenant support."""

import os
import re

import json

from logging_config import get_logger

logger = get_logger(__name__)


def load_user_config(user_tag):
    """Loads the user configuration from the token file.
    
    Args:
        user_tag: The user tag to load configuration for.
        
    Returns:
        dict: The user configuration dictionary.
    """
    token_file = get_website_token_file(user_tag)
    if not os.path.exists(token_file):
        return None
    try:
        with open(token_file, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading config for {user_tag}: {e}")
        return None

def is_sender_allowed(sender_email, user_tag):
    """Checks if the sender is authorized to act on behalf of the user_tag.
    
    Args:
        sender_email: The email address of the sender.
        user_tag: The user tag being requested.
        
    Returns:
        bool: True if authorized, False otherwise.
    """
    if user_tag == "default":
        # Default user is accessible by any authorized 'Scheduler' contact
        # (The calling code already checks is_sender_authorized for general access)
        return True
        
    config = load_user_config(user_tag)
    if not config:
        return False
        
    # Check if 'authorized_senders' is defined in the config
    authorized_senders = config.get("authorized_senders", [])
    
    # Also allow the user's own email (the one used for the website login)
    website_email = config.get("email")
    if website_email:
        authorized_senders.append(website_email)
        
    # Normalize for comparison
    sender_norm = sender_email.lower()
    allowed_norm = [s.lower() for s in authorized_senders]
    
    if sender_norm in allowed_norm:
        return True
        
    logger.warning(f"Sender {sender_email} is NOT authorized for user tag '{user_tag}'. Allowed: {allowed_norm}")
    return False


def extract_user_tag(email_address, system_email=None):
    """Extracts the user tag from an email address (e.g., 'user1' from 'email+user1@gmail.com').
    Returns 'default' if no tag is present.
    
    Args:
        email_address: Email address string or list of email addresses
        system_email: The system's own email address, used to filter To addresses
        
    Returns:
        str: The extracted user tag, or 'default' if no tag is present
    """
    if not email_address:
        return "default"
    
    # email_address is a list — narrow to addresses matching the system's base email
    if isinstance(email_address, list):
        if not email_address:
            return "default"
        
        # If system_email is known, filter to addresses belonging to the system
        if system_email and '@' in system_email:
            base_local = system_email.split('@')[0].split('+')[0].lower()
            base_domain = system_email.split('@')[1].lower()
            matching = [
                addr for addr in email_address
                if '@' in addr
                and addr.split('@')[0].split('+')[0].lower() == base_local
                and addr.split('@')[1].lower() == base_domain
            ]
            if matching:
                email_address = matching

        # Warn if multiple addresses carry different plus-tags
        tags_found = set()
        for addr in email_address:
            local = addr.split('@')[0] if '@' in addr else addr
            if '+' in local:
                tags_found.add(local.split('+', 1)[1])
        if len(tags_found) > 1:
            logger.warning(
                f"Multiple different plus-tags found in To addresses: {tags_found}. "
                "Only the first address will be used."
            )
        email_address = email_address[0]
    
    # Extract the plus tag
    if '+' in email_address:
        # Split on '@' first to get the local part
        local_part = email_address.split('@')[0]
        # Split on '+' to get the tag (only first segment after '+')
        if '+' in local_part:
            tag = local_part.split('+', 1)[1]
            if not tag or not re.match(r'^[a-zA-Z0-9_-]+$', tag):
                logger.warning(f"Invalid user tag format '{tag}' from {email_address}, using 'default'")
                return "default"
            logger.info(f"Extracted user tag: {tag} from {email_address}")
            return tag
    
    logger.info(f"No user tag found in {email_address}, using 'default'")
    return "default"


def get_website_token_file(user_tag="default"):
    """Returns the website token filename for a given user tag.
    
    Args:
        user_tag: The user tag to get the token file for
        
    Returns:
        str: The path to the user's website token file
    """
    return os.path.join("user_tokens", f"{user_tag}.json")


def validate_user_tag(user_tag):
    """Validates that a user tag is safe and has a corresponding token file.
    
    Args:
        user_tag: The user tag to validate
        
    Returns:
        str: The validated user tag
        
    Raises:
        ValueError: If the user tag format is invalid
        FileNotFoundError: If the user's token file doesn't exist
    """
    if user_tag == "default":
        token_file = get_website_token_file(user_tag)
        if not os.path.exists(token_file):
            raise FileNotFoundError(f"No token file found for user '{user_tag}': {token_file}")
        return user_tag
    
    if not re.match(r'^[a-zA-Z0-9_-]+$', user_tag):
        raise ValueError(f"Invalid user tag format: '{user_tag}'. Only alphanumeric, underscore, and hyphen allowed.")
    
    token_file = get_website_token_file(user_tag)
    if not os.path.exists(token_file):
        raise FileNotFoundError(f"No token file found for user '{user_tag}': {token_file}")
    
    return user_tag

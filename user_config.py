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
    
    Security: Every user (including 'default') must have explicit authorization.
    The sender must either be listed in 'authorized_senders' or match the
    website login email in the user's config.
    
    Args:
        sender_email: The email address of the sender.
        user_tag: The user tag being requested (will be normalized to lowercase).
        
    Returns:
        bool: True if authorized, False otherwise.
    """
    # Normalize user_tag to lowercase for consistent lookups
    user_tag = user_tag.lower() if user_tag else "default"
    
    config = load_user_config(user_tag)
    if not config:
        logger.warning(f"No config found for user tag '{user_tag}'")
        return False
        
    # Build list of authorized senders (make a copy to avoid modifying config)
    authorized_senders = list(config.get("authorized_senders", []))
    
    # Also allow the user's own email (the one used for the website login)
    website_email = config.get("email")
    if website_email:
        authorized_senders.append(website_email)
    
    # If no authorized senders configured, deny by default (fail-closed)
    if not authorized_senders:
        logger.warning(f"No authorized senders configured for user tag '{user_tag}'")
        return False
        
    # Normalize for comparison
    sender_norm = sender_email.lower().strip()
    allowed_norm = [s.lower().strip() for s in authorized_senders]
    
    if sender_norm in allowed_norm:
        logger.debug(f"Sender {sender_email} is authorized for user tag '{user_tag}'")
        return True
        
    logger.warning(f"Sender {sender_email} is NOT authorized for user tag '{user_tag}'")
    return False


def extract_user_tag(email_address, system_email=None):
    """Extracts the user tag from an email address (e.g., 'user1' from 'email+user1@gmail.com').
    Returns 'default' if no tag is present.
    
    Security: When processing a list of addresses, system_email MUST be provided
    to filter out addresses that don't belong to this system. This prevents
    attackers from using other systems' plus-tagged addresses.
    
    Args:
        email_address: Email address string or list of email addresses
        system_email: The system's own email address, REQUIRED when email_address is a list
        
    Returns:
        str: The extracted user tag (lowercase), or 'default' if no tag is present
        
    Raises:
        ValueError: If email_address is a list but system_email is not provided
    """
    if not email_address:
        return "default"
    
    # email_address is a list — narrow to addresses matching the system's base email
    if isinstance(email_address, list):
        if not email_address:
            return "default"
        
        # Security: Require system_email when processing list of addresses
        if not system_email or '@' not in system_email:
            logger.error("system_email is required when processing a list of To addresses")
            raise ValueError("system_email must be provided when email_address is a list")
        
        # Filter to addresses belonging to the system
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
            tag = local_part.split('+', 1)[1].lower()  # Normalize to lowercase
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
        
    Raises:
        ValueError: If the user tag format is invalid (path traversal protection)
    """
    # Normalize and validate to prevent path traversal attacks
    user_tag = user_tag.lower() if user_tag else "default"
    if not re.match(r'^[a-zA-Z0-9_-]+$', user_tag):
        raise ValueError(f"Invalid user tag format: '{user_tag}'. Only alphanumeric, underscore, and hyphen allowed.")
    return os.path.join("user_tokens", f"{user_tag}.json")


def validate_user_tag(user_tag):
    """Validates that a user tag is safe and has a corresponding token file.
    
    Args:
        user_tag: The user tag to validate
        
    Returns:
        str: The validated user tag (normalized to lowercase)
        
    Raises:
        ValueError: If the user tag format is invalid
        FileNotFoundError: If the user's token file doesn't exist
    """
    # Normalize to lowercase for consistent file lookups
    user_tag = user_tag.lower() if user_tag else "default"
    
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

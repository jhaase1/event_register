"""User configuration and identification utilities for multi-tenant support."""

import os
import re

from logging_config import get_logger

logger = get_logger(__name__)


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

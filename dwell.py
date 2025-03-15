import time
from datetime import datetime, timedelta
from logging_config import get_logger

# Initialize logger
logger = get_logger(__name__)

def dwell_until(nominal_time, offset_minutes=0, offset_seconds=0):
    """
    Calculates the target time by subtracting the offset (in minutes and seconds) from the nominal time,
    and dwells until that target time.
    
    :param nominal_time: The nominal time as a datetime object.
    :param offset_minutes: The offset in minutes as an float.
    :param offset_seconds: The offset in seconds as an float.
    """
    target_time = nominal_time - timedelta(minutes=offset_minutes, seconds=offset_seconds)
    current_time = datetime.now()
    logger.info(f"Dwelling until {target_time} (current time: {current_time})")

    while current_time < target_time:
        time_to_wait = (target_time - current_time).total_seconds()
        time.sleep(min(time_to_wait, 1))  # Sleep in 1-second intervals to allow for quick checks
        current_time = datetime.now()
    logger.info("Reached the target time.")

def is_within_offset(target_time, offset_minutes=0, offset_seconds=0):
    """
    Determines if the current time is within the offset (in minutes and seconds) of the target time.
    
    :param target_time: The target time as a datetime object.
    :param offset_minutes: The offset in minutes as a float.
    :param offset_seconds: The offset in seconds as a float.
    :return: True if within the offset, False otherwise.
    """
    current_time = datetime.now()
    offset = timedelta(minutes=offset_minutes, seconds=offset_seconds)
    within_offset = target_time - offset <= current_time <= target_time
    logger.info(f"Checking if within offset: {within_offset} (current time: {current_time}, target time: {target_time})")
    return within_offset

# Example usage:
# nominal_time = datetime(2023, 10, 10, 15, 0, 0)  # Example nominal time
# dwell_until(nominal_time, 10, 30)  # Dwell until 10 minutes and 30 seconds before the nominal time

# target_time = datetime(2023, 10, 10, 15, 0, 0)  # Example target time
# print(is_within_offset(target_time, 10, 30))  # Check if within 10 minutes and 30 seconds of the target time

import logging
from datetime import datetime
import os

# Ensure the logs directory exists
os.makedirs("logs", exist_ok=True)

# Initialize logger
log_date = datetime.now().strftime("%Y-%m-%d")
log_file = f"logs/{log_date}.txt"
logging.basicConfig(
    filename=log_file,
    format="%(asctime)s - %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)

def get_logger(name):
    """Returns a logger instance with the given name."""
    return logging.getLogger(name)

import sqlite3
import logging
from typing import Callable
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

def with_retry(
    max_retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 10.0
) -> Callable:
    """Decorator to retry network operations with exponential backoff."""
    return retry(
        stop=stop_after_attempt(max_retries + 1),
        wait=wait_exponential(multiplier=base_delay, max=max_delay),
        retry=retry_if_exception_type((Exception,)),
        reraise=True,
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )

def db_retry() -> Callable:
    """Decorator to retry SQLite operational errors (locks)."""
    return retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=0.1, max=1.0),
        retry=retry_if_exception_type((sqlite3.OperationalError,)),
        reraise=True,
        before_sleep=before_sleep_log(logger, logging.DEBUG),
    )

import pytest
from unittest.mock import Mock
import sqlite3

from deepresearch.utils.retry import with_retry, db_retry


def test_with_retry_success():
    mock_func = Mock(return_value="success")

    @with_retry(max_retries=3, base_delay=0.1, max_delay=0.5)
    def test_func():
        return mock_func()

    result = test_func()
    assert result == "success"
    mock_func.assert_called_once()


def test_with_retry_failure_then_success():
    mock_func = Mock(side_effect=[ConnectionError("Network error"), "success"])

    @with_retry(max_retries=3, base_delay=0.1, max_delay=0.5)
    def test_func():
        return mock_func()

    result = test_func()
    assert result == "success"
    assert mock_func.call_count == 2


def test_with_retry_exhaustion():
    mock_func = Mock(side_effect=ConnectionError("Persistent error"))

    @with_retry(max_retries=2, base_delay=0.1, max_delay=0.5)
    def test_func():
        return mock_func()

    with pytest.raises(ConnectionError):
        test_func()
    assert mock_func.call_count == 3  # Initial + 2 retries


def test_db_retry_sqlite_locked():
    mock_func = Mock(
        side_effect=[sqlite3.OperationalError("database is locked"), "success"]
    )

    @db_retry()
    def test_func():
        return mock_func()

    result = test_func()
    assert result == "success"
    assert mock_func.call_count == 2

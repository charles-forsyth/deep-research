# Release Notes - Stability Improvements

## Enhancements
*   **API Resilience:** Implemented exponential backoff and automatic retries for all core LLM API interactions using the `tenacity` library to protect against transient network failures and rate limits.
*   **Database Concurrency:** Hardened SQLite operations by configuring connection timeouts and wrapping critical read/write transactions with automatic retries, eliminating `OperationalError: database is locked` issues during highly parallel recursive tasks.
*   **Deep Diagnostics:** Added a `debug` flag to the `DeepResearchConfig`. When enabled, unexpected failures will print full Python tracebacks in the terminal for easier diagnosis without disrupting the user flow.

## Developer Changes
*   Added `tenacity` to `pyproject.toml`.
*   Introduced `deepresearch.utils.retry` utility decorators (`with_retry` and `db_retry`).

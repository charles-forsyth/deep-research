# Deep-Research Stability Improvements: Design Doc

## Language & Toolchain
- **Language**: Python 3.12+
- **Toolchain**: `uv` (Package Management), `pytest` (Testing), `ruff` (Linting)

## Architecture Changes
1. **Dependency Addition**: Add `tenacity` for exponential backoff and retry mechanisms.
2. **Retry Utility (`src/deepresearch/utils/retry.py`)**: Centralize retry decorators (e.g., `with_retry()`) that wrap network API calls to `google.genai` to automatically retry on transient network errors (e.g., `TimeoutError`, `ConnectionError`, `InternalServerError`).
3. **Database Resiliency (`src/deepresearch/storage/database.py` & `session.py`)**: Ensure `sqlite3.connect` uses a `timeout=10` parameter. Wrap execute and commit blocks in `tenacity.retry` for `sqlite3.OperationalError` (specifically "database is locked").
4. **Agent Logic (`src/deepresearch/core/agent.py`)**:
    - Wrap `generate_content` and `interactions.get`/`create` with `@with_retry()`.
    - Enhance `try/except Exception as e` blocks to use `logger.exception(e)` when debug is enabled or simply log the traceback.
    - Explicitly catch `KeyboardInterrupt` to raise it out of the retry wrapper.

## Interfaces
`src/deepresearch/utils/retry.py` will expose decorators:
```python
from typing import Callable, Any

def with_retry(
    max_retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 10.0
) -> Callable:
    """Decorator to retry network operations with exponential backoff."""
    pass

def db_retry() -> Callable:
    """Decorator to retry SQLite operational errors (locks)."""
    pass
```

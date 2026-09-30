"""Machine-readable output for `--json` on every command.

Contract: with --json, stdout carries exactly one JSON document and nothing else. Every
progress line, log line or Rich panel the command would normally print goes to stderr
instead, so a caller can always parse stdout. Success exits 0; a failure prints
{"error": "..."} on stdout and exits non-zero. Without --json nothing changes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, redirect_stdout
from typing import Any, TextIO

# The real stdout while json_mode is active (innermost last).
_OUT: list[TextIO] = []

# Session columns that are never part of JSON output: the embedding is a long
# vector of floats that only the semantic search uses.
_SESSION_HIDDEN = {"embedding"}


class JsonError(Exception):
    """A failure to report as {"error": ...} with the given exit code."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


@contextmanager
def json_mode(enabled: bool) -> Iterator[None]:
    """Send everything printed to stdout to stderr, keeping stdout for emit()."""
    if not enabled:
        yield
        return
    _OUT.append(sys.stdout)
    try:
        with redirect_stdout(sys.stderr):
            yield
    finally:
        _OUT.pop()


def json_flag(args: Any) -> bool:
    """True only when --json was really given (a bool True on the namespace).

    Callers that build their own args object (tests, the dashboard) and don't set
    `json` never switch to JSON output by accident, even with a MagicMock.
    """
    return getattr(args, "json", False) is True


def emit(obj: Any) -> None:
    """Write one JSON document to the real stdout."""
    out = _OUT[-1] if _OUT else sys.stdout
    out.write(json.dumps(obj, indent=2, default=str, ensure_ascii=False) + "\n")
    out.flush()


def fail(message: str, code: int = 1, **extra: Any) -> None:
    """Emit {"error": message, ...} and exit with `code`."""
    emit({"error": message, **extra})
    sys.exit(code)


def session_dict(row: Mapping[str, Any] | sqlite3.Row | None, *, result: bool) -> dict:
    """A session row as plain JSON-safe data.

    `files` is stored as a JSON string and comes back as a list. The embedding is
    always left out. With result=False the report text is replaced by its length,
    which keeps list output small.
    """
    if row is None:
        return {}
    d = {k: row[k] for k in row.keys() if k not in _SESSION_HIDDEN}
    files = d.get("files")
    if isinstance(files, str):
        try:
            d["files"] = json.loads(files or "[]")
        except ValueError:
            d["files"] = [files]
    if not result:
        text = d.pop("result", None) or ""
        d["result_chars"] = len(text)
    return d

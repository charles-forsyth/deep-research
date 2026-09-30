"""Test-wide isolation."""

import os

import pytest


@pytest.fixture(autouse=True)
def _no_workspace_leak(monkeypatch):
    """`workspace.use()` sets DR_WORKSPACE for the process (children inherit it);
    never let one test's workspace leak into the next, or the developer's shell
    value into any test."""
    monkeypatch.delenv("DR_WORKSPACE", raising=False)
    yield
    os.environ.pop("DR_WORKSPACE", None)

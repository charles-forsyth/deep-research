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


@pytest.fixture(autouse=True)
def _no_auto_referee(monkeypatch):
    """The Lab referee reads every new draft with a model call; tests that want it call
    `lab.review()` directly with a stubbed `_ask`."""
    monkeypatch.setenv("DR_LAB_REVIEW", "0")
    monkeypatch.setenv("DR_LAB_REFINE", "0")


@pytest.fixture(autouse=True)
def _never_the_real_history_db(monkeypatch, tmp_path_factory):
    """A test that builds a SessionManager() without a path would write to the
    developer's real ~/.config/deepresearch/history.db (16 stray "a gap" rows did,
    2026-10-03). Point the default DB at a throwaway file for every test."""
    from deepresearch.core import session as session_mod

    db = str(tmp_path_factory.mktemp("histdb") / "history.db")
    real_init = session_mod.SessionManager.__init__

    def init(self, db_path=None, *a, **k):
        # a workspace chosen by the test (DR_WORKSPACE) still wins; only the
        # fall-through to the real Main DB is redirected
        real = os.path.realpath(
            os.path.join(
                os.path.expanduser("~"), ".config", "deepresearch", "history.db"
            )
        )
        if (
            db_path is None
            and not session_mod._workspace_db()
            and os.path.realpath(session_mod.user_db_path) == real
        ):
            db_path = db
        real_init(self, db_path, *a, **k)

    monkeypatch.setattr(session_mod.SessionManager, "__init__", init)

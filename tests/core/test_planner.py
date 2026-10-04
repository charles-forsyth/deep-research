"""v0.61.0 Plan first: Google's collaborative planning, before a run is launched."""

from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

from deepresearch.core import planner
from deepresearch.core.config import AGENT_STANDARD


def _it(status, text=""):
    steps = [NS(type="model_output", content=[NS(text=text)])] if text else []
    return NS(id="plan-1", status=status, steps=steps, output_text=text, error=None)


def test_plan_uses_the_standard_agent_with_collaborative_planning():
    c = MagicMock()
    c.interactions.create.return_value = NS(id="plan-1")
    c.interactions.get.side_effect = [
        _it("in_progress"),
        _it("completed", "(1) Search docs"),
    ]
    r = planner.plan(c, "Q?", sleep=lambda s: None)
    assert r["id"] == "plan-1" and r["plan"] == "(1) Search docs"
    kw = c.interactions.create.call_args.kwargs
    assert kw["agent"] == AGENT_STANDARD  # Max planning never returned a plan
    assert kw["agent_config"]["collaborative_planning"] is True
    assert "previous_interaction_id" not in kw


def test_revision_continues_the_plan():
    c = MagicMock()
    c.interactions.create.return_value = NS(id="plan-2")
    c.interactions.get.return_value = _it("completed", "(1) new")
    planner.plan(c, "drop step 4", previous_id="plan-1", sleep=lambda s: None)
    kw = c.interactions.create.call_args.kwargs
    assert kw["previous_interaction_id"] == "plan-1" and kw["input"] == "drop step 4"


def test_a_plan_that_never_comes_is_cancelled():
    c = MagicMock()
    c.interactions.create.return_value = NS(id="plan-1")
    c.interactions.get.return_value = _it("in_progress")
    with pytest.raises(RuntimeError, match="no plan after"):
        planner.plan(c, "Q?", timeout_s=0, sleep=lambda s: None)
    c.interactions.cancel.assert_called_once_with(id="plan-1")


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_or_empty_plans_raise(status):
    c = MagicMock()
    c.interactions.create.return_value = NS(id="plan-1")
    c.interactions.get.return_value = _it(status)
    with pytest.raises(RuntimeError, match=status):
        planner.plan(c, "Q?", sleep=lambda s: None)
    c.interactions.get.return_value = _it("completed", "")
    with pytest.raises(RuntimeError, match="empty"):
        planner.plan(c, "Q?", sleep=lambda s: None)

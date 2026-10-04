"""Plan first (v0.61.0): Google's collaborative planning, before a run is launched.

The standard Deep Research agent writes a research plan (about 10-20 s, well under a
cent) and revises it on request. Approving runs the research as a continuation of the
plan interaction (`previous_interaction_id`), on whichever agent the launch picked.
Planning always uses the standard agent: a Max planning call (tested 2026-10-03) was
still running with no plan after 10 minutes, while a Max run continuing a standard
plan is accepted.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from deepresearch.core.config import AGENT_STANDARD

PLAN_TIMEOUT_S = 180
_CONFIG = {
    "type": "deep-research",
    "thinking_summaries": "auto",
    "collaborative_planning": True,
}


def _text(it: Any) -> str:
    parts = []
    for step in getattr(it, "steps", None) or []:
        if getattr(step, "type", None) == "model_output":
            parts.append(
                "".join(
                    getattr(c, "text", "") or ""
                    for c in getattr(step, "content", None) or []
                )
            )
    return "".join(parts) or str(getattr(it, "output_text", "") or "")


def plan(
    client: Any,
    prompt: str,
    previous_id: str | None = None,
    timeout_s: float = PLAN_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Ask for a plan (or a revision of `previous_id` with `prompt` as the change
    request). Returns {"id", "plan", "seconds"}; raises RuntimeError when Google fails
    or takes longer than `timeout_s` (the interaction is then cancelled)."""
    t0 = time.monotonic()
    kw: dict = {"previous_interaction_id": previous_id} if previous_id else {}
    it = client.interactions.create(
        agent=AGENT_STANDARD,
        input=prompt,
        agent_config=dict(_CONFIG),
        background=True,
        **kw,
    )
    iid = str(getattr(it, "id", "") or "")
    if not iid:
        raise RuntimeError("Google returned no interaction id for the plan")
    while True:
        cur = client.interactions.get(id=iid)
        st = getattr(cur, "status", None)
        if st == "completed":
            text = _text(cur).strip()
            if not text:
                raise RuntimeError("Google returned an empty plan")
            return {"id": iid, "plan": text, "seconds": round(time.monotonic() - t0)}
        if st in ("failed", "cancelled", "incomplete"):
            raise RuntimeError(
                f"planning {st}: {getattr(cur, 'error', '') or ''}".strip()
            )
        if time.monotonic() - t0 > timeout_s:
            try:
                client.interactions.cancel(id=iid)
            except Exception:
                pass
            raise RuntimeError(
                f"no plan after {int(timeout_s)} s; try again or launch without a plan"
            )
        sleep(3)

"""Lab referee (v0.46.0): a second model pass asks whether the test could ever fail or
ever pass. Advice only: stored on the plan, stale when the plan changes, never blocks."""

import json

import pytest

from deepresearch.dashboard import labreview
from deepresearch.dashboard.lab import EmptyReply, Lab

PLAN = {
    "title": "t",
    "question": "Does TLS beat BLS on shallow transits?",
    "success_criteria": "TLS period error lower than BLS",
    "resources": {
        "partition": "standard",
        "nodes": 1,
        "time_limit": "00:10:00",
        "gpus": 0,
    },
    "install": {"modules": [], "conda": [], "pip": []},
    "parameters": {"rp_inj": 0.78},
    "script": "echo hi > outputs/r.txt",
}
REVIEW = {
    "verdict": "flawed",
    "findings": [
        {
            "severity": "low",
            "kind": "other",
            "where": "script",
            "problem": "minor",
            "suggestion": "x",
        },
        {
            "severity": "high",
            "kind": "cannot_pass",
            "where": "rp_inj",
            "problem": "0.78 Re is below the noise floor (SDE < 6).",
            "suggestion": "Use 1.1 Re.",
        },
    ],
    "summary": "The injected planet is too small to detect.",
}


@pytest.fixture
def lab(tmp_path):
    lb = Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={})
    lb.ensure_watcher = lambda: None
    return lb


def _ask(lab, replies):
    calls = []

    def ask(prompt, search):
        calls.append(prompt)
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r, 0.002

    lab._ask = ask
    return calls


def test_review_stores_normalized_findings_on_the_draft(lab):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    calls = _ask(lab, ["```json\n" + json.dumps(REVIEW) + "\n```"])
    out = lab.review(run["id"])
    rv = out["plan"]["review"]
    assert rv["verdict"] == "flawed" and [f["severity"] for f in rv["findings"]] == [
        "high",
        "low",
    ]
    assert "Could this test ever FAIL?" in calls[0] and "Does TLS beat BLS" in calls[0]
    assert (
        out["status"] == "draft" and out["plan"]["script"] == PLAN["script"]
    )  # advice only
    assert not lab.review_stale(out["plan"])


def test_review_retries_once_then_fails_without_touching_the_plan(lab):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    _ask(lab, [EmptyReply("MAX_TOKENS", 0.0), "no json here"])
    with pytest.raises(ValueError, match="no usable review twice"):
        lab.review(run["id"])
    assert "review" not in lab.get(run["id"])["plan"]


def test_editing_the_plan_makes_the_review_stale(lab):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    _ask(lab, [json.dumps(REVIEW)])
    lab.review(run["id"])
    p = dict(lab.get(run["id"])["plan"])
    p["parameters"] = {"rp_inj": 1.1}
    lab.edit_plan(run["id"], p)
    assert lab.review_stale(lab.get(run["id"])["plan"])


def test_only_drafts_are_reviewed(lab):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    lab._update(run["id"], status="queued")
    with pytest.raises(ValueError, match="only drafts"):
        lab.review(run["id"])


def test_a_sound_verdict_with_a_high_finding_is_downgraded():
    rv = labreview.normalize(
        {"verdict": "sound", "findings": [{"severity": "high", "problem": "p"}]}
    )
    assert rv["verdict"] == "concerns"
    assert labreview.normalize({"findings": []})["verdict"] == "sound"
    assert labreview.normalize({"verdict": "odd", "findings": [{"severity": "weird", "kind": "?", "problem": "p"}]})["findings"][0] == {
        "severity": "medium", "kind": "other", "where": "", "problem": "p", "suggestion": ""
    }  # fmt: skip
    with pytest.raises(ValueError):
        labreview.normalize(["not", "a", "dict"])


def test_as_problems_passes_only_high_and_medium_findings_to_the_fixer():
    rv = labreview.normalize(REVIEW)
    probs = labreview.as_problems(rv)
    assert len(probs) == 1 and probs[0].startswith(
        "Referee (high, cannot pass) at rp_inj:"
    )
    assert "Use 1.1 Re." in probs[0]


def test_fix_with_ai_receives_current_referee_findings(lab, monkeypatch):
    class T:
        kind = name = "fake"
        label = "Fake"
        catalog = None
        partitions = {"standard": {"usd_per_hour": 1.0}}
        default_partition = "standard"
        remote_root = "~/drl"

        def describe(self):
            return "Fake"

    lab.targets = {"fake": T()}
    run = lab.create(1, "document", "x", plan=dict(PLAN), target="fake")
    monkeypatch.setattr(lab, "_fresh_catalog", lambda tgt: None)
    monkeypatch.setattr(lab, "_check", lambda tgt, plan: [])
    _ask(lab, [json.dumps(REVIEW)])
    lab.review(run["id"])
    fixed = dict(PLAN, parameters={"rp_inj": 1.1})
    calls = _ask(
        lab,
        [
            "```json\n"
            + json.dumps(
                {
                    "plan": fixed,
                    "changes": ["rp_inj 0.78 -> 1.1 (referee: below noise)"],
                }
            )
            + "\n```"
        ],
    )
    out = lab.fix_plan(run["id"])
    assert "Referee (high, cannot pass) at rp_inj" in calls[0]
    assert (
        '"review"' not in calls[0]
    )  # the fixer sees the findings, not the review object
    assert out["plan"]["parameters"] == {"rp_inj": 1.1}
    assert lab.review_stale(out["plan"])  # the old review judged the old plan


def test_auto_review_runs_after_planning_and_never_fails_the_draft(lab, monkeypatch):
    lab.auto_review = True
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    monkeypatch.setattr(
        lab, "review", lambda rid: (_ for _ in ()).throw(RuntimeError("model down"))
    )
    # planning path: make_plan with a stubbed planner reply
    lab._update(run["id"], status="planning", plan=None)
    _ask(lab, ["```json\n" + json.dumps(PLAN) + "\n```"])
    monkeypatch.setattr(lab, "_check_urls", lambda rid, tgt, plan: [])
    lab.make_plan(run["id"], "prompt")
    assert lab.get(run["id"])["status"] == "draft"

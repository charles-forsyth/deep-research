"""Referee -> fixer loop on new drafts (v0.49.0): a 'flawed' (or high-finding) review sends
the findings to the fixer and the referee reads the result again, up to two rounds, before
a person sees the draft. Only ever another draft: never submits, Undo restores the first
plan, and it stops when the referee is satisfied, nothing changes, or a step fails."""

import json

import pytest

from deepresearch.dashboard import labguard
from deepresearch.dashboard.lab import Lab

PLAN = {
    "title": "t",
    "question": "Does TLS beat BLS on shallow transits?",
    "success_criteria": "TLS period error lower than BLS",
    "resources": {"partition": "standard", "nodes": 1, "time_limit": "00:10:00", "gpus": 0},
    "install": {"modules": [], "conda": [], "pip": []},
    "parameters": {"rp_inj": 0.78},
    "script": "echo v1 > outputs/r.txt",
}  # fmt: skip


def _review(verdict, sev="high"):
    f = [] if verdict == "sound" else [
        {"severity": sev, "kind": "cannot_pass", "where": "rp_inj",
         "problem": "too small to detect", "suggestion": "use 1.1"}
    ]  # fmt: skip
    return {"verdict": verdict, "findings": f, "summary": verdict}


@pytest.fixture
def lab(tmp_path):
    lb = Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={})
    lb.ensure_watcher = lambda: None
    return lb


def _wire(lab, monkeypatch, reviews, fixes):
    """Fake referee replies (in order) and a fake fixer that rewrites the script."""
    seen = {"reviews": 0, "fixes": 0}

    def review(run_id):
        rv = reviews.pop(0)
        if isinstance(rv, Exception):
            raise rv
        seen["reviews"] += 1
        run = lab.get(run_id)
        plan = dict(run["plan"])
        plan["review"] = {**rv, "plan_hash": lab._plan_hash(plan)}
        lab._update(run_id, only_if=("draft",), plan=plan)
        return lab.get(run_id)

    def fix_plan(run_id):
        fx = fixes.pop(0)
        if isinstance(fx, Exception):
            raise fx
        seen["fixes"] += 1
        run = lab.get(run_id)
        plan = dict(run["plan"])
        silent = isinstance(
            fx, tuple
        )  # ("silent", script): edits without a change list
        if silent:
            fx = fx[1]
        if fx:
            plan["script"] = fx
        lab._update(run_id, only_if=("draft",), plan=plan)
        listed = [f"script -> {fx}"] if fx and not silent else []
        return {**lab.get(run_id), "fix": {"changes": listed}}

    monkeypatch.setattr(lab, "review", review)
    monkeypatch.setattr(lab, "fix_plan", fix_plan)
    return seen


def _draft(lab, review):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    plan = dict(run["plan"])
    plan["review"] = {**review, "plan_hash": lab._plan_hash(plan)}
    lab._update(run["id"], plan=plan)
    return run["id"]


def test_flawed_draft_is_revised_until_the_referee_is_satisfied(lab, monkeypatch):
    rid = _draft(lab, _review("flawed"))
    seen = _wire(lab, monkeypatch, [_review("sound")], ["echo v2"])
    lab._auto_refine(rid)
    run = lab.get(rid)
    p = run["plan"]
    assert run["status"] == "draft"  # never submitted
    assert p["script"] == "echo v2" and p["review"]["verdict"] == "sound"
    assert seen == {"reviews": 1, "fixes": 1}
    assert p["refine"] == [
        {"round": 1, "before": "flawed", "findings": 1,
         "changes": ["script -> echo v2"], "after": "sound"}
    ]  # fmt: skip
    assert p["plan_before_refine"]["script"] == PLAN["script"]
    assert (
        "revised by AI after the referee, 1 round; referee now: sound" in run["stage"]
    )


def test_two_rounds_at_most(lab, monkeypatch):
    rid = _draft(lab, _review("flawed"))
    seen = _wire(
        lab, monkeypatch, [_review("flawed"), _review("flawed"), _review("sound")],
        ["echo v2", "echo v3", "echo v4"],
    )  # fmt: skip
    lab._auto_refine(rid)
    p = lab.get(rid)["plan"]
    assert seen["fixes"] == 2 and p["script"] == "echo v3"  # all equal: newest kept
    assert [r["after"] for r in p["refine"]] == ["flawed", "flawed"]
    assert "refine_kept" not in p


def test_sound_or_medium_concerns_are_left_alone(lab, monkeypatch):
    for rv in (_review("sound"), _review("concerns", sev="medium")):
        rid = _draft(lab, rv)
        seen = _wire(lab, monkeypatch, [], [])
        lab._auto_refine(rid)
        assert seen == {"reviews": 0, "fixes": 0}
        assert "refine" not in lab.get(rid)["plan"]


def test_high_finding_under_concerns_still_triggers(lab, monkeypatch):
    rid = _draft(lab, _review("concerns", sev="high"))
    seen = _wire(lab, monkeypatch, [_review("sound")], ["echo v2"])
    lab._auto_refine(rid)
    assert seen["fixes"] == 1


def test_stops_when_the_fixer_changes_nothing_or_fails(lab, monkeypatch):
    rid = _draft(lab, _review("flawed"))
    _wire(lab, monkeypatch, [], [""])
    lab._auto_refine(rid)
    p = lab.get(rid)["plan"]
    assert (
        p["script"] == PLAN["script"]
        and p["refine"][0]["error"] == "the fixer changed nothing"
    )
    rid2 = _draft(lab, _review("flawed"))
    _wire(lab, monkeypatch, [], [RuntimeError("quota")])
    lab._auto_refine(rid2)
    p2 = lab.get(rid2)
    assert p2["status"] == "draft" and "quota" in p2["plan"]["refine"][0]["error"]


def test_never_touches_a_run_that_left_draft(lab, monkeypatch):
    rid = _draft(lab, _review("flawed"))
    lab._update(rid, status="submitting")
    seen = _wire(lab, monkeypatch, [], [])
    lab._auto_refine(rid)
    assert seen["fixes"] == 0 and "refine" not in lab.get(rid)["plan"]


def test_undo_restores_the_plan_as_first_written(lab, monkeypatch):
    rid = _draft(lab, _review("flawed"))
    _wire(lab, monkeypatch, [_review("sound")], ["echo v2"])
    lab._auto_refine(rid)
    out = lab.undo_refine(rid)
    assert out["plan"]["script"] == PLAN["script"] and out["status"] == "draft"
    with pytest.raises(ValueError):
        lab.undo_refine(_draft(lab, _review("sound")))


def test_bookkeeping_stays_out_of_the_referee_prompt():
    from deepresearch.dashboard import labreview

    plan = {
        **PLAN,
        "refine": [{"round": 1}],
        "plan_before_refine": {"script": "SECRET-OLD"},
    }
    assert "SECRET-OLD" not in labreview.build_prompt(plan)


def test_planner_rules_cover_the_whole_question_and_like_for_like():
    rules = " ".join(labguard.GENERAL_RULES)
    assert "Cover the whole question" in rules and "Compare like with like" in rules
    assert "able to fail and able to pass" in rules
    assert json.dumps(rules)  # plain text, serialisable for /api/lab/lessons


def test_an_edit_without_a_change_list_still_counts(lab, monkeypatch):
    """Run #46: the fixer rewrote the script but listed no changes; the loop stopped."""
    rid = _draft(lab, _review("flawed"))
    seen = _wire(lab, monkeypatch, [_review("sound")], [("silent", "echo v2")])
    lab._auto_refine(rid)
    p = lab.get(rid)["plan"]
    assert seen["reviews"] == 1 and p["review"]["verdict"] == "sound"
    assert p["refine"][0]["changes"] == [
        "(the fixer edited the plan without listing changes)"
    ]


def test_a_worse_last_round_is_not_kept(lab, monkeypatch):
    """L7, seen 2026-09-30: flawed -> concerns -> flawed. Keep the 'concerns' version."""
    rid = _draft(lab, _review("flawed"))
    _wire(
        lab, monkeypatch, [_review("concerns", sev="high"), _review("flawed")],
        ["echo v2", "echo v3"],
    )  # fmt: skip
    lab._auto_refine(rid)
    run = lab.get(rid)
    p = run["plan"]
    assert run["status"] == "draft"
    assert p["script"] == "echo v2" and p["review"]["verdict"] == "concerns"
    assert not lab.review_stale(p)  # the kept review judged exactly this plan
    assert p["refine_kept"] == {
        "round": 1, "verdict": "concerns", "instead_of": 2, "instead_of_verdict": "flawed"
    }  # fmt: skip
    assert p["plan_refine_discarded"]["script"] == "echo v3"  # shown, not lost
    assert p["plan_before_refine"]["script"] == PLAN["script"]  # Undo still works
    assert "kept round 1, the best the referee saw" in run["stage"]
    assert [r["after"] for r in p["refine"]] == ["concerns", "flawed"]


def test_when_no_revision_beats_the_first_plan_the_first_is_kept(lab, monkeypatch):
    rid = _draft(lab, _review("concerns", sev="high"))
    _wire(
        lab, monkeypatch, [_review("flawed"), _review("flawed")], ["echo v2", "echo v3"]
    )
    lab._auto_refine(rid)
    run = lab.get(rid)
    assert run["plan"]["script"] == PLAN["script"]
    assert run["plan"]["refine_kept"]["round"] == 0
    assert "the AI revisions were not better" in run["stage"]


def test_review_rank_orders_verdicts_then_findings():
    r = Lab._review_rank
    assert r(_review("sound")) < r(_review("concerns", sev="medium"))
    assert r(_review("concerns", sev="medium")) < r(_review("concerns", sev="high"))
    assert r(_review("concerns", sev="high")) < r(_review("flawed"))
    assert r(None) > r(_review("flawed"))

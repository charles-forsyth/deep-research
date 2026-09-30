"""Lab verdicts (v0.39.0): outcomes, notes on the report, pilot gate, one auto re-plan.

The check shapes below are the real verdict.json contents of runs #79 (both arms at 0%),
#78 (identical numbers), #98 (fair test, claim false) and #97 (claim holds).
"""

import json
import sqlite3
import threading

import pytest

from deepresearch.dashboard import labloop, labverdict
from deepresearch.dashboard.store import DashboardStore
from tests.dashboard.test_lab import PLAN, lab  # noqa: F401  (fixture)
from tests.dashboard.test_lab_v3 import _draft, wlab  # noqa: F401, F811  (fixture)

RUN79 = {
    "pass": False,
    "checks": [
        {"name": "Fertilization rate at 0.5 m proximity", "expected": 30.0, "got": 29.98, "tolerance": 2.0, "pass": True},
        {"name": "Fertilization collapse at 15.0 m separation", "expected": 1.4, "got": 1.42, "tolerance": 1.5, "pass": True},
        {
            "name": "Spatial clustering rescues population below 15m threshold (N=30)",
            "expected": "Significant risk reduction (P_ext_clustered < P_ext_random)",
            "got": "P_ext_random=0.0%, P_ext_clustered=0.0%",
            "tolerance": "N/A",
            "pass": False,
        },
    ],
}  # fmt: skip
RUN78 = {
    "pass": False,
    "checks": [
        {"name": "discrete_boundary_conditions", "expected": "u_0=1.0 and u_K=0.0", "got": "u_0=1.0, u_K=0.0", "pass": True},
        {"name": "diffusion_underestimation_at_Nc", "expected": "u_discrete(Nc) > u_diff(Nc)", "got": "Discrete=0.5168, Diffusion=0.5168, Underestimate=+0.0000", "pass": False},
        {"name": "severe_underestimation_above_Nc", "expected": "Relative underestimation exceeds 15%", "got": "0.0%", "tolerance": 15.0, "pass": False},
    ],
}  # fmt: skip
RUN98 = {
    "pass": False,
    "checks": [
        {"name": "monte_carlo_discrete_consistency", "expected": "MC within 3.5 SE", "got": "max |diff| = 0.0118", "pass": True},
        {"name": "informative_design: diffusion error grows as Nc shrinks", "expected": "larger at Nc=3", "got": "15.8% vs 0.7%", "pass": True},
        {"name": "diffusion_underestimation_at_Nc (claim, fair small-Nc regime Nc=3)", "expected": "u_discrete(Nc) - u_diff(Nc) > 0.01", "got": "Discrete=0.6249, Diffusion=0.7025, diff=-0.0776", "pass": False},
    ],
}  # fmt: skip
RUN97 = {
    "pass": True,
    "checks": [
        {"name": "Fertilization rate at 0.5 m proximity", "expected": 30.0, "got": 29.98, "pass": True},
        {"name": "Test is informative: random layout at N0=30 is at risk but not doomed", "expected": "5% <= P <= 95%", "got": "85.3%", "pass": True},
        {"name": "Spatial clustering rescues population below 15 m threshold", "expected": "P_qe_clustered < P_qe_random", "got": "P_qe_random=85.3%, P_qe_clustered=1.3%", "pass": True},
    ],
}  # fmt: skip


@pytest.mark.parametrize(
    "verdict,outcome",
    [
        (RUN79, "inconclusive"),
        (RUN78, "inconclusive"),
        (RUN98, "refuted"),
        (RUN97, "confirmed"),
    ],
)
def test_real_runs_get_the_right_outcome(verdict, outcome):
    a = labverdict.assess(verdict, "completed")
    assert a and a["outcome"] == outcome, a


def test_declared_kinds_win_and_validation_failure_is_broken():
    v = {
        "checks": [
            {"name": "rescue works", "kind": "validation", "pass": False},
            {"name": "claim", "kind": "claim", "pass": True},
        ]
    }
    a = labverdict.assess(v, "completed")
    assert a["outcome"] == "broken" and a["inferred"] is False
    assert labverdict.check_kind({"name": "x", "kind": "informative"}) == (
        "informative",
        False,
    )


def test_failed_informative_check_is_inconclusive_not_refuted():
    v = {
        "checks": [
            {
                "name": "baseline arm at risk",
                "kind": "informative",
                "got": "0.0%",
                "pass": False,
            },
            {
                "name": "the claim",
                "kind": "claim",
                "got": "A=0.1, B=0.3",
                "pass": False,
            },
        ]
    }
    assert labverdict.assess(v, "completed")["outcome"] == "inconclusive"


def test_failed_job_is_broken_and_unfinished_has_no_outcome():
    assert labverdict.assess(None, "failed")["outcome"] == "broken"
    assert labverdict.assess(RUN97, "running") is None
    assert labverdict.assess(None, "completed") is None


def test_identical_arms_audit_makes_it_inconclusive():
    v = {**RUN97, "audit": ["IDENTICAL arms A and B"]}
    assert labverdict.assess(v, "completed")["outcome"] == "inconclusive"


def test_planner_prompt_asks_for_kinds_and_provenance():
    from deepresearch.dashboard import labguard

    b = labguard.prompt_block("anything", None)
    assert '"kind"' in b and "informative" in b and "parameter_sources" in b
    from deepresearch.dashboard.lab import PLAN_PROMPT

    assert "parameter_sources" in PLAN_PROMPT


def test_anchor_quote_and_gist():
    report = (
        "# Title\n\nIntro sentence that is long enough to count here.\n\n"
        "At a separation of 10 meters, fertilization of broadcast spawning corals dropped "
        "below 10% [cite: 45]. Other text follows here for a while.\n"
    )
    run = {
        "scope": "suggestion",
        "plan": {
            "question": "Can clustering rescue broadcast spawning corals from fertilization collapse?"
        },
    }
    q = labloop.anchor_quote(report, run)
    assert q.startswith("At a separation of 10 meters") and "cite" not in q
    md = "**Result**\n\nREFUTED: the diffusion error has the opposite sign.\n\n**Key numbers**\n- x"
    assert (
        labloop.result_gist(md) == "REFUTED: the diffusion error has the opposite sign."
    )


def _complete(
    lab,  # noqa: F811
    run_id,
    verdict,
    note="**Result**\n\nINCONCLUSIVE: both arms at 0%.",
    then=None,
):
    lab.replies.append(note)
    if then:  # the automatic re-plan's reply comes after the write-up
        lab.replies.append(then)
    lab._update(run_id, status="fetching", job_id="1", slurm_state="COMPLETED")
    orig = lab.fake.fetch

    def fetch(rid, dest):
        files = orig(rid, dest)
        (dest / "outputs" / "verdict.json").write_text(json.dumps(verdict))
        return files

    lab.fake.fetch = fetch
    lab._finish(lab.get(run_id), lab.fake)
    lab.fake.fetch = orig


def _sync_threads(monkeypatch):
    """Run the background re-plan inline so the test sees its result."""

    class T:
        def __init__(self, target, args=(), daemon=None, **k):
            self.t, self.a = target, args

        def start(self):
            self.t(*self.a)

    monkeypatch.setattr(labloop.threading, "Thread", T)


def _db_with_report(lab, text):  # noqa: F811
    DashboardStore(lab.db_path)  # creates annotations table
    with sqlite3.connect(lab.db_path) as c:
        c.execute(
            "INSERT INTO sessions (id, interaction_id, prompt, status, result, created_at, updated_at) "
            "VALUES (1, 'i', 'Coral extinction risk', 'completed', ?, 'now', 'now')",
            (text,),
        )


REPLAN = {
    "plan": {**PLAN, "parameters": {"area_m2": 40000}, "parameter_sources": {"area_m2": "Mumby 2024"}},
    "changes": ["area 1 ha -> 4 ha so random neighbours are beyond 15 m (Mumby 2024)"],
    "notes": "",
}  # fmt: skip


def test_inconclusive_run_attaches_note_and_replans_once(lab, monkeypatch):  # noqa: F811
    _sync_threads(monkeypatch)
    _db_with_report(
        lab,
        "Spatial clustering can rescue corals below the 15 meter threshold of fertilization.",
    )
    run = lab.create(
        1,
        "document",
        "x",
        "",
        plan={
            **PLAN,
            "question": "Does clustering rescue corals below the 15 meter threshold?",
        },
    )
    _complete(lab, run["id"], RUN79, then="```json\n" + json.dumps(REPLAN) + "\n```")
    done = lab.get(run["id"])
    assert done["stage"] == "Completed: INCONCLUSIVE"
    assert done["assessment"]["outcome"] == "inconclusive"
    # a note on the report, never an edit of it
    anns = DashboardStore(lab.db_path).list_annotations(1)
    assert len(anns) == 1 and anns[0]["note"].startswith(f"Lab run #{run['id']} (")
    assert "INCONCLUSIVE" in anns[0]["note"] and anns[0]["color"] == "amber"
    assert "15 meter threshold" in anns[0]["quote"]
    with sqlite3.connect(lab.db_path) as c:
        assert (
            c.execute("SELECT result FROM sessions WHERE id=1")
            .fetchone()[0]
            .startswith("Spatial clustering")
        )
    # one automatic re-plan: a draft, never submitted
    kids = [r for r in lab.runs_for(1) if r.get("rerun_of") == run["id"]]
    assert len(kids) == 1
    k = kids[0]
    assert k["status"] == "draft" and k["plan"]["auto_replan_of"] == run["id"]
    assert "inconclusive run" in k["stage"] and k["plan"]["fix_changes"]
    assert not lab.fake.submitted
    # the re-planned run is inconclusive too: no second automatic re-plan
    _complete(lab, k["id"], RUN79)
    assert not [r for r in lab.runs_for(1) if r.get("rerun_of") == k["id"]]
    # and the first run is never re-planned twice
    assert lab._maybe_auto_replan(lab.get(run["id"])) is False


def test_refuted_and_confirmed_runs_are_not_replanned(lab, monkeypatch):  # noqa: F811
    _sync_threads(monkeypatch)
    _db_with_report(
        lab,
        "Diffusion approximations underestimate extinction risk near the threshold.",
    )
    a = lab.create(1, "document", "x", "", plan=PLAN)
    _complete(lab, a["id"], RUN98, "**Result**\n\nREFUTED.")
    b = lab.create(1, "document", "x", "", plan=PLAN)
    _complete(lab, b["id"], RUN97, "**Result**\n\nCONFIRMED.")
    assert lab.get(a["id"])["stage"] == "Completed: REFUTED"
    assert lab.get(b["id"])["stage"] == "Completed: CONFIRMED"
    assert all(not r.get("rerun_of") for r in lab.runs_for(1))
    colors = {
        x["note"].split(" ")[2]: x["color"]
        for x in DashboardStore(lab.db_path).list_annotations(1)
    }
    assert colors == {f"#{a['id']}": "magenta", f"#{b['id']}": "green"}


def test_note_is_refreshed_not_duplicated(lab):  # noqa: F811
    _db_with_report(lab, "Some long enough sentence about the claim under test here.")
    run = lab.create(1, "document", "x", "", plan=PLAN)
    _complete(lab, run["id"], RUN97, "**Result**\n\nCONFIRMED once.")
    lab._update(run["id"], result_md="**Result**\n\nCONFIRMED again.")
    lab._attach_note(lab.get(run["id"]))
    anns = DashboardStore(lab.db_path).list_annotations(1)
    assert len(anns) == 1 and "CONFIRMED again." in anns[0]["note"]


def test_pilot_that_cannot_discriminate_stops_and_replans(wlab, monkeypatch):  # noqa: F811
    _sync_threads(monkeypatch)
    run = _draft(wlab)
    r = wlab.submit(run["id"])
    task = r["smoke"]["task"]
    wlab.fake.files[f"{run['id']}/smoke/outputs/verdict.json"] = json.dumps(RUN79)
    wlab.replies.append("```json\n" + json.dumps(REPLAN) + "\n```")
    wlab.fake.finish(
        task, 0, "[STAGE] Done\n", run=run["id"], outputs=["outputs/result.txt"]
    )
    wlab.poll(wlab.get(run["id"]))
    now = wlab.get(run["id"])
    # the full run was never started
    assert f"full-{run['id']}" not in wlab.fake.tasks and not wlab.fake.sbatched
    assert now["status"] == "draft"
    assert now["smoke"]["pilot"]["outcome"] == "inconclusive"
    assert now["plan"]["auto_replanned"] is True and now["plan"]["plan_before_fix"]
    assert now["plan"]["parameters"] == {"area_m2": 40000}
    assert "re-planned by AI" in now["stage"]


def test_pilot_that_discriminates_goes_on_to_the_full_run(wlab):  # noqa: F811
    run = _draft(wlab)
    r = wlab.submit(run["id"])
    wlab.fake.files[f"{run['id']}/smoke/outputs/verdict.json"] = json.dumps(RUN97)
    wlab.fake.finish(
        r["smoke"]["task"],
        0,
        "[STAGE] Done\n",
        run=run["id"],
        outputs=["outputs/result.txt"],
    )
    wlab.poll(wlab.get(run["id"]))
    assert wlab.get(run["id"])["status"] == "queued"


def test_findings_and_lists_carry_the_outcome(lab):  # noqa: F811
    from deepresearch.dashboard import projects as pj

    run = lab.create(1, "document", "x", "", plan={**PLAN, "title": "Coral PVA"})
    lab._update(
        run["id"],
        status="completed",
        verdict=json.dumps(RUN98),
        result_md="**Result**\n\nREFUTED.",
    )
    r = lab.get(run["id"])
    assert pj.verdict_line(r).startswith("REFUTED")
    assert "REFUTED" in pj.lab_findings([r])


def test_no_thread_leak_marker():
    assert isinstance(labloop._AUTO_REPLANNING, set)
    assert threading.active_count() >= 1

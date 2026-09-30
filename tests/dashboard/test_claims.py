"""Claims board (v0.45.0): one claim per tested question, the best outcome leads."""

from deepresearch.dashboard.claims import board, board_md


def run(i, q, status="completed", checks=None, session=1, rerun_of=None, stage=""):
    return {
        "id": i,
        "session_id": session,
        "status": status,
        "stage": stage,
        "rerun_of": rerun_of,
        "plan": {"question": q, "title": q[:20]},
        "verdict": {"checks": checks, "pass": True} if checks is not None else None,
    }


OK_VAL = {"name": "sanity", "kind": "validation", "expected": 1, "got": 1, "pass": True}
CLAIM_OK = {
    "name": "tc",
    "kind": "claim",
    "expected": 2.269,
    "got": 2.271,
    "pass": True,
}
CLAIM_BAD = {
    "name": "ustar",
    "kind": "claim",
    "expected": 0.61,
    "got": 0.63,
    "pass": False,
}
INF_BAD = {
    "name": "power",
    "kind": "informative",
    "expected": 0.5,
    "got": 0.1,
    "pass": False,
}


def test_outcomes_and_order():
    b = board(
        [
            run(1, "Does A hold?", checks=[OK_VAL, CLAIM_OK]),
            run(2, "Does B hold?", checks=[OK_VAL, CLAIM_OK, CLAIM_BAD]),
            run(3, "Does C hold?", checks=[OK_VAL, INF_BAD, CLAIM_OK]),
            run(4, "Does D hold?", status="failed"),
            run(5, "Does E hold?", status="running", stage="Running"),
        ]
    )
    assert [c["outcome"] for c in b["claims"]] == [
        "refuted", "confirmed", "inconclusive", "pending", "broken"
    ]  # fmt: skip
    assert b["counts"] == {
        "refuted": 1,
        "confirmed": 1,
        "inconclusive": 1,
        "pending": 1,
        "broken": 1,
    }
    refuted = b["claims"][0]
    assert refuted["run_id"] == 2 and [k["kind"] for k in refuted["checks"]] == [
        "claim",
        "claim",
        "validation",
    ]


def test_drafts_and_cancelled_runs_are_not_claims():
    b = board(
        [
            run(1, "Q?", status="draft"),
            run(2, "R?", status="planning"),
            run(3, "S?", status="cancelled"),
        ]
    )
    assert b["total"] == 0 and board_md(b) == ""


def test_reruns_group_and_the_best_newest_outcome_leads():
    b = board(
        [
            run(1, "Does A hold?", status="failed"),
            run(
                2,
                "Does A hold? (reworded by a re-plan)",
                checks=[OK_VAL, INF_BAD],
                rerun_of=1,
            ),
            run(3, "Does A hold? again", checks=[OK_VAL, CLAIM_OK], rerun_of=2),
            run(4, "Does A hold? later", status="failed", rerun_of=3),
        ]
    )
    assert b["total"] == 1
    c = b["claims"][0]
    assert (
        c["outcome"] == "confirmed" and c["run_id"] == 3
    )  # a later broken try does not hide it
    assert [a["run_id"] for a in c["attempts"]] == [4, 3, 2, 1]


def test_same_question_in_the_same_report_groups_without_rerun_link():
    b = board(
        [
            run(1, "Does A hold?", status="failed"),
            run(2, "does a hold", checks=[OK_VAL, CLAIM_OK]),
        ]
    )
    assert b["total"] == 1 and b["claims"][0]["outcome"] == "confirmed"
    # a different report asking the same thing is a separate claim
    b2 = board(
        [
            run(1, "Does A hold?", checks=[OK_VAL, CLAIM_OK]),
            run(2, "Does A hold?", checks=[OK_VAL, CLAIM_OK], session=2),
        ]
    )
    assert b2["total"] == 2


def test_a_new_attempt_in_progress_replaces_a_broken_lead():
    b = board(
        [
            run(1, "Q?", status="failed"),
            run(2, "Q?", status="smoke", stage="Pilot", rerun_of=1),
        ]
    )
    assert b["claims"][0]["outcome"] == "pending" and b["claims"][0]["run_id"] == 2


def test_markdown_lists_claim_checks_with_expected_and_got():
    md = board_md(
        board(
            [run(2, "Does B hold?", checks=[OK_VAL, CLAIM_OK, CLAIM_BAD])],
            {1: {"prompt": "Ising"}},
        )
    )
    assert "## Claims tested by Lab runs" in md and "**REFUTED**: Does B hold?" in md
    assert "ustar: expected 0.61, got 0.63 (fail)" in md and "sanity" not in md

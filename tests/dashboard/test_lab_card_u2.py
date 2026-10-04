"""U2 (v0.60.0): the calm Lab card and the review dialog's decision summary.

The card logic (status phrase, the one primary button, the "..." menu, the criteria-changed
box) runs for real in node against a tiny DOM-free stub, so the tests check behaviour, not
source text. Skipped where node is missing (CI installs it for `node --check`).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from deepresearch.dashboard import lab as labm

STATIC = Path(labm.__file__).parent / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node not installed")

# just enough of app.js for lab.js to load; LAB is a const object literal
PRELUDE = r"""
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clip = (s, n) => (s = String(s || "").replace(/\s+/g, " ").trim()).length > n ? s.slice(0, n - 1) + "\u2026" : s;
const renderMd = (s) => String(s); const WS = { q: (u) => u }; const S = {};
const $ = () => null; const $$ = () => [];
"""


def run_js(expr: str):
    src = (STATIC / "lab.js").read_text()
    code = (
        PRELUDE
        + src
        + f"\n;process.stdout.write(JSON.stringify((() => {{ {expr} }})()));"
    )
    out = subprocess.run(
        [NODE, "-e", code], capture_output=True, text=True, timeout=30, check=True
    ).stdout
    return json.loads(out)


def card(r: dict) -> dict:
    return run_js(
        f"const r = {json.dumps(r)}; LAB.withChildren([r].concat(r._kids || []));"
        "const h = LAB.runHtml(r);"
        'const btns = [...h.matchAll(/<button[^>]*data-la="([a-z]+)"[^>]*>/g)].map(m => m[1]);'
        'const prim = [...h.matchAll(/class="btn small primary" data-la="([a-z]+)"/g)].map(m => m[1]);'
        "return { status: LAB.statusPhrase(r), btns, prim, html: h,"
        "  more: LAB.moreItems(r).filter(x => !x.sep).map(x => x.id) };"
    )


PLAN = {
    "title": "T",
    "question": "Q?",
    "script": "echo hi",
    "resources": {"partition": "standard"},
}


@pytest.mark.parametrize(
    "run, phrase, primary",
    [
        ({"status": "draft"}, "Needs your review", ["review"]),
        ({"status": "queued", "job_id": "9"}, "Waiting for a node", ["log"]),
        ({"status": "running", "job_id": "9", "elapsed": "00:12:30"}, "Running, 12 min", ["log"]),
        ({"status": "smoke"}, "Pilot on the check node", []),
        ({"status": "failed", "job_id": "9"}, "Failed", ["fixfailed"]),
        ({"status": "completed", "result_md": "x", "assessment": {"outcome": "confirmed"}}, "Confirmed", ["nb"]),
        ({"status": "completed", "result_md": "x", "assessment": {"outcome": "refuted"}}, "Refuted", ["nb"]),
        ({"status": "cancelled", "job_id": "9"}, "Stopped", []),
    ],
)  # fmt: skip
def test_one_status_phrase_and_at_most_one_primary_button(run, phrase, primary):
    c = card({"id": 7, "plan": PLAN, **run})
    assert c["status"]["text"] == phrase
    assert c["prim"] == primary  # never two primary buttons on a card
    # visible row: primary + Details + "..."; the rest live in the menu
    assert c["btns"].count("more") == 1
    assert set(c["btns"]) - {"more", "details", *primary} == set() or c["btns"] == [
        *primary,
        "more",
    ]


def test_a_broken_run_with_a_fix_draft_says_the_fix_is_ready():
    r = {"id": 5, "plan": PLAN, "status": "failed", "job_id": "9", "_kids": [{"id": 6, "rerun_of": 5, "status": "draft", "plan": PLAN}]}  # fmt: skip
    assert card(r)["status"]["text"] == "Failed: a fix is ready"
    r2 = {**r, "status": "completed", "assessment": {"outcome": "broken"}}
    assert card(r2)["status"]["text"] == "Broken: a fix is ready"
    r3 = {**r, "_kids": [{"id": 6, "rerun_of": 5, "status": "completed", "plan": PLAN}]}
    assert (
        card(r3)["status"]["text"] == "Failed"
    )  # the fix already ran: nothing waiting


def test_menu_holds_the_other_actions_and_stop_or_delete():
    done = card(
        {"id": 7, "plan": PLAN, "status": "completed", "job_id": "9", "result_md": "x"}
    )
    assert done["more"] == ["plan", "log", "rerun", "del"]  # Notebook is the primary
    live = card({"id": 8, "plan": PLAN, "status": "running", "job_id": "9"})
    assert live["more"] == [
        "plan",
        "cancel",
    ]  # Live log is the primary; no Delete while live
    for c in (done, live):  # nothing in the menu duplicates the primary button
        assert not set(c["prim"]) & set(c["more"])


def test_exit_codes_node_cost_and_pilot_rounds_live_under_details():
    r = {
        "id": 7, "plan": PLAN, "status": "completed", "job_id": "123", "node": "n-1",
        "elapsed": "00:03:00", "exit_code": "0:0", "estimate_usd": 0.4, "ai_cost_usd": 0.02,
        "provenance": {"fingerprint": "abcd", "sources": []},
        "smoke": {"rounds": [{"round": 1, "passed": True, "rc": 0, "seconds": 8}]},
    }  # fmt: skip
    h = card(r)["html"]
    head, details = h.split('class="lab-details"', 1)
    for s in ("job 123", "0:0", "$0.40", "abcd", "round 1: passed"):
        assert s not in head and s in details, s
    assert "lab-steps" not in head  # the step bar shows only while active
    live = card({**r, "status": "running"})["html"]
    assert "Step 4 of 7: running" in live.split('class="lab-details"', 1)[0]


def test_card_escapes_titles_and_errors():
    evil = "<img src=x onerror=alert(1)>"
    r = {"id": 7, "status": "failed", "job_id": "9", "error": evil,
         "plan": {**PLAN, "title": evil, "question": evil}}  # fmt: skip
    h = card(r)["html"]
    assert "<img" not in h and h.count("&lt;img") >= 3


def test_decision_summary_shows_where_cost_referee_and_changed_criteria():
    p = {
        **PLAN, "approach": "Fit the decay rate (approx. 2 per s) with 0.5% noise. Then compare.",
        "software": [{"name": "numpy"}], "success_criteria": "rate within 25%",
        "resources": {"partition": "standard", "cores": 4, "time_limit": "00:30:00"},
        "review": {"verdict": "concerns", "findings": [{"severity": "high"}]},
        "plan_before_fix": {"success_criteria": "rate within 5%"},
    }  # fmt: skip
    h = run_js(
        f"LAB.partitions = {{standard: {{cpus: 32}}}}; return LAB.decisionHtml({{id: 3, estimate_usd: 0.42, plan: {json.dumps(p)}}}, {json.dumps(p)}, true);"
    )
    assert (
        "Fit the decay rate (approx. 2 per s) with 0.5% noise." in h
    )  # whole first sentence, decimals kept
    assert "standard, 4 cores, up to 00:30:00" in h and "$0.42" in h
    assert "has concerns, 1 finding" in h
    assert "Success criteria changed by the AI fix" in h
    assert "rate within 5%" in h and "rate within 25%" in h
    assert 'data-x="sumsubmit"' in h
    same = {**p, "plan_before_fix": {"success_criteria": "rate within 25%"}}
    h2 = run_js(
        f"return LAB.decisionHtml({{id: 3, plan: {json.dumps(same)}}}, {json.dumps(same)}, false);"
    )
    assert (
        "Success criteria changed" not in h2 and "sumsubmit" not in h2
    )  # read-only: no Submit


def test_criteria_change_from_a_fixed_failed_run_diff():
    p = {**PLAN, "success_criteria": "b", "fix_diff": {"fields": [{"key": "success_criteria", "before": '"a"', "after": '"b"'}]}}  # fmt: skip
    cc = run_js(f"return LAB.criteriaChange({json.dumps(p)});")
    assert cc == {"src": "the fix of the failed run", "before": "a", "after": "b"}


def test_fix_notes_drop_review_lines_without_leaving_fragments():
    notes = 'Fixed the CSV path. REVIEW: expected values changed (removed "0.5% - 20.0%"; added "0.1%"). Then normalised columns.'
    assert run_js(f"return LAB.fixNotes({json.dumps(notes)});") == (
        "Fixed the CSV path. Then normalised columns."
    )
    only = 'REVIEW: the verdict values changed (removed "0.5% - 20.0%", added f">= {min_fires)'
    assert run_js(f"return LAB.fixNotes({json.dumps(only)});") == ""


def test_review_sections_are_folded_below_the_summary():
    src = (STATIC / "lab.js").read_text()
    i = src.index("  review(el, s, r, readOnly = false) {")
    body = src[i : src.index("\n  async loadTargets()", i)]
    assert body.index("this.decisionHtml(r, p, editable)") < body.index(
        'class="lab-toc"'
    )
    folds = re.findall(r'<details class="lab-fold" id="lr-sec-(\d)">', body)
    assert folds == ["1", "2", "3", "4", "5"]
    assert "d.open = true" in body  # the contents links open a folded section

"""Adversarial review of a Lab plan before submit (v0.46.0).

A second model pass reads the reviewed plan the way a sceptical referee would and asks:

- Could this test ever FAIL? (a claim check with a tolerance so wide, an expected value
  computed from the same code, or a comparison whose arms cannot differ, always passes)
- Could it ever PASS? (a threshold the method cannot reach at this sample size, a signal
  below the noise floor, a pilot too small to show anything)
- Does it test the report's claim, or something easier next to it?
- Are the informative checks real controls, or restatements of the claim?

The answer is advice only: it never edits the plan and never blocks submit. Findings are
stored on the plan (`plan.review`) so the person reviewing the draft sees them next to the
pre-flight warnings, with a one-click "Fix with AI" that passes them to the existing fixer.

Run #36 (Kepler-10b): the alias check compared BLS and TLS where BLS was already fine, so
the pilot could never discriminate. Run #41: the injected planet sat below the noise floor
(SDE 4.7 vs the 6.0 threshold), so the claim could never be judged. Both are exactly what
"could it ever pass / fail?" catches before a cluster round is spent.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

REVIEW_PROMPT = """You are a sceptical referee reviewing a computational test BEFORE it runs on
an HPC cluster. The plan below was written to test a claim from a research report. Do not
rewrite it. Find the ways the test could be meaningless.

Ask, for every check the script writes to outputs/verdict.json and for the design:
1. Could this test ever FAIL? A claim check that passes whatever the result (tolerance so
   wide it cannot fail, "expected" computed by the same code as "got", a comparison whose
   two arms are identical or cannot differ, a check on a value the script sets itself).
2. Could it ever PASS? A threshold the method cannot reach with these sizes, samples or
   run time; a signal below the noise; a pilot (LAB_SMOKE=1) too small for the
   informative checks to show anything; a required input that is likely missing.
3. Does it test the report's claim, or an easier question next to it?
4. Are the informative checks real controls (a positive control, the regime check, a
   baseline that is neither ~0% nor ~100%), or restatements of the claim?
5. Parameters: any value that decides the outcome but has no source or is implausible?

Report only real problems you can point to in the plan (quote the check name, parameter
or line). No style advice. If the test is sound, say so with an empty findings list.

Facts about this cluster (do not flag these as problems): compute nodes HAVE outbound
internet, so jobs may download data and build software from source; the harness installs
what "install" lists before the script runs; LAB_SMOKE=1 marks the cut-down pilot run.

REPORT QUESTION / CLAIM:
{question}

SUCCESS CRITERIA IN THE PLAN:
{criteria}

PLAN (JSON, script included):
{plan}

Return JSON only, in a ```json block:
{{"verdict": "sound" | "concerns" | "flawed",
"findings": [{{"severity": "high" | "medium" | "low",
  "kind": "cannot_fail" | "cannot_pass" | "wrong_question" | "weak_control" | "parameter" | "other",
  "where": "check name, parameter or script line",
  "problem": "one or two sentences",
  "suggestion": "one sentence: what to change"}}],
"summary": "one sentence for the reviewer"}}"""

KINDS = (
    "cannot_fail",
    "cannot_pass",
    "wrong_question",
    "weak_control",
    "parameter",
    "other",
)
SEVERITIES = ("high", "medium", "low")
VERDICTS = ("sound", "concerns", "flawed")
PLAN_CHARS = 50_000


def build_prompt(plan: dict, question: str = "") -> str:
    body = {
        k: v
        for k, v in plan.items()
        if k not in ("warnings", "review", "plan_before_fix", "fix_diff", "url_checks")
    }
    return REVIEW_PROMPT.format(
        question=(question or plan.get("question") or plan.get("title") or "")[:2000],
        criteria=str(plan.get("success_criteria") or "(none stated)")[:2000],
        plan=json.dumps(body, indent=1)[:PLAN_CHARS],
    )


def normalize(out: Any) -> dict:
    """A review dict with known values only; raises ValueError if unusable."""
    if not isinstance(out, dict):
        raise ValueError("review is not a JSON object")
    verdict = str(out.get("verdict") or "").lower().strip()
    findings = []
    for f in out.get("findings") or []:
        if not isinstance(f, dict) or not str(f.get("problem") or "").strip():
            continue
        sev = str(f.get("severity") or "medium").lower()
        kind = str(f.get("kind") or "other").lower()
        findings.append(
            {
                "severity": sev if sev in SEVERITIES else "medium",
                "kind": kind if kind in KINDS else "other",
                "where": str(f.get("where") or "")[:200],
                "problem": str(f.get("problem") or "")[:600],
                "suggestion": str(f.get("suggestion") or "")[:400],
            }
        )
    if verdict not in VERDICTS:
        verdict = "concerns" if findings else "sound"
    # a "sound" verdict with high-severity findings is not sound
    if verdict == "sound" and any(f["severity"] == "high" for f in findings):
        verdict = "concerns"
    findings.sort(key=lambda f: SEVERITIES.index(f["severity"]))
    return {
        "verdict": verdict,
        "findings": findings[:12],
        "summary": str(out.get("summary") or "")[:400],
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
    }


def as_problems(review: dict | None) -> list[str]:
    """Findings as fixer input lines ("Fix with AI" passes these as the problems)."""
    if not review:
        return []
    return [
        f"Referee ({f['severity']}, {f['kind'].replace('_', ' ')}) at {f['where'] or 'plan'}: "
        f"{f['problem']} Suggested: {f['suggestion']}".strip()
        for f in review.get("findings") or []
        if f.get("severity") in ("high", "medium")
    ]


def summary_line(review: dict | None) -> str:
    if not review:
        return ""
    n = len(review.get("findings") or [])
    high = sum(1 for f in review.get("findings") or [] if f.get("severity") == "high")
    if review.get("verdict") == "sound" and not n:
        return "Referee: the test looks sound."
    return (
        f"Referee: {review.get('verdict')}, {n} finding{'s' if n != 1 else ''}"
        + (f" ({high} high)" if high else "")
        + (f". {review['summary']}" if review.get("summary") else "")
    )

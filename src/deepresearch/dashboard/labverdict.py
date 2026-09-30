"""Lab verdicts: what a finished run's checks mean (v0.39.0).

A run's `outputs/verdict.json` holds checks. Pass/fail alone mislabels science: run #79
"failed" because both arms saturated at 0% extinction (the test could not tell), while
run #98 "failed" because the claim was false. Both showed the same red label.

Each check has a kind:
- validation: the model or data is sane (kernel matches data, Monte Carlo matches the
  exact solution, boundary conditions). Must pass, or nothing else can be trusted.
- informative: the test can discriminate (baseline arm neither 0% nor 100%, positive
  control persists, right regime, no clipped rates).
- claim: the report's claim itself, tested two-sided; may go either way.

A run gets one outcome:
- confirmed: validation and informativeness hold and every claim check passed.
- refuted: validation and informativeness hold and a claim check failed.
- inconclusive: the test could not tell (an informativeness check failed, the arms of a
  comparison came out identical, or the claim check shows the same value on both sides).
- broken: the job failed, or a validation check failed.

Checks from before v0.39.0 have no kind; it is inferred from the name and the expected
value, and the result says so (`inferred: true`). Pure functions, no model calls.
"""

from __future__ import annotations

import re
from typing import Any

KINDS = ("validation", "informative", "claim")
OUTCOMES = ("confirmed", "refuted", "inconclusive", "broken")

_INFORMATIVE = re.compile(
    r"informativ|positive.control|negative.control|baseline|regime|design|"
    r"discriminat|not.saturat|no.rate.clipping|not.doomed|at.risk",
    re.I,
)
_CLAIM = re.compile(
    r"claim|rescue|underestimat|overestimat|outperform|reduc|increas|decreas|"
    r"improv|superior|inferior|faster|slower|significan|differ|distance.from|"
    r"better|worse|beats?\b|exceed",
    re.I,
)
_NUM = re.compile(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", re.I)


def check_kind(c: dict) -> tuple[str, bool]:
    """(kind, inferred). A declared kind wins; otherwise infer from the name/expected."""
    k = str(c.get("kind") or "").lower().strip()
    if k in KINDS:
        return k, False
    if k in ("validate", "sanity", "known-answer", "known_answer"):
        return "validation", False
    if k in ("informativeness", "design", "control"):
        return "informative", False
    name = str(c.get("name") or "")
    exp = c.get("expected")
    if re.search(r"\bclaim\b", name, re.I):
        return "claim", True
    if _INFORMATIVE.search(name):
        return "informative", True
    # a comparison between two computed quantities ("A < B") is a claim; a number with a
    # tolerance is a known-answer (validation) check
    if isinstance(exp, str) and re.search(r"[<>]|\bvs\.?\b", exp):
        return "claim", True
    if _CLAIM.search(name):
        return "claim", True
    return "validation", True


def _saturated(c: dict) -> bool:
    """A failed comparison whose two sides came out equal: the test could not tell
    (run #79: 'P_ext_random=0.0%, P_ext_clustered=0.0%'; #78: 'Discrete=0.5168,
    Diffusion=0.5168')."""
    got = c.get("got")
    if not isinstance(got, str):
        return False
    nums = [float(x) for x in _NUM.findall(got.replace(",", " "))]
    # the first two numbers are the two arms; digits inside names (Nc=3) are rare here
    # and only make this say "saturated" when both sides really print the same value
    if len(nums) == 1:
        # a single effect size of exactly zero ("0.0%", "+0.0000"): no effect at all
        return nums[0] == 0.0
    return len(nums) >= 2 and abs(nums[0] - nums[1]) <= 1e-12 * max(1.0, abs(nums[0]))


def _short(name: Any, limit: int = 70) -> str:
    """A check name for a sentence: no half-cut parentheses."""
    n = " ".join(str(name or "?").split())
    if len(n) <= limit:
        return n
    head = n.split(" (")[0]
    return head if 12 <= len(head) <= limit else n[: limit - 3].rstrip() + "..."


def assess(verdict: dict | None, status: str = "completed") -> dict[str, Any] | None:
    """Outcome of a run from its verdict. None when there is nothing to judge yet."""
    if status == "failed":
        return {
            "outcome": "broken",
            "why": "The job failed, so it tested nothing.",
            "groups": {},
            "inferred": False,
        }
    if status != "completed":
        return None
    if not isinstance(verdict, dict) or not verdict.get("checks"):
        return None
    groups: dict[str, list[dict]] = {k: [] for k in KINDS}
    inferred = False
    for c in verdict["checks"]:
        if not isinstance(c, dict):
            continue
        kind, inf = check_kind(c)
        inferred = inferred or inf
        groups[kind].append(c)
    audit = verdict.get("audit") or []
    names = lambda cs: "; ".join(_short(c.get("name")) for c in cs[:3])  # noqa: E731

    bad_val = [c for c in groups["validation"] if c.get("pass") is False]
    if bad_val:
        return _out(
            "broken",
            f"A validation check failed ({names(bad_val)}), so the model itself is not "
            "trustworthy and its other numbers should not be read as findings.",
            groups, inferred,
        )  # fmt: skip
    bad_inf = [c for c in groups["informative"] if c.get("pass") is False]
    if bad_inf:
        return _out(
            "inconclusive",
            f"The test could not discriminate ({names(bad_inf)}): the design needs "
            "different parameters before the claim can be judged.",
            groups, inferred,
        )  # fmt: skip
    identical = [a for a in audit if str(a).startswith("IDENTICAL")]
    if identical:
        return _out(
            "inconclusive",
            "The two arms of the comparison produced identical numbers, so the "
            "comparison did not really run.",
            groups, inferred,
        )  # fmt: skip
    claims = groups["claim"]
    bad_claim = [c for c in claims if c.get("pass") is False]
    if bad_claim and all(_saturated(c) for c in bad_claim):
        return _out(
            "inconclusive",
            f"The claim check found no difference at all between the compared values "
            f"({names(bad_claim)}), so the test could not tell: both arms saturated, or "
            "the parameters sit where the two cannot differ. Change the design so the "
            "baseline is neither certain nor impossible.",
            groups, inferred,
        )  # fmt: skip
    if bad_claim:
        ok = len(claims) - len(bad_claim)
        return _out(
            "refuted",
            f"The claim did not hold ({names(bad_claim)})"
            + (f"; {ok} of {len(claims)} claim checks passed" if ok else "")
            + ". Validation and design checks passed, so this is a real negative result.",
            groups, inferred,
        )  # fmt: skip
    unknown = [c for c in claims if c.get("pass") is not True]
    if unknown:
        return _out(
            "inconclusive", "A claim check has no pass/fail value.", groups, inferred
        )
    if claims:
        return _out(
            "confirmed",
            f"Every claim check passed ({names(claims)}), with validation and design "
            "checks holding.",
            groups, inferred,
        )  # fmt: skip
    if verdict.get("pass") is True:
        return _out(
            "confirmed",
            "Every known-answer check passed (a reproduction; no separate claim test).",
            groups, inferred,
        )  # fmt: skip
    return _out("inconclusive", "No overall pass value.", groups, inferred)


def _out(outcome: str, why: str, groups: dict, inferred: bool) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "why": why,
        "inferred": inferred,
        "groups": {
            k: [
                {
                    "name": c.get("name"),
                    "pass": c.get("pass"),
                    "expected": c.get("expected"),
                    "got": c.get("got"),
                }
                for c in v
            ]
            for k, v in groups.items()
        },
    }


def outcome_line(run: dict) -> str:
    """One line for summaries and lists, e.g. 'REFUTED: the claim did not hold (...)'."""
    a = run.get("assessment") or assess(run.get("verdict"), run.get("status") or "")
    if not a:
        return "no verdict"
    return f"{a['outcome'].upper()}: {a['why']}"


PLANNER_RULES = [
    'Label every check in outputs/verdict.json with "kind": "validation" (the model or '
    "data is sane: reproduces a known value, Monte Carlo matches an exact result, "
    'boundary conditions hold), "informative" (the test can discriminate: the baseline '
    "arm is neither ~0% nor ~100%, a positive control behaves as expected, the "
    "parameters are in the regime where the claim could show, no clipped rates) or "
    '"claim" (the report\'s claim itself). Include at least one informative check '
    "whenever the job compares arms or tests a claim.",
    "Test the claim two-sided: report the sign and size of the effect (e.g. 'A=0.62, "
    "B=0.70, diff=-0.08'), not only whether the expected direction held. Put the two "
    'compared values first in "got". A claim check may fail; that is a result.',
    "Match the arms: only the factor under test may differ between them (same "
    "demography, same dispersal, same seeds where possible).",
    'Calibrate from the literature: fill "parameter_sources" with a citation, or '
    '"assumed" and why, for every parameter, and print derived quantities that show '
    "the regime (e.g. low-density growth rate, carrying capacity vs starting size, the "
    "threshold the claim is about) so saturation is visible in the log.",
]

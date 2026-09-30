"""Lab verdict loop (v0.39.0): notes on the report, pilot gate, one automatic re-plan.

Mixed into `Lab` (lab.py). Three jobs:

1. `_attach_note`: a finished run attaches one note to its report (an annotation, the
   same kind a reader makes by highlighting). The report text is never edited. The note
   carries the outcome and the result in two or three sentences; the full write-up stays
   on the run and feeds summaries and audio (projects.lab_findings).
2. `_pilot_gate`: the cut-down pilot (LAB_SMOKE=1, formerly "smoke test") writes its own
   verdict.json. When its checks say the test cannot discriminate (an informative check
   failed, or both arms saturated), the full run is not started: that would spend an
   hour of cluster time to learn nothing.
3. `_maybe_auto_replan`: an INCONCLUSIVE run (pilot or full) is re-planned once by the AI,
   which is told why the test could not tell and must change the design (parameters,
   arms, controls) with literature sources. The new plan is a draft: a person still
   reviews and submits it. Once means once per original run: a re-plan of a re-plan is
   never made automatically.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime
from typing import Any

from deepresearch.dashboard import labverdict

NOTE_PREFIX = "Lab run #"
_AUTO_REPLANNING: set[tuple[str, int]] = (
    set()
)  # run ids whose re-plan runs in this process

REPLAN_PROMPT = """A computational job you planned to test a claim from a research report
ran and was INCONCLUSIVE: the test could not tell whether the claim holds.

WHY IT WAS INCONCLUSIVE: {why}

CHECKS (outputs/verdict.json): {checks}

RESULTS NOTE:
{note}

LOG TAIL:
{log}

{target}

{lessons}

PLAN THAT RAN (JSON):
{plan}

Redesign the job so the test CAN discriminate, then return the complete new plan. Keep
the question, software and outputs; change the design:
- Put the model in the regime where the claim could show: if both arms saturated (0% or
  100% everywhere), change the parameters (density, area, starting sizes, rates, carrying
  capacity, horizon) so the baseline arm is neither certain nor impossible.
- Calibrate every parameter from the literature (use web search, at most 5) and fill
  "parameter_sources" with a citation or "assumed: why" per parameter.
- Match the arms: only the factor under test may differ.
- Add or tighten "informative" checks that would have caught this (baseline arm between
  5% and 95%, a positive control, the regime printed in the log), and keep the claim
  check two-sided so it may come out either way.
- Make LAB_SMOKE=1 compute the same checks on a small sample so the pilot can catch
  saturation before the full run.
Do not change the claim to one that is easier to confirm.

Return JSON only, in a ```json block:
{{"plan": <the complete new plan, same keys>,
"changes": ["one short line per design change, and why"],
"notes": "anything the reviewer should know, or empty"}}"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def result_gist(md: str, limit: int = 600) -> str:
    """The **Result** paragraph of a Lab write-up, plain text."""
    md = md or ""
    m = re.search(
        r"\*\*Result\*\*:?\s*(.+?)(?:\n\s*\*\*[A-Z][^*]{2,40}\*\*|\Z)", md, re.S
    )
    text = m.group(1) if m else md
    text = re.sub(r"[*_`#>]+", "", text)
    text = " ".join(text.split())
    text = re.sub(r"^Result:?\s+", "", text)
    return text[:limit].rsplit(" ", 1)[0] + "..." if len(text) > limit else text


def anchor_quote(report: str, run: dict) -> str:
    """Text in the report the note hangs on: the highlighted passage for a selection
    run, else the report sentence that shares the most words with the run's question."""
    if run.get("scope") == "selection" and (run.get("selection") or "").strip():
        sel = " ".join(str(run["selection"]).split())
        return sel[:300]
    plan = run.get("plan") or {}
    q = " ".join(str(plan.get(k) or "") for k in ("question", "title")).lower()
    tok = lambda t: {w.rstrip("s") for w in re.findall(r"[a-z][a-z0-9]{3,}", t)}  # noqa: E731
    words = tok(q)
    sents: list[tuple[str, set[str]]] = []
    for para in (report or "").split("\n"):
        if para.lstrip().startswith(("#", "|", "[")):
            continue
        for sent in re.split(r"(?<=[.!?])\s+", para):
            s = sent.strip()
            if not 40 <= len(s) <= 400:
                continue
            clean = re.sub(r"\[cite:[^\]]*\]", "", s).strip()
            sents.append((clean, tok(clean.lower())))
    # rare words (fertilization, spawning) outweigh common ones (population, risk)
    import math

    df: dict[str, int] = {}
    for _, ws in sents:
        for w in ws & words:
            df[w] = df.get(w, 0) + 1
    n = max(1, len(sents))
    best, score = "", 0.0
    for clean, ws in sents:
        hit = sum(math.log(1 + n / df[w]) for w in ws & words)
        if hit > score:
            best, score = clean, hit
    if best:
        # up to the first markup character, so the reader can find it in the rendered text
        best = re.split(r"[*_`\[]", best)[0].strip()
    return best[:300]


class LabVerdictMixin:
    """Methods mixed into Lab. Relies on Lab's get/create/_update/_ask/_add_cost/target."""

    db_path: str

    # ---- 1. attach a note to the report ----------------------------------
    def _attach_note(self, run: dict) -> int | None:
        """Add or refresh this run's note on its report. Returns the annotation id."""
        if run.get("status") != "completed":
            return None
        a = run.get("assessment") or labverdict.assess(run.get("verdict"), "completed")
        title = str((run.get("plan") or {}).get("title") or "untitled")
        head = f"{NOTE_PREFIX}{run['id']} ({title}): " + (
            f"{a['outcome'].upper()}. {a['why']}" if a else "completed (no checks)."
        )
        # the old write-ups (before v0.39.0) open with "a known-answer check failed" even
        # for a real negative result; the outcome line above already says what it was
        gist = result_gist(run.get("result_md") or "")
        if re.match(r"(?i)(result\s*)?a known-answer check failed", gist):
            gist = ""
        note = head + (f"\n\n{gist}" if gist else "")
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            tbl = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='annotations'"
            ).fetchone()
            if not tbl:
                return None  # dashboard store not initialised (CLI-only use)
            cur = conn.execute(
                "SELECT id FROM annotations WHERE session_id = ? AND note LIKE ?",
                (run["session_id"], f"{NOTE_PREFIX}{run['id']} (%"),
            ).fetchone()
            if cur:
                conn.execute(
                    "UPDATE annotations SET note = ?, updated_at = ? WHERE id = ?",
                    (note, _now(), cur["id"]),
                )
                conn.commit()
                return int(cur["id"])
            rep = conn.execute(
                "SELECT result FROM sessions WHERE id = ?", (run["session_id"],)
            ).fetchone()
            quote = anchor_quote(rep["result"] if rep else "", run) or title
            color = {
                "confirmed": "green",
                "refuted": "magenta",
                "inconclusive": "amber",
                "broken": "amber",
            }.get(a["outcome"] if a else "", "cyan")
            c = conn.execute(
                "INSERT INTO annotations (session_id, quote, occurrence, note, color, "
                "created_at, updated_at) VALUES (?, ?, 0, ?, ?, ?, ?)",
                (run["session_id"], quote, note, color, _now(), _now()),
            )
            conn.commit()
            return int(c.lastrowid or 0)
        except sqlite3.Error:
            return None  # a note is a bonus; never fail a finished run over it
        finally:
            conn.close()

    # ---- 2. pilot gate ------------------------------------------------------
    def _pilot_gate(self, run: dict, tgt) -> dict | None:
        """The pilot's own verdict, assessed. Returns the assessment when the pilot says
        the test cannot discriminate (the full run should not start), else None."""
        try:
            raw = tgt.read_file(run["id"], "smoke/outputs/verdict.json", 200000)
        except Exception:
            return None
        if not raw.strip():
            return None
        try:
            v = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(v, dict) or not isinstance(v.get("checks"), list):
            return None
        a = labverdict.assess(
            {"checks": [c for c in v["checks"] if isinstance(c, dict)][:50]},
            "completed",
        )
        # only "cannot discriminate" stops the full run: a validation miss on a tiny
        # sample is often noise, and a refuting pilot is exactly what the full run checks
        if a and a["outcome"] == "inconclusive":
            return a
        return None

    # ---- 3. one automatic re-plan ------------------------------------------
    def _auto_replan_allowed(self, run: dict) -> bool:
        plan = run.get("plan") or {}
        if plan.get("auto_replan_of") or plan.get("auto_replanned"):
            return False  # already the product of one, or already re-planned (pilot)
        with self._conn() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                "SELECT plan FROM lab_runs WHERE rerun_of = ?", (run["id"],)
            ).fetchall()
        for r in rows:
            try:
                if (json.loads(r[0] or "{}") or {}).get("auto_replan_of") == run["id"]:
                    return False
            except ValueError:
                continue
        return True

    def _maybe_auto_replan(self, run: dict, pilot: bool = False) -> bool:
        """Start the one automatic re-plan in the background. True when started."""
        key = self._key(run["id"])  # type: ignore[attr-defined]
        if key in _AUTO_REPLANNING or not self._auto_replan_allowed(run):
            return False
        _AUTO_REPLANNING.add(key)
        threading.Thread(
            target=self._auto_replan, args=(run["id"], pilot), daemon=True
        ).start()
        return True

    def _auto_replan(self, run_id: int, pilot: bool) -> None:
        try:
            self._auto_replan_inner(run_id, pilot)
        except Exception as e:
            run = self.get(run_id) or {}  # type: ignore[attr-defined]
            if pilot and run.get("status") == "draft":
                self._update(  # type: ignore[attr-defined]
                    run_id, only_if=("draft",),
                    stage="Pilot could not discriminate; automatic re-plan failed",
                    error=f"automatic re-plan failed: {str(e)[:300]}",
                )  # fmt: skip
        finally:
            _AUTO_REPLANNING.discard(self._key(run_id))  # type: ignore[attr-defined]

    def replan_prompt(self, run: dict, assessment: dict, log: str) -> str:
        from deepresearch.dashboard import labguard
        from deepresearch.dashboard.lab import PLAN_DIFF_SKIP, _describe

        tgt = self.target(run.get("target"))  # type: ignore[attr-defined]
        plan = {
            k: v for k, v in (run.get("plan") or {}).items() if k not in PLAN_DIFF_SKIP
        }
        checks = (run.get("verdict") or {}).get("checks") or [
            c for g in (assessment.get("groups") or {}).values() for c in g
        ]
        return REPLAN_PROMPT.format(
            why=assessment.get("why") or "",
            checks=json.dumps(checks)[:4000],
            note=(run.get("result_md") or "(none: stopped after the pilot)")[:6000],
            log=log[-5000:] or "(none)",
            target=_describe(tgt, full=True) if tgt else "",
            lessons=labguard.prompt_block(
                json.dumps(plan)[:20000], getattr(self, "state_dir", None)
            ),
            plan=json.dumps(plan, indent=1)[:60000],
        )

    def _auto_replan_inner(self, run_id: int, pilot: bool) -> None:
        from deepresearch.dashboard.lab import (
            PLAN_DIFF_SKIP,
            build_sbatch,
            estimate_cost,
            plan_diff,
        )

        run = self.get(run_id)  # type: ignore[attr-defined]
        if not run:
            return
        if pilot:
            assessment = (run.get("smoke") or {}).get("pilot") or {}
            log = str(
                ((run.get("smoke") or {}).get("rounds") or [{}])[-1].get("log_tail")
                or ""
            )
        else:
            assessment = run.get("assessment") or {}
            p = self.results_dir / f"run_{run_id}" / "job.log"  # type: ignore[attr-defined]
            log = p.read_text("utf-8", "replace") if p.exists() else ""
        prompt = self.replan_prompt(run, assessment, log)
        new, out, why = self._ask_plan(run_id, prompt, search=True)  # type: ignore[attr-defined]
        if new is None:
            raise ValueError(f"the AI returned no usable plan twice ({why})")
        changes = [str(c) for c in (out.get("changes") or [])][:20]  # type: ignore[union-attr]
        old = {
            k: v for k, v in (run.get("plan") or {}).items() if k not in PLAN_DIFF_SKIP
        }
        new["fix_changes"] = [f"re-plan after inconclusive run: {c}" for c in changes]
        new["fix_notes"] = str(out.get("notes") or "")  # type: ignore[union-attr]
        new["fix_diff"] = plan_diff(old, new)
        new["auto_replan_of"] = run_id
        why = str(assessment.get("why") or "")[:300]
        if pilot:
            # same run, back to review with the redesigned plan (undo restores the old)
            tgt = self.target(run.get("target"))  # type: ignore[attr-defined]
            new["plan_before_fix"] = old
            new["auto_replanned"] = True
            new["warnings"] = self._check(tgt, new)  # type: ignore[attr-defined]
            self._update(  # type: ignore[attr-defined]
                run_id, only_if=("draft",), plan=new,
                stage="Pilot could not discriminate; re-planned by AI: review, then submit",
                error=None,
                script=build_sbatch(run_id, new, tgt, self._safe_sources(new))  # type: ignore[attr-defined]
                if tgt else None,
                estimate_usd=estimate_cost(tgt, new),
            )  # fmt: skip
            return
        new["caveats"] = (
            str(new.get("caveats") or "")
            + f" Automatic re-plan of inconclusive run #{run_id}: {why}"
        ).strip()
        created = self.create(  # type: ignore[attr-defined]
            run["session_id"],
            run["scope"],
            run.get("selection") or "",
            run.get("request") or "",
            run.get("target"),
            rerun_of=run_id,
            plan=new,
            data_sources=list(run.get("data_sources") or []) or None,
        )
        self._update(  # type: ignore[attr-defined]
            created["id"],
            stage=f"Re-planned by AI after inconclusive run #{run_id}: review, then submit",
        )

    def pilot_verdict_view(self, run: dict) -> dict[str, Any] | None:
        return (run.get("smoke") or {}).get("pilot")

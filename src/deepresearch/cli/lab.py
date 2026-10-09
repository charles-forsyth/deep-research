"""`deep-research lab ...`: Lab runs from the command line (v0.63.0).

list, suggestions, plan, show, submit, status, cancel, log. Everything except `status`
goes through the running dashboard's local HTTP API, because that process owns the Lab:
planning runs there in a background thread, the watcher follows submitted jobs there,
and the bifrost sign-in is loaded there. A short-lived CLI process would lose all three
when it exits. `status` reads the workspace DB directly (no dashboard needed).

The CLI talks to 127.0.0.1 on the dashboard's port with the current workspace in the
X-DR-Workspace header, the same way the web page does. Writes are JSON, so they pass the
dashboard's same-origin and content-type checks. `submit` and `cancel` spend or stop
cluster work, so they ask first, or need --yes (always with --json).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from deepresearch.cli.status import LAB_ACTIVE, LAB_FINAL, LAB_WAITING

SUBMIT_TIMEOUT = 900  # submit blocks while the pilot is queued (often 1-5 minutes)
POLL_SECONDS = 5.0  # `plan --wait` polling interval


class LabCliError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# ---- talking to the dashboard ---------------------------------------------------


def _base() -> str:
    """http://127.0.0.1:<port> of the running dashboard, or LabCliError (exit 3)."""
    from deepresearch.dashboard import daemon

    state = daemon.read_state()
    if not state:
        raise LabCliError(
            "the dashboard is not running; Lab runs live there. Start it with "
            "`deep-research dashboard --start`",
            3,
        )
    return f"http://127.0.0.1:{int(state['port'])}"


def _workspace() -> str:
    from deepresearch.core import workspace as W

    return W.current_slug()


def _call(method: str, path: str, body: Any = None, timeout: float = 60) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(_base() + path, data=data, method=method)
    req.add_header("X-DR-Workspace", _workspace())
    if method != "GET":
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error") or e.reason
        except ValueError:
            msg = e.reason
        raise LabCliError(f"{msg} (HTTP {e.code})", 1) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LabCliError(f"could not reach the dashboard: {e}", 3) from e


# ---- shaping runs for people and agents -----------------------------------------


def brief(run: dict) -> dict:
    """The parts of a run worth reading: no script, no fix diffs, a short pilot tail."""
    plan = run.get("plan") if isinstance(run.get("plan"), dict) else {}
    plan = plan or {}
    rv = plan.get("review") if isinstance(plan.get("review"), dict) else None
    smoke = run.get("smoke") if isinstance(run.get("smoke"), dict) else None
    last = ((smoke or {}).get("rounds") or [None])[-1] if smoke else None
    assessment = run.get("assessment") or {}
    out = {
        "id": run.get("id"),
        "session_id": run.get("session_id"),
        "status": run.get("status"),
        "stage": run.get("stage"),
        "plan_busy": bool(run.get("plan_busy")),
        "error": run.get("error"),
        "title": plan.get("title"),
        "question": plan.get("question"),
        "approach": plan.get("approach"),
        "computable": plan.get("computable"),
        "why_not": plan.get("why_not"),
        "software": [
            s.get("name") if isinstance(s, dict) else s
            for s in plan.get("software") or []
        ],
        "parameters": plan.get("parameters"),
        "resources": plan.get("resources"),
        "expected_outputs": plan.get("expected_outputs"),
        "success_criteria": plan.get("success_criteria"),
        "warnings": plan.get("warnings") or [],
        "estimate_usd": run.get("estimate_usd"),
        "ai_cost_usd": run.get("ai_cost_usd"),
        "target": run.get("target_label") or run.get("target"),
        "review": (
            {
                "verdict": rv.get("verdict"),
                "summary": rv.get("summary"),
                "findings": [
                    {
                        k: f.get(k)
                        for k in ("severity", "kind", "where", "problem", "suggestion")
                    }
                    for f in rv.get("findings") or []
                    if isinstance(f, dict)
                ],
                "stale": bool(run.get("review_stale")),
            }
            if rv
            else None
        ),
        "review_error": plan.get("review_error"),
        "fix_changes": plan.get("fix_changes"),
        "fix_concerns": plan.get("fix_concerns"),
        "pilot": (
            {
                "rounds": len((smoke or {}).get("rounds") or []),
                "passed": last.get("passed"),
                "rc": last.get("rc"),
                "class": last.get("class"),
                "missing": last.get("missing"),
                "note": last.get("note"),
                "log_tail": str(last.get("log_tail") or "")[-1500:],
            }
            if isinstance(last, dict)
            else None
        ),
        "job_id": run.get("job_id"),
        "slurm_state": run.get("slurm_state"),
        "node": run.get("node"),
        "elapsed": run.get("elapsed"),
        "outcome": assessment.get("outcome"),
        "outcome_why": assessment.get("why"),
        "verdict": run.get("verdict"),
        "result_md": run.get("result_md"),
        "files": [f.get("path") for f in run.get("files") or [] if isinstance(f, dict)],
        "rerun_of": run.get("rerun_of"),
        "created_at": run.get("created_at"),
        "submitted_at": run.get("submitted_at"),
        "finished_at": run.get("finished_at"),
    }
    return out


def _print_brief(b: dict) -> None:
    print(f"Lab run {b['id']} (report #{b['session_id']}): {b['status']}")
    if b["stage"]:
        print(f"  {b['stage']}")
    if b["plan_busy"]:
        print("  (planning or the referee is still working on this plan)")
    if b["title"]:
        print(f"\n{b['title']}")
    for key in ("question", "approach"):
        if b[key]:
            print(f"  {key.capitalize()}: {b[key]}")
    if b["computable"] is False and b["why_not"]:
        print(f"  Not computable: {b['why_not']}")
    r = b["resources"] or {}
    if r:
        print(
            f"  Resources: {r.get('partition')}, {r.get('nodes', 1)} node(s), "
            f"time {r.get('time_limit')}, GPUs {r.get('gpus', 0)}"
        )
    if b["estimate_usd"] is not None:
        print(
            f"  Estimate: ${b['estimate_usd']:.2f} cluster, AI so far ${b['ai_cost_usd'] or 0:.2f}"
        )
    for w in b["warnings"]:
        print(f"  WARNING: {w}")
    rv = b["review"]
    if rv:
        stale = " (stale: the plan changed after it read it)" if rv["stale"] else ""
        print(f"\nReferee: {rv['verdict']}{stale}")
        for f in rv["findings"]:
            print(f"  [{f['severity']}] {f['problem']}")
    elif b["review_error"]:
        print(f"\nReferee did not run: {b['review_error']}")
    if b["fix_changes"]:
        print("\nAI fix changes:")
        for c in b["fix_changes"]:
            print(f"  - {c}")
    if b["fix_concerns"]:
        print(f"AI fix concerns: {b['fix_concerns']}")
    p = b["pilot"]
    if p:
        print(
            f"\nPilot: {'passed' if p['passed'] else 'failed'} (round {p['rounds']}, rc {p['rc']})"
        )
    if b["job_id"]:
        print(
            f"\nJob {b['job_id']} {b['slurm_state'] or ''} {b['node'] or ''} {b['elapsed'] or ''}"
        )
    if b["outcome"]:
        print(f"Outcome: {b['outcome'].upper()}: {b['outcome_why']}")
    if b["error"]:
        print(f"Error: {b['error'][:500]}")
    if b["result_md"]:
        print(f"\n{b['result_md']}")
    if b["files"]:
        print(f"\nFiles: {', '.join(b['files'][:20])}")


def _summary(r: dict) -> dict:
    plan = r.get("plan") or {}
    return {
        "id": r.get("id"),
        "session_id": r.get("session_id"),
        "report": r.get("session_title"),
        "title": plan.get("title"),
        "status": r.get("status"),
        "stage": r.get("stage"),
        "partition": (plan.get("resources") or {}).get("partition"),
        "estimate_usd": r.get("estimate_usd"),
        "job_id": r.get("job_id"),
        "outcome": (r.get("assessment") or {}).get("outcome"),
        "updated_at": r.get("updated_at"),
    }


# ---- commands --------------------------------------------------------------------


def _confirm(args, what: str) -> None:
    from deepresearch.cli.jsonout import json_flag

    if getattr(args, "yes", False):
        return
    if json_flag(args) or not sys.stdin.isatty():
        raise LabCliError(f"{what}: pass --yes to go ahead", 2)
    print(f"{what}. Go ahead? [y/N] ", end="", flush=True, file=sys.stderr)
    if sys.stdin.readline().strip().lower() not in ("y", "yes"):
        raise LabCliError("not confirmed; nothing changed", 1)


def _wait_plan(rid: int, timeout: float) -> dict:
    """Until planning, the referee and its fixer rounds are done (`plan_busy` false)."""
    end = time.monotonic() + timeout
    run = _call("GET", f"/api/lab/{rid}")
    while time.monotonic() < end:
        if run.get("status") != "planning" and not run.get("plan_busy"):
            return run
        time.sleep(POLL_SECONDS)
        run = _call("GET", f"/api/lab/{rid}")
    return run


def cmd_list(args) -> Any:
    runs = [_summary(r) for r in _call("GET", "/api/lab/runs")["runs"]]
    if args.report:
        runs = [r for r in runs if str(r["session_id"]) == str(args.report)]
    if args.status:
        want = {
            "active": LAB_ACTIVE,
            "waiting": LAB_WAITING,
            "finished": LAB_FINAL,
        }.get(args.status, (args.status,))
        runs = [r for r in runs if r["status"] in want]
    return runs[: args.limit]


def _print_list(runs: list[dict]) -> None:
    if not runs:
        print("No Lab runs match.")
    for r in runs:
        extra = f" ({r['outcome']})" if r["outcome"] else ""
        print(
            f"run {r['id']:<4} {r['status']:<10} report #{r['session_id']:<4} "
            f"{(r['title'] or r['report'] or '')[:70]}{extra}"
        )


def cmd_suggestions(args) -> Any:
    data = _call("GET", f"/api/sessions/{int(args.report)}/lab")
    sug = data.get("suggestions")
    if args.refresh or not sug:
        sug = _call(
            "POST",
            f"/api/sessions/{int(args.report)}/lab/suggestions",
            {"refresh": bool(args.refresh)},
            timeout=300,
        )
    return {"report": int(args.report), **(sug or {"suggestions": []})}


def cmd_plan(args) -> Any:
    body: dict[str, Any] = {"scope": "document"}
    request = (args.request or "").strip()
    if args.selection:
        body.update(scope="selection", selection=args.selection)
    elif args.suggestion is not None:
        sug = (
            _call("GET", f"/api/sessions/{int(args.report)}/lab").get("suggestions")
            or {}
        )
        items = sug.get("suggestions") or []
        if not items:
            raise LabCliError(
                f"report #{args.report} has no suggestions yet; run "
                f"`deep-research lab suggestions {args.report}` first",
                2,
            )
        if not 1 <= args.suggestion <= len(items):
            raise LabCliError(f"--suggestion must be 1-{len(items)}", 2)
        x = items[args.suggestion - 1]
        text = f"{x.get('title')}: {x.get('question')}\nApproach: {x.get('approach')}"
        request = text + (f"\n{request}" if request else "")
        body["scope"] = "suggestion"
    if request:
        body["request"] = request
    if args.source:
        body["data_sources"] = args.source
    if args.target:
        body["target"] = args.target
    run = _call("POST", f"/api/sessions/{int(args.report)}/lab", body)
    if args.wait:
        run = _wait_plan(int(run["id"]), args.timeout)
    return brief(run)


def cmd_show(args) -> Any:
    run = _call("GET", f"/api/lab/{int(args.run)}")
    return run if args.full else brief(run)


def cmd_submit(args) -> Any:
    run = _call("GET", f"/api/lab/{int(args.run)}")
    if run.get("status") != "draft":
        raise LabCliError(
            f"run {args.run} is {run.get('status')}; only a reviewed draft can be submitted",
            1,
        )
    est = run.get("estimate_usd")
    title = (run.get("plan") or {}).get("title") or ""
    _confirm(
        args,
        f"Submit Lab run {args.run} ({title}) to the cluster, estimate "
        + (f"${est:.2f}" if isinstance(est, (int, float)) else "unknown"),
    )
    return brief(
        _call("POST", f"/api/lab/{int(args.run)}/submit", timeout=SUBMIT_TIMEOUT)
    )


def cmd_cancel(args) -> Any:
    run = _call("GET", f"/api/lab/{int(args.run)}")
    _confirm(args, f"Cancel Lab run {args.run} ({run.get('status')})")
    return brief(_call("POST", f"/api/lab/{int(args.run)}/cancel", timeout=120))


def cmd_log(args) -> Any:
    d = _call("GET", f"/api/lab/{int(args.run)}/log")
    text = d.get("text") or ""
    if args.tail:
        text = "\n".join(text.splitlines()[-args.tail :])
    return {"run": int(args.run), "source": d.get("source"), "text": text}


def cmd_status(args) -> Any:
    from deepresearch.cli.status import workspace_status
    from deepresearch.core import workspace as W

    w = workspace_status(W.current_slug(), float(args.since))
    return {"workspace": w["workspace"], "since_hours": float(args.since), **w["lab"]}


def _print_status(d: dict) -> None:
    print(f"Lab, workspace {d['workspace']}:")
    for key, label in (
        ("active", "On the cluster"),
        ("waiting", "Waiting for review"),
        ("recent", f"Finished in the last {d['since_hours']:g} h"),
    ):
        print(f"{label}: {len(d[key])}")
        for x in d[key]:
            extra = f" ({x['outcome']})" if x["outcome"] else ""
            print(
                f"  run {x['id']:<4} {x['status']:<10} {(x['title'] or x['report'])[:70]}{extra}"
            )
            if x["stage"]:
                print(f"            {x['stage'][:100]}")


COMMANDS = {
    "list": cmd_list,
    "suggestions": cmd_suggestions,
    "plan": cmd_plan,
    "show": cmd_show,
    "submit": cmd_submit,
    "cancel": cmd_cancel,
    "log": cmd_log,
    "status": cmd_status,
}


def handle(args) -> int:
    from deepresearch.cli.jsonout import emit, json_flag

    cmd = getattr(args, "lab_command", None) or "status"
    try:
        out = COMMANDS[cmd](args)
    except LabCliError as e:
        if json_flag(args):
            emit({"error": str(e)})
        else:
            print(f"[ERROR] {e}", file=sys.stderr)
        return e.code
    if json_flag(args):
        emit(out)
    elif cmd == "list":
        _print_list(out)
    elif cmd == "status":
        _print_status(out)
    elif cmd == "suggestions":
        for i, s in enumerate(out.get("suggestions") or [], 1):
            print(
                f"{i}. {s.get('title')}\n   {s.get('question')}\n   {s.get('approach')}\n"
            )
        if not out.get("suggestions"):
            print("No suggestions.")
    elif cmd == "log":
        print(out["text"])
    elif cmd == "show" and args.full:
        print(json.dumps(out, indent=2, default=str))
    else:
        _print_brief(out)
    return 0


def add_parser(subparsers, add_json) -> None:
    p = subparsers.add_parser(
        "lab",
        help="Lab runs: list, plan, show, submit, status, cancel, log",
        description=(
            "Lab runs from the command line: the same runs as the dashboard's Lab. Plan, "
            "submit, cancel, log and suggestions go through the running dashboard (it "
            "owns planning, the job watcher and the cluster sign-in); status reads the "
            "workspace directly. RUN is a Lab run id, REPORT a report (session) id. "
            "Every subcommand takes --json; bare `lab` is `lab status`."
        ),
    )
    sub = p.add_subparsers(dest="lab_command")
    add_json(p)  # for bare `lab --json` (= lab status)

    q = sub.add_parser("list", help="Lab runs, newest first")
    q.add_argument(
        "--status",
        help="A run status, or a group: active (on the cluster), waiting (planning or "
        "draft), finished",
    )
    q.add_argument("--report", help="Only runs for this report id")
    q.add_argument("--limit", type=int, default=20, help="default: %(default)s")
    add_json(q)

    q = sub.add_parser(
        "suggestions",
        help="AI-suggested computations for a report (generated once, a few cents)",
    )
    q.add_argument("report", metavar="REPORT")
    q.add_argument("--refresh", action="store_true", help="Generate them again")
    add_json(q)

    q = sub.add_parser(
        "plan",
        help="Create a Lab run and plan it (AI, a few cents); never submits",
        description=(
            "Create a Lab run on a report and let the AI plan it, then the referee review "
            "it. Default scope is the whole report; --suggestion N plans suggestion N "
            "from `lab suggestions`; --selection plans a passage. Nothing reaches the "
            "cluster until `lab submit`."
        ),
    )
    q.add_argument("report", metavar="REPORT")
    q.add_argument("--request", help="What to compute, in your words")
    q.add_argument(
        "--suggestion", type=int, metavar="N", help="Plan suggestion N (1-based)"
    )
    q.add_argument("--selection", help="Plan this passage of the report instead")
    q.add_argument(
        "--source", action="append", metavar="NAME", help="Data source (repeatable)"
    )
    q.add_argument("--target", help="Compute target (default: the configured one)")
    q.add_argument(
        "--wait",
        action="store_true",
        help="Wait until the plan and its referee review are ready",
    )
    q.add_argument(
        "--timeout",
        type=float,
        default=900,
        help="Seconds to wait (default: %(default)s)",
    )
    add_json(q)

    q = sub.add_parser("show", help="One run: plan, referee, pilot, job, outcome")
    q.add_argument("run", metavar="RUN")
    q.add_argument(
        "--full", action="store_true", help="The raw run, including script and diffs"
    )
    add_json(q)

    q = sub.add_parser(
        "submit",
        help="Submit a reviewed draft to the cluster (asks first, or --yes)",
        description=(
            "Submit a draft: the pilot runs first on the check partition, then the real "
            "job. Blocks until the pilot is queued (usually 1-5 minutes). Spends cluster "
            "money, so it asks first; --json always needs --yes."
        ),
    )
    q.add_argument("run", metavar="RUN")
    q.add_argument("--yes", action="store_true", help="Do not ask")
    add_json(q)

    q = sub.add_parser("cancel", help="Cancel a run (asks first, or --yes)")
    q.add_argument("run", metavar="RUN")
    q.add_argument("--yes", action="store_true", help="Do not ask")
    add_json(q)

    q = sub.add_parser("log", help="The job log (pilot, live or fetched)")
    q.add_argument("run", metavar="RUN")
    q.add_argument("--tail", type=int, metavar="N", help="Only the last N lines")
    add_json(q)

    q = sub.add_parser(
        "status",
        help="Runs on the cluster, waiting for review, finished recently (no dashboard needed)",
    )
    q.add_argument(
        "--since", type=float, default=24, metavar="HOURS", help="default: %(default)s"
    )
    add_json(q)
    p.set_defaults(since=24)

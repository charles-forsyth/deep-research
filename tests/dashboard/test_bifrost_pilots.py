"""R3 (v0.56.0): pilots and planning checks through bifrost on the check partition.

The same in-memory cluster as the R2 tests stands behind the fake bifrost server, so the
real Lab code runs: a submitted plan becomes a pilot job on `check` (LAB_SMOKE=1, 15
minutes, at most the node's cores), its result is read through bifrost, and the full run
follows as its own job. FakeTarget.run/sh raise, so any SSH use fails the test."""

import json

import pytest

from deepresearch.dashboard import bifrost as bf
from tests.dashboard.test_bifrost import FakeTarget, server  # noqa: F401  (fixture)
from tests.dashboard.test_bifrost_jobs import Cluster, calls


@pytest.fixture
def plab(server, tmp_path):  # noqa: F811
    from deepresearch.dashboard.lab import Lab

    fake, c, _ = server
    cl = Cluster(fake)
    jobs = bf.BifrostJobs(
        c, max_usd_per_run=10,
        http_get=lambda url, mx: cl.blobs[url], http_put=lambda url, data, h: None,
    )  # fmt: skip
    t = FakeTarget()
    t.partitions = {
        "computehigh": {"cpus": 22, "mem_gb": 85, "usd_per_hour": 1.87},
        "check": {"cpus": 2, "mem_gb": 15, "usd_per_hour": 0.13},
        "gpul4": {"cpus": 8, "mem_gb": 60, "gpus": 1, "usd_per_hour": 1.2},
    }
    t.warm = None  # retired in R4
    # the Lab must never touch SSH while bifrost is signed in
    for name in ("upload", "read_file", "missing_outputs", "submit", "sbatch_uploaded"):
        setattr(t, name, lambda *a, _n=name, **k: pytest.fail(f"SSH used: {_n}"))
    lb = Lab(
        str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"ursa": t}, bifrost=c
    )
    lb.bifrost_jobs = jobs
    lb.ensure_watcher = lambda: None
    lb.CHECK_POLL_S = 0
    lb._ask = lambda prompt, search: ("Result: done.", 0.0)
    return lb, cl, fake, t


PLAN = {
    "title": "Pi by sampling", "question": "Is pi 3.14?", "approach": "Monte Carlo",
    "script": "python -c 'print(3.14)' > outputs/pi.txt\n",
    "resources": {"partition": "computehigh", "nodes": 2, "time_limit": "02:00:00",
                  "cores": 16},
    "expected_outputs": ["outputs/pi.txt"],
}  # fmt: skip


def draft(lb, plan=None):
    with lb._conn() as conn:
        cur = conn.execute(
            "INSERT INTO lab_runs (session_id, scope, status, target, plan, estimate_usd)"
            " VALUES (1, 'document', 'draft', 'ursa', ?, 3.0)",
            (json.dumps(plan or PLAN),),
        )
        conn.commit()
        return cur.lastrowid


def test_pilot_runs_on_check_through_bifrost(plab):
    lb, cl, fake, _ = plab
    rid = draft(lb)
    run = lb.submit(rid)
    assert run["status"] == "smoke" and run["smoke"]["job_id"] == "500"
    assert "check partition" in run["stage"]
    sent = calls(fake, "job_submit")[0]["script"]
    assert "#SBATCH --partition=check" in sent
    assert "#SBATCH --time=00:15:00" in sent and "#SBATCH --nodes=1" in sent
    assert "#SBATCH --cpus-per-task=2" in sent  # capped at the check node's 2 cores
    assert "export LAB_SMOKE=1" in sent and 'LAB_SMOKE="${LAB_SMOKE:-0}"' not in sent
    # the run's own batch file is still the full run's
    assert "--partition=computehigh" in (
        lb.get(rid)["script"] or "--partition=computehigh"
    )
    assert lb.get(rid)["cluster_jobs"][0]["why"] == "pilot round 1"


def test_passing_pilot_starts_the_full_run_as_its_own_job(plab):
    lb, cl, fake, _ = plab
    rid = draft(lb)
    lb.submit(rid)
    cl.finish("500", files={"outputs/pi.txt": b"3.14\n", "stage.txt": b"Done\n"},
              log=["[STAGE] Running", "[INFO] PILOT: cut-down run", "done"])  # fmt: skip
    lb.poll(lb.get(rid))
    run = lb.get(rid)
    assert run["status"] == "queued" and run["job_id"] == "501", (
        run["stage"],
        run["error"],
    )
    full = calls(fake, "job_submit")[1]["script"]
    assert "#SBATCH --partition=computehigh" in full and "#SBATCH --nodes=2" in full
    assert 'export LAB_SMOKE="${LAB_SMOKE:-0}"' in full
    assert run["smoke"]["rounds"][0]["passed"] is True
    assert [j["why"] for j in run["cluster_jobs"]] == ["pilot round 1", "full"]


def test_missing_output_fails_the_pilot_round(plab):
    lb, cl, fake, _ = plab
    rid = draft(lb)
    lb.submit(rid)
    cl.finish("500", files={"outputs/pi.txt": b""}, log=["Traceback: boom"])
    lb._smoke_fix = lambda *a, **k: None  # the AI fixer is not under test here
    lb.poll(lb.get(rid))
    run = lb.get(rid)
    last = run["smoke"]["rounds"][-1]
    assert last["passed"] is False and last["missing"] == ["outputs/pi.txt"]
    assert "Traceback" in last["log_tail"]


def test_pilot_timeout_without_errors_counts_as_clean(plab):
    lb, cl, fake, _ = plab
    rid = draft(lb)
    lb.submit(rid)
    cl.finish("500", state="TIMEOUT", files={"outputs/pi.txt": b"3\n"}, log=["step 1"])
    lb.poll(lb.get(rid))
    run = lb.get(rid)
    assert run["smoke"]["rounds"][0]["rc"] == 124
    assert run["smoke"]["rounds"][0]["passed"] is True and run["status"] == "queued"


def test_gpu_plan_skips_the_pilot_as_before(plab):
    """The check node has no GPU, so a GPU plan goes straight to its full run (as it
    did on the CPU-only warm node)."""
    lb, cl, fake, _ = plab
    plan = {**PLAN, "resources": {"partition": "gpul4", "nodes": 1, "gpus": 1,
                                  "time_limit": "03:00:00"}}  # fmt: skip
    rid = draft(lb, plan)
    run = lb.submit(rid)
    sent = calls(fake, "job_submit")[0]["script"]
    assert run["status"] == "queued" and "#SBATCH --partition=gpul4" in sent
    assert "--partition=check" not in sent


def test_watcher_batches_pilot_jobs_with_full_runs(plab):
    lb, cl, fake, _ = plab
    a, b = draft(lb), draft(lb)
    lb.submit(a)
    lb.submit(b)
    fake.calls.clear()
    lb._stop.clear()
    lb._stop.wait = lambda t=None: lb._stop.set()  # type: ignore[method-assign]
    lb._watch_loop(interval=0)
    lists = calls(fake, "jobs_list")
    assert len(lists) == 1 and sorted(lists[0]["job_ids"]) == ["500", "501"]


def test_cancel_during_pilot_cancels_the_bifrost_job(plab):
    lb, cl, fake, _ = plab
    rid = draft(lb)
    lb.submit(rid)
    lb.cancel(rid)
    assert cl.jobs["500"]["state"] == "CANCELLED"
    assert lb.get(rid)["status"] == "cancelled"


def test_pilot_log_comes_from_bifrost(plab):
    lb, cl, fake, _ = plab
    rid = draft(lb)
    lb.submit(rid)
    cl.jobs["500"]["state"] = "RUNNING"
    cl.jobs["500"]["log"] = ["[INFO] PILOT: cut-down run", "step 1"]
    d = lb.log(rid, 0)
    assert "step 1" in d["text"] and d["source"] == "smoke"


def test_planning_checks_run_as_a_check_job(plab):
    lb, cl, fake, t = plab
    orig = cl.confirm

    def confirm_and_finish(a):
        out = orig(a)
        cl.finish(out["job_id"], log=["=== CHECK 1: module gromacs", "gromacs/2026.3"])
        return out

    fake.answers["job_submit_confirm"] = confirm_and_finish
    rc, out = lb._check_run(t, "probe-7-1", "#!/bin/bash\nmodule show gromacs\n", 120)
    assert rc == 0 and "gromacs/2026.3" in out
    sent = calls(fake, "job_submit")[0]["script"]
    assert "#SBATCH --partition=check" in sent and "#SBATCH --cpus-per-task=1" in sent
    assert "module show gromacs" in sent and sent.count("#!/bin/bash") == 1
    assert lb._checks_where(t) == "on the check partition"


def test_warm_worker_is_gone(plab):
    """R4: no warm-node API is left on the Lab."""
    lb, cl, fake, t = plab
    for name in ("warm_status", "warm_start", "warm_stop", "_warm_full_ok", "_keep_warm",
                 "start_warm_keeper", "keep_warm_once"):  # fmt: skip
        assert not hasattr(lb, name), name


def test_real_run_on_check_gets_a_warning(plab):
    lb, cl, fake, t = plab
    w = lb.match_warnings(t, {**PLAN, "resources": {"partition": "check"}})
    assert any("test partition" in x for x in w)


def test_pilot_plan_fits_the_check_node(plab):
    lb, cl, fake, t = plab
    p, where = lb._pilot_plan(t, {**PLAN, "resources": {"partition": "computehigh",
                                                        "cores": 16, "nodes": 3}})  # fmt: skip
    r = p["resources"]
    assert where == "the check partition"
    assert (r["partition"], r["nodes"], r["cores"], r["time_limit"]) == (
        "check",
        1,
        2,
        "00:15:00",
    )
    g, gwhere = lb._pilot_plan(
        t, {**PLAN, "resources": {"partition": "gpul4", "gpus": 1}}
    )
    assert g["resources"]["partition"] == "gpul4" and gwhere == "gpul4"


# ---- the AI fix loop on bifrost pilots (moved from the warm-node tests in R4) ---------


def _fail(cl, jid, rc, files=None, log=None):
    """A pilot job that ended FAILED with exit code rc."""
    cl.finish(jid, state="FAILED", files=files, log=log)
    cl.jobs[jid]["exit_code"] = f"{rc}:0"


class _Inline:
    def __init__(self, target, args=(), daemon=None, **kw):
        self.t, self.a = target, args

    def start(self):
        self.t(*self.a)


def _fixed_reply(changes=("write pi.txt",)):
    fixed = {
        **PLAN,
        "script": "echo 3.14 > outputs/pi.txt\n",
        "install": {"modules": ["python-sci"]},
    }
    return (
        "```json\n"
        + json.dumps({"plan": fixed, "changes": list(changes), "notes": ""})
        + "\n```"
    )


def test_failed_pilot_is_fixed_by_ai_and_comes_back_for_review(plab, monkeypatch):
    from deepresearch.dashboard import lab as labm

    lb, cl, fake, _ = plab
    rid = draft(lb)
    lb.submit(rid)
    _fail(cl, "500", "1", files={},
              log=["Traceback", "FileNotFoundError: inputs/x.json"])  # fmt: skip
    lb._ask = lambda prompt, search: (_fixed_reply(), 0.0)
    monkeypatch.setattr(labm.threading, "Thread", _Inline)
    lb.poll(lb.get(rid))
    cur = lb.get(rid)
    assert cur["status"] == "smoke" and cur["smoke"]["round"] == 2, cur["stage"]
    assert "smoke round 1" in cur["plan"]["fix_changes"][0]
    assert cur["smoke"]["job_id"] == "501"  # round 2 is a new pilot job on check
    cl.finish("501", files={"outputs/pi.txt": b"3.14\n"}, log=["done"])
    lb.poll(lb.get(rid))
    done = lb.get(rid)
    # the AI changed the plan: it passes, but a person approves the change first
    assert done["status"] == "draft" and "review the changes" in done["stage"]
    assert [j["why"] for j in done["cluster_jobs"]] == [
        "pilot round 1",
        "pilot round 2",
    ]


def test_pilot_gives_up_after_max_rounds(plab):
    lb, cl, fake, _ = plab
    rid = draft(lb)
    lb.submit(rid)
    sm = dict(lb.get(rid)["smoke"], round=lb.SMOKE_MAX_ROUNDS)
    lb._update(rid, smoke=sm)
    _fail(cl, "500", "2", files={}, log=["error: boom"])
    lb.poll(lb.get(rid))
    f = lb.get(rid)
    assert f["status"] == "failed" and "full run was not started" in f["error"]
    assert "error: boom" in (lb.results_dir / f"run_{rid}" / "job.log").read_text()


def test_pilot_install_failure_is_not_retried_forever(plab, monkeypatch):
    from deepresearch.dashboard import lab as labm

    lb, cl, fake, _ = plab
    seen = {}

    def ask(prompt, search):
        seen["prompt"] = prompt
        return _fixed_reply(("x",)), 0.0

    lb._ask = ask
    monkeypatch.setattr(labm.threading, "Thread", _Inline)
    rid = draft(lb)
    lb.submit(rid)
    log = ["[ERROR] no install method produced a working environment (tried: pixi)"]
    _fail(cl, "500", "4", files={}, log=log)
    lb.poll(lb.get(rid))
    assert "DIAGNOSIS: No install method" in seen["prompt"]
    _fail(cl, "501", "4", files={}, log=log)
    lb.poll(lb.get(rid))
    f = lb.get(rid)
    assert (
        f["status"] == "failed"
        and "(install); not handed to the AI again" in f["stage"]
    )
    assert (
        "software setup" in f["error"]
        and f["smoke"]["rounds"][-1]["class"] == "install"
    )


def test_pilot_fix_interrupted_by_restart_is_resumed(plab, monkeypatch):
    """Run #88: a dashboard restart mid-fix used to fail the run; now the fix resumes."""
    from deepresearch.dashboard import lab as labm

    lb, cl, fake, _ = plab
    rid = draft(lb)
    lb.submit(rid)
    sm = dict(lb.get(rid)["smoke"], fixing=True)
    sm["rounds"] = [{"round": 1, "rc": 1, "log_tail": "boom", "missing": []}]
    lb._update(rid, smoke=sm)  # as left by a dashboard that died mid-fix
    labm._SMOKE_FIXING.discard(("main", rid))
    called = []
    monkeypatch.setattr(lb, "_smoke_fix", lambda *a: called.append(a))
    lb.poll(lb.get(rid))
    import time as _t

    for _ in range(50):
        if called:
            break
        _t.sleep(0.02)
    assert called and called[0][0] == rid and called[0][1] == "boom"
    assert lb.get(rid)["status"] == "smoke"
    labm._SMOKE_FIXING.discard(("main", rid))


def test_a_pilot_left_on_the_retired_warm_node_is_closed_out(plab):
    """R4: a run still in "smoke" with a warm-node task (no job id) is failed with a
    clear message instead of being polled over SSH forever."""
    lb, cl, fake, _ = plab
    rid = draft(lb)
    lb._update(
        rid, status="smoke", smoke={"task": "smoke-9-1", "round": 1, "rounds": []}
    )
    lb.poll(lb.get(rid))
    r = lb.get(rid)
    assert r["status"] == "failed" and "retired warm Lab node" in r["error"]


def test_without_bifrost_there_is_no_pilot(plab):
    """Pilots only run through bifrost now: signed out, the plan is not piloted on any
    warm node (the SSH submit path is all that is left until it goes too)."""
    lb, cl, fake, t = plab
    lb.bifrost_jobs = None
    assert lb._smoke_applies(t, PLAN) is False


def test_cluster_checks_need_bifrost(plab):
    """Signed out, a planning check never runs anywhere (no warm node to fall back on):
    it answers with the sign-in hint and touches nothing."""
    lb, cl, fake, t = plab
    lb.bifrost_jobs = None
    before = len(fake.calls)
    rc, out = lb._check_run(t, "probe-1-1", "#!/bin/bash\necho hi\n", 5)
    assert rc is None and "deep-research cluster login" in out
    assert len(fake.calls) == before

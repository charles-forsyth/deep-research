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
    t.warm = {"partition": "computehigh", "max_full_min": 120, "smoke_min": 15}
    # the warm worker must never be touched while bifrost is signed in
    for name in ("warm_enqueue", "warm_task", "ensure_warm", "upload", "read_file",
                 "missing_outputs", "warm_status"):  # fmt: skip
        setattr(t, name, lambda *a, _n=name, **k: pytest.fail(f"SSH/warm used: {_n}"))
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
    rc, out = lb._warm_exec(t, "probe-7-1", "#!/bin/bash\nmodule show gromacs\n", 120)
    assert rc == 0 and "gromacs/2026.3" in out
    sent = calls(fake, "job_submit")[0]["script"]
    assert "#SBATCH --partition=check" in sent and "#SBATCH --cpus-per-task=1" in sent
    assert "module show gromacs" in sent and sent.count("#!/bin/bash") == 1
    assert lb._checks_where(t) == "on the check partition"


def test_warm_worker_is_reported_off_when_bifrost_runs_the_lab(plab):
    lb, cl, fake, t = plab
    assert lb.warm_status() == {"enabled": False, "replaced_by": "check"}
    assert lb._warm_full_ok(t, {**PLAN, "resources": {"partition": "computehigh",
                                                      "time_limit": "00:10:00"}}) is False  # fmt: skip


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

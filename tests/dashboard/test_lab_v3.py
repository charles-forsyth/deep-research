"""Lab plan v3: warm node, smoke test + AI fix loop, install ladder, probes, matching."""

import json
import subprocess

import pytest

from deepresearch.dashboard import lab as labm
from deepresearch.dashboard.lab import Lab, build_sbatch
from tests.dashboard.test_lab import PLAN, FakeTarget


class WarmFake(FakeTarget):
    """FakeTarget plus an in-memory warm node: tasks finish when the test says so."""

    warm = {
        "partition": "standard", "idle_min": 20, "hours": 4.0, "max_par": 2,
        "max_full_min": 120, "smoke_min": 15,
    }  # fmt: skip

    def __init__(self):
        super().__init__()
        self.tasks: dict[str, dict] = {}
        self.uploads: list[tuple[int, dict, bool]] = []
        self.files: dict[str, str] = {}
        self.ensures = 0
        self.sbatched: list[int] = []
        self.catalog = None

    def ensure_warm(self):
        self.ensures += 1
        return "running:77"

    def upload(self, run_id, files, fresh=True):
        self.uploads.append((run_id, files, fresh))

    def sbatch_uploaded(self, run_id):
        self.sbatched.append(run_id)
        return "5151"

    def warm_enqueue(self, task, files, need_sec, exclusive=False):
        self.tasks[task] = {
            "where": "queue",
            "rc": None,
            "files": files,
            "exclusive": exclusive,
            "started": None,
            "finished": None,
            "node": "",
            "now": 100,
        }

    def warm_task(self, task):
        return self.tasks.get(
            task,
            {
                "where": "missing",
                "rc": None,
                "started": None,
                "finished": None,
                "node": "",
                "now": 100,
            },
        )

    def warm_task_log(self, task, limit=60000):
        return self.tasks.get(task, {}).get("log", "")

    def warm_cancel(self, task):
        self.tasks[task]["cancelled"] = True

    def finish(self, task, rc, log="", run=None, outputs=()):
        self.tasks[task].update(
            where="done", rc=rc, started=10, finished=15, node="c3-0", log=log
        )
        if run is not None:
            self.files[f"{run}/smoke/job.log"] = log
            self.present = set(outputs)

    def read_file(self, run_id, rel, limit=60000):
        return self.files.get(f"{run_id}/{rel}", "")

    def missing_outputs(self, run_id, sub, patterns):
        return [p for p in patterns if p not in getattr(self, "present", set())]


@pytest.fixture
def wlab(tmp_path):
    t = WarmFake()
    lb = Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"fake": t})
    replies: list = []
    lb._ask = lambda prompt, search: (replies.pop(0), 0.01)  # type: ignore[method-assign]
    lb.ensure_watcher = lambda: None  # type: ignore[method-assign]
    lb.fake, lb.replies = t, replies  # type: ignore[attr-defined]
    return lb


def _draft(lb, **over):
    plan = {**PLAN, "expected_outputs": ["outputs/result.txt"], **over}
    return lb.create(1, "document", "x", "", plan=plan)


def test_submit_runs_smoke_first_then_full_run_on_warm_node(wlab):
    run = _draft(wlab)
    r = wlab.submit(run["id"])
    assert r["status"] == "smoke"
    assert wlab.fake.uploads and wlab.fake.uploads[0][2] is True  # fresh folder
    task = r["smoke"]["task"]
    sh = wlab.fake.tasks[task]["files"]["run.sh"]
    assert "LAB_SMOKE=1" in sh and 'cd "$S"' in sh and "timeout" in sh
    wlab.fake.finish(
        task, 0, "[STAGE] Done\n", run=run["id"], outputs=["outputs/result.txt"]
    )
    wlab.poll(wlab.get(run["id"]))
    now = wlab.get(run["id"])
    assert now["status"] == "queued" and now["job_id"] == f"warm:full-{run['id']}"
    full = wlab.fake.tasks[f"full-{run['id']}"]
    assert full["exclusive"] and "bash run.sbatch" in full["files"]["run.sh"]
    assert now["smoke"]["rounds"][0]["passed"] is True


def test_long_or_gpu_runs_use_their_own_slurm_job(wlab):
    run = _draft(wlab, resources={**PLAN["resources"], "time_limit": "05:00:00"})
    wlab.submit(run["id"])
    t = wlab.get(run["id"])["smoke"]["task"]
    wlab.fake.finish(t, 0, "", run=run["id"], outputs=["outputs/result.txt"])
    wlab.poll(wlab.get(run["id"]))
    assert wlab.get(run["id"])["job_id"] == "5151"  # sbatch of the uploaded folder
    gpu = _draft(wlab, resources={**PLAN["resources"], "gpus": 1})
    r = wlab.submit(gpu["id"])
    assert (
        r["status"] == "queued" and r["job_id"] == "4242"
    )  # no CPU smoke for GPU plans


def test_failed_smoke_is_fixed_by_ai_and_comes_back_for_review(wlab):
    run = _draft(wlab)
    wlab.submit(run["id"])
    t1 = wlab.get(run["id"])["smoke"]["task"]
    wlab.fake.finish(
        t1, 1, "Traceback\nFileNotFoundError: inputs/x.json\n", run=run["id"]
    )
    fixed = {
        **PLAN,
        "script": "echo fixed > outputs/result.txt",
        "expected_outputs": ["outputs/result.txt"],
    }
    wlab.replies.append(
        "```json\n"
        + json.dumps(
            {
                "plan": fixed,
                "changes": ["write result.txt instead of reading x.json"],
                "notes": "",
            }
        )
        + "\n```"
    )
    wlab._smoke_fix = labm.Lab._smoke_fix.__get__(
        wlab
    )  # run the fix inline, not a thread
    orig_thread = labm.threading.Thread

    class Inline:
        def __init__(self, target, args, daemon):
            self.t, self.a = target, args

        def start(self):
            self.t(*self.a)

    labm.threading.Thread = Inline  # type: ignore[misc,assignment]
    try:
        wlab.poll(wlab.get(run["id"]))
    finally:
        labm.threading.Thread = orig_thread  # type: ignore[misc]
    cur = wlab.get(run["id"])
    assert cur["status"] == "smoke" and cur["smoke"]["round"] == 2
    assert "smoke round 1" in cur["plan"]["fix_changes"][0]
    assert wlab.fake.uploads[-1][2] is False  # re-upload keeps the folder
    t2 = cur["smoke"]["task"]
    wlab.fake.finish(t2, 0, "", run=run["id"], outputs=["outputs/result.txt"])
    wlab.poll(wlab.get(run["id"]))
    done = wlab.get(run["id"])
    # the AI changed the plan: it passes, but a person approves the change first
    assert done["status"] == "draft" and "review the changes" in done["stage"]
    assert f"full-{run['id']}" not in wlab.fake.tasks


def test_smoke_gives_up_after_max_rounds(wlab):
    run = _draft(wlab)
    wlab.submit(run["id"])
    cur = wlab.get(run["id"])
    sm = dict(cur["smoke"], round=wlab.SMOKE_MAX_ROUNDS)
    wlab._update(run["id"], smoke=sm)
    wlab.fake.finish(sm["task"], 2, "error: boom", run=run["id"])
    wlab.poll(wlab.get(run["id"]))
    f = wlab.get(run["id"])
    assert f["status"] == "failed" and "full run was not started" in f["error"]
    assert (
        wlab.results_dir / f"run_{run['id']}" / "job.log"
    ).read_text() == "error: boom"


def test_cancel_during_smoke_cancels_the_task(wlab):
    run = _draft(wlab)
    wlab.submit(run["id"])
    task = wlab.get(run["id"])["smoke"]["task"]
    assert wlab.cancel(run["id"])["status"] == "cancelled"
    assert wlab.fake.tasks[task].get("cancelled")


def test_warm_full_run_status_maps_to_slurm_states(tmp_path):
    tgt = labm.SlurmSSHTarget({"name": "x", "ssh_host": "h", "partitions": {}})
    tgt.warm_task = lambda t: {
        "where": "done",
        "rc": 0,
        "started": 10,
        "finished": 75,  # type: ignore[method-assign]
        "node": "n0",
        "now": 80,
    }
    tgt.read_file = lambda rid, rel, limit=0: "Installing\nDone\n"  # type: ignore[method-assign]
    st = tgt.status(3, "warm:full-3")
    assert (
        st["slurm_state"] == "COMPLETED"
        and st["elapsed"] == "0:01:05"
        and st["stage"] == "Done"
    )
    tgt.warm_task = lambda t: {
        "where": "done",
        "rc": 124,
        "started": 1,
        "finished": 2,
        "node": "",
        "now": 2,
    }  # type: ignore[method-assign]
    assert tgt.status(3, "warm:full-3")["slurm_state"] == "TIMEOUT"


def test_warm_worker_script_runs_tasks_and_honours_exclusive(tmp_path):
    q = tmp_path / "queue"
    for name, excl in (("a", False), ("full", True), ("b", False)):
        d = q / name
        d.mkdir(parents=True)
        (d / "run.sh").write_text(f"echo ran-{name}\nsleep 1\n")
        (d / "need_sec").write_text("30")
        if excl:
            (d / "exclusive").touch()
        subprocess.run(["sleep", "1.05"])
    script = labm._worker_script()
    out = subprocess.run(
        ["bash", "-c", script],
        env={
            "WARM": str(tmp_path),
            "IDLE_SEC": "2",
            "POLL": "1",
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=40,
    ).stdout
    assert all(
        (tmp_path / "done" / n / "rc").read_text().strip() == "0"
        for n in "a b full".split()
    )
    lines = [
        ln for ln in out.splitlines() if "task full started" in ln or "finished" in ln
    ]
    started = out.index("task full started")
    assert (
        out.index("task a finished") < started
        and out.index("task b finished") < started
    )
    assert "idle for 2s, exiting" in out
    assert (tmp_path / "done" / "a" / "log").read_text().strip() == "ran-a"
    assert lines


def test_install_ladder_rungs_and_verification():
    t = FakeTarget()
    py = dict(
        PLAN,
        install={
            "modules": ["python-sci"],
            "pip": ["simpy"],
            "verify": ["python -c 'import simpy'"],
        },
    )
    s = build_sbatch(1, py, t)
    assert (
        s.index("ladder_try layered-venv")
        < s.index("ladder_try isolated-venv")
        < s.index("ladder_try pixi-conda-forge")
    )
    assert "LADDER_IMPORTS=simpy" in s and "import simpy" in s
    assert "exit 4" in s  # nothing verified: stop with the list of tries
    conda = dict(
        PLAN, install={"conda": ["python=3.12", "rdkit"], "channels": ["conda-forge"]}
    )
    c = build_sbatch(1, conda, t)
    assert (
        c.index("ladder_try pixi ")
        < c.index("ladder_try pixi-loose")
        < c.index("ladder_try pip-venv")
    )
    assert "-c conda-forge -c bioconda" in c
    img = dict(PLAN, install={"pip": ["x"], "apptainer": ["docker://a/b:1"]})
    assert "continuing with the container image" in build_sbatch(1, img, t)
    sp = dict(PLAN, install={"spack": ["hpl@2.3"]})
    ss = build_sbatch(1, sp, t)
    assert "upstreams" in ss and "spack install" in ss and "spack load hpl@2.3" in ss
    mod = dict(PLAN, install={"modules": ["mafft/99"]})
    ms = build_sbatch(1, mod, t)
    assert "module load mafft/99 ||" in ms and "LADDER_MOD_FALLBACK" in ms
    for script in (s, c, ss, ms):
        assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0


def test_import_names_map_packages():
    assert labm.import_names(
        ["scikit-learn", "pyyaml==6", "biopython", "gcc", "--x"]
    ) == ["Bio", "sklearn", "yaml"]


def test_probe_commands_are_read_only_and_refuse_anything_else():
    ok = labm._probe_cmd(
        {"kind": "help", "cmd": "SU2_CFD --help", "load": ["su2/8.2.0"]}
    )
    assert ok and "timeout 30 SU2_CFD --help" in ok and "module load su2/8.2.0" in ok
    assert labm._probe_cmd({"kind": "help", "cmd": "rm -rf /"}) is None
    assert labm._probe_cmd({"kind": "help", "cmd": "SU2_CFD case.cfg"}) is None
    assert labm._probe_cmd({"kind": "help", "cmd": "x --help; rm -rf ~"}) is None
    assert labm._probe_cmd({"kind": "url", "url": "file:///etc/passwd"}) is None
    assert labm._probe_cmd({"kind": "pyhelp", "target": "os.system('x')"}) is None
    assert "curl -sSIL" in labm._probe_cmd(
        {"kind": "url", "url": "https://example.org/a.csv"}
    )
    script, accepted = labm.probe_script(
        [
            {"kind": "module", "name": "su2/8.2.0"},
            {"kind": "shell", "cmd": "id"},
            {"kind": "pyversion", "package": "ortools"},
        ]
    )
    assert len(accepted) == 2 and "=== CHECK 2" in script
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0


def test_script_urls_and_url_warnings():
    plan = {
        "script": "curl -sSfL https://x.org/data.csv -o d\n# docs https://y.org/${V}/f\n",
        "inputs": ["https://z.org/t.dat"],
    }
    assert labm.script_urls(plan) == ["https://x.org/data.csv", "https://z.org/t.dat"]
    plan["url_checks"] = [
        {"url": "https://x.org/data.csv", "ok": False, "status": "HTTP 404"}
    ]
    assert "HTTP 404" in Lab.url_warnings(plan)[0]


def test_planner_probes_go_into_the_plan_prompt(wlab):
    run = wlab.create(1, "document", "Use SU2 for a cavity", "")
    wlab.replies.append(
        '```json\n{"checks": [{"kind": "help", "cmd": "SU2_CFD --help"}]}\n```'
    )
    seen = {}

    def ask(prompt, search):
        if "you may check facts" in prompt:
            return wlab.replies.pop(0), 0.0
        seen["plan"] = prompt
        return "```json\n" + json.dumps(PLAN) + "\n```", 0.0

    wlab._ask = ask
    wlab._warm_exec = lambda tgt, task, script, wait: (
        0,
        "=== CHECK 1\nSU2 v8.2.0 usage: SU2_CFD cfg",
    )
    wlab.env_notes = lambda tgt: ""
    wlab.make_plan(run["id"], "cavity")
    assert (
        "FACTS CHECKED ON THE CLUSTER" in seen["plan"] and "SU2 v8.2.0" in seen["plan"]
    )
    assert wlab.get(run["id"])["status"] == "draft"


def test_workload_shape_and_partition_suggestion():
    t = FakeTarget()
    t.partitions = {
        "standard": {"gpus": 0},
        "computehigh": {"gpus": 0},
        "gpul4": {"gpus": 1},
        "highmem": {},
        "spot": {"spot": True},
    }
    gpu = {"resources": {"gpus": 1, "partition": "standard"}}
    assert labm.workload_shape(gpu) == "gpu"
    assert labm.suggest_partition(t, gpu)[0] == "gpul4"
    mpi = {"resources": {"partition": "standard"}, "script": "srun SU2_CFD c.cfg"}
    assert labm.workload_shape(mpi) == "mpi"
    assert labm.suggest_partition(t, mpi) == ("standard", "")  # fine as asked
    part, why = labm.suggest_partition(t, mpi, {"standard"})
    assert part == "computehigh" and "stocked out" in why


def test_time_from_history(wlab):
    done = _draft(wlab, software=[{"name": "simpy"}])
    wlab._update(done["id"], status="completed", elapsed="00:02:00")
    plan = {**PLAN, "software": [{"name": "simpy"}]}
    assert labm.time_from_history(wlab.db_path, plan) == "00:15:00"  # 3x2 min + 5 -> 15
    other = {**PLAN, "software": [{"name": "zzz"}], "install": {"conda": ["zzz"]}}
    assert labm.time_from_history(wlab.db_path, other) is None


def test_stockout_moves_a_queued_job_to_another_partition(wlab, monkeypatch):
    t = wlab.fake
    t.partitions = {"standard": {}, "computehigh": {}}
    run = _draft(wlab, resources={**PLAN["resources"], "gpus": 1})  # gpu: skips smoke
    wlab.submit(run["id"])
    wlab._update(
        run["id"],
        plan={
            **wlab.get(run["id"])["plan"],
            "resources": {**PLAN["resources"], "partition": "standard"},
        },
    )
    t.status = lambda rid, job: {
        "slurm_state": "PENDING",
        "reason": "",
        "elapsed": "",
        "node": "",  # type: ignore[method-assign]
        "exit_code": "",
        "started": "",
        "stage": "",
        "log_size": 0,
        "node_fails": labm.NODE_FAIL_WARN,
    }
    monkeypatch.setattr(labm, "stocked_out_partitions", lambda tgt: {"standard"})
    wlab.poll(wlab.get(run["id"]))
    moved = wlab.get(run["id"])
    assert moved["plan"]["resources"]["partition"] == "computehigh"
    assert (
        "stocked" not in moved["stage"]
        and "Moved from standard to computehigh" in moved["stage"]
    )
    assert t.cancelled and moved["job_id"] == "5151"

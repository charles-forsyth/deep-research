"""Lab plan v3: warm node, smoke test + AI fix loop, install ladder, probes, matching."""

import json
import re
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


def test_ensure_warm_scales_out_with_the_backlog(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "squeue").write_text("#!/bin/bash\necho '226 RUNNING'\n")
    (bin_ / "sbatch").write_text(f"#!/bin/bash\necho x >> {tmp_path}/sb.log; echo 77\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    home = tmp_path / "home"
    w = home / "deep-research-lab" / "warm"
    (w / "workers").mkdir(parents=True)
    (w / "workers" / "226.json").write_text('{"job": 226, "draining": false}')
    tgt = labm.SlurmSSHTarget(
        {
            "name": "x",
            "ssh_host": "h",
            "partitions": {"computehigh": {}},
            "warm": {"max_workers": 3},
        }
    )

    def sh(cmd, stdin=None, timeout=0):
        env = {"HOME": str(home), "PATH": f"{bin_}:/usr/bin:/bin"}
        return subprocess.run(
            ["bash", "-c", cmd], input=stdin, env=env, capture_output=True, timeout=30
        ).stdout.decode()

    tgt.sh = sh  # type: ignore[method-assign]
    assert tgt.ensure_warm() == "running:226"  # empty queue: the one worker is enough
    assert not (tmp_path / "sb.log").exists()
    for k in range(5):
        (w / "queue" / f"t{k}").mkdir(parents=True)
    assert tgt.ensure_warm().startswith("started:")
    assert (tmp_path / "sb.log").read_text().count(
        "x"
    ) == 2  # 1 + 5 // 2 = 3 workers, capped at 3


def test_ladder_module_first_and_verify_imports_installed_in_fallbacks():
    """A python-sci plan with no pip: use the module as is; fallbacks get numba too."""
    t = FakeTarget()
    plan = dict(
        PLAN,
        install={
            "modules": ["python-sci/2026.09"],
            "verify": ["python -c 'import numba, scipy.sparse, os'"],
        },
    )
    s = build_sbatch(1, plan, t)
    assert "LADDER_OK=module" in s
    assert s.index("LADDER_OK=module") < s.index("ladder_try isolated-venv")
    assert "ladder_try layered-venv" not in s  # nothing to layer
    iso = s[s.index("ladder_try isolated-venv") :].split("\n", 1)[0]
    assert "numba" in iso and " os" not in iso
    px = s[s.index("ladder_try pixi-conda-forge") :].split("\n", 1)[0]
    assert "numba" in px
    assert labm.verify_imports(
        [
            "python -c 'import networkx, EoN; from sklearn.cluster import KMeans'",
            "lmp -h",
        ]
    ) == ["networkx", "EoN", "sklearn"]
    # the verify list is part of the env key: different checks, different cache dirs
    other = dict(
        PLAN,
        install={
            "modules": ["python-sci/2026.09"],
            "verify": ["python -c 'import numpy'"],
        },
    )
    assert re.search(r"LADDER_KEY=(\w+)", s).group(1) != re.search(
        r"LADDER_KEY=(\w+)", build_sbatch(1, other, t)
    ).group(1)
    assert subprocess.run(["bash", "-n"], input=s, text=True).returncode == 0


# ---------------------------------------------------------------- v0.34 resilience


@pytest.mark.parametrize(
    "log,cls",
    [
        (
            "[ERROR] no install method produced a working environment (tried: a b)",
            "install",
        ),
        (
            "FATAL:   While making image from oci registry: unable to parse image name /x.sif",
            "container",
        ),
        ("stl_vector.h:1128: ... Assertion '__n < this->size()' failed.", "tool-crash"),
        ("lmp: /lib64/libc.so.6: version `GLIBC_2.34' not found", "glibc"),
        ("ERROR: lmp binary lacks granular support", "missing-feature"),
        ("ZeroDivisionError: division by zero", "numerical"),
        ("Traceback\nKeyError: 'x'", "script"),
    ],
)
def test_classify_failure(log, cls):
    assert labm.classify_failure(log)[0] == cls


def test_fix_concerns_catch_real_ai_fix_mistakes():
    # run #56: a failed fit returns the reference exponent
    a = {
        "script": "tau_ref = 1.27\ndef fit(x):\n    if len(x) < 2:\n        return float('nan'), 0.0\n"
    }
    b = {
        "script": "tau_ref = 1.27\ndef fit(x):\n    if len(x) < 2:\n        return 1.27, 0.0\n"
    }
    assert any("reference value 1.27" in c for c in labm.fix_concerns(a, b))
    # run #59: k3_v -> k3_u inside the RK4 update
    rk = "v_next = v + (dt / 6.0) * (k1_v + 2.0 * k2_v + 2.0 * {} + k4_v)\n"
    c = labm.fix_concerns({"script": rk.format("k3_v")}, {"script": rk.format("k3_u")})
    assert any("k3_v -> k3_u" in x for x in c)
    # run #64: library swapped out, verdict loosened
    a = {"script": "import EoN\nchecks = [{'expected': 0.85, 'tolerance': 0.25}]\n"}
    b = {"script": "import heapq\nchecks = [{'expected': 0.80, 'tolerance': 0.20}]\n"}
    c = labm.fix_concerns(a, b)
    assert any("EoN" in x for x in c) and any("tolerance" in x for x in c)
    # strings and titles are not formulas
    t1 = {"script": 'ax.set_title("Barabási-Albert: size vs f")\n'}
    t2 = {"script": 'ax.set_title("Barabasi-Albert: size vs f")\n'}
    assert labm.fix_concerns(t1, t2) == []


def test_smoke_install_failure_is_not_retried_forever(wlab):
    run = _draft(wlab)
    wlab.submit(run["id"])
    t1 = wlab.get(run["id"])["smoke"]["task"]
    log = "[ERROR] no install method produced a working environment (tried: pixi)\n"
    # round 1 fails on install: goes to the AI with a diagnosis
    seen = {}

    def ask(prompt, search):
        seen["prompt"] = prompt
        fixed = {
            **PLAN,
            "script": "echo ok > outputs/result.txt",
            "expected_outputs": ["outputs/result.txt"],
        }
        return "```json\n" + json.dumps(
            {"plan": fixed, "changes": ["x"], "notes": ""}
        ) + "\n```", 0.0

    wlab._ask = ask
    orig_thread = labm.threading.Thread

    class Inline:
        def __init__(self, target, args, daemon):
            self.t, self.a = target, args

        def start(self):
            self.t(*self.a)

    labm.threading.Thread = Inline  # type: ignore[misc,assignment]
    try:
        wlab.fake.finish(t1, 4, log, run=run["id"])
        wlab.poll(wlab.get(run["id"]))
        assert "DIAGNOSIS: No install method" in seen["prompt"]
        t2 = wlab.get(run["id"])["smoke"]["task"]
        wlab.fake.finish(t2, 4, log, run=run["id"])
        wlab.poll(wlab.get(run["id"]))
    finally:
        labm.threading.Thread = orig_thread  # type: ignore[misc]
    f = wlab.get(run["id"])
    assert (
        f["status"] == "failed"
        and "(install); not handed to the AI again" in f["stage"]
    )
    assert "software setup" in f["error"]
    assert f["smoke"]["rounds"][-1]["class"] == "install"


def test_local_container_is_used_in_place_not_pulled():
    t = FakeTarget()
    plan = dict(
        PLAN,
        install={
            "apptainer": ["/apps/containers/cuda-12.4-devel.sif", "docker://a/b:1"]
        },
    )
    s = build_sbatch(1, plan, t)
    assert "apptainer pull" in s and "docker://a/b:1" in s
    assert "pull $HOME/deep-research-lab/images/apps" not in s
    assert "export IMG_CUDA_12_4_DEVEL=/apps/containers/cuda-12.4-devel.sif" in s
    assert subprocess.run(["bash", "-n"], input=s, text=True).returncode == 0


def test_new_probe_kinds_are_read_only():
    f = labm._probe_cmd(
        {"kind": "features", "cmd": "lmp", "load": ["openmpi", "lammps"]}
    )
    assert f and "timeout 30 lmp -h" in f and "module load lammps" in f
    assert labm._probe_cmd({"kind": "features", "cmd": "lmp -in x"}) is None
    assert labm._probe_cmd({"kind": "features", "cmd": "rm;x"}) is None
    c = labm._probe_cmd({"kind": "conda", "package": "lammps"})
    assert c and "pixi search" in c
    assert labm._probe_cmd({"kind": "conda", "package": "x; rm -rf ~"}) is None


def test_ladder_pinned_binary_conda_plan():
    """Run #75: lammps=2023.08.02 (glibc 2.28) must not get python=3.12 forced next to it,
    the relaxed rung keeps the pin verify depends on, and no pip rung pretends to provide
    the lmp binary; every verify line must pass, not just the last."""
    t = FakeTarget()
    plan = dict(
        PLAN,
        install={
            "conda": ["lammps=2023.08.02", "numpy"],
            "channels": ["conda-forge"],
            "verify": ["lmp -h | grep -q GRANULAR", "python -c 'import numpy'"],
        },
    )
    s = build_sbatch(1, plan, t)
    first = s[s.index("ladder_try pixi ") :].split("\n", 1)[0]
    assert "python=3.12" not in first and "lammps=2023.08.02" in first
    loose = s[s.index("ladder_try pixi-loose") :].split("\n", 1)[0]
    assert "lammps=2023.08.02" in loose
    assert "ladder_try pip-venv" not in s
    assert "while IFS= read -r line" in s and "set -o pipefail" in s

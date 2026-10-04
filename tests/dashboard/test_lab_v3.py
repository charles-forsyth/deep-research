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


def test_probe_with_a_here_document_runs(tmp_path):
    """Job 505: the pyhelp probe holds a here-document, which broke the one-line
    `( cmd )` wrapper (syntax error, so no answer). Each probe now runs from its file."""
    script, ok = labm.probe_script(
        [
            {"kind": "pyhelp", "target": "json.dumps"},
            {"kind": "help", "cmd": "ls --help"},
        ]
    )
    assert len(ok) == 2 and "<<" in labm._probe_cmd(ok[0])
    r = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path), "HOME": str(tmp_path)},
    )  # fmt: skip
    assert "syntax error" not in r.stdout + r.stderr
    after = r.stdout.split("=== CHECK 1", 1)[1].split("=== CHECK 2", 1)
    assert "signature: (obj" in after[0] and "=== CHECK 2" in r.stdout


def test_every_job_trusts_a_ca_bundle_before_the_script_runs():
    """Run 4 lost a pilot round to CERTIFICATE_VERIFY_FAILED from urllib under the
    module Python. The batch file now points TLS clients at a CA bundle up front."""
    plan = {**PLAN, "script": "python fetch.py\n"}
    s = labm.build_sbatch(9, plan, FakeTarget(), [])
    head, _, user = s.partition("cat > user_script.sh")
    assert "export SSL_CERT_FILE" in head and "REQUESTS_CA_BUNDLE" in head
    assert "certifi" in head and "/etc/pki/tls/certs/ca-bundle.crt" in head
    assert 'if [ -z "${SSL_CERT_FILE:-}" ]' in head  # a plan's own setting wins
    assert subprocess.run(["bash", "-n"], input=s, text=True).returncode == 0


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
    wlab._check_run = lambda tgt, task, script, wait: (
        0,
        "=== CHECK 1\nSU2 v8.2.0 usage: SU2_CFD cfg",
    )
    wlab._jobs_for = lambda tgt: object()  # signed in to bifrost: checks run on check
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
    # plain CPU work and sweeps go to the cheap e2 partitions first (cluster 2026-10-03)
    cpu = {"resources": {"partition": "highmem"}, "script": "python a.py"}
    assert labm.suggest_partition(t, cpu, {"highmem"})[0] == "standard"
    assert labm.suggest_partition(t, cpu, {"highmem", "standard"})[0] == "computehigh"
    sweep = {"resources": {"partition": "highmem"}, "approach": "parameter sweep"}
    assert labm.workload_shape(sweep) == "sweep"
    assert labm.suggest_partition(t, sweep, {"highmem"})[0] == "spot"


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


def test_ssh_submit_dodges_a_stocked_out_partition(wlab, monkeypatch):
    """Over SSH too: the batch file uploaded with the real run names the partition that
    can start a node, not the stocked-out one in the plan."""
    t = wlab.fake
    t.partitions = {"standard": {}, "computehigh": {}}
    t.warm = None
    monkeypatch.setattr(labm, "stocked_out_partitions", lambda tgt: {"standard"})
    run = _draft(
        wlab, smoke=False, resources={**PLAN["resources"], "partition": "standard"}
    )
    wlab.submit(run["id"])
    sent = t.submitted[run["id"]]
    assert "#SBATCH --partition=computehigh" in sent["run.sbatch"]
    assert '"partition": "computehigh"' in sent["plan.json"]


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
    assert "while IFS= read -r line" in s and "set +e +o pipefail" in s


def test_pixi_hook_runs_without_nounset():
    """conda activation scripts reference unset variables (hwloc: ZSH_VERSION, run #77)."""
    plan = dict(PLAN, install={"conda": ["gmsh", "numpy"], "channels": ["conda-forge"]})
    s = build_sbatch(1, plan, FakeTarget())
    assert 'set +u; eval "$(cd' in s and 'pixi shell-hook)"; set -u' in s


def test_missing_import_warning():
    plan = dict(
        PLAN,
        install={"modules": ["python-sci/2026.09"]},
        script="python3 - <<'EOF'\nimport numpy\nimport simpy\nfrom skrf import Network\nEOF\n",
    )
    w = labm.missing_import_warnings(plan)
    assert w and "simpy" in w[0] and "skrf" in w[0] and "numpy" not in w[0]
    plan["install"]["pip"] = ["simpy", "scikit-rf"]
    assert labm.missing_import_warnings(plan) == []
    # a container Python may carry anything
    assert (
        labm.missing_import_warnings(dict(plan, install={"apptainer": ["docker://x"]}))
        == []
    )


def test_layered_venv_sees_a_module_that_is_itself_a_venv():
    """python-ml is a venv: --system-site-packages alone hid pandas (run #76)."""
    plan = dict(PLAN, install={"modules": ["python-ml/2026.09"], "pip": ["simpy"]})
    s = build_sbatch(1, plan, FakeTarget())
    lay = s[s.index("ladder_try layered-venv") :].split("\n", 1)[0]
    assert "_site_module.pth" in lay and "site.getsitepackages" in lay


def test_ladder_bad_cache_is_versioned_and_verify_has_no_pipefail():
    """Runs #81/#83: a ladder bug marked working envs .bad forever; `lmp -h | grep -q`
    died of SIGPIPE under pipefail."""
    s = labm._LADDER_FUNCS
    assert "set +e +o pipefail" in s
    assert 'echo "$LADDER_VERSION" > "$dir/.bad"' in s and 'touch "$dir/.bad"' not in s
    plan = dict(
        PLAN, install={"conda": ["lammps=2023.08.02"], "channels": ["conda-forge"]}
    )
    sb = build_sbatch(1, plan, FakeTarget())
    assert f"LADDER_VERSION={labm.LADDER_VERSION}" in sb


def test_gmsh_maps_to_python_gmsh_on_conda():
    """Run #86: conda-forge gmsh has no Python module; pip gmsh needs libGLU."""
    plan = dict(PLAN, install={"modules": ["python-sci/2026.09"], "pip": ["gmsh"]})
    s = build_sbatch(1, plan, FakeTarget())
    assert "pixi add python-gmsh" in s
    plan = dict(PLAN, install={"conda": ["gmsh", "numpy"], "channels": ["conda-forge"]})
    s = build_sbatch(1, plan, FakeTarget())
    assert "python-gmsh" in s


def test_su2_max_time_warning():
    script = "cat << EOF > case.cfg\nTIME_DOMAIN= YES\nTIME_ITER= 2000\nEOF\nmpirun SU2_CFD case.cfg\n"
    w = labm.validate_plan(FakeTarget(), dict(PLAN, script=script))
    assert any("MAX_TIME" in x for x in w)
    w = labm.validate_plan(
        FakeTarget(),
        dict(PLAN, script=script.replace("TIME_ITER", "MAX_TIME= 10\nTIME_ITER")),
    )
    assert not any("MAX_TIME" in x for x in w)

"""Lab runs: plan -> review -> submit -> watch -> fetch -> write-up, with a fake cluster.

No network: Gemini is replaced by canned replies and the Slurm target by an in-memory fake.
"""

import io
import json
import subprocess
import tarfile
import threading
import urllib.request

import pytest

from deepresearch.dashboard import lab as labm
from deepresearch.dashboard.lab import Lab, build_sbatch, estimate_cost, extract_json
from tests.dashboard.test_server import _seed, app  # noqa: F401  (fixture)

PLAN = {
    "computable": True,
    "title": "Aspirin descriptors",
    "question": "What are aspirin's molecular weight and logP?",
    "approach": "RDKit descriptors from SMILES.",
    "software": [{"name": "rdkit", "source": "conda-forge", "why": "cheminformatics"}],
    "inputs": ["SMILES CC(=O)Oc1ccccc1C(=O)O"],
    "parameters": {"smiles": "CC(=O)Oc1ccccc1C(=O)O", "n": 3},
    "resources": {
        "partition": "standard",
        "nodes": 1,
        "cores": 2,
        "time_limit": "00:30:00",
        "gpus": 0,
    },
    "install": {
        "modules": [],
        "conda": ["python=3.12", "rdkit"],
        "channels": ["conda-forge"],
        "pip": [],
    },
    "script": 'python -c "print(1)" > outputs/result.txt',
    "expected_outputs": ["outputs/result.txt"],
    "success_criteria": "result.txt exists",
    "caveats": "none",
}


class FakeTarget:
    kind = "fake"
    name = "fake"
    label = "Fake cluster"
    default_partition = "standard"
    partitions = {"standard": {"machine": "c2d", "cpus": 16, "usd_per_hour": 1.45}}

    def __init__(self):
        self.submitted = {}
        self.state = "PENDING"
        self.stage = ""
        self.cancelled = []

    def describe(self):
        return "Fake cluster"

    def job_dir(self, run_id):
        return f"~/lab/run_{run_id}"

    def submit(self, run_id, files):
        self.submitted[run_id] = files
        return "4242"

    def status(self, run_id, job_id):
        return {
            "slurm_state": self.state,
            "reason": "",
            "elapsed": "0:10",
            "node": "n1",
            "exit_code": "0:0",
            "started": "",
            "stage": self.stage,
            "log_size": 5,
        }

    def log(self, run_id, offset=0, limit=200000):
        return "hello"[offset:], 5

    def cancel(self, job_id):
        self.cancelled.append(job_id)

    def fetch(self, run_id, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "outputs").mkdir(exist_ok=True)
        (dest / "outputs" / "result.txt").write_text("MW 180.16\n")
        (dest / "outputs" / "plot.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (dest / "job.log").write_text("[STAGE] Running\ndone\n")
        return [
            {"path": "outputs/result.txt", "size": 10},
            {"path": "outputs/plot.png", "size": 12},
            {"path": "job.log", "size": 20},
            {"path": "outputs/huge.bin", "size": 999_999_999, "skipped": True},
        ]


@pytest.fixture
def lab(tmp_path):
    t = FakeTarget()
    lb = Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"fake": t})
    replies = []

    def ask(prompt, search):
        return replies.pop(0), 0.01

    lb._ask = ask  # type: ignore[method-assign]
    # tests drive poll() by hand; a real watcher thread would race them
    lb.ensure_watcher = lambda: None  # type: ignore[method-assign]
    lb.fake = t  # type: ignore[attr-defined]
    lb.replies = replies  # type: ignore[attr-defined]
    return lb


def test_extract_json_fenced_and_bare():
    assert extract_json('text\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('noise {"b": [1, 2]} tail') == {"b": [1, 2]}
    with pytest.raises(ValueError):
        extract_json("nothing here")


def test_full_lifecycle_plan_review_submit_watch_fetch_writeup(lab, tmp_path):
    lab.replies.append("```json\n" + json.dumps(PLAN) + "\n```")
    run = lab.create(7, "selection", "Aspirin has MW 180.", "compute it")
    assert run["status"] == "planning"
    lab.make_plan(run["id"], "Aspirin report")
    run = lab.get(run["id"])
    assert run["status"] == "draft" and run["plan"]["title"] == "Aspirin descriptors"
    assert "#SBATCH --partition=standard" in run["script"]
    assert run["estimate_usd"] == pytest.approx(0.72)  # 1 node x 0.5 h x $1.45
    assert run["ai_cost_usd"] == pytest.approx(0.01)

    # review: change a parameter and the time limit
    plan = run["plan"]
    plan["parameters"]["n"] = 5
    plan["resources"]["time_limit"] = "01:00:00"
    run = lab.edit_plan(run["id"], plan)
    assert "export PARAM_N=5" in run["script"]
    assert run["estimate_usd"] == pytest.approx(1.45)

    run = lab.submit(run["id"])
    assert run["status"] == "queued" and run["job_id"] == "4242"
    files = lab.fake.submitted[run["id"]]
    assert set(files) == {"run.sbatch", "plan.json", "README.txt"}
    assert json.loads(files["plan.json"])["parameters"]["n"] == 5

    lab.fake.state, lab.fake.stage = "RUNNING", "Installing software"
    lab.poll(lab.get(run["id"]))
    run = lab.get(run["id"])
    assert run["status"] == "running" and run["stage"] == "Installing software"

    lab.fake.state = "COMPLETED"
    lab.replies.append("**Result**\n\nMW is 180.16.")
    lab.poll(lab.get(run["id"]))
    run = lab.get(run["id"])
    assert run["status"] == "completed"
    assert "180.16" in run["result_md"]
    assert {f["path"] for f in run["files"]} >= {
        "outputs/result.txt",
        "outputs/plot.png",
    }
    assert (tmp_path / "lab" / f"run_{run['id']}" / "outputs" / "result.txt").exists()
    assert lab.file_path(run["id"], "outputs/result.txt").read_text() == "MW 180.16\n"
    with pytest.raises(FileNotFoundError):
        lab.file_path(run["id"], "../../h.db")


def test_failed_job_is_reported_as_failed_not_faked(lab):
    lab.replies.append(json.dumps(PLAN))
    run = lab.create(7, "document", "report")
    lab.make_plan(run["id"], "t")
    lab.submit(run["id"])
    lab.fake.state = "FAILED"
    lab.replies.append("**Result**\n\nThe job failed: rdkit did not install.")
    lab.poll(lab.get(run["id"]))
    run = lab.get(run["id"])
    assert run["status"] == "failed" and "failed" in run["result_md"]


def test_not_computable_plan(lab):
    lab.replies.append(
        json.dumps({"computable": False, "why_not": "It is a policy question."})
    )
    run = lab.create(7, "document", "policy text")
    lab.make_plan(run["id"], "t")
    run = lab.get(run["id"])
    assert run["status"] == "plan_failed" and "policy" in run["error"]
    with pytest.raises(ValueError):
        lab.submit(run["id"])


def test_bad_model_reply_fails_planning(lab):
    lab.replies.append("I cannot help with that.")
    run = lab.create(7, "document", "x")
    lab.make_plan(run["id"], "t")
    assert lab.get(run["id"])["status"] == "plan_failed"


def test_empty_search_reply_falls_back_to_plan_without_search(lab):
    # Flash + Google Search can stop on TOO_MANY_TOOL_CALLS with no text (run #14).
    calls = []

    def ask(prompt, search):
        calls.append(search)
        if search:
            raise labm.EmptyReply("TOO_MANY_TOOL_CALLS", 0.013)
        return json.dumps(PLAN), 0.07

    lab._ask = ask
    run = lab.create(7, "document", "x")
    lab.make_plan(run["id"], "t")
    run = lab.get(run["id"])
    assert calls == [True, False]
    assert run["status"] == "draft"
    assert run["ai_cost_usd"] == pytest.approx(0.083)
    assert run["plan"]["caveats"].startswith("Planned without web search")
    assert "TOO_MANY_TOOL_CALLS" in run["plan"]["caveats"]


def test_replan_retries_only_failed_plans(lab):
    lab.replies.append("no json here")
    run = lab.create(7, "document", "x")
    lab.make_plan(run["id"], "t")
    assert lab.get(run["id"])["status"] == "plan_failed"
    run = lab.replan(run["id"])
    assert run["status"] == "planning" and run["error"] is None
    lab.replies.append(json.dumps(PLAN))
    lab.make_plan(run["id"], "t")
    run = lab.get(run["id"])
    assert run["status"] == "draft" and run["plan"]["title"] == "Aspirin descriptors"
    with pytest.raises(ValueError):
        lab.replan(run["id"])  # a draft is not retried


def test_client_is_created_once_across_threads(lab, monkeypatch):
    # Two plans starting together used to build two clients; the loser was garbage-
    # collected mid-request ("Cannot send a request, as the client has been closed").
    import time as _t

    from google import genai

    made = []

    class SlowClient:
        def __init__(self, **kw):
            _t.sleep(0.05)
            made.append(self)

    monkeypatch.setattr(genai, "Client", SlowClient)
    lab._config = lambda: type("C", (), {"api_key": "k"})()
    got = []
    ts = [threading.Thread(target=lambda: got.append(lab._client())) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(made) == 1 and all(g is made[0] for g in got)


def test_ask_raises_empty_reply_with_finish_reason(lab):
    class Resp:
        text = None
        usage_metadata = None
        candidates = [type("C", (), {"finish_reason": type("F", (), {"name": "X"})})]

    class Models:
        def generate_content(self, **kw):
            return Resp()

    lab._genai = type("G", (), {"models": Models()})()
    with pytest.raises(labm.EmptyReply, match="finish reason: X"):
        Lab._ask(lab, "p", search=False)


def test_only_drafts_submit_and_cancel_calls_scancel(lab):
    lab.replies.append(json.dumps(PLAN))
    run = lab.create(7, "document", "x")
    lab.make_plan(run["id"], "t")
    lab.submit(run["id"])
    with pytest.raises(ValueError):
        lab.submit(run["id"])  # no double submit
    lab.fake.state = "RUNNING"
    run = lab.cancel(run["id"])
    assert run["status"] == "cancelled" and lab.fake.cancelled == ["4242"]


def test_rerun_copies_plan_as_new_draft(lab):
    lab.replies.append(json.dumps(PLAN))
    run = lab.create(7, "document", "x")
    lab.make_plan(run["id"], "t")
    new = lab.create(
        7, "document", "x", rerun_of=run["id"], plan=lab.get(run["id"])["plan"]
    )
    assert new["status"] == "draft" and new["rerun_of"] == run["id"] and new["script"]


def test_suggestions_cached_until_refresh(lab):
    lab.replies.append(
        json.dumps({"suggestions": [{"title": "A", "question": "q"}], "note": "n"})
    )
    s1 = lab.suggestions(7, "t", "report")
    assert s1["suggestions"][0]["title"] == "A" and not s1["cached"]
    s2 = lab.suggestions(7, "t", "report")
    assert s2["cached"]
    lab.replies.append(json.dumps({"suggestions": [], "note": "nothing"}))
    assert lab.suggestions(7, "t", "report", refresh=True)["suggestions"] == []


def test_purge_session_removes_runs_and_files(lab, tmp_path):
    lab.replies.append(json.dumps(PLAN))
    run = lab.create(9, "document", "x")
    (tmp_path / "lab" / f"run_{run['id']}").mkdir(parents=True)
    lab.purge_session([9])
    assert lab.get(run["id"]) is None
    assert not (tmp_path / "lab" / f"run_{run['id']}").exists()


def test_sbatch_harness_is_safe_and_complete(lab):
    t = lab.fake
    plan = dict(PLAN)
    plan["install"] = {
        "modules": ["gromacs", "bad; rm -rf /"],
        "conda": ["rdkit", "$(evil)"],
        "pip": ["vllm"],
        "apptainer": ["docker://biocontainers/blast:2.2.31"],
    }
    plan["resources"] = {
        "partition": "nope",
        "nodes": 2,
        "time_limit": "bogus",
        "gpus": 1,
    }
    plan["parameters"] = {"model id": "a b'c"}
    s = build_sbatch(5, plan, t)
    assert "#SBATCH --partition=standard" in s  # unknown partition falls back
    assert "#SBATCH --time=01:00:00" in s  # invalid limit replaced
    assert "#SBATCH --gres=gpu:1" in s and "#SBATCH --nodes=2" in s
    assert "module load gromacs" in s and "rm -rf /" not in s and "$(evil)" not in s
    assert (
        "pixi add rdkit python=3.12 pip" in s
        and "pip install --progress-bar off vllm" in s
    )
    assert "apptainer pull" in s
    assert "export PARAM_MODEL_ID='a b'\"'\"'c'" in s
    assert "set -euo pipefail" in s and 'stage "Running"' in s
    assert "DR_LAB_EOF" in s


def test_pip_with_conda_python_still_gets_pip(lab):
    plan = dict(PLAN)
    plan["install"] = {"conda": ["python=3.11"], "pip": ["qiskit"]}
    s = build_sbatch(1, plan, lab.fake)
    assert (
        "pixi add python=3.11 pip" in s and "pip install --progress-bar off qiskit" in s
    )


def test_pip_index_url_allowed_but_not_arbitrary_flags(lab):
    plan = dict(PLAN)
    plan["install"] = {
        "pip": [
            "torch==2.5.1",
            "--extra-index-url https://download.pytorch.org/whl/cu124",
            "--trusted-host evil.example",
            "--index-url http://insecure.example/simple",
        ]
    }
    s = build_sbatch(1, plan, lab.fake)
    assert "--extra-index-url https://download.pytorch.org/whl/cu124" in s
    assert "evil.example" not in s and "insecure.example" not in s
    assert "envs/torch-2-5-1-" in s  # readable prefix ignores the index URL


def test_containers_load_the_apptainer_module(lab):
    plan = dict(PLAN)
    plan["install"] = {"apptainer": ["docker://python:3.12-slim"]}
    s = build_sbatch(1, plan, lab.fake)
    assert "module load apptainer" in s and "apptainer pull" in s
    assert "export IMG_PYTHON=" in s


def test_estimate_cost_handles_formats():
    t = FakeTarget()
    assert estimate_cost(
        t, {"resources": {"nodes": 2, "time_limit": "1-00:00:00"}}
    ) == pytest.approx(69.6)
    assert estimate_cost(t, {"resources": {"time_limit": "90"}}) == pytest.approx(
        2.17, abs=0.01
    )
    assert estimate_cost(None, {}) is None


def test_watcher_thread_advances_runs(lab):
    lab.replies.append(json.dumps(PLAN))
    run = lab.create(7, "document", "x")
    lab.make_plan(run["id"], "t")
    lab.submit(run["id"])
    lab.fake.state = "COMPLETED"
    lab.replies.append("**Result** ok")
    done = threading.Event()
    orig = lab._finish

    def finish(r, tgt):
        orig(r, tgt)
        done.set()

    lab._finish = finish  # type: ignore[method-assign]
    lab._stop.set()
    lab._stop.clear()
    t = threading.Thread(target=lab._watch_loop, kwargs={"interval": 0.05}, daemon=True)
    t.start()
    assert done.wait(5)
    lab._stop.set()
    assert lab.get(run["id"])["status"] == "completed"


def test_real_target_fetch_parses_tar_and_blocks_traversal(tmp_path, monkeypatch):
    tgt = labm.SlurmSSHTarget({"name": "x", "ssh_host": "h", "partitions": {}})
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in {"outputs/a.csv": b"x,1\n", "../evil.txt": b"no"}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    class R:
        def __init__(self, out, rc=0):
            self.stdout, self.returncode, self.stderr = out, rc, b""

    calls = []

    def run(cmd, stdin=None, timeout=120):
        calls.append(cmd)
        if cmd.startswith("cd") and "find" in cmd:
            return R(b"4\toutputs/a.csv\n2\t../evil.txt\n")
        return R(buf.getvalue())

    monkeypatch.setattr(tgt, "run", run)
    files = tgt.fetch(3, tmp_path / "dest")
    assert [f["path"] for f in files] == ["outputs/a.csv"]
    assert (tmp_path / "dest" / "outputs" / "a.csv").read_bytes() == b"x,1\n"
    assert not (tmp_path / "evil.txt").exists()


def test_status_parses_sacct_squeue_stage(monkeypatch):
    tgt = labm.SlurmSSHTarget({"name": "x", "ssh_host": "h", "partitions": {}})
    out = (
        "RUNNING|00:05:00|node-1|0:0|2026-09-26T21:00:00\n::SQ::\nRUNNING|None|5:00|node-1\n"
        "::ST::\nInstalling software\n::SZ::\n1234\n"
    )
    monkeypatch.setattr(tgt, "sh", lambda cmd, stdin=None, timeout=120: out)
    st = tgt.status(1, "99")
    assert st["slurm_state"] == "RUNNING" and st["stage"] == "Installing software"
    assert st["log_size"] == 1234 and st["node"] == "node-1"
    assert st["node_fails"] == 0  # old output without the NODE_FAIL count


def test_status_counts_node_failures(monkeypatch):
    """Run #29: 14 NODE_FAIL requeues looked like a normal 'waiting for a node'."""
    tgt = labm.SlurmSSHTarget({"name": "x", "ssh_host": "h", "partitions": {}})
    out = (
        "PENDING|00:00:00|None assigned|0:0|None\n::SQ::\nPENDING|BeginTime|0:00|\n"
        "::NF::\n14\n::ST::\n\n::SZ::\n0\n"
    )
    monkeypatch.setattr(tgt, "sh", lambda cmd, stdin=None, timeout=120: out)
    st = tgt.status(1, "202")
    assert st["slurm_state"] == "PENDING" and st["node_fails"] == 14


def test_queue_label_says_when_nodes_keep_failing(tmp_path):
    fake = FakeTarget()
    fake.state = "PENDING"
    lab = Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"fake": fake})
    lab.ensure_watcher = lambda: None  # type: ignore[method-assign]
    plan = {**PLAN, "resources": {**PLAN["resources"], "partition": "spot"}}
    run = lab.create(1, "document", "t", plan=plan)
    lab.submit(run["id"])
    base = fake.status

    fake.status = lambda rid, jid: {
        **base(rid, jid),
        "reason": "BeginTime",
        "node_fails": 1,
    }
    lab.poll(lab.get(run["id"]))
    st = lab.get(run["id"])["stage"]
    assert (
        st.startswith("Requeued after 1 node failure on spot") and "another" not in st
    )

    fake.status = lambda rid, jid: {**base(rid, jid), "node_fails": 14}
    lab.poll(lab.get(run["id"]))
    st = lab.get(run["id"])["stage"]
    assert (
        "14 node failures on spot" in st and "resubmitting on another partition" in st
    )

    fake.status = lambda rid, jid: {**base(rid, jid), "node_fails": 0}
    lab.poll(lab.get(run["id"]))
    assert lab.get(run["id"])["stage"] == "Queued, waiting for a node"


# ---- HTTP API ----------------------------------------------------------------


def test_api_lab_endpoints(app, tmp_path):  # noqa: F811
    api = app["api"]
    fake = FakeTarget()
    api.lab.targets = {"fake": fake}
    api.lab.results_dir = tmp_path / "labres"
    replies = [json.dumps(PLAN)]
    api.lab._ask = lambda prompt, search: (replies.pop(0), 0.02)
    sid = _seed(api, "Aspirin", "Aspirin MW is 180.")
    call = app["call"]

    status, data = call("GET", f"/api/sessions/{sid}/lab")
    assert status == 200 and data["configured"] and data["runs"] == []

    status, err = call(
        "POST", f"/api/sessions/{sid}/lab", {"scope": "selection", "selection": ""}
    )
    assert status == 400

    api.lab.make_plan = lambda rid, title: Lab.make_plan(api.lab, rid, title)
    status, run = call(
        "POST",
        f"/api/sessions/{sid}/lab",
        {"scope": "selection", "selection": "Aspirin MW is 180."},
    )
    assert status == 200
    import time

    for _ in range(50):
        if api.lab.get(run["id"])["status"] == "draft":
            break
        time.sleep(0.05)
    status, run = call("GET", f"/api/lab/{run['id']}")
    assert run["status"] == "draft" and run["target_label"] == "Fake cluster"

    plan = run["plan"]
    plan["parameters"]["n"] = 9
    status, run = call("PUT", f"/api/lab/{run['id']}/plan", {"plan": plan})
    assert status == 200 and "PARAM_N=9" in run["script"]

    api.lab.ensure_watcher = (
        lambda: None
    )  # the test drives poll() itself; no racing thread
    status, run = call("POST", f"/api/lab/{run['id']}/submit")
    assert status == 200 and run["status"] == "queued"
    api.lab._stop.set()  # keep the background watcher out of this test

    fake.state = "COMPLETED"
    replies.append("**Result** MW 180.16")
    api.lab.poll(api.lab.get(run["id"]))
    status, run = call("GET", f"/api/lab/{run['id']}")
    assert run["status"] == "completed"

    status, raw = call("GET", f"/api/lab/{run['id']}/file?path=outputs/result.txt")
    assert status == 200 and b"180.16" in raw
    status, _ = call("GET", f"/api/lab/{run['id']}/file?path=../../history.db")
    assert status == 404

    status, rr = call("POST", f"/api/lab/{run['id']}/rerun")
    assert status == 200 and rr["status"] == "draft" and rr["rerun_of"] == run["id"]

    status, _ = call("DELETE", f"/api/lab/{rr['id']}")
    assert status == 200

    # retry plan: refused unless planning failed, then plans again in the background
    status, _ = call("POST", f"/api/lab/{run['id']}/replan")
    assert status == 409
    replies.extend(["garbage", json.dumps(PLAN)])
    status, bad = call(
        "POST", f"/api/sessions/{sid}/lab", {"scope": "selection", "selection": "x"}
    )
    for _ in range(50):
        if api.lab.get(bad["id"])["status"] == "plan_failed":
            break
        time.sleep(0.05)
    status, again = call("POST", f"/api/lab/{bad['id']}/replan")
    assert status == 200 and again["status"] == "planning"
    for _ in range(50):
        if api.lab.get(bad["id"])["status"] == "draft":
            break
        time.sleep(0.05)
    assert api.lab.get(bad["id"])["status"] == "draft"

    # deleting the session removes its lab runs
    call("DELETE", f"/api/sessions/{sid}")
    assert api.lab.get(run["id"]) is None


def test_api_lab_without_target(app):  # noqa: F811
    api = app["api"]
    api.lab.targets = {}
    sid = _seed(api)
    status, data = app["call"]("GET", f"/api/sessions/{sid}/lab")
    assert status == 200 and data["configured"] is False
    status, err = app["call"]("POST", f"/api/sessions/{sid}/lab", {"scope": "document"})
    assert status == 400 and "lab_targets.json" in err["error"]


def test_static_lab_js_served(app):  # noqa: F811
    port = app["port"]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/lab.js") as r:
        assert b"const LAB" in r.read()


# ---- review fixes (v0.19.7) ---------------------------------------------------


def test_hours_reads_time_limits_like_slurm():
    from deepresearch.dashboard.lab import _hours

    assert _hours("30") == pytest.approx(0.5)  # MM
    assert _hours("30:00") == pytest.approx(0.5)  # MM:SS, not 30 hours
    assert _hours("01:30:00") == pytest.approx(1.5)
    assert _hours("1-12") == pytest.approx(36)  # D-HH
    assert _hours("1-12:30") == pytest.approx(36.5)  # D-HH:MM
    assert _hours("2-00:00:00") == pytest.approx(48)


def test_env_cache_key_differs_for_every_package_set(lab):
    import re

    base = ["matplotlib", "numpy", "pandas", "scipy", "seaborn", "scikit-learn"]
    base += ["statsmodels"]

    def key(conda, chans=("conda-forge",)):
        plan = dict(PLAN, install={"conda": conda, "channels": list(chans)})
        return re.search(r"LADDER_KEY=(\S+)", build_sbatch(1, plan, lab.fake)).group(1)

    assert key(base) != key(base + ["xarray"])  # used to collide at 60 chars
    assert key(base) != key(base, ("conda-forge", "bioconda"))
    assert key(base) == key(list(reversed(base)))  # order does not matter
    s = build_sbatch(1, dict(PLAN), lab.fake)
    assert "flock 9" in s and "flock -u 9" in s  # concurrent builds wait
    assert "envs/python-3-12-rdkit-" in s  # readable prefix


def test_cancel_during_planning_is_not_overwritten(lab):
    run = lab.create(7, "document", "x")
    gate, go = threading.Event(), threading.Event()

    def ask(prompt, search):
        gate.set()
        go.wait(5)
        return json.dumps(PLAN), 0.01

    lab._ask = ask  # type: ignore[method-assign]
    t = threading.Thread(target=lab.make_plan, args=(run["id"], "t"))
    t.start()
    assert gate.wait(5)
    lab.cancel(run["id"])
    go.set()
    t.join(5)
    assert lab.get(run["id"])["status"] == "cancelled"


def test_cancel_between_watcher_read_and_write_wins(lab):
    lab.replies.append(json.dumps(PLAN))
    run = lab.create(7, "document", "x")
    lab.make_plan(run["id"], "t")
    lab.submit(run["id"])
    stale = lab.get(run["id"])  # what the watcher read at the start of its pass
    lab.fake.state = "RUNNING"
    lab.cancel(run["id"])
    lab.poll(stale)
    assert lab.get(run["id"])["status"] == "cancelled"
    lab.fake.state = "COMPLETED"
    lab.poll(stale)  # would fetch, pay for a write-up and mark completed
    assert lab.get(run["id"])["status"] == "cancelled"
    assert lab.get(run["id"])["result_md"] is None


def test_cancel_after_job_ended_skips_write_up(lab):
    lab.replies.append(json.dumps(PLAN))
    run = lab.create(7, "document", "x")
    lab.make_plan(run["id"], "t")
    lab.submit(run["id"])
    lab._update(run["id"], status="analyzing", slurm_state="COMPLETED")

    def gone(job_id):
        raise labm.TargetError("Invalid job id specified")

    lab.fake.cancel = gone
    stale = lab.get(run["id"])
    assert lab.cancel(run["id"])["status"] == "cancelled"  # no scancel, no 502
    lab._ask = lambda p, search: pytest.fail("paid for a write-up after cancel")
    lab.poll(stale)
    assert lab.get(run["id"])["status"] == "cancelled"


def test_fetch_failures_give_up_after_a_few_tries(lab):
    lab.replies.append(json.dumps(PLAN))
    run = lab.create(7, "document", "x")
    lab.make_plan(run["id"], "t")
    lab.submit(run["id"])
    lab.fake.state = "COMPLETED"

    def broken(run_id, dest):
        raise labm.TargetError("cluster command timed out after 600s")

    lab.fake.fetch = broken
    for _ in range(labm.MAX_FETCH_TRIES - 1):
        with pytest.raises(labm.TargetError):
            lab.poll(lab.get(run["id"]))
        assert lab.get(run["id"])["status"] == "fetching"
    lab.poll(lab.get(run["id"]))
    r = lab.get(run["id"])
    assert r["status"] == "failed" and "still on the cluster" in r["error"]


def test_cost_counts_searches_cached_and_thinking_tokens():
    from types import SimpleNamespace as NS

    from deepresearch.dashboard.lab import _cost, _search_count

    u = NS(
        prompt_token_count=1_000_000,
        cached_content_token_count=400_000,
        candidates_token_count=100_000,
        thoughts_token_count=100_000,
        tool_use_prompt_token_count=0,
    )
    # 600k fresh * 0.75 + 400k cached * 0.075 + 200k out * 3.75 + 10 searches * 0.014
    assert _cost(u, 10) == pytest.approx(0.45 + 0.03 + 0.75 + 0.14)
    resp = NS(
        candidates=[NS(grounding_metadata=NS(web_search_queries=["a", "b", "c"]))]
    )
    assert _search_count(resp) == 3
    assert _search_count(NS(candidates=None)) == 0


def test_model_follows_followup_setting(lab):
    from types import SimpleNamespace as NS

    lab._config = lambda: NS(followup_model="gemini-x", api_key="k")
    assert lab._model() == "gemini-x"
    lab._config = lambda: None
    assert lab._model() == labm.PLAN_MODEL


def test_bad_suggestions_reply_reports_what_it_cost(lab):
    lab.replies.append("no json here")
    with pytest.raises(ValueError, match=r"\$0\.0100 spent"):
        lab.suggestions(7, "t", "report")


def test_api_refuses_to_delete_session_with_live_lab_job(app, tmp_path):  # noqa: F811
    api = app["api"]
    fake = FakeTarget()
    api.lab.targets = {"fake": fake}
    api.lab.results_dir = tmp_path / "labres"
    api.lab._ask = lambda prompt, search: (json.dumps(PLAN), 0.02)
    api.lab.ensure_watcher = lambda: None
    sid = _seed(api, "Aspirin", "Aspirin MW is 180.")
    run = api.lab.create(int(sid), "document", "x")
    api.lab.make_plan(run["id"], "t")
    api.lab.submit(run["id"])
    status, err = app["call"]("DELETE", f"/api/sessions/{sid}")
    assert status == 409 and f"#{run['id']}" in err["error"]
    assert api.lab.get(run["id"])["status"] == "queued"
    api.lab.cancel(run["id"])
    status, _ = app["call"]("DELETE", f"/api/sessions/{sid}")
    assert status == 200 and api.lab.get(run["id"]) is None


# ---------------------------------------------------------------- cluster catalog

CATALOG = {
    "schema": "ursa-catalog/1",
    "cluster": "Test cluster",
    "generated": "2026-09-27T15:51:10+00:00",
    "summary": {
        "modules": 5,
        "spack_packages": 1694,
        "fields": ["molecular dynamics"],
        "gpu": {"driver": "580.178.04", "gpu": "NVIDIA L4", "cuda_max": "13.0"},
    },
    "partitions": [
        {
            "name": "standard",
            "default": True,
            "max_nodes": 32,
            "cpus_per_node": 16,
            "mem_gb_per_node": 124,
            "gpus_per_node": 0,
            "gpu_type": None,
            "usd_per_node_hour": 1.45,
            "spot": False,
            "use_for": "general",
        },
        {
            "name": "gpul4",
            "default": False,
            "max_nodes": 8,
            "cpus_per_node": 8,
            "mem_gb_per_node": 62,
            "gpus_per_node": 1,
            "gpu_type": "NVIDIA L4 24 GB",
            "usd_per_node_hour": 1.15,
            "spot": False,
            "use_for": "GPU",
        },
        {
            "name": "spot",
            "default": False,
            "max_nodes": 32,
            "cpus_per_node": 16,
            "mem_gb_per_node": 124,
            "gpus_per_node": 0,
            "gpu_type": None,
            "usd_per_node_hour": 0.74,
            "spot": True,
            "use_for": "sweeps",
        },
    ],
    "rules": ["Slurm CPUs are physical cores."],
    "install_tools": [{"name": "uv", "how": "on PATH", "use": "uv pip install"}],
    "modules": {
        "core": [
            "gcc/13.5.0",
            "openmpi/5.0.10",
            "python-ml/2026.09",
            "blast-plus/2.17.0",
        ],
        "mpi_dependent": {
            "openmpi": {
                "requires": "module load openmpi",
                "modules": [
                    "gromacs/2026.1",
                    "gromacs/2026.1-cuda",
                    "lammps/20250722.4",
                ],
            }
        },
    },
    "recipes": [
        {
            "name": "GROMACS (CPU, MPI)",
            "field": "molecular dynamics",
            "load": ["openmpi", "gromacs/2026.1"],
            "run": "srun gmx_mpi mdrun",
            "partition": "standard",
        }
    ],
    "containers": [
        {"path": "/apps/containers/vllm-0.6.4-cuda12.4.sif", "size_gb": 5.6}
    ],
    "how_to_load": "module load <name>",
    "job_header": {"path": "/apps/docs/templates/job-header.sh", "use": "source it"},
}


def _cat_target(tmp_path, monkeypatch, catalog=CATALOG):
    tgt = labm.SlurmSSHTarget(
        {
            "name": "ursa",
            "label": "Ursa",
            "ssh_host": "h",
            "catalog_path": "/apps/docs/catalog.json",
            "partitions": {
                "standard": {"machine": "c2d", "cpus": 16, "usd_per_hour": 9.99}
            },
        }
    )
    calls = []

    def sh(cmd, stdin=None, timeout=120):
        calls.append(cmd)
        return json.dumps(catalog)

    monkeypatch.setattr(tgt, "sh", sh)
    tgt.calls = calls  # type: ignore[attr-defined]
    return tgt


def test_catalog_fetch_caches_and_replaces_partitions(tmp_path, monkeypatch):
    tgt = _cat_target(tmp_path, monkeypatch)
    cat = tgt.load_catalog(tmp_path)
    assert cat["summary"]["gpu"]["driver"] == "580.178.04"
    assert tgt.calls == ["cat /apps/docs/catalog.json"]
    # live prices/partitions win over the hand-written table
    assert tgt.partitions["standard"]["usd_per_hour"] == 1.45
    assert (
        tgt.partitions["spot"]["spot"] is True and tgt.partitions["gpul4"]["gpus"] == 1
    )
    assert tgt.default_partition == "standard"
    cached = json.loads((tmp_path / "catalog-ursa.json").read_text())
    assert cached["catalog"]["schema"] == "ursa-catalog/1" and cached["fetched"]

    # a new target object reads the cache without touching the cluster
    tgt2 = _cat_target(tmp_path, monkeypatch)
    tgt2.load_catalog(tmp_path)
    assert tgt2.calls == [] and tgt2.catalog["cluster"] == "Test cluster"
    tgt2.load_catalog(tmp_path, refresh=True)
    assert tgt2.calls == ["cat /apps/docs/catalog.json"]


def test_catalog_rejects_unknown_format_and_keeps_cache(tmp_path, monkeypatch):
    tgt = _cat_target(tmp_path, monkeypatch)
    tgt.load_catalog(tmp_path)
    bad = _cat_target(tmp_path, monkeypatch, catalog={"hello": 1})
    bad.load_catalog(tmp_path)  # from cache: fine
    with pytest.raises(labm.TargetError):
        bad.load_catalog(tmp_path, refresh=True)
    assert bad.catalog["schema"] == "ursa-catalog/1"  # stale beats nothing


def test_describe_brief_and_full_come_from_the_catalog(tmp_path, monkeypatch):
    tgt = _cat_target(tmp_path, monkeypatch)
    tgt.load_catalog(tmp_path)
    brief = tgt.describe_brief()
    assert "1694 Spack packages" in brief and "CUDA <= 13.0" in brief
    assert "gromacs" in brief and "molecular dynamics: GROMACS (CPU, MPI)" in brief
    full = tgt.describe_full()
    assert "module load openmpi && module load gromacs/2026.1" in full
    assert "Modules after `module load openmpi`: gromacs/2026.1" in full
    assert "/apps/containers/vllm-0.6.4-cuda12.4.sif" in full
    assert "`spot`" in full and "$0.74/node-hour" in full
    assert labm._describe(tgt, full=True) == full
    assert labm._describe(None, full=True) == "No cluster configured."


def test_validate_plan_catches_what_would_fail_on_the_cluster(tmp_path, monkeypatch):
    tgt = _cat_target(tmp_path, monkeypatch)
    tgt.load_catalog(tmp_path)
    good = {
        **PLAN,
        "resources": {
            "partition": "standard",
            "nodes": 2,
            "time_limit": "01:00:00",
            "gpus": 0,
        },
        "install": {"modules": ["openmpi", "gromacs/2026.1"], "conda": [], "pip": []},
    }
    assert labm.validate_plan(tgt, good) == []
    bad = {
        **PLAN,
        "resources": {
            "partition": "standard",
            "nodes": 40,
            "time_limit": "01:00:00",
            "gpus": 1,
        },
        "install": {
            "modules": ["gromacs/2026.1", "lammps/2024", "nosuch"],
            "conda": ["blast-plus", "python=3.12"],
            "pip": [],
            "apptainer": ["/apps/containers/missing.sif"],
        },
    }
    w = " | ".join(labm.validate_plan(tgt, bad))
    assert "has none; use gpul4" in w
    assert "40 nodes requested; 'standard' has at most 32" in w
    assert "'gromacs/2026.1' needs `module load openmpi`" in w
    assert "'lammps/2024' is not installed; available: lammps/20250722.4" in w
    assert "Module 'nosuch' is not installed" in w
    assert "Already installed as modules (faster than installing): blast-plus" in w
    assert "missing.sif" in w
    nopart = {
        **PLAN,
        "resources": {"partition": "gpu", "nodes": 1, "time_limit": "1:00:00"},
    }
    assert "Partition 'gpu' does not exist" in labm.validate_plan(tgt, nopart)[0]
    assert labm.validate_plan(None, PLAN) == []


def test_sbatch_spot_requeue_and_ntasks_per_node(tmp_path, monkeypatch):
    tgt = _cat_target(tmp_path, monkeypatch)
    tgt.load_catalog(tmp_path)
    plan = {
        **PLAN,
        "resources": {
            "partition": "spot",
            "nodes": 2,
            "ntasks_per_node": 16,
            "time_limit": "00:30:00",
        },
    }
    sb = build_sbatch(9, plan, tgt)
    assert "#SBATCH --requeue" in sb and "#SBATCH --ntasks-per-node=16" in sb
    std = build_sbatch(
        9,
        {**PLAN, "resources": {"partition": "standard", "ntasks_per_node": "x; rm"}},
        tgt,
    )
    assert "--requeue" not in std and "x; rm" not in std
    # an invalid rank count is ignored: the shared-partition default request is used
    assert "#SBATCH --ntasks-per-node=1\n#SBATCH --cpus-per-task=2" in std


def test_plans_carry_warnings_and_prompts_use_catalog(tmp_path, monkeypatch):
    tgt = _cat_target(tmp_path, monkeypatch)
    lb = Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"ursa": tgt})
    seen = []

    def ask(prompt, search):
        seen.append(prompt)
        return "```json\n" + json.dumps(
            {**PLAN, "install": {"modules": ["lammps"], "conda": [], "pip": []}}
        ) + "\n```", 0.01

    lb._ask = ask  # type: ignore[method-assign]
    run = lb.create(1, "document", "text")
    lb.make_plan(run["id"], "t")
    got = lb.get(run["id"])
    assert got["status"] == "draft" and "1 warnings" in got["stage"]
    assert "needs `module load openmpi`" in got["plan"]["warnings"][0]
    assert "Tested recipes" in seen[0] and "module load gromacs/2026.1" in seen[0]
    # editing recomputes (and the client cannot inject its own warnings)
    fixed = {
        **got["plan"],
        "install": {"modules": ["openmpi", "lammps"]},
        "warnings": ["x"],
    }
    assert lb.edit_plan(run["id"], fixed)["plan"]["warnings"] == []
    st = lb.catalog()
    assert st["available"] and st["modules"] == 5 and st["gpu"]["cuda_max"] == "13.0"


def test_api_catalog_endpoints(app, tmp_path, monkeypatch):  # noqa: F811
    api = app["api"]
    tgt = _cat_target(tmp_path, monkeypatch)
    api.lab.targets = {"ursa": tgt}
    api.lab.state_dir = tmp_path
    call = app["call"]

    status, first = call("GET", "/api/lab/catalog")
    assert status == 200 and first["available"]
    assert tgt.calls == ["cat /apps/docs/catalog.json"]
    status, ref = call("POST", "/api/lab/catalog/refresh", {})
    assert status == 200 and ref["recipes"] == 1 and len(tgt.calls) == 2
    status, t = call("GET", "/api/lab/targets")
    assert t["targets"][0]["partitions"]["spot"]["usd_per_hour"] == 0.74


def test_api_catalog_without_catalog_path(app):  # noqa: F811
    api = app["api"]
    api.lab.targets = {"fake": FakeTarget()}
    status, st = app["call"]("GET", "/api/lab/catalog")
    assert status == 200 and st["available"] is False


def test_sbatch_sources_site_header_only_from_catalog(tmp_path, monkeypatch):
    tgt = _cat_target(tmp_path, monkeypatch)
    plain = build_sbatch(3, PLAN, tgt)  # catalog not loaded yet
    assert "job-header.sh" not in plain
    tgt.load_catalog(tmp_path)
    sb = build_sbatch(3, PLAN, tgt)
    assert (
        "[ -r /apps/docs/templates/job-header.sh ] && . /apps/docs/templates/job-header.sh"
        in sb
    )
    assert sb.index("job-header.sh") < sb.index('stage "Installing software"')
    assert "ursa_conda_libs" in sb  # pip-in-conda fix delegates to the site helper
    assert "site job header /apps/docs/templates/job-header.sh" in tgt.describe_full()
    evil = {**CATALOG, "job_header": {"path": "/x; rm -rf ~"}}
    bad = _cat_target(tmp_path / "e", monkeypatch, catalog=evil)
    bad.load_catalog(tmp_path / "e")
    evil_sb = build_sbatch(3, PLAN, bad)
    assert "job-header" not in evil_sb and "/x; rm" not in evil_sb


def test_extract_json_repairs_unescaped_backslashes_in_scripts():
    # run #21: a plan whose script held regex/LaTeX backslashes failed with
    # "Invalid \\escape" and the whole plan was lost
    reply = (
        "```json\n"
        '{"title": "eclipse", "script": "grep -E \'\\d+\' f\\nsed \'s/\\(a\\)/b/\'", '
        '"good": "already \\\\d fine", "quote": "say \\"hi\\"", "deg": "\\u00b0", '
        '"note": "angle \\alpha"}\n```'
    )
    d = extract_json(reply)
    assert d["script"] == "grep -E '\\d+' f\nsed 's/\\(a\\)/b/'"
    assert d["good"] == "already \\d fine"  # a correct escape is not doubled
    assert d["quote"] == 'say "hi"' and d["deg"] == "\u00b0"
    assert d["note"] == "angle \\alpha"


def test_extract_json_allows_raw_control_characters():
    d = extract_json('{"script": "line1\nline2\tx"}'.replace("\\n", "\n"))
    assert d["script"].startswith("line1")
    raw = '{"script": "a\nb\tc"}'.replace("\\n", "\n").replace("\\t", "\t")
    assert extract_json(raw)["script"] == "a\nb\tc"


def test_make_plan_survives_bad_escapes(lab):
    bad = json.dumps(PLAN).replace(
        "print(1)", "import re; print(re.findall(r'\\\\d', 'a1'))"
    )
    bad = bad.replace("\\\\d", "\\d")  # the model's unescaped backslash
    lab.replies.append("```json\n" + bad + "\n```")
    run = lab.create(1, "selection", "eclipse")
    lab.make_plan(run["id"], "t")
    got = lab.get(run["id"])
    assert got["status"] == "draft", got.get("error")
    assert "re.findall(r'\\d'" in got["plan"]["script"]


def _py_target():
    tgt = labm.SlurmSSHTarget(
        {"name": "u", "ssh_host": "h", "partitions": {"standard": {}}}
    )
    tgt.catalog = {
        "modules": {
            "core": [
                "python-sci/2026.09",
                "python-ml/2026.09",
                "busco/6.1.0",
                "octave/11.1.0",
            ],
            "mpi_dependent": {},
        },
        "module_health": {
            "broken": {"snakemake/9.14.0": "BROKEN snakemake: No module named x"}
        },
    }
    tgt.catalog["modules"]["core"].append("snakemake/9.14.0")
    return tgt


def test_validate_flags_broken_module_by_name_or_version():
    tgt = _py_target()
    for m in ("snakemake", "snakemake/9.14.0"):
        w = labm.validate_plan(tgt, {"install": {"modules": [m]}})
        assert any("broken on the cluster" in x for x in w), w
    assert labm.validate_plan(tgt, {"install": {"modules": ["octave"]}}) == []


def test_validate_python_stack_rules():
    tgt = _py_target()
    ok = labm.validate_plan(tgt, {"install": {"modules": ["python-sci"]}})
    assert ok == []
    two = labm.validate_plan(tgt, {"install": {"modules": ["python-sci", "python-ml"]}})
    assert any("only one Python environment" in x for x in two)
    stray = labm.validate_plan(
        tgt, {"install": {"modules": ["python/3.12.14", "py-numpy"]}}
    )
    assert any("not a working stack" in x for x in stray)
    mixed = labm.validate_plan(
        tgt, {"install": {"modules": ["python-sci"], "conda": ["skyfield"]}}
    )
    assert any("both provide Python" in x for x in mixed)


def test_describe_full_lists_broken_modules():
    tgt = _py_target()
    assert "Broken modules" in tgt.describe_full()
    assert "snakemake/9.14.0" in tgt.describe_full()


# ---- Fix with AI -------------------------------------------------------------


def _fix_lab(tmp_path, replies):
    tgt = labm.SlurmSSHTarget(
        {"name": "u", "ssh_host": "h", "partitions": {"standard": {}}}
    )
    tgt.catalog = {
        "generated": "2026-09-27T18:00:00+00:00",
        "modules": {"core": ["python-sci/2026.09", "ffmpeg/8.1"], "mpi_dependent": {}},
    }
    lab = Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"u": tgt})
    lab._fresh_catalog = lambda t: None  # type: ignore[method-assign]
    lab.ensure_watcher = lambda: None  # type: ignore[method-assign]
    calls = []

    def ask(prompt, search):
        calls.append(prompt)
        return replies[len(calls) - 1], 0.01

    lab._ask = ask  # type: ignore[method-assign]
    return lab, tgt, calls


def _draft(lab, plan):
    run = lab.create(1, "selection", "text", "", "u", plan=plan)
    return run["id"]


BAD = {
    "title": "t",
    "script": "uv venv v\nuv pip install skyfield\npython3 x.py",
    "resources": {
        "partition": "standard",
        "nodes": 1,
        "cores": 4,
        "time_limit": "00:10:00",
    },
    "install": {
        "modules": ["python/3.12.14", "ffmpeg/8.1"],
        "pip": ["skyfield", "numpy"],
    },
}


def test_fix_plan_repairs_and_rechecks(tmp_path):
    good = dict(
        BAD,
        install={"modules": ["python-sci", "ffmpeg/8.1"], "pip": []},
        script="python3 x.py",
    )
    reply = (
        "```json\n"
        + json.dumps(
            {"plan": good, "changes": ["python/3.12.14 -> python-sci"], "notes": ""}
        )
        + "\n```"
    )
    lab, tgt, calls = _fix_lab(tmp_path, [reply])
    rid = _draft(lab, BAD)
    assert lab.get(rid)["plan"]["warnings"]
    out = lab.fix_plan(rid)
    assert len(calls) == 1 and "not a working stack" in calls[0]
    assert out["fix"]["remaining"] == [] and out["fix"]["changes"] == [
        "python/3.12.14 -> python-sci"
    ]
    assert out["plan"]["install"]["modules"] == ["python-sci", "ffmpeg/8.1"]
    assert out["plan"]["plan_before_fix"]["install"]["modules"][0] == "python/3.12.14"
    assert out["status"] == "draft"  # never submits
    diff = out["plan"]["fix_diff"]
    assert {f["key"] for f in diff["fields"]} == {"install"}
    assert "python/3.12.14" in diff["fields"][0]["before"]
    back = lab.undo_fix(rid)
    assert back["plan"]["install"]["modules"][0] == "python/3.12.14"


def test_fix_plan_stops_after_two_rounds(tmp_path):
    still_bad = (
        "```json\n"
        + json.dumps({"plan": BAD, "changes": [], "notes": "cannot"})
        + "\n```"
    )
    lab, tgt, calls = _fix_lab(tmp_path, [still_bad, still_bad, still_bad])
    rid = _draft(lab, BAD)
    out = lab.fix_plan(rid)
    assert len(calls) == 2 and out["fix"]["rounds"] == 2 and out["fix"]["remaining"]


def test_fix_plan_noop_when_clean(tmp_path):
    good = dict(
        BAD, install={"modules": ["python-sci"], "pip": []}, script="python3 x.py"
    )
    lab, tgt, calls = _fix_lab(tmp_path, [])
    rid = _draft(lab, good)
    out = lab.fix_plan(rid)
    assert calls == [] and out["fix"]["rounds"] == 0


def test_validate_flags_redundant_pip_and_stale_catalog():
    tgt = labm.SlurmSSHTarget(
        {"name": "u", "ssh_host": "h", "partitions": {"standard": {}}}
    )
    tgt.catalog = {
        "generated": "2026-09-27T18:00:00+00:00",
        "modules": {"core": ["python-sci/2026.09"], "mpi_dependent": {}},
    }
    w = labm.validate_plan(
        tgt,
        {
            "install": {
                "modules": ["python-sci"],
                "pip": ["skyfield>=1.49", "rebound"],
            },
            "catalog_generated": "2026-09-27T14:00:00+00:00",
        },
    )
    assert any("already provides skyfield" in x and "rebound" not in x for x in w), w
    assert any("Planned against the cluster catalog" in x for x in w), w


# ---- script checks -----------------------------------------------------------


def test_check_script_flags_unk_token_and_python_syntax():
    s = (
        "set -euo pipefail\n"
        "python3 - << 'EOF'\n"
        "r = {'strain': 'A'}\n"
        "print(f\">\" + '<unk>' + \"r['strain']}\")\n"
        "EOF\n"
    )
    s = s.replace("\" + '<unk>' + \"", "<unk>")
    w = labm.check_script(s)
    assert any("garbled model token" in x and "line 4" in x for x in w), w
    assert any("Python syntax error" in x and "line 4" in x for x in w), w


def test_check_script_clean_and_cat_py_heredoc():
    ok = "set -e\ncat > a.py << 'PY'\nimport sys\nprint(sys.argv)\nPY\npython3 a.py\n"
    assert labm.check_script(ok) == []
    bad = "cat > a.py <<'PY'\ndef f(:\n  pass\nPY\n"
    assert any("Python syntax error" in x for x in labm.check_script(bad))


def test_check_script_bash_syntax_and_unquoted_heredoc_skipped():
    assert any(
        "Shell syntax error" in x for x in labm.check_script("if true; then\necho x\n")
    )
    # unquoted heredoc with $ is expanded by the shell first: not compiled
    assert labm.check_script('python3 - <<EOF\nprint("$HOME")\nEOF\n') == []


def test_validate_plan_includes_script_checks():
    tgt = labm.SlurmSSHTarget(
        {"name": "u", "ssh_host": "h", "partitions": {"standard": {}}}
    )
    w = labm.validate_plan(
        tgt, {"script": "python3 - << 'EOF'\nx = (\nEOF\n", "install": {}}
    )
    assert any("Python syntax error" in x for x in w), w


def test_extract_json_keeps_first_object_when_text_follows():
    assert extract_json('```json\n{"a": 1}\n{"b": 2}\n```') == {"a": 1}
    assert extract_json('{"plan": {"x": "a\\d"}}\n\nNotes: done') == {
        "plan": {"x": "a\\d"}
    }


def _failed_lab(tmp_path, reply, log):
    tgt = labm.SlurmSSHTarget(
        {"name": "u", "ssh_host": "h", "partitions": {"standard": {}}}
    )
    tgt.catalog = {
        "generated": "g",
        "modules": {"core": ["python-sci/2026.09"], "mpi_dependent": {}},
    }
    lab = Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"u": tgt})
    lab._fresh_catalog = lambda t: None  # type: ignore[method-assign]
    lab.ensure_watcher = lambda: None  # type: ignore[method-assign]
    prompts = []

    def ask(prompt, search):
        prompts.append(prompt)
        return reply, 0.02

    lab._ask = ask  # type: ignore[method-assign]
    plan = {
        "title": "t",
        "question": "q",
        "script": "python3 x.py --bad-flag",
        "resources": {"partition": "standard"},
        "install": {"modules": ["python-sci"]},
    }
    rid = lab.create(1, "selection", "x", "", "u", plan=plan)["id"]
    lab._update(
        rid, status="failed", job_id="9", exit_code="1:0", stage="Ended: FAILED"
    )
    d = lab.results_dir / f"run_{rid}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "job.log").write_text(log)
    return lab, rid, plan, prompts


def test_fix_failed_makes_new_draft_and_leaves_failed_run(tmp_path):
    good = {
        "title": "t",
        "question": "q",
        "script": "python3 x.py",
        "resources": {"partition": "standard"},
        "install": {"modules": ["python-sci"]},
    }
    reply = (
        "```json\n"
        + json.dumps(
            {
                "plan": good,
                "changes": ["drop --bad-flag (unrecognized argument)"],
                "notes": "",
            }
        )
        + "\n```"
    )
    log = (
        "setup\n" * 3000
        + "x.py: error: unrecognized arguments: --bad-flag\n[STAGE] Failed (exit 2)\n"
    )
    lab, rid, plan, prompts = _failed_lab(tmp_path, reply, log)
    out = lab.fix_failed(rid)
    assert (
        "unrecognized arguments: --bad-flag" in prompts[0]
    )  # the log tail reached the model
    assert out["id"] != rid and out["status"] == "draft" and out["rerun_of"] == rid
    assert out["plan"]["script"] == "python3 x.py"
    assert out["fix"]["changes"] == ["drop --bad-flag (unrecognized argument)"]
    d = out["plan"]["fix_diff"]["script"]
    assert "+python3 x.py" in d and any(
        ln.startswith("-") and not ln.startswith("---") for ln in d.splitlines()
    )
    assert (
        lab.get(rid)["status"] == "failed"
        and lab.get(rid)["plan"]["script"] == plan["script"]
    )


def test_fix_failed_only_for_failed_runs(tmp_path):
    lab, rid, plan, prompts = _failed_lab(tmp_path, "{}", "log")
    lab._update(rid, status="completed")
    with pytest.raises(ValueError):
        lab.fix_failed(rid)
    assert prompts == []


def test_log_for_fix_keeps_early_traceback():
    log = "Traceback (most recent call last):\n  File x\nKeyError: 'a'\n" + (
        "noise line\n" * 5000
    )
    out = labm._log_for_fix(log)
    assert "KeyError: 'a'" in out and len(out) < 12000


def test_fix_failed_flags_dropped_option(tmp_path):
    good = {
        "title": "t",
        "question": "q",
        "script": "python3 x.py",
        "resources": {"partition": "standard"},
        "install": {"modules": ["python-sci"]},
    }
    reply = (
        "```json\n"
        + json.dumps({"plan": good, "changes": ["remove --bad-flag"], "notes": ""})
        + "\n```"
    )
    lab, rid, plan, prompts = _failed_lab(tmp_path, reply, "error: --bad-flag\n")
    out = lab.fix_failed(rid)
    assert out["fix"]["notes"].startswith(
        "REVIEW: this fix removes option(s) --bad-flag"
    )


def test_fix_failed_retries_once_when_changes_missing(tmp_path):
    good = {
        "title": "t",
        "question": "q",
        "script": "python3 x.py --bad-flag --ok",
        "resources": {"partition": "standard"},
        "install": {"modules": ["python-sci"]},
    }
    silent = (
        "```json\n" + json.dumps({"plan": good, "changes": [], "notes": ""}) + "\n```"
    )
    told = (
        "```json\n"
        + json.dumps({"plan": good, "changes": ["add --ok"], "notes": ""})
        + "\n```"
    )
    lab, rid, plan, prompts = _failed_lab(tmp_path, silent, "log")
    replies = [silent, told]
    lab._ask = lambda p, search: (replies.pop(0), 0.01)  # type: ignore[method-assign]
    out = lab.fix_failed(rid)
    assert out["fix"]["changes"] == ["add --ok"] and replies == []


def test_pip_on_python_module_uses_module_python_venv(lab):
    """Issue #113: pip packages on top of python-sci must not get a separate Python.

    Run #30 built a Pixi env with its own python=3.12 for ortools/tflite, which hid
    python-sci's matplotlib and failed with ModuleNotFoundError.
    """
    plan = dict(PLAN)
    plan["install"] = {
        "modules": ["python-sci/2026.09"],
        "pip": ["flatbuffers", "tflite"],
    }
    s = build_sbatch(1, plan, lab.fake)
    assert "module load python-sci/2026.09" in s
    # first rung: a venv layered on the module's Python (install ladder, v0.31)
    lay = s.index("ladder_try layered-venv")
    assert "python3 -m venv --system-site-packages" in s[lay:]
    assert "-m pip install --progress-bar off flatbuffers tflite" in s[lay:]
    # a separate Pixi Python is only the last fallback, after the isolated venv
    assert (
        lay
        < s.index("ladder_try isolated-venv")
        < s.index("ladder_try pixi-conda-forge")
    )
    assert (
        "LADDER_IMPORTS=' flatbuffers tflite'" in s
        or "LADDER_IMPORTS='flatbuffers tflite'" in s
    )
    assert "flock 9" in s and "flock -u 9" in s
    # the cache key includes the module, so python-ml gets its own venv
    ml = dict(plan, install={"modules": ["python-ml/2026.09"], "pip": ["tflite"]})
    sci = dict(plan, install={"modules": ["python-sci/2026.09"], "pip": ["tflite"]})
    k = __import__("re").compile(r"LADDER_KEY=(\S+)")
    assert k.search(build_sbatch(1, ml, lab.fake)).group(1) != k.search(
        build_sbatch(1, sci, lab.fake)
    ).group(1)


def test_pip_with_conda_or_no_python_module_still_uses_pixi(lab):
    plain = dict(PLAN, install={"pip": ["qiskit"]})
    assert "pixi add python=3.12 pip" in build_sbatch(1, plain, lab.fake)
    mixed = dict(
        PLAN, install={"modules": ["python-sci"], "conda": ["rdkit"], "pip": ["x"]}
    )
    assert "pixi add rdkit" in build_sbatch(1, mixed, lab.fake)


def test_validate_flags_script_venv_that_hides_packages():
    tgt = _py_target()
    plan = {
        "install": {"modules": ["python-sci"], "pip": ["tflite"]},
        "script": 'uv venv --system-site-packages "$TMPDIR/v"\n. "$TMPDIR/v/bin/activate"\n',
    }
    w = labm.validate_plan(tgt, plan)
    assert any("builds its own Python venv" in x for x in w), w
    plan["script"] = "python3 run.py\n"
    assert not any("own Python venv" in x for x in labm.validate_plan(tgt, plan))


def test_submit_moves_an_old_run_folder_aside_instead_of_overwriting(
    tmp_path, monkeypatch
):
    """A run id can repeat (fresh DB, restore); the old folder's logs must survive."""
    from deepresearch.dashboard.lab import SlurmSSHTarget

    tgt = SlurmSSHTarget(
        {
            "name": "t",
            "type": "slurm-ssh",
            "ssh_host": "x",
            "remote_root": str(tmp_path / "root"),
            "partitions": {"standard": {"cpus": 1}},
        }
    )
    old = tmp_path / "root" / "run_1"
    (old / "outputs").mkdir(parents=True)
    (old / "job.log").write_text("old log")
    (old / "outputs" / "result.txt").write_text("old result")

    def sh(cmd, stdin=None, timeout=120):
        cmd = cmd.replace("sbatch --parsable run.sbatch", "echo 99")
        r = subprocess.run(
            ["bash", "-c", cmd], input=stdin, capture_output=True, check=True
        )
        return r.stdout.decode()

    monkeypatch.setattr(tgt, "sh", sh)
    assert tgt.submit(1, {"run.sbatch": "#!/bin/bash\n", "plan.json": "{}"}) == "99"
    prev = [
        p for p in (tmp_path / "root").iterdir() if p.name.startswith("run_1.prev-")
    ]
    assert len(prev) == 1
    assert (prev[0] / "outputs" / "result.txt").read_text() == "old result"
    assert (tmp_path / "root" / "run_1" / "run.sbatch").exists()
    assert not (tmp_path / "root" / "run_1" / "job.log").exists()


def test_all_runs_lists_every_run_with_report_title(app, tmp_path):  # noqa: F811
    api = app["api"]
    api.lab.targets = {"fake": FakeTarget()}
    a = _seed(api, "Aspirin question", "Aspirin MW is 180.")
    b = _seed(api, "Caffeine question", "Caffeine MW is 194.")
    api.lab.create(a, "document", "x", plan=dict(PLAN))
    api.lab.create(b, "document", "y", plan=dict(PLAN))
    status, data = app["call"]("GET", "/api/lab/runs")
    assert status == 200
    runs = data["runs"]
    assert [r["session_id"] for r in runs] == [b, a]  # newest first
    assert runs[0]["session_title"].startswith("Caffeine") and "script" not in runs[0]
    assert "selection" not in runs[0] and "result_md" not in runs[0]
    assert runs[0]["plan"] == {
        "title": PLAN["title"],
        "resources": {"partition": "standard"},
    }
    assert runs[0]["target_label"] == "Fake cluster"


def test_all_runs_without_sessions_table(tmp_path):
    lab = Lab(
        str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"fake": FakeTarget()}
    )
    lab.create(1, "document", "t", plan=dict(PLAN))
    assert lab.all_runs()[0]["session_prompt"] is None


def test_plan_diff_script_and_fields():
    from deepresearch.dashboard.lab import plan_diff

    a = {"script": "a\nb\nc", "resources": {"partition": "spot"}, "warnings": ["w"]}
    b = {"script": "a\nB\nc", "resources": {"partition": "standard"}, "warnings": []}
    d = plan_diff(a, b)
    assert "-b" in d["script"].splitlines() and "+B" in d["script"].splitlines()
    assert [f["key"] for f in d["fields"]] == ["resources"]  # warnings ignored
    assert plan_diff(a, a) == {"script": "", "fields": [], "truncated": False}
    big = plan_diff({"script": "x\n" * 900}, {"script": "y\n" * 900}, max_lines=50)
    assert big["truncated"] and len(big["script"].splitlines()) == 50


def test_ortools_on_python_module_gets_an_isolated_venv(lab):
    """Run #38 (2026-09-29): CP-SAT Solve() segfaults (exit 139) when OR-Tools sits on
    top of python-sci's site-packages (clashing abseil/protobuf); it solves in a plain
    venv. So OR-Tools plans get a venv without --system-site-packages, plus the usual
    scientific stack the module would have provided."""
    plan = dict(PLAN)
    plan["install"] = {
        "modules": ["python-sci/2026.09"],
        "pip": ["ortools", "flatbuffers", "tflite"],
    }
    s = build_sbatch(1, plan, lab.fake)
    assert "module load python-sci/2026.09" in s
    # the isolated rung comes first for OR-Tools; the layered one is only a fallback
    iso = s.index("ladder_try isolated-venv")
    assert iso < s.index("ladder_try layered-venv")
    assert (
        "-m pip install --progress-bar off numpy pandas matplotlib scipy ortools "
        "flatbuffers tflite" in s[iso:]
    )
    # a pinned spec is recognised too, and the cache key differs from a layered env
    pinned = dict(
        plan, install={"modules": ["python-sci/2026.09"], "pip": ["ortools==9.15.6755"]}
    )
    sp = build_sbatch(1, pinned, lab.fake)
    assert sp.index("ladder_try isolated-venv") < sp.index("ladder_try layered-venv")
    k = __import__("re").compile(r"LADDER_KEY=(\S+)")
    layered = dict(plan, install={"modules": ["python-sci/2026.09"], "pip": ["tflite"]})
    assert k.search(s).group(1) != k.search(build_sbatch(1, layered, lab.fake)).group(1)

"""Lab runs: plan -> review -> submit -> watch -> fetch -> write-up, with a fake cluster.

No network: Gemini is replaced by canned replies and the Slurm target by an in-memory fake.
"""

import io
import json
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
    assert "envs/torch-2-5-1" in s  # cache key ignores the index URL


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

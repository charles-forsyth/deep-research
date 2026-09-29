"""Lab state-machine races and stuck states found in the 2026-09-28 review.

Each test failed on v0.27.0 and passes with the fix.
"""

import json
import threading
import time

import pytest

from deepresearch.dashboard import lab as labmod
from deepresearch.dashboard import server as srv
from deepresearch.dashboard.lab import Lab
from deepresearch.dashboard.server import Api, ApiError

PLAN = {
    "computable": True,
    "title": "t",
    "question": "q",
    "resources": {
        "partition": "standard",
        "nodes": 1,
        "time_limit": "00:30:00",
        "gpus": 0,
    },
    "install": {"modules": [], "conda": [], "pip": []},
    "script": "echo hi > outputs/r.txt",
}


class SlowTarget:
    kind = "fake"
    name = "fake"
    label = "Fake"
    default_partition = "standard"
    partitions = {"standard": {"usd_per_hour": 1.0}}
    remote_root = "~/drl"

    def __init__(self, delay=0.3):
        self.delay = delay
        self.submits: list[int] = []
        self.cancelled: list[str] = []
        self.state = ""
        self.stage = ""

    def describe(self):
        return "Fake"

    def submit(self, run_id, files):
        time.sleep(self.delay)  # tar + ssh + sbatch take seconds for real
        self.submits.append(run_id)
        return str(1000 + len(self.submits))

    def cancel(self, job_id):
        self.cancelled.append(job_id)

    def status(self, run_id, job_id):
        return {
            "slurm_state": self.state,
            "reason": "",
            "elapsed": "",
            "node": "",
            "exit_code": "",
            "started": "",
            "stage": self.stage,
            "log_size": 0,
        }

    def fetch(self, run_id, dest):
        return []


@pytest.fixture
def lab(tmp_path):
    t = SlowTarget()
    lb = Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"fake": t})
    lb.ensure_watcher = lambda: None
    lb._analyze = lambda run, dest, final: ("note", 0.0)
    lb.tgt = t  # type: ignore[attr-defined]
    return lb


@pytest.fixture
def api(tmp_path, monkeypatch, lab):
    monkeypatch.setattr(srv, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(srv, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    return Api(lab.db_path, spawn=lambda args, p: 1, lab=lab)


def call(api, method, path, body=None):
    try:
        return api.dispatch(method, path, {}, body)
    except ApiError as e:
        return e.status, e.message


def test_double_submit_sends_only_one_slurm_job(lab):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    errs: list[Exception] = []

    def go():
        try:
            lab.submit(run["id"])
        except Exception as e:
            errs.append(e)

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert lab.tgt.submits == [run["id"]]
    assert len(errs) == 1 and "already submitted" in str(errs[0])
    r = lab.get(run["id"])
    assert r["status"] == "queued" and r["job_id"] == "1001"


def test_run_stuck_in_submitting_can_be_cancelled_then_deleted(api, lab):
    sid = api.sessions.create_session("iid", "p")
    run = lab.create(sid, "document", "x", plan=dict(PLAN))
    lab._update(run["id"], status="submitting", stage="Uploading data source obs")
    code, body = call(api, "POST", f"/api/lab/{run['id']}/cancel")
    assert code == 200 and body["status"] == "cancelled"
    assert call(api, "DELETE", f"/api/lab/{run['id']}")[0] == 200
    assert not lab.active()


def test_watcher_fails_a_submit_left_over_from_a_dead_dashboard(lab):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    lab._update(run["id"], status="submitting")  # no submit in flight here
    lab.poll(lab.get(run["id"]))
    r = lab.get(run["id"])
    assert r["status"] == "failed" and "stopped while submitting" in r["error"]
    assert not lab.active()


def test_watcher_leaves_an_in_flight_submit_alone(lab):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    t = threading.Thread(target=lab.submit, args=(run["id"],))
    t.start()
    time.sleep(0.1)
    lab.poll(lab.get(run["id"]))  # the watcher runs while sbatch is slow
    assert lab.get(run["id"])["status"] == "submitting"
    t.join()
    assert lab.get(run["id"])["status"] == "queued"


def test_cancel_during_submit_scancels_the_job_it_gets_back(lab):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    t = threading.Thread(target=lab.submit, args=(run["id"],))
    t.start()
    time.sleep(0.1)
    assert lab.cancel(run["id"])["status"] == "cancelled"
    t.join()
    r = lab.get(run["id"])
    assert r["status"] == "cancelled" and r["job_id"] == "1001"
    assert lab.tgt.cancelled == ["1001"]


def test_edit_plan_racing_submit_does_not_reset_a_queued_job(lab):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    orig = lab._check
    lab.tgt.delay = 0

    def slow_check(tgt, plan):
        time.sleep(0.3)
        return orig(tgt, plan)

    errs: list[Exception] = []

    def edit():
        try:
            lab.edit_plan(run["id"], dict(PLAN))
        except Exception as e:
            errs.append(e)

    lab._check = slow_check
    t = threading.Thread(target=edit)
    t.start()
    time.sleep(0.05)
    lab._check = orig
    lab.submit(run["id"])
    t.join()
    r = lab.get(run["id"])
    assert r["status"] == "queued" and r["job_id"] == "1001"
    assert errs and "only drafts can be edited" in str(errs[0])


def test_fix_plan_does_not_overwrite_a_run_submitted_meanwhile(lab):
    bad = {**PLAN, "resources": {**PLAN["resources"], "partition": "nope"}}
    run = lab.create(1, "document", "x", plan=bad)
    fixed = {**PLAN, "script": "echo FIXED-BY-AI > outputs/r.txt"}
    gate = threading.Event()
    lab.tgt.delay = 0

    def ask(prompt, search):
        gate.wait(2)
        return "```json\n" + json.dumps(
            {"plan": fixed, "changes": ["x"]}
        ) + "\n```", 0.0

    lab._ask = ask
    errs: list[Exception] = []

    def fix():
        try:
            lab.fix_plan(run["id"])
        except Exception as e:
            errs.append(e)

    t = threading.Thread(target=fix)
    t.start()
    time.sleep(0.1)
    lab.submit(run["id"])
    submitted = lab.get(run["id"])["script"]
    gate.set()
    t.join()
    after = lab.get(run["id"])
    assert after["status"] == "queued"
    assert after["script"] == submitted and "FIXED-BY-AI" not in after["script"]
    assert errs and "not applied" in str(errs[0])


def test_poll_finishes_a_running_job_slurm_forgot_using_the_done_marker(lab):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    lab._update(run["id"], status="running", job_id="55", slurm_state="RUNNING")
    lab.tgt.stage = "Done"
    lab.poll(lab.get(run["id"]))
    r = lab.get(run["id"])
    assert r["status"] == "completed"


def test_poll_gives_up_on_a_forgotten_job_after_a_few_empty_polls(lab, monkeypatch):
    monkeypatch.setattr(labmod, "GONE_POLLS", 3)
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    lab._update(run["id"], status="running", job_id="55", slurm_state="RUNNING")
    lab.tgt.stage = "Running"
    for _ in range(2):
        lab.poll(lab.get(run["id"]))
    assert lab.get(run["id"])["status"] == "running"
    lab.poll(lab.get(run["id"]))
    assert lab.get(run["id"])["status"] == "failed"  # fetched; no Done marker


def test_a_queued_job_with_an_empty_state_stays_queued(lab):
    """Only 'running' falls back; a queued job may simply not be in sacct yet."""
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    lab._update(run["id"], status="queued", job_id="55")
    for _ in range(10):
        lab.poll(lab.get(run["id"]))
    assert lab.get(run["id"])["status"] == "queued"

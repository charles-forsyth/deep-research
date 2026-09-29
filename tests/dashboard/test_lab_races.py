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


# ---- a submit that never reached the cluster stays a draft (2026-09-29) ----------


def test_gcloud_reauth_error_names_the_fix():
    from deepresearch.dashboard.lab import GCLOUD_LOGIN_HINT, _gcloud_error

    stderr = (
        "ERROR: (gcloud.compute.ssh) There was a problem refreshing your current auth "
        "tokens: Reauthentication failed. cannot prompt during non-interactive "
        "execution.\nPlease run:\n\n  $ gcloud auth login\n\nto obtain new credentials."
        "\n\nIf you have already logged in with a different account, run:\n\n  $ gcloud "
        "config set account ACCOUNT\n\nto select an already authenticated account to use."
    )
    assert _gcloud_error(stderr) == GCLOUD_LOGIN_HINT
    other = "ERROR: (gcloud.compute.ssh) Could not fetch resource: instance not found\n"
    assert "instance not found" in _gcloud_error(other)


def test_submit_that_never_reached_the_cluster_goes_back_to_draft(lab, api):
    from deepresearch.dashboard.lab import NotSubmitted

    def no_cluster(run_id, files):
        raise NotSubmitted("Google sign-in has expired")

    real = lab.tgt.submit
    lab.tgt.submit = no_cluster
    rid = lab.create(1, "document", "x", plan=dict(PLAN))["id"]
    with pytest.raises(NotSubmitted):
        lab.submit(rid)
    run = lab.get(rid)
    assert run["status"] == "draft"
    assert run["stage"] == "Not submitted"
    assert "sign-in" in run["error"]
    assert not run["job_id"] and not run["submitted_at"]
    # the API says why (502), and the run is still editable
    lab.tgt.submit = no_cluster
    st, msg = call(api, "POST", f"/api/lab/{rid}/submit")
    assert st == 502 and "sign-in" in msg
    assert lab.get(rid)["status"] == "draft"
    # once the login is fixed, Submit works on the same run
    lab.tgt.submit = real
    assert lab.submit(rid)["job_id"]


def test_a_real_submit_error_still_fails_the_run(lab):
    from deepresearch.dashboard.lab import TargetError

    def sbatch_refused(run_id, files):
        raise TargetError("sbatch returned 'invalid partition'")

    lab.tgt.submit = sbatch_refused
    rid = lab.create(1, "document", "x", plan=dict(PLAN))["id"]
    with pytest.raises(TargetError):
        lab.submit(rid)
    assert lab.get(rid)["status"] == "failed"


def test_ssh_failure_before_the_command_runs_is_not_submitted(monkeypatch):
    from deepresearch.dashboard import lab as L

    t = L.SlurmSSHTarget.__new__(L.SlurmSSHTarget)
    t._lock = threading.Lock()
    t._argv = ["ssh"]
    t._dest = "host"

    class R:
        returncode = 255
        stderr = b"ssh: connect to host: Connection timed out"
        stdout = b""

    monkeypatch.setattr(L.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(L.NotSubmitted):
        t.run("true")

    def expired(self):
        raise L.TargetError(L.GCLOUD_LOGIN_HINT)

    monkeypatch.setattr(L.SlurmSSHTarget, "_base", expired)
    with pytest.raises(L.NotSubmitted):
        t.run("true")

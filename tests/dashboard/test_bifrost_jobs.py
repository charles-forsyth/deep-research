"""R2 (v0.55.0): the Lab's Slurm jobs through bifrost.

A small in-memory cluster stands behind the same fake bifrost HTTP server the R1 tests
use, so the real client code runs end to end: job_submit -> guards -> confirm, the
batched watcher, line-paged logs, fetch through read and signed links, cancel, and data
sources relayed through staging uploads."""

import hashlib
import json

import pytest

from deepresearch.dashboard import bifrost as bf
from tests.dashboard.test_bifrost import FakeTarget, server  # noqa: F401  (fixture)


def sha16(script: str) -> str:
    return hashlib.sha256(script.encode() + b"\0").hexdigest()[:16]


class Cluster:
    """Jobs, folders, logs and staging as bifrost would report them."""

    def __init__(self, fake):
        self.fake = fake
        self.next_id = 500
        self.jobs: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.uploads: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.worst = 0.42
        self.tamper = False
        self.refuse = ""
        a = fake.answers
        a["job_submit"] = self.job_submit
        a["job_submit_confirm"] = self.confirm
        a["jobs_list"] = self.jobs_list
        a["job_results"] = self.results
        a["results_link"] = self.link
        a["job_log_tail"] = self.log
        a["job_cancel"] = self.cancel
        a["job_cancel_confirm"] = self.cancel_confirm
        a["uploads_list"] = lambda _a: {"uploads": list(self.uploads.values())}
        a["upload_prepare"] = self.upload_prepare
        a["job_show"] = {"efficiency": {"cpu_percent": 80.0}}

    def job_submit(self, a):
        if self.refuse:
            raise_tool(self.fake, "job_submit", self.refuse)
        tok = f"tok{len(self.pending)}"
        script = a["script"]
        self.pending[tok] = {
            "script": script,
            "name": a.get("job_name"),
            "inputs": a.get("inputs") or [],
        }
        return {
            "confirm_token": tok,
            "plan_hash": "abc123",
            "partition": "computehigh",
            "script_sha256": "0" * 16 if self.tamper else sha16(script),
            "worst_case_usd": self.worst,
            "remote_dir": "~/bifrost-jobs/x",
            "warnings": [],
        }

    def confirm(self, a):
        p = self.pending.pop(a["confirm_token"])
        jid = str(self.next_id)
        self.next_id += 1
        self.jobs[jid] = {
            "state": "PENDING",
            "script": p["script"],
            "inputs": p["inputs"],
            "files": {},
            "log": [],
            "restarts": 0,
        }
        return {"job_id": jid, "remote_dir": f"~/bifrost-jobs/{jid}", "message": "ok"}

    def jobs_list(self, a):
        return [
            {
                "job_id": j,
                "state": v["state"],
                "nodes": "c3-0",
                "elapsed_s": 75,
                "exit_code": "0",
                "restarts": v["restarts"],
            }
            for j, v in self.jobs.items()
            if j in (a.get("job_ids") or [])
        ]

    def results(self, a):
        j = self.jobs[a["job_id"]]
        if "read" in a:
            data = j["files"].get(a["read"])
            if data is None:
                raise_tool(self.fake, "job_results", "no such file")
            if b"\0" in data:
                raise_tool(
                    self.fake, "job_results", f"{a['read']} is binary; use results_link"
                )
            off = int(a.get("read_offset") or 0)
            n = int(a.get("read_bytes") or 16384)
            chunk = data[off : off + n]
            return {
                "chunk": {"file_bytes": len(data), "offset": off, "bytes": len(chunk)},
                "preview_untrusted": {"text": chunk.decode()},
            }
        files = [{"path": k, "bytes": len(v)} for k, v in sorted(j["files"].items())]
        return {"files": files, "total_files": len(files)}

    def link(self, a):
        j = self.jobs[a["job_id"]]
        out = []
        for f in a["files"]:
            url = f"https://storage.example/{a['job_id']}/{f}"
            self.blobs[url] = j["files"][f]
            out.append({"file": f, "bytes": len(j["files"][f]), "download_url": url})
        return {"links": out}

    def log(self, a):
        lines = self.jobs[a["job_id"]]["log"]
        total = len(lines)
        n = int(a.get("lines") or 100)
        s = int(a.get("start_line") or 0)
        if s <= 0:
            s = max(1, total - n + 1)
        win = lines[s - 1 : s - 1 + n]
        return {
            "total_lines": total,
            "first_line": s if win else 0,
            "last_line": s + len(win) - 1 if win else 0,
            "untrusted": {"text": "".join(x + "\n" for x in win)},
        }

    def cancel(self, a):
        return {"confirm_token": "c-" + a["job_id"], "job_id": a["job_id"]}

    def cancel_confirm(self, a):
        jid = a["confirm_token"][2:]
        self.jobs[jid]["state"] = "CANCELLED"
        return {"action": "cancel", "job_id": jid}

    def upload_prepare(self, a):
        uid = f"u{len(self.uploads):016d}"
        self.uploads[a["filename"]] = {
            "upload_id": uid,
            "filename": a["filename"],
            "bytes": a["bytes"],
        }
        return {
            "upload_id": uid,
            "upload_url": f"https://storage.example/up/{uid}",
            "headers": {"x": "1"},
        }

    def finish(self, jid, state="COMPLETED", files=None, log=None):
        j = self.jobs[jid]
        j["state"] = state
        j["files"].update(files or {})
        j["log"] = log or ["[STAGE] Running", "done"]


def raise_tool(fake, name, msg):
    # the fake server turns an exception in an answer into a tool error
    raise ToolError(msg)


class ToolError(Exception):
    pass


@pytest.fixture
def blab(server, tmp_path, monkeypatch):  # noqa: F811
    from deepresearch.dashboard.lab import Lab

    fake, c, _ = server
    cl = Cluster(fake)
    puts: list[tuple[str, int]] = []
    jobs = bf.BifrostJobs(
        c, max_usd_per_run=10,
        http_get=lambda url, mx: cl.blobs[url],
        http_put=lambda url, data, h: puts.append((url, len(data))),
    )  # fmt: skip
    t = FakeTarget()
    t.warm = None  # no warm worker: every run is its own Slurm job (pilots: R3)
    lb = Lab(
        str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"ursa": t}, bifrost=c
    )
    lb.bifrost_jobs = jobs
    lb.ensure_watcher = lambda: None
    lb._ask = lambda prompt, search: ("Result: done.", 0.0)
    return lb, cl, fake, puts


PLAN = {
    "title": "Pi by sampling", "question": "Is pi 3.14?", "approach": "Monte Carlo",
    "script": "python -c 'print(3.14)' > outputs/pi.txt\n",
    "resources": {"partition": "computehigh", "nodes": 1, "time_limit": "00:20:00",
                  "cpus_per_task": 2},
    "expected_outputs": ["outputs/pi.txt"], "smoke": False,
}  # fmt: skip


def draft(lb, plan=None):
    with lb._conn() as conn:
        cur = conn.execute(
            "INSERT INTO lab_runs (session_id, scope, status, target, plan, estimate_usd)"
            " VALUES (1, 'document', 'draft', 'ursa', ?, 0.30)",
            (json.dumps(plan or PLAN),),
        )
        conn.commit()
        return cur.lastrowid


def calls(fake, name):
    return [a for n, a in fake.calls if n == name]


# ---- submit ---------------------------------------------------------------------


def test_submit_goes_through_bifrost_with_the_built_script(blab):
    lb, cl, fake, _ = blab
    rid = draft(lb)
    run = lb.submit(rid)
    assert run["status"] == "queued" and run["job_id"] == "500"
    sent = calls(fake, "job_submit")[0]
    assert (
        sent["script"] == run["script"]
        and "#SBATCH --cpus-per-task=2" in sent["script"]
    )
    assert sent["job_name"].startswith("lab-") and calls(fake, "job_submit_confirm")
    cj = run["cluster_jobs"]
    assert (
        cj[0]["job_id"] == "500"
        and cj[0]["why"] == "full"
        and cj[0]["worst_usd"] == 0.42
    )


def test_a_plan_for_another_script_is_never_confirmed(blab):
    lb, cl, fake, _ = blab
    cl.tamper = True
    rid = draft(lb)
    with pytest.raises(Exception, match="different script"):
        lb.submit(rid)
    assert not calls(fake, "job_submit_confirm")
    run = lb.get(rid)
    assert run["status"] == "draft" and "different script" in run["error"]


def test_over_budget_is_refused_before_confirm(blab):
    lb, cl, fake, _ = blab
    cl.worst = 1.20  # over 3 x the reviewed $0.30 estimate
    rid = draft(lb)
    with pytest.raises(Exception, match="per-run limit"):
        lb.submit(rid)
    assert not calls(fake, "job_submit_confirm") and lb.get(rid)["status"] == "draft"


def test_submit_count_per_run_is_capped(server):  # noqa: F811
    fake, c, _ = server
    Cluster(fake)
    jobs = bf.BifrostJobs(c)
    with pytest.raises(bf.SubmitRefused, match="already submitted 6"):
        jobs.submit("#!/bin/bash\n", "x", submits_so_far=bf.MAX_SUBMITS_PER_RUN)
    assert not calls(fake, "job_submit")


def test_signed_out_keeps_the_draft(blab, tmp_path):
    lb, cl, fake, _ = blab
    (tmp_path / bf.TOKEN_FILE).unlink()
    rid = draft(lb)
    with pytest.raises(Exception, match="cluster login"):
        lb.submit(rid)
    assert lb.get(rid)["status"] == "draft"


# ---- watch / fetch -----------------------------------------------------------------


def test_watcher_batches_and_fetches_through_bifrost(blab):
    lb, cl, fake, _ = blab
    a, b = draft(lb), draft(lb)
    lb.submit(a)
    lb.submit(b)
    fake.calls.clear()
    # one real watcher round: the loop stops itself once the round has run
    lb._stop.clear()
    lb._stop.wait = lambda t=None: lb._stop.set()  # type: ignore[method-assign]
    lb._watch_loop(interval=0)
    assert len(calls(fake, "jobs_list")) == 1  # one call for both runs
    assert sorted(calls(fake, "jobs_list")[0]["job_ids"]) == ["500", "501"]
    cl.finish("500", files={
        "outputs/pi.txt": b"3.14\n", "outputs/plot.png": b"\x89PNG\0bin",
        "job.log": b"[STAGE] Running\ndone\n", "stage.txt": b"Running\nDone\n",
        "job.sbatch": b"#!/bin/bash\ncurl https://signed.example/secret\n",
    })  # fmt: skip
    lb.poll(lb.get(a))
    run = lb.get(a)
    assert run["status"] == "completed", (run["stage"], run["error"])
    d = lb.results_dir / f"run_{a}"
    assert (d / "outputs/pi.txt").read_text() == "3.14\n"
    assert (d / "outputs/plot.png").read_bytes() == b"\x89PNG\0bin"  # via a signed link
    assert not (d / "job.sbatch").exists()  # bifrost's copy carries signed input links
    assert (d / "run.sbatch").read_text() == run["script"]
    assert json.loads((d / "plan.json").read_text())["title"] == "Pi by sampling"


def test_running_job_reads_its_stage_at_most_once_a_minute(blab):
    lb, cl, fake, _ = blab
    rid = draft(lb)
    lb.submit(rid)
    cl.jobs["500"]["state"] = "RUNNING"
    cl.jobs["500"]["files"]["stage.txt"] = b"Installing software\nRunning\n"
    lb.poll(lb.get(rid))
    lb.poll(lb.get(rid))
    run = lb.get(rid)
    assert run["status"] == "running" and run["stage"] == "Running"
    reads = [a for a in calls(fake, "job_results") if a.get("read") == "stage.txt"]
    assert len(reads) == 2  # size probe + tail, once


def test_a_started_job_drops_the_queue_label(blab):
    lb, cl, fake, _ = blab
    rid = draft(lb)
    lb.submit(rid)
    lb.poll(lb.get(rid))
    assert lb.get(rid)["stage"].startswith("Queued")
    cl.jobs["500"]["state"] = "RUNNING"  # no stage.txt yet
    lb.poll(lb.get(rid))
    assert lb.get(rid)["stage"] == "Running"
    cl.jobs["500"]["files"]["stage.txt"] = b"Installing software\n"
    lb.poll(lb.get(rid))  # an empty read is not cached: the marker shows at once
    assert lb.get(rid)["stage"] == "Installing software"


def test_node_failures_come_from_restarts(blab):
    lb, cl, fake, _ = blab
    rid = draft(lb)
    lb.submit(rid)
    cl.jobs["500"]["restarts"] = 2
    lb.poll(lb.get(rid))
    assert "Requeued after 2 node failures" in lb.get(rid)["stage"]


# ---- logs ------------------------------------------------------------------------


def test_live_log_pages_by_line_and_continues_locally(blab):
    lb, cl, fake, _ = blab
    rid = draft(lb)
    lb.submit(rid)
    cl.jobs["500"]["state"] = "RUNNING"
    cl.jobs["500"]["log"] = ["one", "two", "three"]
    d1 = lb.log(rid, 0)
    assert d1["text"] == "one\ntwo\nthree\n" and d1["size"] == -3
    cl.jobs["500"]["log"] += ["four"]
    d2 = lb.log(rid, d1["size"])
    assert d2["text"] == "four\n" and d2["size"] == -4
    assert lb.log(rid, d2["size"])["text"] == ""
    # finished: the local job.log continues after the same line
    cl.finish("500", files={"job.log": b"one\ntwo\nthree\nfour\nfive\n"})
    lb.poll(lb.get(rid))
    assert lb.get(rid)["status"] == "completed"
    assert lb.log(rid, -4)["text"] == "five\n"


# ---- cancel ----------------------------------------------------------------------


def test_cancel_goes_to_bifrost_not_ssh(blab):
    lb, cl, fake, _ = blab
    rid = draft(lb)
    lb.submit(rid)
    lb.cancel(rid)
    assert cl.jobs["500"]["state"] == "CANCELLED"
    assert calls(fake, "job_cancel") == [{"job_id": "500"}]
    assert lb.get(rid)["status"] == "cancelled"


def test_runs_submitted_over_ssh_stay_on_ssh(blab):
    """A run already queued over SSH before the upgrade is watched and cancelled there."""
    lb, cl, fake, _ = blab
    t = lb.targets["ursa"]
    with lb._conn() as conn:
        conn.execute(
            "INSERT INTO lab_runs (session_id, scope, status, target, job_id, plan) "
            "VALUES (1, 'document', 'queued', 'ursa', '77', '{}')"
        )
        conn.commit()
    seen = []
    t.status = lambda rid, job: seen.append(("status", job)) or {
        "slurm_state": "PENDING", "reason": "", "elapsed": "", "node": "",
        "exit_code": "", "started": "", "stage": "", "log_size": 0}  # fmt: skip
    t.cancel = lambda job: seen.append(("cancel", job))
    run = [r for r in lb.active() if r["job_id"] == "77"][0]
    lb.poll(run)
    lb.cancel(run["id"])
    assert seen == [("status", "77"), ("cancel", "77")]
    assert not calls(fake, "jobs_list") and not calls(fake, "job_cancel")


# ---- data relay --------------------------------------------------------------------


def test_relay_source_is_staged_once_and_passed_as_input(blab, tmp_path, monkeypatch):
    lb, cl, fake, puts = blab
    monkeypatch.setenv("DR_LOCAL_ROOTS", str(tmp_path))
    data = tmp_path / "mydata"
    data.mkdir()
    (data / "a.csv").write_text("x,y\n1,2\n")
    from deepresearch.sources.model import DataSource
    from deepresearch.sources.service import check

    check(
        lb.sources,
        lb.sources.add(DataSource(name="mydata", kind="local_folder", uri=str(data))),
    )
    src = lb.sources.get("mydata")
    assert src.effective_staging == "relay"
    plan = {**PLAN, "data_sources": ["mydata"]}
    r1 = lb.submit(draft(lb, plan))
    assert len(puts) == 1
    sent = calls(fake, "job_submit")[0]
    uid = sent["inputs"][0]
    assert uid.startswith("u")
    assert 'tar xzf "inputs/mydata-' in sent["script"] and "flock 8" in sent["script"]
    assert r1["cluster_jobs"][0]["inputs"] == [uid]
    # the next run reuses the staged copy: no second upload, same input id
    lb.submit(draft(lb, plan))
    assert len(puts) == 1 and calls(fake, "job_submit")[1]["inputs"] == [uid]

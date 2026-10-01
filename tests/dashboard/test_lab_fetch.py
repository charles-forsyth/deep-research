"""Laptop fetch (v0.50.0): URLs a site refuses to the cluster are fetched on this machine,
staged as a local data source, attached to the plan, and the fixer is told to read them
from $DS_<NAME>. Only draft plans; nothing submitted; AI-written URLs are treated as
untrusted (no private addresses, no redirects into the LAN, size caps)."""

import io
import json
import urllib.error

import pytest

from deepresearch.dashboard import labfetch
from deepresearch.dashboard.lab import Lab

URL = "https://www.loc.gov/collections/chronicling-america/?q=orange&fo=json"
PLAN = {
    "title": "t",
    "question": "q",
    "resources": {"partition": "computehigh", "nodes": 1, "time_limit": "00:10:00", "gpus": 0},
    "install": {"modules": [], "conda": [], "pip": []},
    "script": f"curl -s '{URL}' -o a.json\ncurl -s https://example.org/missing.csv -o b.csv\n",
    "url_checks": [
        {"url": URL, "ok": False, "status": "HTTP 429"},
        {"url": "https://example.org/missing.csv", "ok": False, "status": "HTTP 404"},
    ],
}  # fmt: skip


def test_only_blocked_not_broken_urls_are_offered():
    got = labfetch.blocked_urls(PLAN)
    assert got == [{"url": URL, "status": "HTTP 429"}]  # the 404 is the plan's mistake
    done = {**PLAN, "laptop_fetch": {"urls": [URL]}}
    assert labfetch.blocked_urls(done) == []
    gone = {**PLAN, "script": "echo no downloads"}
    assert labfetch.blocked_urls(gone) == []  # URL no longer in the plan


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://x.org/a", "http://127.0.0.1:7420/api/settings",
     "http://localhost/x", "http://192.168.1.240:8080/", "http://169.254.169.254/latest",
     "http://100.127.141.37:8080/", "http://[::1]/"],
)  # fmt: skip
def test_untrusted_urls_are_refused(url, monkeypatch):
    monkeypatch.undo()  # the real address check, not the autouse stub
    with pytest.raises(labfetch.FetchRefused):
        labfetch.check_url(url)


class _Resp(io.BytesIO):
    def __init__(self, data, ctype="application/json", status=200):
        super().__init__(data)
        self.headers = {"Content-Type": ctype}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class _Opener:
    def __init__(self, replies):
        self.replies = replies
        self.seen = []

    def open(self, req, timeout=0):
        self.seen.append((req.full_url, req.headers.get("User-agent")))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture(autouse=True)
def _public(monkeypatch):
    monkeypatch.setattr(labfetch, "_public_host", lambda host: None)


def test_fetch_writes_files_and_provenance(tmp_path):
    op = _Opener([_Resp(b'{"a": 1}')])
    out = labfetch.fetch([URL], tmp_path, opener=op, sleep=lambda s: None)
    f = out["files"][0]
    assert f["url"] == URL and f["bytes"] == 8 and f["file"].endswith(".json")
    assert (tmp_path / f["file"]).read_bytes() == b'{"a": 1}'
    prov = json.loads((tmp_path / "urls.json").read_text())
    assert prov["files"][0]["sha256"] == f["sha256"]
    assert "deep-research-lab" in op.seen[0][1]  # an honest user agent


def test_429_backs_off_once_then_succeeds(tmp_path):
    err = urllib.error.HTTPError(URL, 429, "Too Many", {"Retry-After": "3"}, None)
    slept = []
    op = _Opener([err, _Resp(b"ok", "text/plain")])
    out = labfetch.fetch([URL], tmp_path, opener=op, sleep=slept.append)
    assert len(out["files"]) == 1 and slept == [3]


def test_size_cap_and_no_partial_files(tmp_path, monkeypatch):
    monkeypatch.setattr(labfetch, "MAX_FILE_BYTES", 10)
    op = _Opener([_Resp(b"x" * 100)])
    out = labfetch.fetch([URL], tmp_path, opener=op, sleep=lambda s: None)
    assert out["files"] == [] and "size limit" in out["failed"][0]["error"]
    assert [p.name for p in tmp_path.iterdir()] == ["urls.json"]


def test_redirects_into_the_lan_are_refused(monkeypatch):
    monkeypatch.undo()  # real address check
    h = labfetch._SafeRedirects()
    with pytest.raises(labfetch.FetchRefused):
        h.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1/secret")


def test_file_names_are_safe_and_distinct():
    a = labfetch.file_name(URL, "application/json")
    b = labfetch.file_name(URL + "&page=2", "application/json")
    assert a != b and "/" not in a and a.endswith(".json")
    assert labfetch.file_name("https://x.org/../../etc/passwd").startswith("passwd-")


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setenv("DR_LOCAL_ROOTS", str(tmp_path))
    monkeypatch.setattr(labfetch, "fetch_root", lambda: tmp_path / "lab-fetch")
    lb = Lab(
        str(tmp_path / "h.db"), lambda: None, tmp_path, targets={}, workspace="demo"
    )
    lb.ensure_watcher = lambda: None
    return lb


def test_laptop_fetch_stages_attaches_and_asks_the_fixer(lab, monkeypatch, tmp_path):
    def fake_fetch(urls, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "a-1.json").write_text("{}")
        return {"files": [{"file": "a-1.json", "url": urls[0], "bytes": 2}],
                "failed": [], "bytes": 2}  # fmt: skip

    monkeypatch.setattr(labfetch, "fetch", fake_fetch)
    asked = {}

    def fix_plan(run_id, extra_problems=None):
        asked["problems"] = extra_problems
        return {**lab.get(run_id), "fix": {"changes": ["read $DS_..."]}}

    monkeypatch.setattr(lab, "fix_plan", fix_plan)
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    out = lab.laptop_fetch(run["id"])
    name = f"fetch-demo-run{run['id']}"
    plan = lab.get(run["id"])["plan"]
    assert plan["data_sources"] == [name] and plan["laptop_fetch"]["urls"] == [URL]
    src = lab.sources.get(name)
    assert (
        src.kind == "local_folder"
        and src.status == "ok"
        and src.protection_level == "P1"
    )
    assert (
        f"${src.env_var}/a-1.json" in asked["problems"][0]
        and URL in asked["problems"][0]
    )
    assert out["laptop_fetch"]["source"] == name and out["status"] == "draft"
    # the fetched URL is no longer offered
    assert labfetch.blocked_urls(plan) == []


def test_laptop_fetch_refuses_non_drafts_and_unknown_urls(lab):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    with pytest.raises(ValueError, match="no blocked"):
        lab.laptop_fetch(run["id"], ["https://evil.example/not-in-plan"])
    lab._update(run["id"], status="queued")
    with pytest.raises(ValueError, match="only drafts"):
        lab.laptop_fetch(run["id"])


# ---- refused while it ran (v0.51.0, L8) ------------------------------------------
LOG_42 = """[STAGE] Running
--- RUNNING IN FULL PRODUCTION MODE ---
Warning: HTTP 429 for 1890 riverside_orange, backoff 4s
Warning: HTTP 429 for 1890 riverside_orange, backoff 8s
Warning: HTTP 429 for 1895 all, backoff 4s
[STAGE] Done
"""
PLAN_42 = {
    **PLAN,
    "script": "import requests\nBASE = 'https://www.loc.gov/collections/chronicling-america/'\n"
    "for y in YEARS:\n    requests.get(BASE, params={'dates': y, 'fo': 'json'})\n",
    "url_checks": [],
}


def test_the_real_run_42_log_is_recognised_without_a_url_in_it():
    info = labfetch.runtime_blocked(LOG_42, PLAN_42)
    assert info and info["statuses"] == ["429"] and info["count"] == 3
    assert info["hosts"] == ["www.loc.gov"]
    assert info["urls"] == ["https://www.loc.gov/collections/chronicling-america/"]
    assert "HTTP 429 for 1890 riverside_orange" in info["lines"][0]


@pytest.mark.parametrize(
    "line",
    [
        "requests.exceptions.HTTPError: 403 Client Error: Forbidden for url: https://data.x.org/a.csv",
        "urllib.error.HTTPError: HTTP Error 429: Too Many Requests",
        "curl: (22) The requested URL returned error: 403 Forbidden",
    ],
)  # fmt: skip
def test_other_refusal_lines_are_recognised(line):
    plan = {**PLAN, "script": "curl -sf https://data.x.org/a.csv -o a.csv"}
    info = labfetch.runtime_blocked("ok\n" + line + "\n", plan)
    assert info is not None and info["count"] == 1


def test_numbers_that_happen_to_be_403_or_429_are_not_refusals():
    """Real logs from runs #5 and #33: '429' inside tables and file sizes."""
    log = (
        "-rw-rw-r-- 1 u u  1429 Sep 30 verdict.json\n"
        "|  0|  -3.727702|  429.000|  403.1 |\n"
        "Iteration 403: residual 4.29e-03\n"
    )
    assert labfetch.runtime_blocked(log, PLAN) is None


def test_no_web_in_the_plan_means_no_offer():
    plan = {**PLAN, "script": "python sim.py"}
    assert labfetch.runtime_blocked("HTTP 429 for x\n", plan) is None


def _finished(lab, plan, status="completed"):
    run = lab.create(1, "document", "x", plan=dict(plan))
    lab._update(run["id"], status=status)
    return run["id"]


def test_fix_blocked_with_fixed_urls_fetches_into_a_new_draft(lab, monkeypatch):
    rid = _finished(lab, PLAN_42, "completed")
    lab._note_runtime_blocked(rid, LOG_42, lab.get(rid)["plan"])
    calls = {}

    def laptop_fetch(run_id, urls=None, extra_problems=None):
        calls["fetch"] = lab.get(run_id)["plan"]["url_checks"]
        calls["problems"] = extra_problems
        return {**lab.get(run_id), "laptop_fetch": {"files": [1]}}

    monkeypatch.setattr(lab, "laptop_fetch", laptop_fetch)
    out = lab.fix_blocked(rid)
    assert out["id"] != rid and out["rerun_of"] == rid and out["status"] == "draft"
    assert calls["fetch"][0]["url"].startswith("https://www.loc.gov/")
    assert "fewer, larger responses" in calls["problems"][0]  # the loop is fixed too
    # the copied script still calls loc.gov (fake fetch changed nothing): said plainly
    assert out["still_calls"] == ["www.loc.gov"]
    assert "still calls www.loc.gov" in lab.get(out["id"])["stage"]
    assert lab.get(rid)["status"] == "completed"  # the finished run is untouched
    assert "runtime_blocked" not in lab.get(out["id"])["plan"]  # bookkeeping not copied


def test_fix_blocked_when_the_job_builds_urls_asks_the_fixer(lab, monkeypatch):
    plan = {**PLAN, "script": "for p in range(9): get(f'https://api.x.org/q?page={p}')"}
    rid = _finished(lab, plan, "failed")
    lab._note_runtime_blocked(rid, "HTTP Error 429: Too Many Requests\n", plan)
    asked = {}

    def fix_plan(run_id, extra_problems=None):
        asked["p"] = extra_problems
        return {**lab.get(run_id), "fix": {"changes": ["bulk fetch"]}}

    monkeypatch.setattr(lab, "fix_plan", fix_plan)
    out = lab.fix_blocked(rid)
    assert out["status"] == "draft" and out["rerun_of"] == rid
    assert (
        "fewer, larger responses" in asked["p"][0] and "never as zero" in asked["p"][0]
    )


def test_fix_blocked_refuses_runs_without_refusals(lab):
    rid = _finished(lab, PLAN, "completed")
    with pytest.raises(ValueError):
        lab.fix_blocked(rid)


def test_fix_blocked_keeps_the_draft_when_the_fix_fails(lab, monkeypatch):
    plan = {**PLAN, "script": "for p in range(9): get(f'https://api.x.org/q?page={p}')"}
    rid = _finished(lab, plan, "failed")
    lab._note_runtime_blocked(rid, "HTTP Error 429: Too Many Requests\n", plan)

    def fix_plan(run_id, extra_problems=None):
        raise RuntimeError("503 UNAVAILABLE")

    monkeypatch.setattr(lab, "fix_plan", fix_plan)
    out = lab.fix_blocked(rid)
    assert out["status"] == "draft" and out["rerun_of"] == rid
    assert "503" in out["fix_error"] and "did not finish" in lab.get(out["id"])["stage"]


def test_busy_model_is_retried_then_answers(lab, monkeypatch):
    """A Gemini 503 'high demand' (2026-10-01) is retried with backoff, not fatal."""
    from types import SimpleNamespace

    class Busy(Exception):
        code = 503

    calls = {"n": 0}

    def gen(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise Busy("503 UNAVAILABLE")
        return SimpleNamespace(text="ok", usage_metadata=None, candidates=[])

    client = SimpleNamespace(models=SimpleNamespace(generate_content=gen))
    monkeypatch.setattr(lab, "_client", lambda: client)
    monkeypatch.setattr(lab, "_model", lambda: "m")
    monkeypatch.setattr(lab, "BUSY_BACKOFF_S", (0.0, 0.0, 0.0))
    assert lab._ask("p", search=False)[0] == "ok" and calls["n"] == 3

    class Bad(Exception):
        code = 400

    def bad(**kw):
        raise Bad("400")

    client.models.generate_content = bad
    with pytest.raises(Bad):
        lab._ask("p", search=False)

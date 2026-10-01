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

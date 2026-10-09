"""`deep-research status`, `list --status` and `search --no-answer`: the cheap reads for
checking back on long work from a terminal or an agent harness. No model calls."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from deepresearch import __main__ as cli_main
from deepresearch.cli import commands, status
from deepresearch.core import workspace as W
from deepresearch.core.session import SessionManager


def _run(monkeypatch, capsys, argv):
    monkeypatch.setattr("sys.argv", ["deep-research", *argv])
    code = 0
    try:
        cli_main.main()
    except SystemExit as e:
        code = int(e.code or 0)
    out, err = capsys.readouterr()
    return json.loads(out), err, code


def _dead_pid() -> int:
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """Main workspace in a temp config dir: one live run, one dead run, one finished
    report, one old report, and Lab runs in each group."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(status, "dashboard_status", lambda: {"running": False})
    db = W.get("main").db_path
    os.makedirs(os.path.dirname(db), exist_ok=True)
    mgr = SessionManager(db)
    live = mgr.create_session("int-live", "Live question", pid=os.getpid())
    dead = mgr.create_session("int-dead", "Dead question", pid=_dead_pid())
    done = mgr.create_session("int-done", "Done question", pid=0)
    mgr.update_session("int-done", "completed", "# Report")
    old = mgr.create_session("int-old", "Old question", pid=0)
    mgr.update_session("int-old", "completed", "# Old")
    long_ago = (datetime.now() - timedelta(days=10)).isoformat()
    with sqlite3.connect(db) as c:
        c.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (long_ago, old))
    from deepresearch.dashboard.lab import Lab

    Lab(db, lambda: None, tmp_path / "state")  # creates the lab tables
    now = datetime.now().isoformat()
    verdict = json.dumps({"checks": [{"name": "c", "kind": "claim", "pass": True}]})
    with sqlite3.connect(db) as c:
        for status_, stage, fin, v in (
            ("queued", "Waiting for a node", None, None),
            ("draft", "Plan ready for review", None, None),
            ("completed", "Completed", now, verdict),
            ("failed", "Smoke test failed 3 times", long_ago, None),
        ):
            c.execute(
                "INSERT INTO lab_runs (session_id, scope, request, target, status, "
                "stage, plan, verdict, created_at, updated_at, finished_at) "
                "VALUES (?, 'document', 'q', 'ursa-major', ?, ?, ?, ?, ?, ?, ?)",
                (
                    done,
                    status_,
                    stage,
                    json.dumps({"title": f"T-{status_}"}),
                    v,
                    now,
                    fin or now if status_ != "failed" else long_ago,
                    fin,
                ),
            )
    return {"db": db, "live": live, "dead": dead, "done": done, "old": old}


def test_status_json_groups_runs(lib, monkeypatch, capsys):
    d, _, code = _run(monkeypatch, capsys, ["status", "--json"])
    assert code == 0
    assert d["dashboard"] == {"running": False}
    (w,) = d["workspaces"]
    assert w["workspace"] == "main"
    r = w["research"]
    assert [s["id"] for s in r["running"]] == [lib["live"]]
    assert r["running"][0]["age_min"] == 0
    recent = {s["id"]: s["status"] for s in r["recent"]}
    # the dead run is swept to crashed and shows as recent; the 10-day-old one does not
    assert recent == {lib["dead"]: "crashed", lib["done"]: "completed"}
    assert r["counts"]["completed"] == 2
    lab = w["lab"]
    assert [x["status"] for x in lab["active"]] == ["queued"]
    assert [x["status"] for x in lab["waiting"]] == ["draft"]
    assert [x["title"] for x in lab["recent"]] == [
        "T-completed"
    ]  # old failure left out
    assert lab["recent"][0]["outcome"] == "confirmed"
    assert "result" not in json.dumps(r["recent"][0]).replace("result_chars", "")


def test_status_since_widens_the_window(lib, monkeypatch, capsys):
    d, _, _ = _run(monkeypatch, capsys, ["status", "--since", "720", "--json"])
    w = d["workspaces"][0]
    assert lib["old"] in {s["id"] for s in w["research"]["recent"]}
    assert "failed" in {x["status"] for x in w["lab"]["recent"]}


def test_status_all_workspaces(lib, monkeypatch, capsys):
    W.create("Second space", slug="second")
    d, _, _ = _run(monkeypatch, capsys, ["status", "--all-workspaces", "--json"])
    assert [w["workspace"] for w in d["workspaces"]] == ["main", "second"]
    assert d["workspaces"][1]["research"]["running"] == []


def test_status_text_output(lib, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["deep-research", "status"])
    with pytest.raises(SystemExit) as e:
        cli_main.main()
    out = capsys.readouterr().out
    assert e.value.code == 0
    assert "Research running: 1" in out and "Live question" in out
    assert "1 on the cluster, 1 waiting for review" in out


def test_status_makes_no_model_call(lib, monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("status must not create a Gemini client")

    monkeypatch.setattr(commands.genai, "Client", boom)
    _, _, code = _run(monkeypatch, capsys, ["status", "--json"])
    assert code == 0


def test_list_status_filter_sweeps_dead_runs(lib, monkeypatch, capsys):
    real_init = SessionManager.__init__
    monkeypatch.setattr(  # conftest points a path-less manager at a throwaway DB
        SessionManager,
        "__init__",
        lambda self, db_path=lib["db"]: real_init(self, db_path),
    )
    rows, _, code = _run(monkeypatch, capsys, ["list", "--status", "running", "--json"])
    assert code == 0 and [r["id"] for r in rows] == [lib["live"]]
    rows, _, _ = _run(monkeypatch, capsys, ["list", "--status", "crashed", "--json"])
    assert [r["id"] for r in rows] == [lib["dead"]]
    rows, _, _ = _run(
        monkeypatch, capsys, ["list", "--status", "completed", "--limit", "1", "--json"]
    )
    assert [r["id"] for r in rows] == [lib["done"]]  # newest first


class _Emb:
    def __init__(self, values):
        self.embeddings = [type("E", (), {"values": values})()]


class _FakeModels:
    def __init__(self):
        self.generated = 0

    def embed_content(self, model, contents):
        return _Emb([1.0, 0.0])

    def generate_content(self, model, contents):
        self.generated += 1
        return type("R", (), {"text": "answer [Session #1]"})()


def test_search_no_answer_skips_the_model_call(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "h.db")
    mgr = SessionManager(db)
    a = mgr.create_session("i-a", "About slurm", pid=0)
    mgr.update_session("i-a", "completed", "Slurm text")
    b = mgr.create_session("i-b", "About ceph", pid=0)
    mgr.update_session("i-b", "completed", "Ceph text")
    with sqlite3.connect(db) as c:
        c.execute("UPDATE sessions SET embedding = '[1.0, 0.0]' WHERE id = ?", (a,))
        c.execute("UPDATE sessions SET embedding = '[0.0, 1.0]' WHERE id = ?", (b,))
    real_init = SessionManager.__init__
    monkeypatch.setattr(
        SessionManager, "__init__", lambda self, db_path=db: real_init(self, db_path)
    )
    models = _FakeModels()
    monkeypatch.setattr(
        commands.genai, "Client", lambda **k: type("C", (), {"models": models})()
    )
    monkeypatch.setattr(commands, "DeepResearchConfig", lambda: type("Cfg", (), {
        "api_key": "x", "followup_model": "m"})())  # fmt: skip
    d, _, code = _run(monkeypatch, capsys, ["search", "slurm", "--no-answer", "--json"])
    assert code == 0 and models.generated == 0
    assert d["answer"] is None and d["model"] is None
    assert [m["session_id"] for m in d["matches"]][0] == a
    d, _, _ = _run(monkeypatch, capsys, ["search", "slurm", "--json"])
    assert models.generated == 1 and d["answer"].startswith("answer")

"""--json on every command: stdout is exactly one JSON document, logs go to stderr.

These run the real CLI entry point against a throwaway history DB. Nothing here calls
the Gemini API: the client, agent and detach are replaced with fakes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deepresearch import __main__ as cli_main
from deepresearch.__main__ import build_parser
from deepresearch.cli import commands
from deepresearch.core.session import SessionManager


def _run(monkeypatch, capsys, argv: list[str]) -> tuple[Any, str, int]:
    """Run `deep-research <argv>`; return (parsed stdout JSON, stderr, exit code)."""
    monkeypatch.setattr("sys.argv", ["deep-research", *argv])
    code = 0
    try:
        cli_main.main()
    except SystemExit as e:
        code = int(e.code or 0)
    out, err = capsys.readouterr()
    return json.loads(out), err, code


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A history DB with a completed root, a child and a failed run."""
    path = str(tmp_path / "history.db")
    mgr = SessionManager(path)
    root = mgr.create_session("int-root", "Root question", ["/nope/a.pdf"], pid=0)
    mgr.update_session("int-root", "completed", "# Report\n\nBody text.")
    child = mgr.create_session("int-child", "Child question", parent_id=root, depth=2)
    mgr.update_session("int-child", "completed", "Child report")
    mgr.create_session("int-bad", "Bad question", pid=0)
    mgr.update_session("int-bad", "failed", "API Error")
    with sqlite3.connect(path) as c:
        c.execute("UPDATE sessions SET embedding = '[0.1, 0.2]' WHERE id = ?", (root,))

    real_init = SessionManager.__init__

    def init(self, db_path: str = path):
        real_init(self, db_path)

    monkeypatch.setattr(SessionManager, "__init__", init)
    monkeypatch.setattr(commands, "user_db_path", path)
    return SimpleNamespace(path=path, root=root, child=child)


def test_every_command_has_json_flag():
    parser = build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    for name, p in sub.choices.items():  # type: ignore[union-attr]
        if name == "sources":
            inner = next(a for a in p._actions if a.dest == "sources_cmd")
            for sname, sp in inner.choices.items():  # type: ignore[union-attr]
                assert "--json" in sp.format_help(), f"sources {sname}"
            continue
        assert "--json" in p.format_help(), name


def test_list_json(db, monkeypatch, capsys):
    rows, _, code = _run(monkeypatch, capsys, ["list", "--json", "--limit", "5"])
    assert code == 0
    assert {r["prompt"] for r in rows} == {
        "Root question",
        "Child question",
        "Bad question",
    }
    for r in rows:
        assert "embedding" not in r and "result" not in r
        assert isinstance(r["result_chars"], int)
        assert isinstance(r["files"], list)


def test_show_json_has_report_and_provenance(db, monkeypatch, capsys):
    d, _, code = _run(monkeypatch, capsys, ["show", str(db.root), "--json"])
    assert code == 0
    assert d["result"].startswith("# Report")
    assert d["files"] == ["/nope/a.pdf"]
    assert "embedding" not in d
    assert d["provenance"]["fingerprint"]
    assert "children" not in d


def test_show_json_recursive_nests_children(db, monkeypatch, capsys):
    d, _, _ = _run(monkeypatch, capsys, ["show", str(db.root), "--json", "--recursive"])
    assert [c["id"] for c in d["children"]] == [db.child]
    assert d["children"][0]["result"] == "Child report"


def test_show_json_by_interaction_id(db, monkeypatch, capsys):
    d, _, _ = _run(monkeypatch, capsys, ["show", "int-root", "--json"])
    assert d["id"] == db.root


def test_show_json_missing_is_error_and_nonzero(db, monkeypatch, capsys):
    d, _, code = _run(monkeypatch, capsys, ["show", "999", "--json"])
    assert code == 1 and "not found" in d["error"]


def test_show_json_refuses_save(db, monkeypatch, capsys, tmp_path):
    d, _, code = _run(
        monkeypatch, capsys, ["show", "1", "--json", "--save", str(tmp_path / "x")]
    )
    assert code == 2 and "--save" in d["error"]


def test_tree_json(db, monkeypatch, capsys):
    d, _, _ = _run(monkeypatch, capsys, ["tree", str(db.root), "--json"])
    assert d["id"] == db.root and d["children"][0]["id"] == db.child
    assert "result" not in d  # tree is structure only
    forest, _, _ = _run(monkeypatch, capsys, ["tree", "--json"])
    assert isinstance(forest, list) and all("children" in t for t in forest)


def test_delete_json(db, monkeypatch, capsys):
    d, _, code = _run(monkeypatch, capsys, ["delete", str(db.child), "--json"])
    assert code == 0 and d == {"id": str(db.child), "deleted": True, "ids": [db.child]}
    d, _, code = _run(monkeypatch, capsys, ["delete", str(db.child), "--json"])
    assert code == 1 and d["deleted"] is False


def test_delete_removes_the_whole_tree_and_its_rows(db, monkeypatch, capsys, tmp_path):
    """K10: the CLI deletes like the dashboard: children, meta, usage, audio, notes."""
    from deepresearch.dashboard.features import Features
    from deepresearch.dashboard.store import DashboardStore

    store = DashboardStore(db.path)
    store.set_meta(db.root, starred=True)
    Features(db.path, lambda: None, tmp_path / "audio")  # creates its tables
    audio = tmp_path / "audio" / "session_1_full_Charon.mp3"
    audio.parent.mkdir(exist_ok=True)
    audio.write_bytes(b"x")
    with sqlite3.connect(db.path) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS run_meta (session_id INTEGER PRIMARY KEY, "
            "depth INTEGER, breadth INTEGER, estimate_usd REAL, rerun_of INTEGER, "
            "launched_at TEXT)"
        )
        c.execute("INSERT INTO run_meta (session_id, depth) VALUES (?, 2)", (db.root,))
        c.execute(
            "INSERT INTO session_usage (session_id, usage) VALUES (?, '{}')", (db.root,)
        )
        c.execute(
            "INSERT INTO audio_exports (kind, ref_id, mode, voice, path) "
            "VALUES ('session', ?, 'full', 'Charon', ?)",
            (db.root, str(audio)),
        )
    d, _, code = _run(monkeypatch, capsys, ["delete", str(db.root), "--json"])
    assert code == 0 and sorted(d["ids"]) == sorted([db.root, db.child])
    with sqlite3.connect(db.path) as c:
        for table in ("session_meta", "run_meta", "session_usage", "audio_exports"):
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
        left = c.execute("SELECT id FROM sessions").fetchall()
    assert (db.child,) not in left and (db.root,) not in left
    assert not audio.exists()


def test_delete_refused_while_a_lab_run_is_on_the_cluster(db, monkeypatch, capsys):
    from deepresearch.dashboard.lab import Lab

    Lab(db.path, lambda: None, Path(db.path).parent)  # creates lab tables
    with sqlite3.connect(db.path) as c:
        c.execute(
            "INSERT INTO lab_runs (session_id, scope, request, target, status) "
            "VALUES (?, 'document', 'q', 'ursa-major', 'running')",
            (db.root,),
        )
    d, _, code = _run(monkeypatch, capsys, ["delete", str(db.root), "--json"])
    assert code == 1 and d["deleted"] is False and "Cancel the lab runs" in d["error"]
    with sqlite3.connect(db.path) as c:
        assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 3


def test_estimate_json_matches_formula(monkeypatch, capsys):
    d, _, code = _run(
        monkeypatch,
        capsys,
        ["estimate", "q", "--depth", "2", "--breadth", "3", "--json"],
    )
    assert code == 0
    assert d["nodes"] == 4  # 1 + 3
    assert d["output_tokens"] == 4 * 60_000
    assert d["cost_usd"] > 0 and d["pricing"]["output_per_1m"] == 12.0


def test_start_json_prints_session_id(db, monkeypatch, capsys):
    monkeypatch.setattr(commands, "detach_process", lambda args, log: 4321)
    d, err, code = _run(monkeypatch, capsys, ["start", "New question", "--json"])
    assert code == 0 and d["pid"] == 4321 and d["status"] == "running"
    row = SessionManager().get_session(str(d["session_id"]))
    assert row["prompt"] == "New question" and row["pid"] == 4321
    assert "Research started" not in err  # the human lines are not printed at all


def test_start_json_forwards_to_child_without_json(db, monkeypatch, capsys):
    seen = {}

    def fake_detach(args, log):
        seen["args"] = args
        return 1

    monkeypatch.setattr(commands, "detach_process", fake_detach)
    _run(monkeypatch, capsys, ["start", "Q", "--json", "--depth", "2"])
    assert "--json" not in seen["args"]  # the worker writes its normal log
    assert seen["args"][:2] == ["research", "Q"]


class _FakeAgent:
    """Stands in for DeepResearchAgent: writes a report to the adopted row."""

    report = "Final report"
    status = "completed"

    def __init__(self, **kw):
        pass

    def start_research_poll(self, request):
        print("[INFO] polling...")  # must land on stderr under --json
        mgr = SessionManager()
        if request.adopt_session_id:
            mgr.update_session_interaction_id(request.adopt_session_id, "int-new")
        else:
            mgr.create_session("int-new", request.prompt)
        mgr.update_session("int-new", self.status, self.report)
        return "int-new"

    start_research_stream = start_research_poll


def test_research_json_returns_finished_session(db, monkeypatch, capsys):
    monkeypatch.setattr(commands, "DeepResearchAgent", _FakeAgent)
    d, err, code = _run(monkeypatch, capsys, ["research", "Fresh q", "--json"])
    assert code == 0
    assert d["prompt"] == "Fresh q" and d["result"] == "Final report"
    assert "polling" in err


def test_research_json_failure_is_nonzero(db, monkeypatch, capsys):
    class Failing(_FakeAgent):
        status = "failed"
        report = "API Error (failed): boom"

    monkeypatch.setattr(commands, "DeepResearchAgent", Failing)
    d, _, code = _run(monkeypatch, capsys, ["research", "Doomed q", "--json"])
    assert code == 1 and "failed" in d["error"]
    assert d["session"]["result"].startswith("API Error")


def test_followup_json(db, monkeypatch, capsys):
    class Agent:
        def __init__(self, **kw):
            pass

        def follow_up(self, request):
            assert request.interaction_id == "int-root"
            return "The answer"

    monkeypatch.setattr(commands, "DeepResearchAgent", Agent)
    d, _, code = _run(monkeypatch, capsys, ["followup", str(db.root), "Why?", "--json"])
    assert code == 0
    assert d == {
        "session_id": db.root,
        "interaction_id": "int-root",
        "prompt": "Why?",
        "sources": [],
        "answer": "The answer",
    }


def test_followup_json_empty_answer_is_error(db, monkeypatch, capsys):
    class Agent:
        def __init__(self, **kw):
            pass

        def follow_up(self, request):
            return ""

    monkeypatch.setattr(commands, "DeepResearchAgent", Agent)
    d, _, code = _run(monkeypatch, capsys, ["followup", str(db.root), "?", "--json"])
    assert code == 1 and "no text" in d["error"]


class _Emb:
    def __init__(self, values):
        self.embeddings = [SimpleNamespace(values=values)]


class _Client:
    def __init__(self, **kw):
        self.models = self

    def embed_content(self, model, contents):
        return _Emb([0.1, 0.2])

    def generate_content(self, model, contents):
        assert "Root question" in contents
        return SimpleNamespace(text="Answer citing [Session #1]")


def test_search_json(db, monkeypatch, capsys):
    monkeypatch.setattr(commands.genai, "Client", _Client)
    monkeypatch.setattr(
        commands,
        "DeepResearchConfig",
        lambda: SimpleNamespace(api_key="k", followup_model="m"),
    )
    d, err, code = _run(monkeypatch, capsys, ["search", "roots", "--json"])
    assert code == 0
    assert d["answer"].startswith("Answer citing")
    ids = [m["session_id"] for m in d["matches"]]
    assert db.root in ids and all(0 <= m["score"] <= 1.0001 for m in d["matches"])
    assert d["model"] == "m"
    assert "Searching" in err  # progress went to stderr


def test_cleanup_json_without_force_is_a_dry_run(monkeypatch, capsys):
    deleted = []

    class Stores:
        def list(self):
            return [SimpleNamespace(name="fileSearchStores/tmp1", display_name="")]

        def delete(self, name):
            deleted.append(name)

    client = SimpleNamespace(file_search_stores=Stores())
    monkeypatch.setattr(commands.genai, "Client", lambda **k: client)
    monkeypatch.setattr(
        commands, "DeepResearchConfig", lambda: SimpleNamespace(api_key="k")
    )
    monkeypatch.setattr(commands, "_protected_stores", lambda: set())
    d, _, code = _run(monkeypatch, capsys, ["cleanup", "--json"])
    assert code == 0 and d["dry_run"] is True and not deleted
    assert d["would_delete"][0]["name"] == "fileSearchStores/tmp1"
    d, _, code = _run(monkeypatch, capsys, ["cleanup", "--json", "--force"])
    assert d["deleted"] == ["fileSearchStores/tmp1"] and deleted


def test_auth_logout_json(tmp_path, monkeypatch, capsys):
    p = tmp_path / ".env"
    p.write_text("GEMINI_API_KEY=x\n")
    monkeypatch.setattr(commands, "user_config_path", str(p))
    d, _, _ = _run(monkeypatch, capsys, ["auth", "logout", "--json"])
    assert d == {"logged_out": True, "path": str(p)} and p.read_text() == ""


def test_dashboard_status_json(monkeypatch, capsys):
    from deepresearch.dashboard import daemon

    monkeypatch.setattr(daemon, "read_state", lambda: None)
    d, err, code = _run(monkeypatch, capsys, ["dashboard", "--status", "--json"])
    assert d["running"] is False and d["pid"] is None and d["exit_code"] == 3
    assert "not running" in err


def test_dashboard_foreground_json_refused(monkeypatch, capsys):
    d, _, code = _run(monkeypatch, capsys, ["dashboard", "--foreground", "--json"])
    assert code == 2 and "foreground" in d["error"]


def test_config_error_is_json(monkeypatch, capsys):
    def boom():
        raise ValueError("GEMINI_API_KEY is not set")

    monkeypatch.setattr(commands, "DeepResearchConfig", boom)
    d, _, code = _run(monkeypatch, capsys, ["search", "x", "--json"])
    assert code == 2 and "GEMINI_API_KEY" in d["error"]


def test_sources_list_json_uses_same_contract(tmp_path, monkeypatch, capsys):
    from deepresearch.cli import sources as src_cli
    from deepresearch.sources import SourceRegistry

    reg = SourceRegistry(str(tmp_path / "h.db"))
    monkeypatch.setattr(src_cli, "SourceRegistry", lambda: reg)
    rows, _, code = _run(monkeypatch, capsys, ["sources", "list", "--json"])
    assert code == 0 and rows == []
    d, _, code = _run(monkeypatch, capsys, ["sources", "show", "nope", "--json"])
    assert code == 1 and "nope" in d["error"]


def test_without_json_output_is_unchanged(db, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["deep-research", "list"])
    cli_main.main()
    out = capsys.readouterr().out
    assert "Recent Research Sessions" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_auth_login_keeps_every_other_setting(tmp_path, monkeypatch, capsys):
    """K15: login replaces only GEMINI_API_KEY; comments and other keys survive."""
    import stat

    p = tmp_path / ".env"
    before = (
        "OPENAI_API_KEY=o\n# a comment\n\n  # indented comment\n"
        "GEMINI_FOLLOWUP_MODEL=gemini-3.8-flash\nGEMINI_API_KEY=old\nDB_PASSWORD=p\n"
    )
    p.write_text(before)
    p.chmod(0o644)
    monkeypatch.setattr(commands, "user_config_path", str(p))
    monkeypatch.setattr(commands.Prompt, "ask", lambda *a, **k: "AIzaNEW")
    _run(monkeypatch, capsys, ["auth", "login", "--json"])
    assert p.read_text() == before.replace(
        "GEMINI_API_KEY=old", "GEMINI_API_KEY=AIzaNEW"
    )
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert [f.name for f in tmp_path.iterdir()] == [".env"]  # no temp file left


def test_auth_login_creates_the_file_owner_only(tmp_path, monkeypatch, capsys):
    import stat

    p = tmp_path / "sub" / ".env"
    monkeypatch.setattr(commands, "user_config_path", str(p))
    monkeypatch.setattr(commands.Prompt, "ask", lambda *a, **k: "AIzaX")
    _run(monkeypatch, capsys, ["auth", "login", "--json"])
    assert p.read_text() == "GEMINI_API_KEY=AIzaX\n"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_auth_logout_keeps_other_settings(tmp_path, monkeypatch, capsys):
    p = tmp_path / ".env"
    p.write_text("A=1\nGEMINI_API_KEY=x\n# note\nB=2\n")
    monkeypatch.setattr(commands, "user_config_path", str(p))
    d, _, _ = _run(monkeypatch, capsys, ["auth", "logout", "--json"])
    assert d["logged_out"] is True and p.read_text() == "A=1\n# note\nB=2\n"
    d, _, _ = _run(monkeypatch, capsys, ["auth", "logout", "--json"])
    assert d["logged_out"] is False and p.read_text() == "A=1\n# note\nB=2\n"


def test_set_env_value_failed_write_leaves_the_file(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("A=1\nGEMINI_API_KEY=x\n")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(commands.os, "replace", boom)
    with pytest.raises(OSError):
        commands.set_env_value(str(p), "GEMINI_API_KEY", "y")
    assert p.read_text() == "A=1\nGEMINI_API_KEY=x\n"
    assert [f.name for f in tmp_path.iterdir()] == [".env"]


def test_research_refuses_depth_and_breadth_beyond_the_dashboard_limits(
    monkeypatch, capsys
):
    """K7: the CLI has the dashboard's limits (depth 1-5, breadth 1-10)."""
    for argv in (
        ["research", "q", "--depth", "9", "--json"],
        ["start", "q", "--breadth", "50", "--json"],
        ["estimate", "q", "--depth", "0", "--json"],
    ):
        d, _, code = _run(monkeypatch, capsys, argv)
        assert code == 2 and "depth must be 1-5 and breadth 1-10" in d["error"], argv

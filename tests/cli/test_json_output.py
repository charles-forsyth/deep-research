"""--json on every command: stdout is exactly one JSON document, logs go to stderr.

These run the real CLI entry point against a throwaway history DB. Nothing here calls
the Gemini API: the client, agent and detach are replaced with fakes.
"""

from __future__ import annotations

import json
import sqlite3
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
    assert code == 0 and d == {"id": str(db.child), "deleted": True}
    d, _, code = _run(monkeypatch, capsys, ["delete", str(db.child), "--json"])
    assert code == 1 and d["deleted"] is False


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
    assert d == {"logged_out": True, "path": str(p)} and not p.exists()


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

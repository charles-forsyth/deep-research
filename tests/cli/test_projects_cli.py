"""`deep-research projects` (v0.47.0): same data as the dashboard, --json for scripts,
membership changes only, and never mistaken for a research prompt."""

import json
import sqlite3
import subprocess
import sys

import pytest

from deepresearch.core.session import SessionManager


@pytest.fixture
def home(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    (cfg / "deepresearch").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    db = cfg / "deepresearch" / "history.db"
    sm = SessionManager(str(db))
    a = sm.create_session("i1", "Coral extinction question")
    b = sm.create_session("i2", "Ising model question")
    with sqlite3.connect(db) as c:
        c.execute(
            "UPDATE sessions SET status='completed', result='Report [1](https://x.org/a)'"
        )
    return {"cfg": cfg, "db": db, "a": a, "b": b, "tmp": tmp_path}


def run(home, *argv, check=True):
    env = {"XDG_CONFIG_HOME": str(home["cfg"]), "PATH": "/usr/bin:/bin", "HOME": str(home["tmp"]),
           "GEMINI_API_KEY": "test-key-not-used"}  # fmt: skip
    p = subprocess.run([sys.executable, "-m", "deepresearch", *argv], capture_output=True,
                       text=True, env=env, timeout=120)  # fmt: skip
    if check:
        assert p.returncode == 0, p.stderr + p.stdout
    return p


def test_create_list_show_add_remove_round_trip(home):
    out = json.loads(
        run(
            home,
            "projects",
            "create",
            "Coral Paper",
            "--report",
            str(home["a"]),
            "--json",
        ).stdout
    )
    pid = out["id"]
    lst = json.loads(run(home, "projects", "list", "--json").stdout)
    assert [p["title"] for p in lst["projects"]] == ["Coral Paper"] and lst[
        "inbox"
    ] == 1
    run(home, "projects", "add", "coral", "--report", str(home["b"]))
    d = json.loads(run(home, "projects", "show", str(pid), "--json").stdout)
    assert sorted(r["id"] for r in d["reports"]) == sorted([home["a"], home["b"]])
    assert "claims" in d
    run(home, "projects", "remove", "Coral Paper", "--report", str(home["b"]))
    d = json.loads(run(home, "projects", "show", "Coral", "--json").stdout)
    assert [r["id"] for r in d["reports"]] == [home["a"]]
    # the report itself still exists: remove is membership only
    with sqlite3.connect(home["db"]) as c:
        assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2


def test_export_formats_and_folder_paths(home):
    run(home, "projects", "create", "Coral Paper", "--report", str(home["a"]))
    out = home["tmp"] / "exports"
    p = run(
        home, "projects", "export", "coral", "-f", "md", "-o", str(out) + "/", "--json"
    )
    info = json.loads(p.stdout)
    assert info["path"].endswith("coral-paper.md") and (out / "coral-paper.md").exists()
    run(home, "projects", "export", "coral", "-f", "zip", "-o", str(out))
    assert (out / "coral-paper.zip").stat().st_size > 0
    bib = run(home, "projects", "export", "coral", "-f", "bib", "-o", "-").stdout
    assert bib.startswith("% Sources cited in the deep-research project")
    bad = run(home, "projects", "export", "coral", "-f", "zip", "-o", "-", check=False)
    assert bad.returncode == 1 and "cannot go to stdout" in bad.stderr


def test_ambiguous_and_missing_projects_fail_cleanly(home):
    run(home, "projects", "create", "Coral A")
    run(home, "projects", "create", "Coral B")
    p = run(home, "projects", "show", "coral", "--json", check=False)
    assert p.returncode == 1 and "matches 2 projects" in json.loads(p.stdout)["error"]
    p = run(home, "projects", "show", "nothing", check=False)
    assert p.returncode == 1 and "no project matches" in p.stderr
    p = run(home, "projects", "add", "Coral A", check=False)
    assert p.returncode == 1 and "nothing to do" in p.stderr


def test_projects_is_a_command_not_a_paid_research_prompt(home):
    """An unknown first word becomes a research prompt (a paid run); `projects` must not."""
    run(home, "projects")
    with sqlite3.connect(home["db"]) as c:
        assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2


def test_workspace_flag_selects_the_workspace(home):
    run(home, "workspace", "create", "Demo")
    run(home, "-W", "demo", "projects", "create", "Only In Demo")
    assert json.loads(run(home, "projects", "list", "--json").stdout)["projects"] == []
    demo = json.loads(run(home, "-W", "demo", "projects", "list", "--json").stdout)[
        "projects"
    ]
    assert [p["title"] for p in demo] == ["Only In Demo"]

"""Copy into another workspace (v0.42.0): whole trees, remapped ids, nothing changed in
the source, all-or-nothing in the target."""

import json
import sqlite3
from pathlib import Path

import pytest

from deepresearch.core import workspace as W
from deepresearch.core import wscopy
from deepresearch.core.session import SessionManager
from deepresearch.dashboard.lab import Lab
from deepresearch.dashboard.projects import ProjectStore
from deepresearch.dashboard.store import DashboardStore
from deepresearch.sources.model import DataSource
from deepresearch.sources.registry import SourceRegistry
from tests.dashboard.test_lab import PLAN


@pytest.fixture
def two(tmp_path, monkeypatch):
    """Main with a project, a report tree, notes, Lab runs (+outputs), a source, a
    notebook; and an empty Demo."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    base = tmp_path / "cfg" / "deepresearch"
    base.mkdir(parents=True)
    db = str(base / "history.db")
    sm = SessionManager(db)
    # a few unrelated rows first so ids differ between workspaces
    for i in range(3):
        sm.create_session(f"x{i}", f"unrelated {i}")
    root = sm.create_session("i-root", "Coral extinction risk")
    child = sm.create_session("i-child", "sub question", parent_id=root, depth=2)
    for sid, text in (
        (root, "Root report. See Session #%d." % child),
        (child, "Child text"),
    ):
        with sqlite3.connect(db) as c:
            c.execute(
                "UPDATE sessions SET result=?, status='completed' WHERE id=?",
                (text, sid),
            )
    store = DashboardStore(db)
    store.create_annotation(root, "Root report", note="my note")
    reg = SourceRegistry(db)
    src = reg.add(DataSource(name="reef-data", kind="web", uri="https://example.org/x.csv",
                             options={"store": "fileSearchStores/abc", "store_hash": "h"}))  # fmt: skip
    reg.record_use(src, "session", root)
    lab = Lab(db, lambda: None, base, targets={})
    r1 = lab.create(root, "document", "x", "", plan={**PLAN, "title": "first"})
    lab._update(r1["id"], status="completed", verdict=json.dumps({"pass": False, "checks": []}),
                result_md="**Result**\n\nSee Lab run #%d and Session #%d." % (r1["id"], root))  # fmt: skip
    r2 = lab.create(
        root, "document", "x", "", rerun_of=r1["id"], plan={**PLAN, "title": "rerun"}
    )
    lab._update(r2["id"], status="running", job_id="99")
    store.create_annotation(
        root, "Root report", note=f"Lab run #{r1['id']} (first): REFUTED."
    )
    out = base / "lab" / f"run_{r1['id']}" / "outputs"
    out.mkdir(parents=True)
    (out / "result.txt").write_text("42")
    ps = ProjectStore(db)
    proj = ps.create("Coral project")
    ps.add_item(proj["id"], "session", root, home=True)
    nb = store.create_notebook("Coral notes", "Notes on Session #%d" % root)
    ps.add_item(proj["id"], "notebook", nb["id"])
    ps.add_item(proj["id"], "source", src.id)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE projects SET summary=? WHERE id=?",
                  (f"Summary citing Session #{root} and Lab run #{r1['id']}.", proj["id"]))  # fmt: skip
    demo = W.create("Demo")
    SessionManager(demo.db_path).create_session("d0", "demo's own report")
    return {"db": db, "base": base, "root": root, "child": child, "r1": r1["id"],
            "r2": r2["id"], "proj": proj["id"], "nb": nb["id"], "src": src.id, "demo": demo}  # fmt: skip


def _dump(db: str) -> dict:
    c = sqlite3.connect(db)
    out = {}
    for (t,) in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        out[t] = c.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
    c.close()
    return out


def test_plan_counts_the_whole_tree(two):
    p = wscopy.plan(two["db"], projects=[two["proj"]])
    assert p.counts() == {
        "projects": 1,
        "reports": 2,
        "lab_runs": 2,
        "sources": 1,
        "notebooks": 1,
    }
    # a sub-report pulls in its root
    p2 = wscopy.plan(two["db"], sessions=[two["child"]])
    assert set(p2.sessions) == {two["root"], two["child"]}


def test_copy_project_remaps_everything_and_leaves_source_alone(two):
    before = _dump(two["db"])
    files_before = sorted(str(p) for p in (two["base"] / "lab").rglob("*"))
    res = wscopy.copy("main", "demo", projects=[two["proj"]])
    assert _dump(two["db"]) == before  # the source DB is byte-for-byte the same data
    assert sorted(str(p) for p in (two["base"] / "lab").rglob("*")) == files_before
    sm, lm = res["reports"], res["lab_runs"]
    new_root, new_child = sm[two["root"]], sm[two["child"]]
    assert new_root != two["root"]  # demo already had a report, ids differ
    d = sqlite3.connect(two["demo"].db_path)
    d.row_factory = sqlite3.Row
    assert (
        d.execute("SELECT parent_id FROM sessions WHERE id=?", (new_child,)).fetchone()[
            0
        ]
        == new_root
    )
    assert d.execute("SELECT result FROM sessions WHERE id=?", (new_root,)).fetchone()[
        0
    ] == (
        "Root report. See Session #%d." % two["child"]
    )  # report text itself is never rewritten
    # Lab runs: session, rerun_of, in-flight -> draft, write-up renumbered
    r1 = d.execute("SELECT * FROM lab_runs WHERE id=?", (lm[two["r1"]],)).fetchone()
    r2 = d.execute("SELECT * FROM lab_runs WHERE id=?", (lm[two["r2"]],)).fetchone()
    assert r1["session_id"] == new_root and r1["status"] == "completed"
    assert (
        f"Lab run #{lm[two['r1']]}" in r1["result_md"]
        and f"Session #{new_root}" in r1["result_md"]
    )
    assert r2["rerun_of"] == lm[two["r1"]]
    assert (
        r2["status"] == "draft" and r2["job_id"] is None
    )  # never watch a job it did not submit
    # outputs copied under the new run number
    assert (
        two["demo"].lab_dir / f"run_{lm[two['r1']]}" / "outputs" / "result.txt"
    ).read_text() == "42"
    # notes: both, with the Lab note renumbered
    notes = [
        r["note"]
        for r in d.execute(
            "SELECT note FROM annotations WHERE session_id=?", (new_root,)
        )
    ]
    assert "my note" in notes and f"Lab run #{lm[two['r1']]} (first): REFUTED." in notes
    # project, memberships, summary, notebook
    pid = res["projects"][two["proj"]]
    items = {
        (r["kind"], r["ref_id"], r["is_home"])
        for r in d.execute("SELECT * FROM project_items WHERE project_id=?", (pid,))
    }
    assert ("session", new_root, 1) in items
    assert ("notebook", res["notebooks"][two["nb"]], 0) in items
    assert ("source", res["sources"][two["src"]], 0) in items
    summ = d.execute("SELECT summary FROM projects WHERE id=?", (pid,)).fetchone()[0]
    assert f"Session #{new_root}" in summ and f"Lab run #{lm[two['r1']]}" in summ
    nb = d.execute(
        "SELECT content FROM notebooks WHERE id=?", (res["notebooks"][two["nb"]],)
    ).fetchone()[0]
    assert nb == f"Notes on Session #{new_root}"
    # data source copied without its Gemini index; provenance kept
    opts = json.loads(
        d.execute("SELECT options FROM data_sources WHERE name='reef-data'").fetchone()[
            0
        ]
    )
    assert "store" not in opts and "store_hash" not in opts
    assert (
        d.execute(
            "SELECT COUNT(*) FROM data_source_uses WHERE used_by_kind='session' AND used_by_id=?",
            (new_root,),
        ).fetchone()[0]
        == 1
    )
    # demo's own report untouched
    assert (
        d.execute("SELECT prompt FROM sessions WHERE id=1").fetchone()[0]
        == "demo's own report"
    )


def test_same_named_source_in_target_is_reused_not_overwritten(two):
    reg = SourceRegistry(two["demo"].db_path)
    reg.add(DataSource(name="reef-data", kind="web", uri="https://other.example/y.csv"))
    res = wscopy.copy("main", "demo", sessions=[two["root"]])
    assert res["sources_reused"] == ["reef-data"]
    d = sqlite3.connect(two["demo"].db_path)
    assert (
        d.execute("SELECT uri FROM data_sources WHERE name='reef-data'").fetchone()[0]
        == "https://other.example/y.csv"
    )
    assert d.execute("SELECT COUNT(*) FROM data_sources").fetchone()[0] == 1


def test_failed_copy_leaves_target_unchanged(two, monkeypatch):
    before = _dump(two["demo"].db_path)
    real = wscopy._insert
    calls = {"n": 0}

    def boom(dst, table, row, skip=("id",)):
        calls["n"] += 1
        if table == "projects":
            raise RuntimeError("disk full")
        return real(dst, table, row, skip)

    monkeypatch.setattr(wscopy, "_insert", boom)
    with pytest.raises(RuntimeError):
        wscopy.copy("main", "demo", projects=[two["proj"]])
    after = _dump(two["demo"].db_path)
    # the schema step may add empty tables; no data row may appear or change
    assert {t: r for t, r in after.items() if r} == {
        t: r for t, r in before.items() if r
    }
    assert not any(p.name.startswith("run_") for p in two["demo"].lab_dir.iterdir())


def test_copy_refuses_bad_requests(two):
    with pytest.raises(wscopy.CopyError):
        wscopy.copy("main", "main", sessions=[two["root"]])
    with pytest.raises(wscopy.CopyError):
        wscopy.copy("main", "demo")
    with pytest.raises(wscopy.CopyError):
        wscopy.copy("main", "demo", projects=[999])
    with pytest.raises(W.WorkspaceError):
        wscopy.copy("main", "nope", sessions=[two["root"]])
    W.update("demo", archived=True)
    with pytest.raises(wscopy.CopyError):
        wscopy.copy("main", "demo", sessions=[two["root"]])


def test_copy_back_and_forth_is_independent(two):
    res = wscopy.copy("main", "demo", sessions=[two["root"]])
    new_root = res["reports"][two["root"]]
    # changing the copy does not touch Main
    with sqlite3.connect(two["demo"].db_path) as d:
        d.execute("UPDATE sessions SET prompt='changed' WHERE id=?", (new_root,))
    with sqlite3.connect(two["db"]) as c:
        assert (
            c.execute(
                "SELECT prompt FROM sessions WHERE id=?", (two["root"],)
            ).fetchone()[0]
            == "Coral extinction risk"
        )
    assert Path(two["demo"].db_path) != Path(two["db"])

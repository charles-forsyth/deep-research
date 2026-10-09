"""`deep-research lab ...` against a real dashboard API on a local port, with the fake
cluster and canned model replies from test_lab. No network, no Gemini, no Slurm."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from deepresearch import __main__ as cli_main
from deepresearch.cli import lab as labcli
from deepresearch.dashboard import daemon
from deepresearch.dashboard.lab import Lab
from tests.dashboard.test_lab import PLAN, FakeTarget
from tests.dashboard.test_server import _seed, app  # noqa: F401  (fixture)


def _run(monkeypatch, capsys, argv, stdin_tty=False):
    monkeypatch.setattr("sys.argv", ["deep-research", *argv])
    monkeypatch.setattr("sys.stdin.isatty", lambda: stdin_tty, raising=False)
    code = 0
    try:
        cli_main.main()
    except SystemExit as e:
        code = int(e.code or 0)
    out, err = capsys.readouterr()
    try:
        return json.loads(out), err, code
    except ValueError:
        return out, err, code


@pytest.fixture
def dash(app, monkeypatch, tmp_path):  # noqa: F811
    """The test dashboard as 'the running dashboard', with a fake cluster."""
    api = app["api"]
    fake = FakeTarget()
    api.lab.targets = {"fake": fake}
    api.lab.results_dir = tmp_path / "labres"
    api.lab.ensure_watcher = lambda: None  # tests drive poll() themselves
    replies: list[str] = []
    api.lab._ask = lambda prompt, search: (replies.pop(0), 0.02)
    # make_plan normally runs the referee too; it is off in tests (conftest)
    monkeypatch.setattr(
        daemon,
        "read_state",
        lambda: {"pid": 1, "host": "127.0.0.1", "port": app["port"]},
    )
    sid = _seed(api, "Aspirin", "Aspirin MW is 180.")
    return {"api": api, "fake": fake, "replies": replies, "sid": sid}


def _draft(dash) -> int:
    dash["replies"].append(json.dumps(PLAN))
    run = dash["api"].lab.create(dash["sid"], "document", "Aspirin MW is 180.")
    dash["api"].lab.make_plan(run["id"], "Aspirin")
    assert dash["api"].lab.get(run["id"])["status"] == "draft"
    return run["id"]


def test_plan_wait_returns_the_reviewed_draft(dash, monkeypatch, capsys):
    monkeypatch.setattr(labcli, "POLL_SECONDS", 0.05)
    dash["replies"].append(json.dumps(PLAN))
    d, _, code = _run(
        monkeypatch,
        capsys,
        [
            "lab",
            "plan",
            str(dash["sid"]),
            "--request",
            "compute MW",
            "--wait",
            "--json",
        ],
    )
    assert code == 0, d
    assert d["status"] == "draft" and d["title"] == "Aspirin descriptors"
    assert d["plan_busy"] is False
    assert d["estimate_usd"] and d["resources"]["partition"] == "standard"
    assert "script" not in d  # the brief view leaves the script out
    run = dash["api"].lab.get(d["id"])
    assert run["request"] == "compute MW" and run["scope"] == "document"


def test_plan_suggestion_needs_suggestions(dash, monkeypatch, capsys):
    d, _, code = _run(
        monkeypatch,
        capsys,
        ["lab", "plan", str(dash["sid"]), "--suggestion", "1", "--json"],
    )
    assert code == 2 and "lab suggestions" in d["error"]


def test_plan_suggestion_uses_the_chosen_one(dash, monkeypatch, capsys):
    with sqlite3.connect(dash["api"].db_path) as c:
        c.execute(
            "INSERT INTO lab_suggestions VALUES (?, ?, 0.01, '2026-10-09')",
            (
                dash["sid"],
                json.dumps(
                    {
                        "suggestions": [
                            {"title": "A", "question": "qa", "approach": "xa"},
                            {"title": "B", "question": "qb", "approach": "xb"},
                        ]
                    }
                ),
            ),
        )
    dash["api"].lab.make_plan = lambda rid, title: None  # keep it in planning
    d, _, code = _run(
        monkeypatch,
        capsys,
        ["lab", "plan", str(dash["sid"]), "--suggestion", "2", "--json"],
    )
    assert code == 0 and d["status"] == "planning"
    run = dash["api"].lab.get(d["id"])
    assert run["scope"] == "suggestion" and run["request"].startswith("B: qb")
    s, _, _ = _run(
        monkeypatch, capsys, ["lab", "suggestions", str(dash["sid"]), "--json"]
    )
    assert [x["title"] for x in s["suggestions"]] == ["A", "B"]


def test_plan_busy_while_planning_thread_runs(dash):
    rid = _draft(dash)
    gate = threading.Event()
    lab = dash["api"].lab
    orig = Lab._make_plan
    lab._make_plan = lambda r, t: gate.wait(5)  # type: ignore[method-assign]
    th = threading.Thread(target=lab.make_plan, args=(rid, "x"))
    th.start()
    try:
        for _ in range(50):
            if lab.plan_busy(rid):
                break
            time.sleep(0.01)
        assert lab.plan_busy(rid)
        assert dash["api"].lab_get(str(rid), {}, None)["plan_busy"] is True
    finally:
        gate.set()
        th.join()
    assert not lab.plan_busy(rid)
    lab._make_plan = orig.__get__(lab)  # type: ignore[method-assign]


def test_list_show_submit_log_cancel(dash, monkeypatch, capsys):
    rid = _draft(dash)
    rows, _, code = _run(monkeypatch, capsys, ["lab", "list", "--json"])
    assert code == 0 and [r["id"] for r in rows] == [rid]
    assert rows[0]["status"] == "draft" and rows[0]["title"] == "Aspirin descriptors"
    rows, _, _ = _run(
        monkeypatch, capsys, ["lab", "list", "--status", "active", "--json"]
    )
    assert rows == []

    # submit spends money: --json without --yes is refused and changes nothing
    d, _, code = _run(monkeypatch, capsys, ["lab", "submit", str(rid), "--json"])
    assert code == 2 and "--yes" in d["error"]
    assert dash["api"].lab.get(rid)["status"] == "draft"
    # and a person who answers no changes nothing either
    monkeypatch.setattr("sys.stdin.readline", lambda: "n\n")
    out, _, code = _run(
        monkeypatch, capsys, ["lab", "submit", str(rid)], stdin_tty=True
    )
    assert code == 1 and dash["api"].lab.get(rid)["status"] == "draft"

    d, _, code = _run(
        monkeypatch, capsys, ["lab", "submit", str(rid), "--yes", "--json"]
    )
    assert code == 0 and d["status"] == "queued" and d["job_id"] == "4242"
    assert rid in dash["fake"].submitted

    d, _, code = _run(
        monkeypatch, capsys, ["lab", "submit", str(rid), "--yes", "--json"]
    )
    assert code == 1 and "only a reviewed draft" in d["error"]

    d, _, _ = _run(monkeypatch, capsys, ["lab", "log", str(rid), "--json"])
    assert d["text"] == "hello" and d["source"] == "cluster"

    import os

    from deepresearch.core import workspace as W

    os.makedirs(W.base_dir(), exist_ok=True)
    monkeypatch.setattr(  # `lab status` reads the workspace DB, here the test dashboard's
        W.Workspace, "db_path", property(lambda self: dash["api"].db_path)
    )
    st, _, _ = _run(monkeypatch, capsys, ["lab", "status", "--json"])
    assert [x["id"] for x in st["active"]] == [rid]

    d, _, code = _run(
        monkeypatch, capsys, ["lab", "cancel", str(rid), "--yes", "--json"]
    )
    assert code == 0 and d["status"] == "cancelled"
    assert dash["fake"].cancelled == ["4242"]


def test_show_finished_run_has_outcome_and_files(dash, monkeypatch, capsys):
    rid = _draft(dash)
    dash["api"].lab.submit(rid)
    dash["fake"].state = "COMPLETED"
    dash["replies"].append("**Result** MW 180.16")
    dash["api"].lab.poll(dash["api"].lab.get(rid))
    d, _, code = _run(monkeypatch, capsys, ["lab", "show", str(rid), "--json"])
    assert code == 0 and d["status"] == "completed"
    assert "outputs/result.txt" in d["files"] and "180.16" in d["result_md"]
    full, _, _ = _run(
        monkeypatch, capsys, ["lab", "show", str(rid), "--full", "--json"]
    )
    assert "script" in full and "#SBATCH" in full["script"]
    out, _, code = _run(monkeypatch, capsys, ["lab", "show", str(rid)])
    assert code == 0 and "Lab run" in out and "Aspirin descriptors" in out


def test_missing_run_is_an_error(dash, monkeypatch, capsys):
    d, _, code = _run(monkeypatch, capsys, ["lab", "show", "999", "--json"])
    assert code == 1 and "not found" in d["error"]


def test_workspace_goes_in_the_header(dash, monkeypatch, capsys):
    seen = {}
    real = labcli.urllib.request.urlopen

    def spy(req, timeout=60):
        seen["ws"] = req.get_header("X-dr-workspace")
        return real(req, timeout=timeout)

    monkeypatch.setattr(labcli.urllib.request, "urlopen", spy)
    monkeypatch.setattr(labcli, "_workspace", lambda: "demo")
    _run(monkeypatch, capsys, ["lab", "list", "--json"])
    assert seen["ws"] == "demo"


def test_no_dashboard_is_exit_3_but_status_still_works(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(daemon, "read_state", lambda: None)
    d, _, code = _run(monkeypatch, capsys, ["lab", "list", "--json"])
    assert code == 3 and "dashboard --start" in d["error"]
    d, _, code = _run(monkeypatch, capsys, ["lab", "--json"])
    assert code == 0 and d["active"] == [] and d["workspace"] == "main"


def test_lab_is_a_known_command():
    # otherwise `deep-research lab` would start a paid research run about "lab"
    src = (cli_main.__file__ or "").replace(".pyc", ".py")
    text = open(src).read()
    assert '"lab",' in text[text.index("known_commands") :][:800]


def test_submit_of_a_non_draft_never_asks(dash, monkeypatch, capsys):
    rid = _draft(dash)
    dash["api"].lab.cancel(rid)

    def no_prompt():
        raise AssertionError("must not ask to submit a run that is not a draft")

    monkeypatch.setattr("sys.stdin.readline", no_prompt)
    _, err, code = _run(
        monkeypatch, capsys, ["lab", "submit", str(rid)], stdin_tty=True
    )
    assert code == 1 and "only a reviewed draft" in err


def test_brief_keeps_plan_busy_and_drops_the_script():
    b = labcli.brief({"id": 1, "plan_busy": True, "plan": {"script": "x" * 9000}})
    assert b["plan_busy"] is True and "script" not in json.dumps(b)

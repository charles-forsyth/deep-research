"""Data sources in research runs (--source) and follow-up questions (Ask)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from deepresearch.cli.base import FollowUpRequest
from deepresearch.sources import DataSource, SourceRegistry
from deepresearch.sources import usage
from deepresearch.sources.adapters import SourceError
from deepresearch.sources.service import check


@pytest.fixture
def reg(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "d" / "sub").mkdir(parents=True)
    (home / "d" / "notes.md").write_text("The widget count is 42.")
    (home / "d" / "sub" / "data.csv").write_text("a,b\n1,2\n")
    (home / "d" / "image.bin").write_bytes(b"\x00\x01")
    monkeypatch.setenv("DR_LOCAL_ROOTS", str(home))
    r = SourceRegistry(str(tmp_path / "h.db"))
    check(r, r.add(DataSource(name="docs", kind="local_folder", uri=str(home / "d"))))
    return r


def test_research_uploads_flattens_readable_files(reg, tmp_path):
    paths, notes = usage.research_uploads(usage.resolve(reg, ["docs"]), tmp_path / "w")
    assert len(paths) == 1
    names = sorted(p.name for p in (tmp_path / "w" / "docs__flat").iterdir())
    assert names == ["notes.md", "sub__data.csv"]
    assert "2 file(s), 1 skipped" in notes[0]


def test_research_uploads_refuses_oversized(reg, monkeypatch):
    monkeypatch.setattr(usage, "RESEARCH_MAX_BYTES", 5)
    with pytest.raises(SourceError, match="Lab run"):
        usage.research_uploads(usage.resolve(reg, ["docs"]))


def test_unknown_source_is_an_error(reg):
    with pytest.raises(SourceError, match="does not exist"):
        usage.resolve(reg, ["nope"])


def test_ask_prompt_labels_sources_and_keeps_question(reg):
    p = usage.ask_prompt("How many widgets?", usage.resolve(reg, ["docs"]))
    assert '<data_source name="docs"' in p and "widget count is 42" in p
    assert "a,b" in p and "image.bin" not in p
    assert p.rstrip().endswith("Question: How many widgets?")
    assert usage.ask_prompt("q", []) == "q"


def test_ask_context_respects_total_cap(reg, monkeypatch):
    monkeypatch.setattr(usage, "ASK_MAX_BYTES_TOTAL", 10)
    ctx = usage.ask_context(usage.resolve(reg, ["docs"]))
    assert "more files not included" in ctx


def test_followup_records_only_the_typed_question(tmp_path):
    """Source text goes to the model but never into the saved report."""
    from deepresearch.core.agent import DeepResearchAgent

    agent = DeepResearchAgent.__new__(DeepResearchAgent)
    agent.client = MagicMock()
    agent.config = MagicMock(followup_model="m")
    agent.session_manager = MagicMock()
    agent._log = lambda *a, **k: None
    with patch("deepresearch.core.agent._final_text", return_value="42 widgets"):
        agent.follow_up(
            FollowUpRequest(
                interaction_id="i1",
                prompt="<data_source>SECRET BODY</data_source> Question: how many?",
                display_prompt="how many?",
                sources=["docs"],
            )
        )
    sent = agent.client.interactions.create.call_args.kwargs["input"]
    assert "SECRET BODY" in sent
    saved = agent.session_manager.append_to_result.call_args.args[1]
    assert "**Q: how many?**" in saved and "SECRET BODY" not in saved
    assert "*Data sources: docs*" in saved


def test_cli_research_source_becomes_upload(reg, tmp_path, monkeypatch):
    from deepresearch.cli import commands

    monkeypatch.setattr(commands, "user_db_path", reg.db_path)
    seen = {}

    class Agent:
        def __init__(self, **k):
            pass

        def start_research_poll(self, request):
            seen["uploads"] = request.upload_paths

    monkeypatch.setattr(commands, "DeepResearchAgent", Agent)
    args = MagicMock(
        prompt="p", stores=None, stream=False, format=None, upload=None, output=None,
        adopt_session=None, depth=1, breadth=3, quiet=True, source=["docs"],
    )  # fmt: skip
    commands.handle_research(args)
    assert len(seen["uploads"]) == 1 and seen["uploads"][0].endswith("docs__flat")


def test_api_research_and_ask_accept_sources(reg, tmp_path, monkeypatch):
    from http.server import ThreadingHTTPServer
    import threading
    import urllib.error
    import urllib.request

    from deepresearch.dashboard import server as srv

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    spawned = []
    api = srv.Api(reg.db_path, spawn=lambda a, log: spawned.append(a) or 1)
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

    code, _ = post("/api/research", {"prompt": "p", "data_sources": ["docs"]})
    assert code == 200
    a = spawned[-1]
    assert a[a.index("--source") + 1] == "docs"
    code, err = post("/api/research", {"prompt": "p", "data_sources": ["zzz"]})
    assert code == 400 and "does not exist" in err["error"]

    sid = api.sessions.create_session("iid-9", "prompt", None)
    api.sessions.update_session("iid-9", "completed", "report")
    got = {}

    def fake_follow(self, request):
        got["req"] = request
        api.sessions.append_to_result("iid-9", "\nanswer")

    monkeypatch.setattr(
        "deepresearch.core.agent.DeepResearchAgent.follow_up", fake_follow
    )
    monkeypatch.setattr(
        "deepresearch.core.agent.DeepResearchAgent.__init__", lambda self, **k: None
    )
    code, _ = post(
        f"/api/sessions/{sid}/followup",
        {"prompt": "how many?", "data_sources": ["docs"]},
    )
    assert code == 200
    assert (
        "widget count is 42" in got["req"].prompt
        and got["req"].display_prompt == "how many?"
    )
    assert reg.used_by("session", sid)[0]["name"] == "docs"
    httpd.shutdown()

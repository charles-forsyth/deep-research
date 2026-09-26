from unittest.mock import MagicMock, patch
import pytest
import os
from deepresearch.core.agent import DeepResearchAgent
from deepresearch.core.config import DeepResearchConfig
from deepresearch.cli.base import ResearchRequest, FollowUpRequest


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.file_search_stores.create.return_value.name = "stores/test-store"
    client.files.upload.return_value.name = "files/test-file"
    client.files.upload.return_value.state.name = "ACTIVE"
    return client


def test_agent_initialization(mock_client):
    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "fake_key"}),
        patch("deepresearch.core.agent.genai.Client") as mock_genai_client,
    ):
        agent = DeepResearchAgent()
        assert agent.config.api_key == "fake_key"
        mock_genai_client.assert_called_once_with(api_key="fake_key")


def test_agent_auto_upload_and_cleanup(mock_client):
    config = DeepResearchConfig(api_key="test")
    agent = DeepResearchAgent(config)
    agent.client = mock_client
    agent.file_manager = MagicMock()
    agent.file_manager.create_store_from_paths.return_value = "stores/temp-store"

    req = ResearchRequest(prompt="Base prompt", upload_paths=["doc.pdf"])

    mock_client.interactions = MagicMock()
    mock_client.interactions.create.return_value = []

    agent.start_research_stream(req)

    agent.file_manager.create_store_from_paths.assert_called_with(["doc.pdf"])
    call_args = mock_client.interactions.create.call_args
    assert (
        "IMPORTANT: You have access to a File Search Store" in call_args.kwargs["input"]
    )

    tools = call_args.kwargs["tools"]
    assert tools[0]["file_search_store_names"] == ["stores/temp-store"]
    agent.file_manager.cleanup.assert_called_once()


def test_recursive_research():
    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "fake_key"}),
        patch(
            "deepresearch.core.agent.DeepResearchAgent.start_research_poll"
        ) as mock_poll,
        patch(
            "deepresearch.core.agent.DeepResearchAgent.start_research_stream"
        ) as mock_stream,
        patch("deepresearch.core.agent.DeepResearchAgent.analyze_gaps") as mock_gaps,
        patch(
            "deepresearch.core.agent.DeepResearchAgent.synthesize_findings"
        ) as mock_synth,
        patch(
            "deepresearch.core.agent.SessionManager.create_session"
        ) as mock_create_session,
        patch("deepresearch.core.agent.SessionManager.get_session") as mock_get_session,
        patch("deepresearch.core.agent.SessionManager.update_session"),
    ):
        mock_poll.return_value = "interaction_child"
        mock_stream.return_value = "interaction_root"
        mock_gaps.return_value = ["Q1", "Q2"]
        mock_synth.return_value = "Final Report"
        mock_create_session.return_value = 100
        mock_get_session.return_value = {
            "status": "completed",
            "result": "Initial Report",
            "id": 1,
        }

        agent = DeepResearchAgent()
        req = ResearchRequest(prompt="Topic", depth=2)
        agent.start_recursive_research(req)

        assert mock_stream.call_count == 1
        assert mock_poll.call_count == 2
        mock_gaps.assert_called_once()
        mock_synth.assert_called_once()

        args = mock_synth.call_args
        assert args[0][0] == "Topic"
        assert args[0][1] == "Initial Report"
        assert len(args[0][2]) == 2


def test_deep_research_agent_error_coverage():
    config = DeepResearchConfig(api_key="fake")
    agent = DeepResearchAgent(config)
    agent.client = MagicMock()

    req = ResearchRequest(prompt="Test")

    # start_research_stream exception
    agent.client.interactions.create.side_effect = Exception("API Down")
    interaction_id = agent.start_research_stream(req)
    assert interaction_id is None

    # start_research_poll exception
    agent.client.interactions.create.side_effect = Exception("API Down")
    interaction_id = agent.start_research_poll(req)
    assert interaction_id is None

    # KeyboardInterrupt in stream
    agent.client.interactions.create.side_effect = KeyboardInterrupt()
    agent.start_research_stream(req)

    # KeyboardInterrupt in poll
    agent.client.interactions.create.side_effect = KeyboardInterrupt()
    agent.start_research_poll(req)

    # follow_up exception
    agent.client.interactions.create.side_effect = Exception("API Down")
    agent.follow_up(FollowUpRequest(interaction_id="123", prompt="Test"))

    # analyze_gaps exception
    agent.client.models.generate_content.side_effect = Exception("API Down")
    assert agent.analyze_gaps("prompt", "report") == []

    # synthesize_findings exception
    assert "ERROR: Synthesis failed" in agent.synthesize_findings(
        "prompt", "main", ["sub"]
    )


def test_process_stream_output(capsys):
    agent = DeepResearchAgent(MagicMock())

    event1 = MagicMock(event_type="content.delta")
    event1.delta.type = "text"
    event1.delta.text = "Hello "

    event2 = MagicMock(event_type="content.delta")
    event2.delta.type = "thought_summary"
    event2.delta.content.text = "Thinking..."

    stream = [event1, event2]

    agent._process_stream(stream, [None], [None], [False])

    captured = capsys.readouterr()
    assert "Hello " in captured.out
    assert "[THOUGHT] Thinking..." in captured.out


def test_process_stream_new_schema(capsys):
    """google-genai >= 2.0 steps schema: step.delta + interaction.completed."""
    agent = DeepResearchAgent(MagicMock())
    start = MagicMock(event_type="interaction.created", event_id=None)
    start.interaction.id = "int_1"
    delta = MagicMock(event_type="step.delta", event_id="e2")
    delta.delta.type = "text"
    delta.delta.text = "Report body"
    done = MagicMock(event_type="interaction.completed", event_id="e3")
    ids, last, complete = [None], [None], [False]
    agent._process_stream([start, delta, done], ids, last, complete)
    assert ids[0] == "int_1" and last[0] == "e3" and complete[0] is True
    assert "Report body" in capsys.readouterr().out


def test_final_text_from_steps():
    from deepresearch.core.agent import _final_text

    step = MagicMock(type="model_output")
    step.content = [MagicMock(text="Final "), MagicMock(text="answer")]
    inter = MagicMock(output_text=None, steps=[MagicMock(type="thought"), step])
    assert _final_text(inter) == "Final answer"
    assert _final_text(MagicMock(output_text="direct")) == "direct"


def test_recursive_root_adopts_precreated_row(monkeypatch, tmp_path):
    """`start --depth 2` / dashboard runs must fill the row they pre-created."""
    from unittest.mock import MagicMock

    from deepresearch.cli.base import ResearchRequest
    from deepresearch.core import agent as agent_mod
    from deepresearch.core.config import DeepResearchConfig
    from deepresearch.core.session import SessionManager

    db = str(tmp_path / "h.db")
    monkeypatch.setattr(agent_mod, "SessionManager", lambda: SessionManager(db))
    monkeypatch.setattr(agent_mod.genai, "Client", MagicMock())
    a = agent_mod.DeepResearchAgent(config=DeepResearchConfig(api_key="k"), quiet=True)
    seen = {}

    def fake_stream(req, auto_update_status=True):
        seen["adopt"] = req.adopt_session_id
        a.session_manager.update_session_interaction_id(
            req.adopt_session_id, "iid-root"
        )
        a.session_manager.update_session("iid-root", "completed", "root report")
        return "iid-root"

    monkeypatch.setattr(a, "start_research_stream", fake_stream)
    monkeypatch.setattr(a, "analyze_gaps", lambda *x, **k: [])
    sid = a.session_manager.create_session("pending_start", "q")
    a.start_recursive_research(
        ResearchRequest(prompt="q", depth=2, adopt_session_id=sid)
    )
    assert seen["adopt"] == sid
    row = a.session_manager.get_session(sid)
    assert row["interaction_id"] == "iid-root" and row["result"] == "root report"
    with __import__("sqlite3").connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def _agent(monkeypatch, tmp_path, **cfg):
    from deepresearch.core import agent as agent_mod
    from deepresearch.core.session import SessionManager

    db = str(tmp_path / "h.db")
    monkeypatch.setattr(agent_mod, "SessionManager", lambda: SessionManager(db))
    monkeypatch.setattr(agent_mod.genai, "Client", MagicMock())
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
    return agent_mod.DeepResearchAgent(
        config=DeepResearchConfig(api_key="k", **cfg), quiet=True
    )


def test_slow_child_report_is_kept_in_synthesis(monkeypatch, tmp_path):
    """K3: a child that finished late used to be dropped from the synthesis."""
    import threading

    a = _agent(monkeypatch, tmp_path)
    sm = a.session_manager
    release = threading.Event()

    def fake_stream(req, auto_update_status=True):
        sm.create_session("iid-root", req.prompt)
        sm.update_session("iid-root", "running", "root report")
        return "iid-root"

    def fake_child(self, q, d, max_d, b, req, pid):
        if q == "slow":
            release.wait(5)
        return f"report for {q}"

    monkeypatch.setattr(a, "start_research_stream", fake_stream)
    monkeypatch.setattr(a, "analyze_gaps", lambda *x, **k: ["fast", "slow"])
    monkeypatch.setattr(type(a), "_run_recursive_child_safe", fake_child, raising=True)
    got = {}

    def fake_synth(prompt, main, subs):
        got["subs"] = sorted(subs)
        return "final"

    monkeypatch.setattr(a, "synthesize_findings", fake_synth)
    threading.Timer(0.3, release.set).start()
    a.start_recursive_research(ResearchRequest(prompt="q", depth=2, breadth=2))
    assert got["subs"] == ["report for fast", "report for slow"]
    assert sm.get_session("iid-root")["result"] == "final"


def test_gap_questions_are_capped_at_breadth(monkeypatch, tmp_path):
    a = _agent(monkeypatch, tmp_path)
    sm = a.session_manager

    def fake_stream(req, auto_update_status=True):
        sm.create_session("iid-root", req.prompt)
        sm.update_session("iid-root", "running", "root report")
        return "iid-root"

    calls = []
    monkeypatch.setattr(a, "start_research_stream", fake_stream)
    monkeypatch.setattr(a, "analyze_gaps", lambda *x, **k: ["a", "b", "c", "d", "e"])
    monkeypatch.setattr(
        type(a),
        "_run_recursive_child_safe",
        lambda self, q, *rest: calls.append(q) or f"r-{q}",
    )
    monkeypatch.setattr(a, "synthesize_findings", lambda *x: "final")
    a.start_recursive_research(ResearchRequest(prompt="q", depth=2, breadth=2))
    assert sorted(calls) == ["a", "b"]


def test_poll_times_out_and_cancels_at_google(monkeypatch, tmp_path):
    a = _agent(monkeypatch, tmp_path, task_timeout_min=1)
    from deepresearch.core import agent as agent_mod

    clock = iter(range(0, 100000, 30))  # each monotonic() call advances 30 s
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: next(clock))
    a.client.interactions.create.return_value = MagicMock(id="iid-slow")
    a.client.interactions.get.return_value = MagicMock(
        id="iid-slow", status="in_progress"
    )
    a.start_research_poll(ResearchRequest(prompt="q"))
    a.client.interactions.cancel.assert_called_once_with("iid-slow")
    row = a.session_manager.get_session("iid-slow")
    assert row["status"] == "failed" and "Timed out" in row["result"]


def test_poll_stops_on_any_terminal_status(monkeypatch, tmp_path):
    """K5: 'cancelled' or 'incomplete' used to poll forever."""
    a = _agent(monkeypatch, tmp_path)
    a.client.interactions.create.return_value = MagicMock(id="iid-c")
    a.client.interactions.get.return_value = MagicMock(
        id="iid-c", status="cancelled", error=None
    )
    a.start_research_poll(ResearchRequest(prompt="q"))
    assert a.session_manager.get_session("iid-c")["status"] == "cancelled"


def test_poll_survives_transient_status_errors(monkeypatch, tmp_path):
    a = _agent(monkeypatch, tmp_path)
    done = MagicMock(id="iid-t", status="completed", output_text="the report")
    a.client.interactions.create.return_value = MagicMock(id="iid-t")
    a.client.interactions.get.side_effect = [
        ConnectionError("blip"),
        ConnectionError("blip"),
        done,
    ]
    a.start_research_poll(ResearchRequest(prompt="q"))
    row = a.session_manager.get_session("iid-t")
    assert row["status"] == "completed" and row["result"] == "the report"


def test_stream_end_without_final_event_checks_status(monkeypatch, tmp_path):
    """Google closes long streams; a finished run must not reconnect forever."""
    a = _agent(monkeypatch, tmp_path)
    created = MagicMock(event_type="interaction.created", event_id="e1")
    created.interaction.id = "iid-s"
    a.client.interactions.create.return_value = iter([created])
    a.client.interactions.get.return_value = MagicMock(
        status="completed", output_text="streamed report"
    )
    a.start_research_stream(ResearchRequest(prompt="q"))
    row = a.session_manager.get_session("iid-s")
    assert row["status"] == "completed" and row["result"] == "streamed report"


def test_task_timeout_setting(monkeypatch):
    monkeypatch.setenv("DR_TASK_TIMEOUT_MIN", "0")
    assert DeepResearchConfig(api_key="k").task_timeout_min == 0
    monkeypatch.delenv("DR_TASK_TIMEOUT_MIN")
    assert DeepResearchConfig(api_key="k").task_timeout_min == 180

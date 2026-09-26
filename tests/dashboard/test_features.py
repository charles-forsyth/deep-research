"""v0.17 dashboard features: cost, timeline, map, compare, briefs, audio.

No network: Gemini calls are replaced with fakes.
"""

import io
import json
import wave

import pytest

from deepresearch.dashboard import features as fxm
from deepresearch.dashboard import server as srv
from deepresearch.dashboard.features import Features, usage_cost
from tests.dashboard.test_server import _seed, app  # noqa: F401  (fixture)

REPORT = """# Title

Intro paragraph with a claim [cite: 1] and a [link](https://example.com/x).

## Section

| a | b |
|---|---|
| 1 | 2 |

- bullet one
- bullet two

**Sources:**
1. [nature.com](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AAA)
2. [arxiv.org](https://vertexaisearch.cloud.google.com/grounding-api-redirect/BBB)
"""


def test_usage_cost_uses_cached_rate_and_counts_searches():
    u = {
        "total_input_tokens": 1_000_000,
        "total_cached_tokens": 400_000,
        "total_output_tokens": 50_000,
        "total_thought_tokens": 50_000,
        "total_tool_use_tokens": 0,
        "grounding_tool_count": [{"type": "google_search", "count": 10}],
    }
    c = usage_cost(u)
    # 600k fresh * $2 + 400k cached * $0.20 + 100k out * $12 = 1.2 + 0.08 + 1.2
    assert c["model_usd"] == pytest.approx(2.48)
    assert c["searches"] == 10
    assert c["search_usd_if_over_free"] == pytest.approx(0.14)
    assert usage_cost(None) is None


def test_speakable_strips_markup_citations_urls_and_sources():
    text = Features.speakable(REPORT)
    assert "[cite" not in text and "http" not in text and "**" not in text
    assert "nature.com" not in text  # source list dropped
    assert "Intro paragraph with a claim" in text
    assert "link" in text  # link text kept, URL removed
    assert "bullet one" in text and "- bullet" not in text


def test_chunks_respect_limit_and_keep_all_text():
    para = "Sentence number one is here. " * 60
    text = "\n\n".join([para] * 5)
    parts = Features.chunks(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "".join(parts).replace(" ", "").replace("\n", "") == text.replace(
        " ", ""
    ).replace("\n", "")


def test_source_labels_skip_numeric_link_text():
    md = "see [3](https://a.b) and [nature.com](https://x) [Nature.com](https://y)"
    assert Features.source_labels(md) == {"nature.com"}


def test_usage_endpoint_caches_expired_but_retries_transient(app, monkeypatch):  # noqa: F811
    sid = _seed(app["api"])
    calls = []

    class FakeInteractions:
        def get(self, iid):
            calls.append(iid)
            raise RuntimeError("connection reset")

    class FakeClient:
        interactions = FakeInteractions()

    app["api"].fx._genai = FakeClient()
    st, body = app["call"]("GET", f"/api/sessions/{sid}/usage")
    assert st == 200 and body["usage"] is None and "reset" in body["error"]
    app["call"]("GET", f"/api/sessions/{sid}/usage")
    assert len(calls) == 2  # transient error not cached

    def expired(iid):
        calls.append(iid)
        raise RuntimeError("Error code: 404 - not found")

    FakeInteractions.get = lambda self, iid: expired(iid)
    st, body = app["call"]("GET", f"/api/sessions/{sid}/usage")
    assert "expired" in body["error"]
    app["call"]("GET", f"/api/sessions/{sid}/usage")
    assert len(calls) == 3  # expiry is cached


def test_compare_lists_source_differences(app):  # noqa: F811
    api = app["api"]
    a = _seed(api, "Alpha topic", "Old [x.org](https://x) [both.org](https://b)")
    b = _seed(api, "Beta topic", "New [y.org](https://y) [both.org](https://b)")
    st, body = app["call"]("POST", "/api/compare", {"a": a, "b": b})
    assert st == 200
    assert body["sources_only_a"] == ["x.org"]
    assert body["sources_only_b"] == ["y.org"]
    assert body["sources_shared"] == ["both.org"]
    assert body["summary"] is None


def test_map_links_similar_sessions(app):  # noqa: F811
    api = app["api"]
    ids = [_seed(api, f"{i}xxxx prompt", f"R{i}") for i in range(4)]
    vecs = [[1, 0, 0], [0.95, 0.05, 0], [0, 1, 0], [0, 0.9, 0.1]]
    for sid, v in zip(ids, vecs):
        api.sessions.update_embedding(sid, json.dumps(v))
    st, body = app["call"]("GET", "/api/map")
    assert st == 200 and body["indexed"] == 4
    pairs = {(e["a"], e["b"]) for e in body["edges"]}
    assert (ids[0], ids[1]) in pairs and (ids[2], ids[3]) in pairs
    assert (ids[0], ids[2]) not in pairs
    assert all("x" in n and "y" in n for n in body["nodes"])


def _wav(seconds=1.0, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def test_audio_job_file_and_range(app, monkeypatch, tmp_path):  # noqa: F811
    api = app["api"]
    sid = _seed(api, "Audio topic", REPORT)
    monkeypatch.setattr(api.fx, "audio_dir", tmp_path / "audio")
    monkeypatch.setattr(api.fx, "synthesize", lambda text, voice: _wav(2.0))
    monkeypatch.setattr(
        fxm.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError())
    )
    st, est = app["call"](
        "POST", "/api/audio/estimate", {"kind": "session", "id": sid, "mode": "full"}
    )
    assert st == 200 and est["words"] > 5 and "Charon" in est["voices"]
    st, job = app["call"](
        "POST",
        "/api/audio",
        {"kind": "session", "id": sid, "mode": "full", "voice": "Kore"},
    )
    assert st == 200
    import time

    for _ in range(50):
        st, j = app["call"]("GET", f"/api/audio/jobs/{job['job']}")
        if j["status"] != "running":
            break
        time.sleep(0.05)
    assert j["status"] == "done", j
    res = j["result"]
    assert res["seconds"] == pytest.approx(2.0) and res["voice"] == "Kore"
    assert "path" not in res
    st, lst = app["call"]("GET", f"/api/audio?kind=session&id={sid}")
    assert len(lst["audio"]) == 1
    st, data = app["call"]("GET", f"/api/audio/{res['id']}/file")
    assert st == 200 and data[:4] == b"RIFF"
    # cached second request, no new synthesis
    monkeypatch.setattr(api.fx, "synthesize", lambda *a: pytest.fail("re-synthesized"))
    again = api.fx.make_audio("session", sid, "t", REPORT, "full", "Kore")
    assert again["cached"] is True


def test_audio_rejects_bad_voice(app):  # noqa: F811
    sid = _seed(app["api"])
    st, body = app["call"](
        "POST", "/api/audio", {"kind": "session", "id": sid, "voice": "Nope"}
    )
    assert st == 400


def test_range_request_returns_partial_content(app, monkeypatch, tmp_path):  # noqa: F811
    import urllib.request

    api = app["api"]
    p = tmp_path / "a.wav"
    p.write_bytes(b"0123456789")
    with api.fx._conn() as conn:
        conn.execute(
            "INSERT INTO audio_exports (kind, ref_id, mode, voice, path) VALUES "
            "('session', 1, 'full', 'Charon', ?)",
            (str(p),),
        )
        conn.commit()
    # find the server base URL through a call helper request
    st, _ = app["call"]("GET", "/api/health")
    base = None
    for cell in app["call"].__closure__ or []:
        if isinstance(cell.cell_contents, str) and cell.cell_contents.startswith(
            "http"
        ):
            base = cell.cell_contents
    req = urllib.request.Request(
        base + "/api/audio/1/file", headers={"Range": "bytes=2-5"}
    )
    with urllib.request.urlopen(req) as r:
        assert r.status == 206
        assert r.read() == b"2345"
        assert r.headers["Content-Range"] == "bytes 2-5/10"


def test_start_research_records_run_meta_and_rerun_link(app):  # noqa: F811
    api = app["api"]
    old = _seed(api, "Old question")
    st, r = app["call"](
        "POST", "/api/research", {"prompt": "Old question", "depth": 1, "rerun_of": old}
    )
    assert st == 200
    st, s = app["call"]("GET", f"/api/sessions/{r['id']}")
    assert s["run"]["rerun_of"] == old and s["run"]["estimate_usd"] > 0
    st, o = app["call"]("GET", f"/api/sessions/{old}")
    assert o["reruns"] == [r["id"]]


def test_timeline_parses_timestamped_log(app):  # noqa: F811
    api = app["api"]
    sid = _seed(api)
    log = srv.LOG_DIR / f"session_{sid}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "[10:00:01] [INFO] Starting\n\n[10:00:05] [THOUGHT] Planning the search\n"
        "plain text line\n[ERROR] no time here\n"
    )
    st, t = app["call"]("GET", f"/api/sessions/{sid}/timeline")
    assert st == 200 and len(t["lanes"]) == 1
    kinds = [(e["t"], e["kind"]) for e in t["events"]]
    assert kinds == [("10:00:01", "info"), ("10:00:05", "thought"), (None, "error")]


def test_log_timestamps_env(monkeypatch, capsys):
    from deepresearch.utils.logger import log_message, setup_logger

    monkeypatch.setenv("DR_LOG_TIMESTAMPS", "1")
    log_message(setup_logger(), "\n[THOUGHT] hello")
    out = capsys.readouterr().out
    assert "] [THOUGHT] hello" in out and out.startswith("\n[")


def test_research_failure_before_interaction_marks_adopted_row_failed(
    tmp_path, monkeypatch
):
    """A bad key fails before Google returns an interaction id; the row must not stay running."""
    from unittest.mock import MagicMock

    from deepresearch.cli.base import ResearchRequest
    from deepresearch.core import agent as agent_mod
    from deepresearch.core.config import DeepResearchConfig
    from deepresearch.core.session import SessionManager

    db = str(tmp_path / "h.db")
    monkeypatch.setattr(agent_mod, "SessionManager", lambda: SessionManager(db))
    monkeypatch.setattr(agent_mod.genai, "Client", MagicMock())
    a = agent_mod.DeepResearchAgent(
        config=DeepResearchConfig(api_key="bad"), quiet=True
    )
    a.client.interactions.create.side_effect = RuntimeError("400 API key not valid")
    sid = a.session_manager.create_session("pending_start", "q")
    import sqlite3

    for start in (a.start_research_stream, a.start_research_poll):
        with sqlite3.connect(db) as c:
            c.execute(
                "UPDATE sessions SET status='running', result=NULL WHERE id=?", (sid,)
            )
        start(ResearchRequest(prompt="q", adopt_session_id=sid))
        row = a.session_manager.get_session(sid)
        assert row["status"] == "failed" and "API key not valid" in row["result"]


def test_health_reports_invalid_key(app, monkeypatch):  # noqa: F811
    monkeypatch.setattr(app["api"], "_key_valid", lambda: False)
    st, h = app["call"]("GET", "/api/health?check=1")
    assert st == 200 and h["api_key"] is True and h["api_key_valid"] is False
    st, h = app["call"]("GET", "/api/health")
    assert "api_key_valid" not in h  # cheap default, no network


def test_children_and_server_do_not_run_in_callers_cwd():
    import inspect

    from deepresearch.dashboard import daemon, server

    assert "cwd=str(STATE_DIR)" in inspect.getsource(daemon.start)
    assert "cwd=str(LOG_DIR.parent)" in inspect.getsource(server.detach)

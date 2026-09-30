"""v0.38.2: full report text (multi-part outputs), repair, audio completeness."""

import sqlite3
from types import SimpleNamespace

from deepresearch.core import repair
from deepresearch.core.agent import _final_text
from deepresearch.dashboard import projects as pj
from deepresearch.dashboard.features import Features


def _step(text, kind="model_output"):
    return SimpleNamespace(type=kind, content=[SimpleNamespace(text=text)])


def _interaction(*parts, output_text=None):
    steps = [_step("thinking", "thought")] + [_step(p) for p in parts]
    return SimpleNamespace(steps=steps, output_text=output_text or parts[-1])


def test_final_text_joins_every_model_output_part():
    it = _interaction(
        "# Title\n\nPart one. ", "Part two. ", "Part three.\n\n**Sources:**\n1. x"
    )
    # the SDK's output_text holds only the last part; that was the bug
    assert _final_text(it).startswith("# Title\n\nPart one. Part two. Part three.")
    assert _final_text(SimpleNamespace(steps=[], output_text="only")) == "only"


def test_repair_plan_restores_cut_reports_and_keeps_followups():
    parts = ["# T\n\nA. ", "B. ", "C."]
    fu = "\n\n---\n### Follow-up (2026-09-30 10:00)\n\n**Q: why?**\n\nBecause."
    p = repair.plan_one("C." + fu, parts)
    assert p["action"] == "repair" and p["new"] == "# T\n\nA. B. C." + fu
    assert (
        p["kept_followups"]
        and p["before"] == 2
        and p["after"] == len("# T\n\nA. B. C.")
    )
    assert repair.plan_one("# T\n\nA. B. C.", parts)["action"] == "ok"
    assert repair.plan_one("anything", ["one part"])["action"] == "ok"
    s = repair.plan_one("# Synthesized from children", parts)
    assert s["action"] == "skip" and s["full_main"] == "# T\n\nA. B. C."


def test_repair_scan_and_apply_on_a_db(tmp_path):
    db = str(tmp_path / "h.db")
    with sqlite3.connect(db) as c:
        c.execute(
            "CREATE TABLE sessions (id INTEGER PRIMARY KEY, interaction_id TEXT, prompt TEXT, "
            "status TEXT, result TEXT, parent_id INTEGER, embedding TEXT)"
        )
        c.execute(
            "INSERT INTO sessions VALUES (1,'i1','q','completed','C.',NULL,'[1]')"
        )
        c.execute(
            "INSERT INTO sessions VALUES (2,'i2','q','completed','whole',NULL,'[1]')"
        )
        c.execute(
            "INSERT INTO sessions VALUES (3,'gone','q','completed','x',NULL,'[1]')"
        )

    class FakeClient:
        class interactions:  # noqa: N801
            @staticmethod
            def get(iid):
                if iid == "gone":
                    raise RuntimeError("Error code: 404 - not found")
                return (
                    _interaction("A. ", "B. ", "C.")
                    if iid == "i1"
                    else _interaction("whole")
                )

    plans = repair.scan(db, FakeClient())
    assert [p["action"] for p in plans] == ["repair", "ok", "gone"]
    assert repair.apply(db, plans) == 1
    with sqlite3.connect(db) as c:
        rows = dict(c.execute("SELECT id, result FROM sessions").fetchall())
        emb = c.execute("SELECT embedding FROM sessions WHERE id=1").fetchone()[0]
    assert rows[1] == "A. B. C." and rows[2] == "whole" and emb is None


def test_summary_briefing_scales_with_report_length():
    short = "word " * 1000
    long = "word " * 12000
    fx = Features.__new__(Features)
    assert fx.estimate_audio(short, "summary")["words"] == 400
    assert fx.estimate_audio(long, "summary")["words"] == 800


def test_lab_findings_carry_write_ups_into_summaries():
    runs = [
        {
            "id": 97,
            "session_id": 290,
            "status": "completed",
            "plan": {"title": "Coral clustering"},
            "verdict": {"pass": True, "checks": [{"pass": True}]},
            "result_md": "Clustering rescued populations: 85% vs 1.3% extinction.",
        },
        {"id": 99, "session_id": 290, "status": "running", "plan": {}},
    ]
    text = pj.lab_findings(runs)
    assert "Lab run #97" in text and "85% vs 1.3%" in text and "#99" not in text
    prompt = pj.summary_prompt(
        {"title": "P"}, [{"id": 290, "prompt": "q", "result": "r"}], runs
    )
    assert "85% vs 1.3%" in prompt


def test_audio_is_remade_when_the_report_text_changes(tmp_path, monkeypatch):
    fx = Features(str(tmp_path / "h.db"), lambda: None, tmp_path / "audio")
    calls = []

    def fake_synth(text, voice):
        import io
        import wave

        calls.append(text)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\x00\x00" * 2400)
        return buf.getvalue()

    monkeypatch.setattr(fx, "synthesize", fake_synth)
    a = fx.make_audio("session", 1, "t", "Short cut report.", "full", "Charon")
    b = fx.make_audio("session", 1, "t", "Short cut report.", "full", "Charon")
    c = fx.make_audio(
        "session", 1, "t", "The full repaired report. Much longer.", "full", "Charon"
    )
    assert not a["cached"] and b["cached"] and not c["cached"]
    assert len(calls) == 2 and "repaired" in calls[1]

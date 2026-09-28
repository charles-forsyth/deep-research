"""Provenance fingerprints for reports and Lab runs."""

import json

from deepresearch.sources import DataSource, SourceRegistry
from deepresearch.sources.model import Manifest, ManifestEntry
from deepresearch.sources.provenance import (
    fingerprint,
    lab_provenance,
    session_provenance,
)


def _reg(tmp_path):
    return SourceRegistry(str(tmp_path / "h.db"))


def _src(reg, name, h):
    m = Manifest.build([ManifestEntry(path="a.csv", size=1, modified=h)])
    return reg.add(
        DataSource(name=name, kind="web", uri=f"https://x/{name}", manifest=m)
    )


def test_fingerprint_is_stable_and_order_independent():
    assert fingerprint({"a": 1, "b": [1, 2]}) == fingerprint({"b": [1, 2], "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
    assert len(fingerprint({})) == 16


def test_session_fingerprint_changes_with_source_content(tmp_path):
    reg = _reg(tmp_path)
    s = _src(reg, "obs", "v1")
    sess = {"id": 7, "prompt": "p", "files": "[]", "interaction_id": "i"}
    empty = session_provenance(reg.db_path, sess)
    assert empty["sources"] == []
    reg.record_use(s, "session", 7)
    p1 = session_provenance(reg.db_path, sess)
    assert (
        p1["sources"][0]["name"] == "obs" and p1["fingerprint"] != empty["fingerprint"]
    )
    reg.record_use(s, "session", 7)  # same content twice: listed once
    assert len(session_provenance(reg.db_path, sess)["sources"]) == 1
    # the source changed upstream: new manifest hash, new fingerprint
    s.manifest = Manifest.build([ManifestEntry(path="a.csv", size=2, modified="v2")])
    reg.update(s)
    reg.record_use(reg.get("obs"), "session", 8)
    p2 = session_provenance(reg.db_path, {**sess, "id": 8})
    assert p2["fingerprint"] != p1["fingerprint"]


def test_session_provenance_without_registry_tables(tmp_path):
    db = str(tmp_path / "empty.db")
    out = session_provenance(
        db, {"id": 1, "prompt": "p", "files": json.dumps(["/nope/x.pdf"])}
    )
    assert out["sources"] == [] and out["uploads"] == [{"name": "x.pdf", "bytes": None}]


def test_lab_fingerprint_tracks_script_and_sources(tmp_path):
    reg = _reg(tmp_path)
    run = {
        "id": 3,
        "plan": {"script": "echo 1", "resources": {"partition": "standard"}},
    }
    a = lab_provenance(reg.db_path, run)["fingerprint"]
    b = lab_provenance(
        reg.db_path, {**run, "plan": {**run["plan"], "script": "echo 2"}}
    )
    assert a != b["fingerprint"]
    reg.record_use(_src(reg, "dd", "v1"), "lab_run", 3)
    assert lab_provenance(reg.db_path, run)["fingerprint"] != a


def test_export_markdown_carries_provenance(tmp_path, monkeypatch):
    from deepresearch.dashboard import server as srv

    db = str(tmp_path / "h.db")
    api = srv.Api(db, spawn=lambda *a: 1)
    sid = api.sessions.create_session("iid-p", "prompt", None)
    api.sessions.update_session("iid-p", "completed", "report")
    api.sources.record_use(_src(api.sources, "obs", "v1"), "session", sid)
    md = api.export_session(str(sid), {"format": ["md"]}, None)["content"]
    assert "*Provenance: inputs fingerprint `" in md and "obs (https://x/obs" in md
    js = api.export_session(str(sid), {"format": ["json"]}, None)["content"]
    assert js["provenance"]["sources"][0]["name"] == "obs"
    got = api.get_session(str(sid), {}, None)
    assert got["provenance"]["fingerprint"] == js["provenance"]["fingerprint"]

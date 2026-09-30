"""Workspace zip export/import (v0.43.0): round trip, privacy scrub, and every check an
untrusted zip must pass before anything is added."""

import hashlib
import io
import json
import sqlite3
import stat
import zipfile
from pathlib import Path

import pytest

from deepresearch.core import workspace as W
from deepresearch.core import wszip
from deepresearch.core.session import SessionManager
from deepresearch.dashboard.lab import Lab
from deepresearch.dashboard.store import DashboardStore
from deepresearch.sources.model import DataSource
from deepresearch.sources.registry import SourceRegistry
from tests.dashboard.test_lab import PLAN


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg" / "deepresearch").mkdir(parents=True)
    d = W.create("Demo", description="for Mike")
    sm = SessionManager(d.db_path)
    sid = sm.create_session("i1", "Coral question")
    with sqlite3.connect(d.db_path) as c:
        c.execute(
            "UPDATE sessions SET result='Report text', status='completed', pid=4242 WHERE id=?",
            (sid,),
        )
    DashboardStore(d.db_path).create_annotation(sid, "Report", note="a note")
    SourceRegistry(d.db_path).add(DataSource(
        name="reef", kind="gcs", uri="gs://bucket/x", auth_ref="adc",
        options={"store": "fileSearchStores/secret-id", "store_hash": "h", "prefix": "x/"}))  # fmt: skip
    lab = Lab(
        d.db_path,
        lambda: None,
        W.base_dir(),
        targets={},
        results_dir=d.lab_dir,
        workspace="demo",
    )
    r = lab.create(sid, "document", "x", "", plan=PLAN)
    lab._update(r["id"], status="running", job_id="77")
    (d.lab_dir / f"run_{r['id']}" / "outputs").mkdir(parents=True)
    (d.lab_dir / f"run_{r['id']}" / "outputs" / "plot.png").write_bytes(b"\x89PNG fake")
    (d.audio_dir / "session_1_summary_Kore.mp3").write_bytes(b"ID3 fake")
    return d


def _dump(db):
    c = sqlite3.connect(db)
    out = {t: c.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
           for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}  # fmt: skip
    c.close()
    return out


def test_round_trip_creates_a_new_workspace(ws, tmp_path):
    before = _dump(ws.db_path)
    man = wszip.export("demo", tmp_path / "out")
    assert _dump(ws.db_path) == before  # exporting never changes the workspace
    z = Path(man["path"])
    assert z.name.endswith(".drws.zip") and man["counts"]["reports"] == 1
    names = zipfile.ZipFile(z).namelist()
    assert "history.db" in names and "manifest.json" in names
    assert any(n.endswith("outputs/plot.png") for n in names)
    assert not any(n.startswith("audio/") for n in names)  # audio off by default
    new = wszip.import_zip(z)
    assert new.slug == "demo-2" and new.name == "Demo" and new.description == "for Mike"
    assert new.extra["imported_from"] == z.name
    d = sqlite3.connect(new.db_path)
    assert d.execute("SELECT prompt, result FROM sessions").fetchone() == (
        "Coral question",
        "Report text",
    )
    assert d.execute("SELECT note FROM annotations").fetchone()[0] == "a note"
    run = d.execute("SELECT status, job_id FROM lab_runs").fetchone()
    assert run == ("draft", None)  # never watch a job this machine did not submit
    assert (
        new.lab_dir / "run_1" / "outputs" / "plot.png"
    ).read_bytes() == b"\x89PNG fake"
    # importing twice gives a third workspace, never overwrites
    again = wszip.import_zip(z, name="Mike's copy")
    assert again.slug == "mike-s-copy" and W.exists("demo-2")


def test_export_scrubs_personal_and_key_specific_fields(ws, tmp_path):
    man = wszip.export("demo", tmp_path)
    with zipfile.ZipFile(man["path"]) as z:
        raw = z.read("history.db")
    p = tmp_path / "x.db"
    p.write_bytes(raw)
    c = sqlite3.connect(p)
    assert c.execute("SELECT pid FROM sessions").fetchone()[0] is None
    auth, opts = c.execute("SELECT auth_ref, options FROM data_sources").fetchone()
    o = json.loads(opts)
    assert (
        auth == ""
        and "store" not in o
        and "store_hash" not in o
        and o["prefix"] == "x/"
    )
    assert b"secret-id" not in raw
    names = zipfile.ZipFile(man["path"]).namelist()
    assert not any(n.endswith(".env") or "lab_targets" in n for n in names)
    # the workspace itself keeps its values
    c2 = sqlite3.connect(ws.db_path)
    assert c2.execute("SELECT pid FROM sessions").fetchone()[0] == 4242
    assert "secret-id" in c2.execute("SELECT options FROM data_sources").fetchone()[0]


def test_audio_is_included_on_request_and_repointed(ws, tmp_path):
    with sqlite3.connect(ws.db_path) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS audio_exports (id INTEGER PRIMARY KEY, kind TEXT, ref_id INTEGER, mode TEXT, voice TEXT, path TEXT, seconds REAL, cost_usd REAL, script TEXT, created_at TEXT, src_hash TEXT)"
        )
        c.execute("INSERT INTO audio_exports (kind, ref_id, mode, voice, path) VALUES ('session', 1, 'summary', 'Kore', ?)",
                  (str(ws.audio_dir / "session_1_summary_Kore.mp3"),))  # fmt: skip
    man = wszip.export("demo", tmp_path, include_audio=True)
    new = wszip.import_zip(man["path"])
    p = (
        sqlite3.connect(new.db_path)
        .execute("SELECT path FROM audio_exports")
        .fetchone()[0]
    )
    assert p == str(new.audio_dir / "session_1_summary_Kore.mp3") and Path(p).exists()
    # without audio the rows are dropped (the files are not there)
    man2 = wszip.export("demo", tmp_path / "b")
    new2 = wszip.import_zip(man2["path"])
    assert (
        sqlite3.connect(new2.db_path)
        .execute("SELECT COUNT(*) FROM audio_exports")
        .fetchone()[0]
        == 0
    )


# ---- untrusted zips ---------------------------------------------------------------


def _zip(
    tmp_path, members: dict, manifest: dict | None = None, name="bad.zip", modes=None
):
    good_db = tmp_path / "good.db"
    if not good_db.exists():
        SessionManager(str(good_db)).create_session("i", "q")
    members = {
        k: (good_db.read_bytes() if v == "DB" else v) for k, v in members.items()
    }
    files = {
        k: {"sha256": hashlib.sha256(v).hexdigest(), "size": len(v)}
        for k, v in members.items()
    }
    man = (
        manifest
        if manifest is not None
        else {
            "format": wszip.FORMAT,
            "format_version": 1,
            "workspace": {"name": "Bad"},
            "files": files,
        }
    )
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("manifest.json", json.dumps(man))
        for k, v in members.items():
            info = zipfile.ZipInfo(k)
            if modes and k in modes:
                info.external_attr = modes[k] << 16
            z.writestr(info, v)
    return p


def _nothing_added():
    return [w.slug for w in W.list_all()] == ["main"]


@pytest.fixture
def empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg" / "deepresearch").mkdir(parents=True)
    return tmp_path


@pytest.mark.parametrize("evil", ["../escape.txt", "lab/../../escape.txt", "/etc/passwd",
                                  "lab\\..\\x", "secrets/key.txt", "notes.txt"])  # fmt: skip
def test_paths_outside_the_workspace_are_refused(empty, evil):
    z = _zip(empty, {"history.db": "DB", evil: b"x"})
    with pytest.raises(wszip.ZipError):
        wszip.import_zip(z)
    assert _nothing_added()
    assert (
        not (empty / "escape.txt").exists()
        and not (empty / "cfg" / "escape.txt").exists()
    )


def test_symlinks_are_refused(empty):
    z = _zip(empty, {"history.db": "DB", "lab/run_1/link": b"/etc/passwd"},
             modes={"lab/run_1/link": stat.S_IFLNK | 0o777})  # fmt: skip
    with pytest.raises(wszip.ZipError, match="links"):
        wszip.import_zip(z)
    assert _nothing_added()


def test_tampered_file_fails_checksum(empty):
    z = _zip(empty, {"history.db": "DB", "lab/run_1/out.txt": b"original"})
    raw = z.read_bytes().replace(b"original", b"ORIGINAL")
    z.write_bytes(raw)
    with pytest.raises((wszip.ZipError, zipfile.BadZipFile)):
        wszip.import_zip(z)
    assert _nothing_added()
    assert not [
        p for p in (empty / "cfg" / "deepresearch" / "workspaces").glob(".import-*")
    ]


def test_manifest_must_match_contents(empty):
    z = _zip(empty, {"history.db": "DB", "lab/x.txt": b"x"},
             manifest={"format": wszip.FORMAT, "format_version": 1, "files": {
                 "history.db": {"sha256": "0", "size": 1}}})  # fmt: skip
    with pytest.raises(wszip.ZipError, match="manifest"):
        wszip.import_zip(z)


def test_not_a_workspace_zip(empty):
    p = empty / "plain.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(wszip.ZipError, match="manifest"):
        wszip.import_zip(p)
    (empty / "notzip.zip").write_text("nope")
    with pytest.raises(wszip.ZipError, match="not a zip"):
        wszip.import_zip(empty / "notzip.zip")
    z = _zip(empty, {"history.db": "DB"}, name="newer.zip",
             manifest={"format": wszip.FORMAT, "format_version": 99, "files": {}})  # fmt: skip
    with pytest.raises(wszip.ZipError, match="newer"):
        wszip.import_zip(z)
    assert _nothing_added()


def test_corrupt_database_is_refused(empty):
    z = _zip(empty, {"history.db": b"SQLite format 3\x00" + b"\x00" * 200})
    with pytest.raises(wszip.ZipError):
        wszip.import_zip(z)
    assert _nothing_added()


def test_zip_bomb_ratio_is_refused(empty, monkeypatch):
    monkeypatch.setattr(wszip, "MAX_RATIO", 5)
    good = empty / "good.db"
    SessionManager(str(good)).create_session("i", "q")
    big = b"\x00" * 1_000_000
    files = {"history.db": {"sha256": hashlib.sha256(good.read_bytes()).hexdigest(), "size": good.stat().st_size},
             "lab/zeros.bin": {"sha256": hashlib.sha256(big).hexdigest(), "size": len(big)}}  # fmt: skip
    p = empty / "bomb.zip"
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "manifest.json",
            json.dumps({"format": wszip.FORMAT, "format_version": 1, "files": files}),
        )
        z.write(good, "history.db")
        z.writestr("lab/zeros.bin", big)
    with pytest.raises(wszip.ZipError, match="ratio"):
        wszip.inspect(p)


def test_dashboard_export_and_streamed_import(ws, monkeypatch, tmp_path):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from deepresearch.dashboard import server as srv

    main_db = str(W.base_dir() / "history.db")
    SessionManager(main_db)
    api = srv.Api(main_db, spawn=lambda *a: 1,
                  lab=Lab(main_db, lambda: None, tmp_path, targets={}), workspaces=True)  # fmt: skip
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(api))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/api/workspaces/demo/export") as r:
            assert r.headers["Content-Type"] == "application/zip"
            data = r.read()
        assert zipfile.ZipFile(io.BytesIO(data)).read("manifest.json")
        req = urllib.request.Request(base + "/api/workspaces/import?name=From%20Mike",
                                     data=data, method="POST",
                                     headers={"Content-Type": "application/zip"})  # fmt: skip
        with urllib.request.urlopen(req) as r:
            out = json.loads(r.read())
        assert out["slug"] == "from-mike" and W.exists("from-mike")
        # wrong content type and cross-origin are refused
        bad = urllib.request.Request(base + "/api/workspaces/import", data=data, method="POST",
                                     headers={"Content-Type": "application/json"})  # fmt: skip
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        assert e.value.code == 415
        xo = urllib.request.Request(base + "/api/workspaces/import", data=data, method="POST",
                                    headers={"Content-Type": "application/zip", "Origin": "http://evil.example"})  # fmt: skip
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(xo)
        assert e.value.code == 403
    finally:
        httpd.shutdown()

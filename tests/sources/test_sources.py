import http.server
import json
import threading
from pathlib import Path

import pytest

from deepresearch.sources import DataSource, SourceRegistry
from deepresearch.sources import adapters as ad
from deepresearch.sources.model import Manifest, ManifestEntry
from deepresearch.sources.service import check


@pytest.fixture
def reg(tmp_path):
    return SourceRegistry(str(tmp_path / "h.db"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    (root / "data" / "sub").mkdir(parents=True)
    (root / "data" / "a.csv").write_text("x,y\n1,2\n")
    (root / "data" / "sub" / "b.txt").write_text("hello")
    (root / "data" / ".hidden").mkdir()
    (root / "data" / ".hidden" / "secret").write_text("nope")
    monkeypatch.setenv("DR_LOCAL_ROOTS", str(root))
    return root


# ---------------------------------------------------------------- model and registry


def test_names_kinds_and_levels_are_validated():
    with pytest.raises(ValueError):
        DataSource(name="Bad Name", kind="web", uri="https://x")
    with pytest.raises(ValueError):
        DataSource(name="ok-name", kind="ftp", uri="ftp://x")
    with pytest.raises(ValueError):
        DataSource(name="ok-name", kind="web", uri="https://x", protection_level="P9")
    s = DataSource(name="noaa-ghcn", kind="web", uri="https://x", protection_level="p3")
    assert s.protection_level == "P3" and s.env_var == "DS_NOAA_GHCN"


def test_default_staging_relay_for_local_and_s3_direct_for_web_and_gcs():
    mk = lambda k, u: DataSource(name="a1", kind=k, uri=u)  # noqa: E731
    assert mk("web", "https://x").effective_staging == "direct"
    assert mk("gcs", "gs://b").effective_staging == "direct"
    assert mk("s3", "s3://b").effective_staging == "relay"  # Ceph needs the VPN
    assert mk("local_folder", "/x").effective_staging == "relay"
    over = DataSource(name="a1", kind="s3", uri="s3://b", staging="direct")
    assert over.effective_staging == "direct"


def test_registry_round_trip_unique_names_uses(reg):
    s = reg.add(
        DataSource(
            name="lab-data",
            kind="gcs",
            uri="gs://b/p",
            tags=["x"],
            options={"include": ["*.csv"]},
            manifest=Manifest.build([ManifestEntry(path="a.csv", size=3)]),
        )
    )
    got = reg.require("lab-data")
    assert got.id == s.id and got.tags == ["x"] and got.options["include"] == ["*.csv"]
    assert got.manifest and got.manifest.file_count == 1
    with pytest.raises(ValueError):
        reg.add(DataSource(name="lab-data", kind="web", uri="https://x"))
    reg.record_use(got, "lab_run", 31)
    assert reg.used_by("lab_run", 31)[0]["name"] == "lab-data"
    assert reg.uses(got)[0]["manifest_hash"] == got.manifest.hash
    reg.add(DataSource(name="tmp-up", kind="web", uri="https://x", temporary=True))
    assert [x.name for x in reg.list()] == ["lab-data"]
    assert len(reg.list(include_temporary=True)) == 2
    assert reg.delete("lab-data") and reg.get("lab-data") is None
    assert reg.used_by("lab_run", 31) == []


def test_manifest_hash_tracks_content():
    a = Manifest.build([ManifestEntry(path="a", size=1, modified="1")])
    b = Manifest.build([ManifestEntry(path="a", size=1, modified="1")])
    c = Manifest.build([ManifestEntry(path="a", size=2, modified="1")])
    assert a.hash == b.hash != c.hash


# ---------------------------------------------------------------- local


def test_local_folder_manifest_browse_preview_fetch(home, tmp_path):
    s = DataSource(name="d1", kind="local_folder", uri=str(home / "data"))
    a = ad.adapter_for(s)
    m = a.manifest()
    assert sorted(e.path for e in m.entries) == ["a.csv", "sub/b.txt"]  # no dotfiles
    assert m.total_bytes == 13 and m.formats["csv"] == 1
    assert {x["name"] for x in a.list()} == {"a.csv", "sub/"}
    # a folder's size counts every file in it, including the first one listed
    assert {x["name"]: x["size"] for x in a.list()}["sub/"] == 5
    assert a.list("sub")[0]["name"] == "b.txt"
    assert a.preview("a.csv").startswith(b"x,y")
    out = a.fetch(tmp_path / "out")
    assert (out / "sub" / "b.txt").read_text() == "hello"


def test_local_include_exclude(home):
    s = DataSource(
        name="d1",
        kind="local_folder",
        uri=str(home / "data"),
        options={"include": ["*.csv"]},
    )
    assert [e.path for e in ad.adapter_for(s).manifest().entries] == ["a.csv"]


def test_local_rejects_outside_roots_traversal_and_symlinks(home, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.txt").write_text("x")
    with pytest.raises(ad.SourceError, match="outside the allowed"):
        ad.adapter_for(
            DataSource(name="o1", kind="local_folder", uri=str(outside))
        ).manifest()
    a = ad.adapter_for(
        DataSource(name="d1", kind="local_folder", uri=str(home / "data"))
    )
    with pytest.raises(ad.SourceError, match="escapes"):
        a.preview("../../elsewhere/x.txt")
    (home / "data" / "link").symlink_to(outside / "x.txt")
    assert "link" not in [e.path for e in a.manifest().entries]
    (home / "escape").symlink_to(outside)
    with pytest.raises(ad.SourceError, match="outside the allowed"):
        ad.adapter_for(
            DataSource(name="e1", kind="local_folder", uri=str(home / "escape"))
        ).manifest()


def test_local_file(home):
    a = ad.adapter_for(
        DataSource(name="f1", kind="local_file", uri=str(home / "data" / "a.csv"))
    )
    assert [e.path for e in a.manifest().entries] == ["a.csv"]
    assert a.preview().startswith(b"x,y")


def test_check_records_status_without_raising(reg, home):
    good = reg.add(DataSource(name="d1", kind="local_folder", uri=str(home / "data")))
    bad = reg.add(DataSource(name="d2", kind="local_folder", uri=str(home / "missing")))
    assert check(reg, good).status == "ok"
    assert reg.require("d1").manifest.file_count == 2
    b = check(reg, bad)
    assert b.status == "unreachable" and "does not exist" in b.last_error


# ---------------------------------------------------------------- web


@pytest.fixture
def web():
    class H(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", "11")
            self.end_headers()

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "11")
            self.end_headers()
            self.wfile.write(b"hello,world")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_web_manifest_preview_fetch_and_direct(web, tmp_path):
    s = DataSource(name="w1", kind="web", uri=f"{web}/data/obs.csv")
    a = ad.adapter_for(s)
    m = a.manifest()
    assert m.entries[0].path == "obs.csv" and m.total_bytes == 11
    assert a.preview() == b"hello,world"
    assert (a.fetch(tmp_path / "w") / "obs.csv").read_bytes() == b"hello,world"
    assert "curl -fsSL" in a.direct_snippet() and "obs.csv" in a.direct_snippet()
    with pytest.raises(ad.SourceError):
        ad.adapter_for(
            DataSource(name="w2", kind="web", uri="file:///etc/passwd")
        ).manifest()


# ---------------------------------------------------------------- gcs / s3 (CLI faked)


def test_gcs_parses_gcloud_listing(monkeypatch):
    calls = []

    def fake(cmd, timeout=60):
        calls.append(cmd)
        return json.dumps(
            [
                {"name": "p/x.csv", "size": "5", "update_time": "t1"},
                {"name": "p/sub/y.parquet", "size": "7", "update_time": "t2"},
            ]
        )

    monkeypatch.setattr(ad, "_run", fake)
    a = ad.adapter_for(DataSource(name="g1", kind="gcs", uri="gs://bkt/p"))
    m = a.manifest()
    assert [e.path for e in m.entries] == [
        "sub/y.parquet",
        "x.csv",
    ] and m.total_bytes == 12
    assert calls[0][:4] == ["gcloud", "storage", "objects", "list"]
    assert "gs://bkt/p/**" in calls[0]
    assert a.direct_snippet().startswith("gcloud storage rsync -r gs://bkt/p ")
    with pytest.raises(ad.SourceError):
        a.preview("../other/secret")


def test_s3_uses_rclone_remote_and_never_shows_credentials(monkeypatch):
    monkeypatch.setattr(
        ad, "_run", lambda cmd, timeout=60: json.dumps([{"Path": "a/b.txt", "Size": 4}])
    )
    s = DataSource(
        name="c1", kind="s3", uri="s3://forsythc-hdd-bucket/x", auth_ref="rclone:ceph"
    )
    a = ad.adapter_for(s)
    assert a.manifest().entries[0].path == "a/b.txt"
    assert a.direct_snippet() == 'rclone copy ceph:forsythc-hdd-bucket/x "$DEST"\n'
    with pytest.raises(ad.SourceError, match="rclone"):
        ad.adapter_for(DataSource(name="c2", kind="s3", uri="s3://b")).manifest()


def test_missing_tool_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(ad.shutil, "which", lambda name: None)
    with pytest.raises(ad.SourceError, match="not installed"):
        ad._run(["gcloud", "version"])


def test_every_kind_has_an_adapter():
    from deepresearch.sources.model import KINDS

    assert set(KINDS) == set(ad.ADAPTERS) | {
        "public_bucket"
    }  # public: sources/public.py


def test_report_source_reads_history(tmp_path):
    import sqlite3

    from deepresearch.storage.database import DatabaseSchema

    db = str(tmp_path / "h.db")
    DatabaseSchema.init_db(db)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO sessions (id, prompt, result) VALUES (7, 'Q', 'Answer')")
    a = ad.adapter_for(
        DataSource(name="r7", kind="report", uri="report:7", options={"db_path": db})
    )
    assert a.manifest().entries[0].path == "report_7.md"
    assert b"Answer" in a.preview()
    assert Path(a.fetch(tmp_path / "o") / "report_7.md").read_text().startswith("# Q")


def test_cli_add_list_show_browse_preview_rm(home, reg, capsys):
    import argparse

    from deepresearch.cli import sources as cli

    p = argparse.ArgumentParser()
    cli.add_parser(p.add_subparsers(dest="command"))

    def run(*argv):
        return cli.handle(p.parse_args(["sources", *argv]), reg)

    assert run("add", "mydata", str(home / "data"), "--tag", "t") == 0
    s = reg.require("mydata")
    assert s.kind == "local_folder" and s.status == "ok" and s.tags == ["t"]
    capsys.readouterr()
    assert run("list", "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["name"] == "mydata" and listed[0]["env_var"] == "DS_MYDATA"
    assert run("browse", "mydata", "--json") == 0
    assert "a.csv" in capsys.readouterr().out
    assert run("preview", "mydata", "a.csv") == 0
    assert "x,y" in capsys.readouterr().out
    assert run("show", "nope") == 1
    assert run("add", "bad", str(home / "missing")) == 2  # saved, marked unreachable
    assert reg.require("bad").status == "unreachable"
    assert run("rm", "mydata") == 0 and reg.get("mydata") is None
    assert (
        cli.guess_kind("gs://b/p", "gcloud") == "gcs"
        and cli.guess_kind("s3://b", "rclone:x") == "s3"
    )
    assert (
        cli.guess_kind("https://x") == "web" and cli.guess_kind("report:3") == "report"
    )


def test_browse_uses_stored_manifest_instead_of_relisting(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ad, "_run", lambda cmd, timeout=60: calls.append(cmd) or json.dumps([])
    )
    m = Manifest.build(
        [ManifestEntry(path="d/a.txt", size=3), ManifestEntry(path="b.txt", size=4)]
    )
    s = DataSource(
        name="c1", kind="s3", uri="s3://b", auth_ref="rclone:ceph", manifest=m
    )
    items = ad.adapter_for(s).list()
    assert [i["name"] for i in items] == ["d/", "b.txt"] and calls == []
    s.manifest = m.model_copy(update={"truncated": True})
    ad.adapter_for(s).list()
    assert calls, "a truncated manifest is re-listed"

"""Data source safety and correctness bugs found in the 2026-09-28 review.

Each test failed on v0.27.0 and passes with the fix.
"""

import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from deepresearch.sources import DataSource, SourceRegistry
from deepresearch.sources import adapters as ad
from deepresearch.sources import public as pb
from deepresearch.sources import staging as st
from deepresearch.sources.model import Manifest, ManifestEntry
from deepresearch.sources.provenance import session_provenance
from deepresearch.sources.service import check


@pytest.fixture
def reg(tmp_path):
    return SourceRegistry(str(tmp_path / "h.db"))


def _listing(*keys: str, truncated: bool = False, token: str = "") -> bytes:
    rows = "".join(
        f"<Contents><Key>{k}</Key><Size>5</Size><LastModified>x</LastModified></Contents>"
        for k in keys
    )
    tok = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
    return (
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>{tok}{rows}"
        "</ListBucketResult>"
    ).encode()


# ------------------------------------------------------------------ public buckets
def test_bucket_keys_never_write_outside_the_download_folder(tmp_path, monkeypatch):
    victim = tmp_path / "victim" / "owned.txt"
    victim.parent.mkdir()
    evil = [
        "data/" + "/" + str(victim).lstrip("/"),  # data//abs/path
        "data/../../escape.txt",
        "data/ok/fine.csv",
    ]

    def http(url, max_bytes=None):
        return _listing(*evil) if "list-type" in url else b"PWNED"

    monkeypatch.setattr(pb, "_http", http)
    s = DataSource(name="evil", kind="public_bucket", uri="s3://evil-bucket/data")
    s.manifest = pb.PublicBucketAdapter(s).manifest()
    assert [e.path for e in s.manifest.entries] == ["ok/fine.csv"]
    dest = tmp_path / "dest"
    pb.PublicBucketAdapter(s).fetch(dest)
    assert not victim.exists() and not (tmp_path / "escape.txt").exists()
    assert (dest / "ok" / "fine.csv").read_bytes() == b"PWNED"
    # a stale manifest carrying a bad path is refused at fetch time too
    s.manifest = Manifest.build([ManifestEntry(path="/etc/passwd", size=1)])
    with pytest.raises(ad.SourceError):
        pb.PublicBucketAdapter(s).fetch(dest)


def test_safe_join_rules(tmp_path):
    assert ad.safe_rel("a//b/./c") == "a/b/c"
    assert ad.safe_rel("/etc/x") == "etc/x"
    assert ad.safe_rel("../x") is None and ad.safe_rel("") is None
    assert ad.safe_rel("C:/x") is None
    with pytest.raises(ad.SourceError):
        ad.safe_join(tmp_path, "a/../../x")


def test_direct_snippet_handles_keys_with_spaces(tmp_path):
    s = DataSource(name="sp", kind="public_bucket", uri="s3://b/p")
    s.manifest = Manifest.build([ManifestEntry(path="my dir/f.csv", size=1)])
    snippet = pb.PublicBucketAdapter(s).direct_snippet()
    assert "--create-dirs" in snippet and "mkdir" not in snippet
    # run it against a fake curl that just records the -o target
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text(
        '#!/bin/bash\nwhile [ $# -gt 0 ]; do if [ "$1" = -o ]; then shift; '
        'mkdir -p "$(dirname "$1")"; echo ok > "$1"; fi; shift; done\n'
    )
    (bindir / "curl").chmod(0o755)
    dest = tmp_path / "DEST"
    dest.mkdir()
    r = subprocess.run(
        ["bash", "-c", f"set -e; DEST={dest}; cd {tmp_path}; {snippet}"],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )
    assert r.returncode == 0, r.stderr
    assert (dest / "my dir" / "f.csv").exists()
    assert not (dest / "my").exists() or (dest / "my dir").exists()
    assert not (tmp_path / "dir").exists()


def test_a_listing_that_ends_on_the_last_page_is_complete(monkeypatch):
    calls = {"n": 0}

    def http(url, max_bytes=None):
        calls["n"] += 1
        more = calls["n"] < pb.MAX_PAGES
        return _listing(f"k{calls['n']}.csv", truncated=more, token="t" if more else "")

    monkeypatch.setattr(pb, "_http", http)
    s = DataSource(name="p50", kind="public_bucket", uri="s3://b")
    m = pb.PublicBucketAdapter(s).manifest()
    assert m.file_count == pb.MAX_PAGES and not m.truncated


def test_a_non_listing_reply_marks_the_source_unreachable(reg, monkeypatch):
    monkeypatch.setattr(pb, "_http", lambda u, max_bytes=None: b"<html>captive portal")
    s = reg.add(DataSource(name="px", kind="public_bucket", uri="s3://b/p"))
    out = check(reg, s)
    assert out.status == "unreachable" and "listing" in out.last_error
    assert reg.get("px").status == "unreachable"


# ------------------------------------------------------------------ s3 / gcs
def _fake_tool(tmp_path, monkeypatch, name, body):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    p = b / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    return p


def test_s3_fetch_passes_the_include_filter_to_rclone(tmp_path, monkeypatch):
    log = tmp_path / "rclone.log"
    _fake_tool(
        tmp_path,
        monkeypatch,
        "rclone",
        textwrap.dedent(
            f"""
            echo "$@" >> {log}
            if [ "$1" = lsjson ]; then
              echo '[{{"Path":"small.csv","Size":10}},{{"Path":"huge.nc","Size":5}}]'
            fi
            """
        ),
    )
    s = DataSource(
        name="ceph",
        kind="s3",
        uri="s3://bkt/p",
        auth_ref="rclone:ceph",
        options={"include": ["*.csv"], "exclude": ["tmp*"]},
    )
    ad.adapter_for(s).fetch(tmp_path / "dest")
    copy = [ln for ln in log.read_text().splitlines() if ln.startswith("copy")][0]
    assert "--filter - tmp* --filter + *.csv --filter - **" in copy
    snip = ad.adapter_for(s).direct_snippet()
    assert "--filter" in snip and "'+ *.csv'" in snip


def test_gcs_and_s3_preview_return_bytes_for_binary_files(tmp_path, monkeypatch):
    _fake_tool(
        tmp_path, monkeypatch, "gcloud", "printf '\\x89PNG\\r\\n\\x1a\\n\\xff\\xfe'\n"
    )
    s = DataSource(name="g1", kind="gcs", uri="gs://b/p", auth_ref="gcloud")
    assert ad.adapter_for(s).preview("img.png").startswith(b"\x89PNG")
    _fake_tool(tmp_path, monkeypatch, "gcloud", "printf 'caf\\xc3'\n")
    assert ad.adapter_for(s).preview("t.csv") == b"caf\xc3"
    _fake_tool(tmp_path, monkeypatch, "rclone", "printf '\\xff\\x00\\x01'\n")
    s3 = DataSource(name="s1", kind="s3", uri="s3://b/p", auth_ref="rclone:ceph")
    assert ad.adapter_for(s3).preview("x.bin") == b"\xff\x00\x01"


def test_gcs_fetch_with_include_copies_only_matching_files(tmp_path, monkeypatch):
    log = tmp_path / "gcloud.log"
    _fake_tool(
        tmp_path,
        monkeypatch,
        "gcloud",
        textwrap.dedent(
            f"""
            echo "$@" >> {log}
            if [ "$3" = list ]; then
              echo '[{{"name":"p/a.csv","size":1}},{{"name":"p/big.nc","size":9}}]'
            fi
            if [ "$2" = cp ]; then echo x > "$4"; fi
            """
        ),
    )
    s = DataSource(
        name="g2",
        kind="gcs",
        uri="gs://b/p",
        auth_ref="gcloud",
        options={"include": ["*.csv"]},
    )
    out = ad.adapter_for(s).fetch(tmp_path / "dest")
    assert (out / "a.csv").exists() and not (out / "big.nc").exists()
    assert "rsync" not in log.read_text()


# ------------------------------------------------------------------ local
def test_local_fetch_never_silently_stops_at_the_listing_cap(tmp_path, monkeypatch):
    root = tmp_path / "home" / "many"
    root.mkdir(parents=True)
    for i in range(ad.MANIFEST_LIMIT + 3):
        (root / f"f{i:05d}.txt").write_text("x")
    monkeypatch.setenv("DR_LOCAL_ROOTS", str(tmp_path / "home"))
    s = DataSource(name="many", kind="local_folder", uri=str(root))
    out = ad.adapter_for(s).fetch(tmp_path / "dest")
    assert len(list(out.iterdir())) == ad.MANIFEST_LIMIT + 3
    monkeypatch.setattr(ad, "LOCAL_FETCH_MAX", 10)
    with pytest.raises(ad.SourceError, match="more than 10 files"):
        ad.adapter_for(s).fetch(tmp_path / "dest2")


def test_local_preview_refuses_hidden_files_and_links(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    (home / ".config" / "deepresearch").mkdir(parents=True)
    (home / ".config" / "deepresearch" / ".env").write_text("GEMINI_API_KEY=x")
    (home / "notes.txt").write_text("hi")
    (home / "link.txt").symlink_to(home / ".ssh" / "id_ed25519")
    monkeypatch.setenv("DR_LOCAL_ROOTS", str(home))
    a = ad.adapter_for(DataSource(name="home", kind="local_folder", uri=str(home)))
    assert [e.path for e in a.manifest().entries] == ["notes.txt"]
    assert a.preview("notes.txt") == b"hi"
    for bad in (".ssh/id_ed25519", ".config/deepresearch/.env", "link.txt"):
        with pytest.raises(ad.SourceError):
            a.preview(bad)


# ------------------------------------------------------------------ registry
def test_all_digit_names_are_found_by_name_first(reg):
    reg.add(DataSource(name="obs", kind="web", uri="https://a"))
    reg.add(DataSource(name="12", kind="web", uri="https://b"))
    for i in range(12):
        reg.add(DataSource(name=f"x{i}", kind="web", uri=f"https://x{i}"))
    assert reg.get("12").uri == "https://b"
    assert reg.get(1).name == "obs"  # an int is still an id
    reg.add(DataSource(name="05", kind="web", uri="https://c"))  # id 5 exists; fine
    assert reg.get("05").uri == "https://c"


def test_report_source_hash_is_the_same_in_every_process(tmp_path):
    db = tmp_path / "h.db"
    code = textwrap.dedent(
        f"""
        from deepresearch.core.session import SessionManager
        from deepresearch.sources import DataSource
        from deepresearch.sources.adapters import adapter_for
        sm = SessionManager({str(db)!r})
        if not sm.get_session('1'):
            sm.create_session('iid', 'p'); sm.update_session('iid', 'completed', 'text')
        s = DataSource(name='r1', kind='report', uri='report:1',
                       options={{'db_path': {str(db)!r}}})
        print(adapter_for(s).manifest().hash)
        """
    )
    hashes = {
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
        ).stdout.strip()
        for seed in (1, 2, 3)
    }
    assert len(hashes) == 1 and "" not in hashes


def test_deleting_a_source_keeps_old_provenance(reg):
    s = reg.add(DataSource(name="obs", kind="web", uri="https://a"))
    s.manifest = Manifest.build([ManifestEntry(path="a", size=1)])
    reg.update(s)
    reg.record_use(s, "session", 5)
    before = session_provenance(reg.db_path, {"id": 5, "prompt": "p", "files": "[]"})
    reg.delete("obs")
    after = session_provenance(reg.db_path, {"id": 5, "prompt": "p", "files": "[]"})
    assert after["sources"] and after["fingerprint"] == before["fingerprint"]


def test_old_use_rows_get_a_snapshot_backfilled(tmp_path):
    import sqlite3

    db = str(tmp_path / "h.db")
    r1 = SourceRegistry(db)
    s = r1.add(DataSource(name="obs", kind="web", uri="https://a"))
    with sqlite3.connect(db) as c:  # a row written by v0.27.0 (no snapshot columns)
        c.execute(
            "INSERT INTO data_source_uses (source_id, used_by_kind, used_by_id) "
            "VALUES (?, 'session', 9)",
            (s.id,),
        )
    r2 = SourceRegistry(db)  # opening it again back-fills
    r2.delete("obs")
    assert r2.used_by("session", 9)[0]["name"] == "obs"


def test_a_test_running_next_to_an_index_build_keeps_the_index(reg, monkeypatch):
    s = reg.add(DataSource(name="w1", kind="web", uri="https://a"))

    class SlowAdapter:
        def __init__(self, src):
            pass

        def manifest(self, limit=0):
            time.sleep(0.3)
            return Manifest.build([ManifestEntry(path="a", size=1)])

    import deepresearch.sources.service as svc

    monkeypatch.setattr(svc, "adapter_for", SlowAdapter)
    t = threading.Thread(target=check, args=(reg, reg.get("w1")))
    t.start()
    time.sleep(0.05)
    reg.set_options(reg.get("w1"), store="fileSearchStores/abc", store_hash="h")
    t.join()
    got = reg.get("w1")
    assert got.options["store"] == "fileSearchStores/abc" and got.status == "ok"
    assert s.id == got.id


# ------------------------------------------------------------------ relay staging
class _Cluster:
    remote_root = "~/drl"

    def __init__(self):
        self.ready: set = set()
        self.uploads: dict = {}

    def run(self, cmd, stdin=None, timeout=0):
        d = cmd.split("test -f ", 1)[1].split("/.ready", 1)[0]
        return subprocess.CompletedProcess(
            cmd, 0, b"yes\n" if d in self.ready else b"no\n", b""
        )

    def sh(self, cmd, stdin=None, timeout=0):
        d = cmd.split("mkdir -p ", 1)[1].split(".part", 1)[0]
        self.uploads[d] = stdin
        self.ready.add(d)


def test_relay_uploads_again_after_the_local_data_changes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "d").mkdir(parents=True)
    (home / "d" / "obs.csv").write_text("v1")
    monkeypatch.setenv("DR_LOCAL_ROOTS", str(home))
    reg = SourceRegistry(str(tmp_path / "h.db"))
    check(
        reg, reg.add(DataSource(name="obs", kind="local_folder", uri=str(home / "d")))
    )
    t = _Cluster()
    d1 = st.relay_upload(t, reg.get("obs"))
    (home / "d" / "obs.csv").write_text("v2 - new data")
    t.uploads.clear()
    src = reg.get("obs")
    d2 = st.relay_upload(t, src)
    assert d2 != d1 and list(t.uploads) == [d2]
    assert st.remote_dir("~/drl", src) == d2  # the job and sources.json use this


def test_relay_refuses_when_the_size_is_unknown_or_too_big(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "d").mkdir(parents=True)
    (home / "d" / "big.bin").write_bytes(b"x" * 500)
    monkeypatch.setenv("DR_LOCAL_ROOTS", str(home))
    s = DataSource(
        name="obs",
        kind="local_folder",
        uri=str(home / "d"),
        options={"max_relay_bytes": 100},
    )  # never tested: no stored manifest
    with pytest.raises(ad.SourceError, match="over the relay limit"):
        st.relay_upload(_Cluster(), s)
    s.manifest = Manifest.build(
        [ManifestEntry(path="big.bin", size=500)], truncated=True
    )
    with pytest.raises(ad.SourceError, match="could not be fully listed"):
        st.relay_upload(_Cluster(), s, refresh=False)


def test_research_uploads_cleans_up_its_temp_copy(tmp_path, monkeypatch):
    import atexit

    from deepresearch.sources.usage import research_uploads

    home = tmp_path / "home"
    (home / "d").mkdir(parents=True)
    (home / "d" / "a.txt").write_text("x")
    monkeypatch.setenv("DR_LOCAL_ROOTS", str(home))
    registered = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a: registered.append((fn, a)))
    paths, _ = research_uploads(
        [DataSource(name="d1", kind="local_folder", uri=str(home / "d"))]
    )
    assert paths and registered
    fn, a = registered[0]
    fn(*a)
    assert not os.path.exists(a[0])

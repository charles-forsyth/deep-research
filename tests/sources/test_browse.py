"""v0.38 file browser and Google Drive sources (CLIs faked; no network)."""

import json
from pathlib import Path

import pytest

from deepresearch.sources import adapters as ad
from deepresearch.sources import browse, gdrive
from deepresearch.sources.model import DataSource
from tests.dashboard.test_server import app  # noqa: F401  (fixture)

DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
FORM = "application/vnd.google-apps.form"
F1 = "1aaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
F2 = "1bbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
PARENT = "0Pppppppppppppppppp"


@pytest.fixture
def remotes(monkeypatch, tmp_path):
    browse._REMOTES.update(value=None, at=0.0)
    calls = []
    conf = tmp_path / "rclone.conf"
    conf.write_text(
        "[gd]\ntype = drive\ntoken = SECRET-TOKEN\n\n"
        "[lab]\ntype = drive\nteam_drive = 0Alabdrive000000000\ntoken = SECRET2\n\n"
        "[ceph]\ntype = s3\nsecret_access_key = SECRET3\n"
    )

    def fake(cmd, timeout=60):
        calls.append(cmd)
        if cmd[:3] == ["rclone", "listremotes", "--long"]:
            return "gd:     drive\nlab:    drive\nceph:   s3\ngcs:    google cloud storage\n"
        if cmd[:3] == ["rclone", "config", "file"]:
            return f"Configuration file is stored at:\n{conf}\n"
        if cmd[:3] == ["gcloud", "config", "get"]:
            return "me@ucr.edu\n" if cmd[3] == "account" else "proj-a\n"
        if cmd[:3] == ["rclone", "backend", "query"]:
            q = cmd[4]
            if "in parents" in q:
                return json.dumps(
                    [
                        {"id": F1, "name": "Plan: v1/2", "mimeType": DOC, "parents": [PARENT], "modifiedTime": "2026-09-01T00:00:00Z"},
                        {"id": F2, "name": "Budget", "mimeType": SHEET, "parents": [PARENT], "size": "9"},
                        {"id": "1ccccccccccccccccccccccccc", "name": "Survey", "mimeType": FORM},
                        {"id": "1dddddddddddddddddddddddd", "name": "sub", "mimeType": gdrive.FOLDER},
                        {"id": "1eeeeeeeeeeeeeeeeeeeeeeeee", "name": "link", "mimeType": gdrive.SHORTCUT},
                    ]
                )  # fmt: skip
            return "[]"
        if cmd[:3] == ["rclone", "backend", "drives"]:
            return json.dumps([{"id": "0Adrive00000000000", "name": "Lab share"}])
        if cmd[:2] == ["rclone", "lsjson"]:
            return json.dumps(
                [
                    {"Name": "bkt", "IsDir": True, "IsBucket": True, "Size": -1},
                    {"Name": "x.csv", "IsDir": False, "Size": 5, "ModTime": "2026-01-01T00:00:00Z"},
                ]
            )  # fmt: skip
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr(ad, "_run", fake)
    monkeypatch.setattr(browse, "_run", fake)
    monkeypatch.setattr(gdrive, "_run", fake)
    return calls


def test_places_come_from_configured_remotes_only(remotes, monkeypatch, tmp_path):
    monkeypatch.setenv("DR_LOCAL_ROOTS", str(tmp_path))
    pl = browse.places()
    ids = [p["id"] for p in pl]
    assert ids == ["local", "drive:gd", "drive:lab", "gcs", "s3:ceph"]
    assert "SECRET" not in json.dumps(pl)
    assert browse.drive_team_ids() == {"lab": "0Alabdrive000000000"}
    # the rclone config dump (tokens) is never read
    assert not any(c[:3] == ["rclone", "config", "dump"] for c in remotes)
    with pytest.raises(ad.SourceError):
        browse.list_place("drive:ceph")  # wrong type for that remote
    with pytest.raises(ad.SourceError):
        browse.list_place("s3:nope")


def test_drive_listing_labels_docs_and_hides_shortcuts(remotes):
    top = browse.list_place("drive:gd", "")
    assert [i["path"] for i in top["items"]] == ["root", "shared", "drives"]
    assert "dr-signin-check" in remotes[-1][4]  # the top checks the sign-in
    items = browse.list_place("drive:gd", f"folder:{PARENT}")["items"]
    names = [i["name"] for i in items]
    assert names[0] == "sub" and "link" not in names  # folders first, no shortcuts
    doc = next(i for i in items if i["name"] == "Plan: v1/2")
    assert (
        doc["badge"] == "Google Doc" and doc["addable"] and doc["parents"] == [PARENT]
    )
    form = next(i for i in items if i["name"] == "Survey")
    assert not form["addable"] and "cannot be exported" in form["why"]
    drives = browse.list_place("drive:gd", "drives")["items"]
    assert drives[0]["path"] == "folder:0Adrive00000000000"
    with pytest.raises(ad.SourceError):
        browse.list_place("drive:gd", "folder:bad id!")


def test_shared_drive_remote_opens_at_its_drive(remotes):
    browse.list_place("drive:lab", "")
    q = [c for c in remotes if c[:3] == ["rclone", "backend", "query"]][-1]
    assert q[3] == "lab:" and "'0Alabdrive000000000' in parents" in q[4]


def test_expired_sign_in_gets_a_clear_message():
    msg = browse.friendly(
        "couldn't fetch token: invalid_grant: maybe token expired?", "rc"
    )
    assert "rclone config reconnect rc:" in msg


def test_drive_search_escapes_quotes(remotes):
    browse.list_place("drive:gd", "", "Chuck's plan")
    q = [c for c in remotes if c[:3] == ["rclone", "backend", "query"]][-1][4]
    assert "name contains 'Chuck\\'s plan'" in q


def test_drive_spec_for_picked_files_and_folder(remotes):
    items = browse.list_place("drive:gd", f"folder:{PARENT}")["items"]
    picked = [i for i in items if i["name"] in ("Plan: v1/2", "Budget")]
    spec = browse.source_spec("drive:gd", picked)
    assert spec["kind"] == "gdrive" and spec["auth_ref"] == "rclone:gd"
    assert (
        spec["uri"] == f"gdrive://files/{F2},{F1}"
        or spec["uri"] == f"gdrive://files/{F1},{F2}"
    )
    assert {f["id"] for f in spec["options"]["files"]} == {F1, F2}
    folder = browse.source_spec("drive:gd", [next(i for i in items if i["dir"])])
    assert folder["uri"].startswith("gdrive://folder/1ddd")
    with pytest.raises(ad.SourceError):
        browse.source_spec(
            "drive:gd", [next(i for i in items if i["name"] == "Survey")]
        )
    with pytest.raises(ad.SourceError):  # My Drive itself is not a source
        browse.source_spec(
            "drive:gd", [{"name": "My Drive", "path": "root", "dir": True}]
        )


def test_drive_adapter_manifest_and_fetch_with_safe_names(
    remotes, monkeypatch, tmp_path
):
    files = [
        {"id": F1, "name": "Plan: v1/2", "mime": DOC, "parents": [PARENT]},
        {"id": F2, "name": "Budget", "mime": SHEET, "parents": [PARENT]},
    ]
    s = DataSource(
        name="d1",
        kind="gdrive",
        uri=f"gdrive://files/{F1},{F2}",
        auth_ref="rclone:gd",
        options={"files": files},
    )
    a = ad.adapter_for(s)
    m = a.manifest()
    assert [e.path for e in m.entries] == ["Budget.csv", "Plan\uff1a v1\uff0f2.md"]

    def fake_copyid(fid, dest_dir):
        dest_dir.mkdir(parents=True, exist_ok=True)
        p = dest_dir / ("export-" + fid[:3])
        p.write_text(f"content of {fid}")
        return p

    monkeypatch.setattr(a, "_copyid", fake_copyid)
    out = a.fetch(tmp_path / "d")
    got = sorted(p.name for p in out.iterdir())
    assert got == ["Budget.csv", "Plan\uff1a v1\uff0f2.md"]
    assert (out / "Budget.csv").read_text() == f"content of {F2}"
    assert s.effective_staging == "relay"  # a cluster node has no Drive login


def test_drive_adapter_reports_missing_files(remotes):
    gone = "1zzzzzzzzzzzzzzzzzzzzzzzzz"
    s = DataSource(
        name="d2", kind="gdrive", uri=f"gdrive://files/{gone}", auth_ref="rclone:gd",
        options={"files": [{"id": gone, "name": "Old doc", "parents": [PARENT]}]},
    )  # fmt: skip
    with pytest.raises(ad.SourceError, match="Old doc"):
        ad.adapter_for(s).manifest()


def test_drive_uri_validation():
    for bad in (
        "gdrive://folder/a,b",
        "gdrive://files/",
        "gdrive://x/1aaaaaaaaaaa",
        "gdrive://files/../../etc",
    ):
        with pytest.raises(ad.SourceError):
            gdrive.parse_uri(bad)
    assert gdrive.export_name(".env", "text/plain") == "_.env"


def test_local_browse_hides_dotfiles_and_links(tmp_path, monkeypatch):
    monkeypatch.setenv("DR_LOCAL_ROOTS", str(tmp_path))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "a.csv").write_text("x\n1\n")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / ".ssh")
    items = browse.list_place("local", str(tmp_path))["items"]
    assert [i["name"] for i in items] == ["data"]
    with pytest.raises(ad.SourceError):
        browse.list_place("local", "/etc")
    with pytest.raises(ad.SourceError):
        browse.preview("local", str(tmp_path / ".ssh" / "x"))
    spec = browse.source_spec(
        "local",
        [{"name": "a.csv", "path": str(tmp_path / "data" / "a.csv"), "dir": False}],
    )
    assert spec["kind"] == "local_file"
    two = browse.source_spec(
        "local",
        [
            {"name": "a.csv", "path": str(tmp_path / "data" / "a.csv"), "dir": False},
            {
                "name": "b[1].csv",
                "path": str(tmp_path / "data" / "b[1].csv"),
                "dir": False,
            },
        ],
    )
    assert two["kind"] == "local_folder" and two["options"]["include"] == [
        "a.csv",
        "b[[]1[]].csv",
    ]


def test_gcs_and_s3_specs(remotes):
    f = browse.source_spec(
        "gcs", [{"name": "raw", "path": "gs://bkt/raw/", "dir": True}]
    )
    assert f == {
        "uri": "gs://bkt/raw",
        "kind": "gcs",
        "auth_ref": "gcloud",
        "options": {},
        "name": "raw",
        "title": "gs://bkt/raw/",
    }
    two = browse.source_spec(
        "gcs",
        [
            {"name": "a.csv", "path": "gs://bkt/raw/a.csv", "dir": False},
            {"name": "b.csv", "path": "gs://bkt/raw/b.csv", "dir": False},
        ],
    )
    assert two["uri"] == "gs://bkt/raw" and two["options"]["include"] == [
        "a.csv",
        "b.csv",
    ]
    with pytest.raises(ad.SourceError):
        browse.source_spec(
            "gcs", [{"name": "p", "path": "project:proj-a", "dir": True}]
        )
    with pytest.raises(ad.SourceError):
        browse.source_spec(
            "gcs",
            [
                {"name": "a", "path": "gs://bkt/x/a", "dir": False},
                {"name": "b", "path": "gs://bkt/y/b", "dir": False},
            ],
        )
    s3 = browse.source_spec("s3:ceph", [{"name": "bkt", "path": "bkt", "dir": True}])
    assert s3["uri"] == "s3://bkt" and s3["auth_ref"] == "rclone:ceph"
    items = browse.list_place("s3:ceph", "")["items"]
    assert items[0] == {**items[0], "name": "bkt", "path": "bkt", "dir": True}
    with pytest.raises(ad.SourceError):
        browse.list_place("s3:ceph", "bkt/../other")


def test_browse_api_routes(app, remotes, monkeypatch, tmp_path):  # noqa: F811
    call = app["call"]
    monkeypatch.setenv("DR_LOCAL_ROOTS", str(tmp_path))
    st, d = call("GET", "/api/browse/places")
    assert st == 200 and [p["id"] for p in d["places"]][:3] == [
        "local",
        "drive:gd",
        "drive:lab",
    ]
    st, d = call("GET", f"/api/browse/list?place=drive:gd&path=folder:{PARENT}")
    assert st == 200 and d["items"][0]["name"] == "sub"
    st, d = call("GET", "/api/browse/list?place=drive:nope")
    assert st == 502
    doc = {
        "name": "Plan",
        "path": f"file:{F1}",
        "dir": False,
        "id": F1,
        "mime": DOC,
        "parents": [PARENT],
    }
    st, spec = call("POST", "/api/browse/spec", {"place": "drive:gd", "items": [doc]})
    assert st == 200 and spec["kind"] == "gdrive" and spec["name"] == "plan"
    # a taken name gets a suffix
    st, s = call(
        "POST",
        "/api/sources",
        {
            **{k: spec[k] for k in ("uri", "kind", "auth_ref", "options")},
            "name": "plan",
            "test": False,
        },
    )
    assert st == 200 and s["kind"] == "gdrive" and s["effective_staging"] == "relay"
    st, spec2 = call("POST", "/api/browse/spec", {"place": "drive:gd", "items": [doc]})
    assert spec2["name"] == "plan-2"
    st, d = call("POST", "/api/browse/spec", {"place": "drive:gd", "items": []})
    assert st == 400


def test_guess_kind_knows_gdrive():
    from deepresearch.cli.sources import guess_kind

    assert guess_kind(f"gdrive://folder/{F1}") == "gdrive"


def test_gdrive_is_readable_by_research_uploads(tmp_path):
    from deepresearch.sources.usage import UPLOAD_EXT

    assert {"md", "csv", "pdf"} <= UPLOAD_EXT
    assert Path(gdrive.export_name("x", SHEET)).suffix == ".csv"

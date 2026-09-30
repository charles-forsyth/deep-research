"""File browser for adding data sources: this computer, Google Drive, GCS, S3/Ceph.

Only what is already signed in on this machine shows up: rclone Drive and S3 remotes
(the rclone config), Google Cloud Storage through gcloud's login, and the allowed local
folders (DR_LOCAL_ROOTS or $HOME). No credential is stored or shown here; rclone and
gcloud keep them.

A "place" is one of:
  local              the allowed local folders
  gcs                Google Cloud Storage as the gcloud account (projects > buckets)
  drive:<remote>     an rclone Google Drive remote
  s3:<remote>        an rclone S3 remote (CephRDS, AWS, ...)

Paths inside a place are opaque strings the browser hands back unchanged:
  local   an absolute folder path ("" = the allowed roots)
  gcs     "" (projects), "project:<id>" (its buckets), "gs://bucket/prefix/"
  drive   "" (top), "root", "shared", "drives", "folder:<id>"
  s3      "" (buckets), "bucket/prefix"
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from deepresearch.sources import gdrive
from deepresearch.sources.adapters import SourceError, _run, local_roots

LIST_MAX = 500  # items shown per folder
S3_TIMEOUT = 25  # CephRDS without the campus VPN just hangs
_TOKEN: dict[str, Any] = {"value": "", "at": 0.0}
_REMOTES: dict[str, Any] = {"value": None, "at": 0.0}

NATIVE_LABEL = {
    "application/vnd.google-apps.document": "Google Doc",
    "application/vnd.google-apps.spreadsheet": "Google Sheet",
    "application/vnd.google-apps.presentation": "Google Slides",
    "application/vnd.google-apps.drawing": "Google Drawing",
    "application/vnd.google-apps.form": "Google Form",
    "application/vnd.google-apps.site": "Google Site",
    "application/vnd.google-apps.map": "Google My Map",
}


# --------------------------------------------------------------------------- places


def rclone_remotes() -> dict[str, str]:
    """name -> backend type for every rclone remote (cached a minute)."""
    if _REMOTES["value"] is not None and time.time() - _REMOTES["at"] < 60:
        return dict(_REMOTES["value"])
    # `listremotes --long` prints "name: type" only (never tokens or keys)
    try:
        out = _run(["rclone", "listremotes", "--long"], timeout=20)
    except SourceError:
        out = ""
    remotes = {}
    for line in out.splitlines():
        name, sep, typ = line.partition(":")
        if sep and name.strip():
            remotes[name.strip()] = typ.strip()
    _REMOTES.update(value=remotes, at=time.time())
    return dict(remotes)


def drive_team_ids() -> dict[str, str]:
    """remote -> shared drive ID for Drive remotes pinned to one shared drive.

    Read from the rclone config file; only the remote names, types and team_drive
    values are kept (tokens in the same file are never returned or logged).
    """
    try:
        path = _run(["rclone", "config", "file"], timeout=20).strip().splitlines()[-1]
        text = Path(path).read_text()
    except (SourceError, OSError, IndexError):
        return {}
    out: dict[str, str] = {}
    name = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
        elif name and line.startswith("team_drive") and "=" in line:
            val = line.split("=", 1)[1].strip()
            if gdrive.ID_RE.fullmatch(val):
                out[name] = val
    return out


def friendly(err: str, remote: str = "") -> str:
    """A short, actionable message for common rclone/Drive failures."""
    low = err.lower()
    if (
        "invalid_grant" in low
        or "token expired" in low
        or "couldn't fetch token" in low
    ):
        return (
            f"The sign-in for '{remote}' has expired. Run: rclone config reconnect "
            f"{remote}:  (it opens a browser to sign in again)"
        )
    if "notfound" in low.replace(" ", "") or "404" in low:
        return "Not found, or this account no longer has access."
    if "403" in low or "insufficient" in low or "permission" in low:
        return "This account does not have access."
    if "googleapis.com" in low:
        return "Google Drive refused the request (access removed, or the sign-in needs renewing)."
    return err


def gcloud_account() -> str:
    try:
        return _run(["gcloud", "config", "get", "account"], timeout=20).strip()
    except SourceError:
        return ""


def gcloud_project() -> str:
    try:
        return _run(["gcloud", "config", "get", "project"], timeout=20).strip()
    except SourceError:
        return ""


def places() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        {
            "id": "local",
            "kind": "local",
            "label": "This computer",
            "detail": ", ".join(str(r) for r in local_roots()),
        }
    ]
    remotes = rclone_remotes()
    team = drive_team_ids()
    # personal Drives first (they have My Drive, Shared with me and search)
    drives = sorted(
        (n for n, t in remotes.items() if t == "drive"),
        key=lambda n: (n in team, n.lower()),
    )
    for name in drives:
        out.append(
            {
                "id": f"drive:{name}",
                "kind": "drive",
                "label": f"Google Drive: {name}",
                "detail": "shared drive" if name in team else "Google Drive",
                "shared_drive": name in team,
            }
        )
    acct = gcloud_account()
    if acct:
        out.append(
            {
                "id": "gcs",
                "kind": "gcs",
                "label": "Google Cloud Storage",
                "detail": acct,
            }
        )
    for name, typ in sorted(remotes.items(), key=lambda kv: kv[0].lower()):
        if typ == "s3":
            out.append(
                {
                    "id": f"s3:{name}",
                    "kind": "s3",
                    "label": f"S3: {name}",
                    "detail": "rclone remote"
                    + (" (CephRDS: needs the campus VPN)" if name == "ceph" else ""),
                }
            )
    return out


def _place(place: str) -> tuple[str, str]:
    """(kind, remote) for a place id, refusing anything that is not configured."""
    if place == "local":
        return "local", ""
    if place == "gcs":
        return "gcs", ""
    kind, _, remote = place.partition(":")
    if kind in ("drive", "s3") and rclone_remotes().get(remote) == kind:
        return kind, remote
    raise SourceError(f"unknown place: {place}")


# --------------------------------------------------------------------------- helpers


def _item(
    name: str,
    path: str,
    *,
    dir: bool,
    size: int = 0,
    modified: str = "",
    **extra: Any,
) -> dict[str, Any]:
    d = {
        "name": name,
        "path": path,
        "dir": dir,
        "size": max(int(size or 0), 0),
        "modified": modified,
        "addable": True,
    }
    d.update(extra)
    return d


def _sorted(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda x: (not x["dir"], x["name"].lower()))


def _gcs_token() -> str:
    if _TOKEN["value"] and time.time() - _TOKEN["at"] < 300:
        return str(_TOKEN["value"])
    tok = _run(["gcloud", "auth", "print-access-token"], timeout=30).strip()
    if not tok:
        raise SourceError("gcloud is not signed in on this machine")
    _TOKEN.update(value=tok, at=time.time())
    return tok


def _gcs_get(url: str, headers: dict[str, str] | None = None) -> bytes:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {_gcs_token()}", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (fixed https host)
            return bytes(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise SourceError(msg or f"Google Cloud said {e.code}") from e
    except OSError as e:
        raise SourceError(f"Google Cloud unreachable: {e}") from e


def split_gs(path: str) -> tuple[str, str]:
    m = re.fullmatch(r"gs://([a-z0-9][a-z0-9._-]{1,220})/?(.*)", path)
    if not m:
        raise SourceError("not a gs:// path")
    rest = m.group(2)
    if ".." in rest.split("/"):
        raise SourceError("path escapes the bucket")
    return m.group(1), rest


# --------------------------------------------------------------------------- listing


def list_place(place: str, path: str = "", q: str = "") -> dict[str, Any]:
    kind, remote = _place(place)
    if kind == "local":
        return _list_local(path)
    if kind == "gcs":
        return _list_gcs(path)
    if kind == "drive":
        try:
            return _list_drive(remote, path, q)
        except SourceError as e:
            raise SourceError(friendly(str(e), remote)) from e
    return _list_s3(remote, path)


def _inside_roots(p: Path) -> Path:
    real = p.expanduser().resolve()
    if not any(real == r or r in real.parents for r in local_roots()):
        raise SourceError(f"{real} is outside the allowed folders")
    return real


def _list_local(path: str) -> dict[str, Any]:
    if not path:
        return {
            "items": [
                _item(str(r), str(r), dir=True, addable=True) for r in local_roots()
            ],
            "note": "",
        }
    base = _inside_roots(Path(path))
    if not base.is_dir():
        raise SourceError(f"{base} is not a folder")
    items = []
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name.lower())
    except OSError as err:
        raise SourceError(str(err)) from err
    for e in entries:
        if e.name.startswith(".") or e.is_symlink():
            continue  # never dot-files (keys, .env) or links out of the tree
        try:
            st = e.stat()
        except OSError:
            continue
        items.append(
            _item(
                e.name,
                str(base / e.name),
                dir=e.is_dir(),
                size=0 if e.is_dir() else st.st_size,
                modified=time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            )
        )
        if len(items) >= LIST_MAX:
            break
    note = f"first {LIST_MAX} shown" if len(items) >= LIST_MAX else ""
    return {"items": _sorted(items), "note": note}


def _list_gcs(path: str) -> dict[str, Any]:
    api = "https://storage.googleapis.com/storage/v1"
    if not path:
        data = json.loads(
            _gcs_get(
                "https://cloudresourcemanager.googleapis.com/v1/projects?pageSize=500"
                "&filter=lifecycleState:ACTIVE"
            )
        )
        cur = gcloud_project()
        projs = sorted(
            (p["projectId"] for p in data.get("projects") or []),
            key=lambda x: (x != cur, x),
        )
        return {
            "items": [
                _item(
                    p,
                    f"project:{p}",
                    dir=True,
                    addable=False,
                    badge="current" if p == cur else "",
                )
                for p in projs
            ],
            "note": "Projects you can see; open one for its buckets.",
        }
    if path.startswith("project:"):
        proj = path[8:]
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", proj):
            raise SourceError("not a project id")
        data = json.loads(
            _gcs_get(
                f"{api}/b?project={proj}&maxResults=500&fields=items(name,location,updated)"
            )
        )
        return {
            "items": [
                _item(
                    b["name"],
                    f"gs://{b['name']}/",
                    dir=True,
                    modified=(b.get("updated") or "")[:16].replace("T", " "),
                    badge=(b.get("location") or "").lower(),
                )
                for b in data.get("items") or []
            ],
            "note": "",
        }
    bucket, prefix = split_gs(path)
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    url = (
        f"{api}/b/{bucket}/o?delimiter=/&maxResults={LIST_MAX}"
        f"&prefix={urllib.parse.quote(prefix)}"
        "&fields=prefixes,items(name,size,updated),nextPageToken"
    )
    data = json.loads(_gcs_get(url))
    items = [
        _item(p[len(prefix) :].rstrip("/"), f"gs://{bucket}/{p}", dir=True)
        for p in data.get("prefixes") or []
    ]
    for o in data.get("items") or []:
        name = o["name"][len(prefix) :]
        if not name:
            continue  # the folder placeholder object
        items.append(
            _item(
                name,
                f"gs://{bucket}/{o['name']}",
                dir=False,
                size=int(o.get("size") or 0),
                modified=(o.get("updated") or "")[:16].replace("T", " "),
            )
        )
    note = "more items not shown; add the folder" if data.get("nextPageToken") else ""
    return {"items": _sorted(items), "note": note}


def _drive_item(r: dict[str, Any]) -> dict[str, Any] | None:
    mime = r.get("mimeType") or ""
    if mime.startswith(gdrive.SHORTCUT):
        return None  # shortcuts can point anywhere (or nowhere); open the target
    if mime == gdrive.FOLDER:
        return _item(
            r.get("name") or r["id"],
            f"folder:{r['id']}",
            dir=True,
            modified=(r.get("modifiedTime") or "")[:16].replace("T", " "),
            id=r["id"],
        )
    ok = gdrive.readable(mime)
    return _item(
        r.get("name") or r["id"],
        f"file:{r['id']}",
        dir=False,
        size=int(r.get("size") or 0),
        modified=(r.get("modifiedTime") or "")[:16].replace("T", " "),
        id=r["id"],
        parents=r.get("parents") or [],
        mime=mime,
        badge=NATIVE_LABEL.get(mime, ""),
        addable=ok,
        why="" if ok else "cannot be exported as a file",
        link=r.get("webViewLink") or "",
    )


def _list_drive(remote: str, path: str, q: str) -> dict[str, Any]:
    if q.strip():
        term = q.strip()[:100]
        rows = gdrive.query(
            remote,
            f"(name contains {gdrive.quote(term)} or fullText contains "
            f"{gdrive.quote(term)}) and trashed=false",
        )
        items = [x for x in map(_drive_item, rows[:LIST_MAX]) if x]
        return {
            "items": _sorted(items),
            "note": f"{len(rows)} match{'es' if len(rows) != 1 else ''}"
            + (f", first {LIST_MAX} shown" if len(rows) > LIST_MAX else ""),
        }
    team = drive_team_ids().get(remote)
    if not path and team:
        # a remote pinned to one shared drive: its top is that drive
        path = f"folder:{team}"
    if not path:
        # check the sign-in once here: the three entries below are static, and an
        # expired remote should say so now, not on the first click
        gdrive.query(remote, "'root' in parents and name = '.dr-signin-check'")
        return {
            "items": [
                _item("My Drive", "root", dir=True, addable=False),
                _item("Shared with me", "shared", dir=True, addable=False),
                _item("Shared drives", "drives", dir=True, addable=False),
            ],
            "note": "Or search every Doc, Sheet and file by name or text.",
        }
    if path == "drives":
        out = _run(["rclone", "backend", "drives", f"{remote}:"], timeout=60)
        drives = json.loads(out or "[]") or []
        return {
            "items": _sorted(
                [
                    _item(d["name"], f"folder:{d['id']}", dir=True, id=d["id"])
                    for d in drives
                ]
            ),
            "note": "",
        }
    if path == "shared":
        rows = gdrive.query(remote, "sharedWithMe and trashed=false")
    elif path == "root":
        rows = gdrive.query(remote, "'root' in parents and trashed=false")
    elif path.startswith("folder:") and gdrive.ID_RE.fullmatch(path[7:]):
        rows = gdrive.query(
            remote, f"{gdrive.quote(path[7:])} in parents and trashed=false"
        )
    else:
        raise SourceError("not a Drive folder")
    items = [x for x in map(_drive_item, rows) if x]
    note = (
        f"first {LIST_MAX} of {len(items)} shown; search to narrow"
        if len(items) > LIST_MAX
        else ""
    )
    return {"items": _sorted(items)[:LIST_MAX], "note": note}


def _s3_path(path: str) -> str:
    rel = path.strip("/")
    if ".." in rel.split("/"):
        raise SourceError("path escapes the bucket")
    return rel


def _list_s3(remote: str, path: str) -> dict[str, Any]:
    rel = _s3_path(path)
    try:
        out = _run(
            [
                "rclone",
                "lsjson",
                "--max-depth",
                "1",
                "--no-mimetype",
                f"{remote}:{rel}",
            ],
            timeout=S3_TIMEOUT,
        )
    except SourceError as e:
        hint = (
            " (CephRDS needs the campus VPN on this machine)"
            if remote == "ceph"
            else ""
        )
        raise SourceError(f"{e}{hint}") from e
    rows = json.loads(out or "[]")
    items = [
        _item(
            r["Name"],
            "/".join(x for x in (rel, r["Name"]) if x),
            dir=bool(r.get("IsDir")),
            size=int(r.get("Size") or 0),
            modified=(r.get("ModTime") or "")[:16].replace("T", " ")
            if not r.get("IsBucket")
            else "",
        )
        for r in rows[:LIST_MAX]
    ]
    note = f"first {LIST_MAX} shown" if len(rows) > LIST_MAX else ""
    if not rows and not rel:
        note = "No buckets listed." + (
            " CephRDS needs the campus VPN on this machine." if remote == "ceph" else ""
        )
    return {"items": _sorted(items), "note": note}


# --------------------------------------------------------------------------- preview


def preview(place: str, path: str, max_bytes: int = 64_000, size: int = 0) -> bytes:
    """The first bytes of a file. Drive files are downloaded (exported) whole to
    preview them, so big ones are refused up front using the listed size."""
    kind, remote = _place(place)
    if kind == "local":
        p = _inside_roots(Path(path))
        if any(part.startswith(".") for part in p.parts) or Path(path).is_symlink():
            raise SourceError("hidden files and links are not shown")
        if not p.is_file():
            raise SourceError("pick a file")
        with open(p, "rb") as fh:
            return fh.read(max_bytes)
    if kind == "gcs":
        bucket, key = split_gs(path)
        if not key or key.endswith("/"):
            raise SourceError("pick a file")
        return _gcs_get(
            f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/"
            f"{urllib.parse.quote(key, safe='')}?alt=media",
            {"Range": f"bytes=0-{max_bytes - 1}"},
        )[:max_bytes]
    if kind == "s3":
        rel = _s3_path(path)
        try:
            r = subprocess.run(
                ["rclone", "cat", "--count", str(max_bytes), f"{remote}:{rel}"],
                capture_output=True,
                timeout=S3_TIMEOUT * 2,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SourceError(f"rclone: {e}") from e
        if r.returncode:
            raise SourceError(r.stderr.decode("utf-8", "replace").strip()[-300:])
        return r.stdout[:max_bytes]
    # drive
    if size > gdrive.PREVIEW_DOWNLOAD_MAX:
        raise SourceError("too big to preview")
    if not path.startswith("file:") or not gdrive.ID_RE.fullmatch(path[5:]):
        raise SourceError("pick a file")
    import tempfile

    from deepresearch.sources.model import DataSource

    a = gdrive.DriveAdapter(
        DataSource(
            name="preview",
            kind="gdrive",
            uri=f"gdrive://files/{path[5:]}",
            auth_ref=f"rclone:{remote}",
        )
    )
    with tempfile.TemporaryDirectory(prefix="dr-drive-") as tmp:
        try:
            got = a._copyid(path[5:], Path(tmp))
        except SourceError as e:
            raise SourceError(friendly(str(e), remote)) from e
        if got.stat().st_size > gdrive.PREVIEW_DOWNLOAD_MAX:
            raise SourceError("too big to preview")
        with open(got, "rb") as fh:
            return fh.read(max_bytes)


# --------------------------------------------------------------------------- adding


def _glob_escape(name: str) -> str:
    """A file name as an fnmatch/rclone glob that matches only itself."""
    return re.sub(r"([\[\]*?{}])", r"[\1]", name)


def _slug(text: str, fallback: str = "source") -> str:
    s = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", text or "")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40].strip("-")
    return s if len(s) >= 2 else fallback


def _common_parent(paths: list[str], sep: str = "/") -> str:
    parents = {p.rstrip(sep).rsplit(sep, 1)[0] for p in paths}
    if len(parents) != 1:
        raise SourceError(
            "pick items from one folder at a time (or add their common folder)"
        )
    return parents.pop()


def source_spec(place: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """What POST /api/sources needs (uri, kind, auth_ref, options, suggested name)
    for the picked items. One folder becomes a folder source; files (or several
    items from one folder) become that folder with an exact-name filter.
    """
    kind, remote = _place(place)
    if not items:
        raise SourceError("nothing picked")
    paths = [str(i.get("path") or "") for i in items]
    dirs = [bool(i.get("dir")) for i in items]
    names = [str(i.get("name") or "") for i in items]
    single_dir = len(items) == 1 and dirs[0]

    def include_for(parent_rel_names: list[tuple[str, bool]]) -> list[str]:
        return [_glob_escape(n) + ("/**" if d else "") for n, d in parent_rel_names]

    if kind == "drive":
        if single_dir:
            if not paths[0].startswith("folder:"):
                raise SourceError("pick a folder or files (not My Drive itself)")
            fid = paths[0][7:]
            return {
                "uri": f"gdrive://folder/{fid}",
                "kind": "gdrive",
                "auth_ref": f"rclone:{remote}",
                "options": {},
                "name": _slug(names[0], "drive-folder"),
                "title": names[0],
            }
        if any(dirs):
            raise SourceError("add a Drive folder on its own, or pick only files")
        bad = [i["name"] for i in items if not i.get("addable", True)]
        if bad:
            raise SourceError(f"cannot be exported: {', '.join(bad[:3])}")
        files: list[dict[str, Any]] = []
        ids: list[str] = []
        for i in items:
            fid = str(i.get("id") or str(i.get("path", ""))[5:])
            if not gdrive.ID_RE.fullmatch(fid):
                raise SourceError("not a Drive file")
            ids.append(fid)
            files.append(
                {
                    "id": fid,
                    "name": i.get("name") or "",
                    "mime": i.get("mime") or "",
                    "parents": [
                        p for p in i.get("parents") or [] if isinstance(p, str)
                    ],
                }
            )
        if len(files) > gdrive.FILES_MAX:
            raise SourceError(f"up to {gdrive.FILES_MAX} Drive files per source")
        return {
            "uri": "gdrive://files/" + ",".join(ids),
            "kind": "gdrive",
            "auth_ref": f"rclone:{remote}",
            "options": {"files": files},
            "name": _slug(
                names[0] if len(files) == 1 else f"{names[0]} and more", "drive-docs"
            ),
            "title": names[0]
            if len(files) == 1
            else f"{names[0]} and {len(files) - 1} more",
        }

    if kind == "local":
        for p in paths:
            _inside_roots(Path(p))
        if single_dir:
            return {
                "uri": str(Path(paths[0]).resolve()),
                "kind": "local_folder",
                "auth_ref": "",
                "options": {},
                "name": _slug(names[0], "folder"),
                "title": names[0],
            }
        if len(items) == 1:
            return {
                "uri": str(Path(paths[0]).resolve()),
                "kind": "local_file",
                "auth_ref": "",
                "options": {},
                "name": _slug(names[0], "file"),
                "title": names[0],
            }
        parent = _common_parent(paths, os.sep)
        return {
            "uri": parent,
            "kind": "local_folder",
            "auth_ref": "",
            "options": {"include": include_for(list(zip(names, dirs)))},
            "name": _slug(Path(parent).name + "-picked", "files"),
            "title": f"{len(items)} items from {Path(parent).name}",
        }

    if kind == "gcs":
        if single_dir and paths[0].startswith("project:"):
            raise SourceError("open the project and pick a bucket or folder")
        if single_dir:
            bucket, prefix = split_gs(paths[0])
            return {
                "uri": f"gs://{bucket}/{prefix}".rstrip("/"),
                "kind": "gcs",
                "auth_ref": "gcloud",
                "options": {},
                "name": _slug(names[0], "bucket"),
                "title": paths[0],
            }
        parent = _common_parent([p.rstrip("/") for p in paths])
        split_gs(parent + "/")
        return {
            "uri": parent,
            "kind": "gcs",
            "auth_ref": "gcloud",
            "options": {"include": include_for(list(zip(names, dirs)))},
            "name": _slug(
                names[0] if len(items) == 1 else parent.rsplit("/", 1)[-1] + "-picked",
                "files",
            ),
            "title": names[0]
            if len(items) == 1
            else f"{len(items)} items from {parent}",
        }

    # s3 through an rclone remote
    rels = [_s3_path(p) for p in paths]
    if single_dir:
        if not rels[0]:
            raise SourceError("pick a bucket or folder")
        return {
            "uri": f"s3://{rels[0]}",
            "kind": "s3",
            "auth_ref": f"rclone:{remote}",
            "options": {},
            "name": _slug(names[0], "bucket"),
            "title": f"{remote}:{rels[0]}",
        }
    parent = _common_parent(rels) if all("/" in r for r in rels) else ""
    if not parent:
        raise SourceError("pick files inside a bucket")
    return {
        "uri": f"s3://{parent}",
        "kind": "s3",
        "auth_ref": f"rclone:{remote}",
        "options": {"include": include_for(list(zip(names, dirs)))},
        "name": _slug(
            names[0] if len(items) == 1 else parent.rsplit("/", 1)[-1] + "-picked",
            "files",
        ),
        "title": names[0]
        if len(items) == 1
        else f"{len(items)} items from {remote}:{parent}",
    }

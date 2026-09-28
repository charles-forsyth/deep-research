"""Adapters: one per source kind. Each can test, list, preview and fetch its source.

Buckets go through tools already on the laptop and the cluster (`gcloud storage` for
GCS, `rclone` for S3/CephRDS), so there are no new Python dependencies and no secrets
in this program: credentials stay in gcloud ADC and the rclone config.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shlex
import shutil
import subprocess
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from deepresearch.sources.model import DataSource, Manifest, ManifestEntry

MANIFEST_LIMIT = 5000  # files listed before a manifest is marked truncated
PREVIEW_BYTES = 64_000
TIMEOUT = 60


class SourceError(Exception):
    """A source cannot be reached or read; the message is shown to the user."""


def local_roots() -> list[Path]:
    """Folders local sources may live in: DR_LOCAL_ROOTS (os.pathsep list) or $HOME."""
    raw = os.getenv("DR_LOCAL_ROOTS", "")
    roots = [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]
    return [r.resolve() for r in (roots or [Path.home()])]


def _run(cmd: list[str], timeout: int = TIMEOUT) -> str:
    exe = shutil.which(cmd[0])
    if not exe:
        raise SourceError(f"'{cmd[0]}' is not installed on this machine")
    try:
        r = subprocess.run(
            [exe, *cmd[1:]], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        raise SourceError(f"{cmd[0]} timed out after {timeout}s") from e
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise SourceError((msg[-1] if msg else f"{cmd[0]} failed")[:300])
    return r.stdout


def _filtered(s: DataSource, rel: str) -> bool:
    inc = s.options.get("include") or []
    exc = s.options.get("exclude") or []
    if inc and not any(fnmatch.fnmatch(rel, p) for p in inc):
        return False
    return not any(fnmatch.fnmatch(rel, p) for p in exc)


class SourceAdapter(ABC):
    def __init__(self, source: DataSource):
        self.s = source

    @abstractmethod
    def manifest(self, limit: int = MANIFEST_LIMIT) -> Manifest: ...

    @abstractmethod
    def preview(self, path: str = "", max_bytes: int = PREVIEW_BYTES) -> bytes: ...

    @abstractmethod
    def fetch(self, dest: Path) -> Path:
        """Copy the source (after include/exclude) into dest; return dest."""

    def test(self) -> Manifest:
        return self.manifest(limit=200)

    def cached_manifest(self) -> Manifest:
        """Manifest for browsing: the stored one when it is complete, else a fresh
        listing. Buckets over the VPN take 30+ s to list, so every folder click must
        not re-list the whole source."""
        m = self.s.manifest
        if m is not None and not m.truncated and len(m.entries) >= m.file_count:
            return m
        return self.manifest(limit=MANIFEST_LIMIT)

    def list(self, path: str = "", limit: int = 500) -> list[dict[str, Any]]:
        """Immediate children of path (dirs end with '/'), for the browse view."""
        prefix = path.strip("/")
        seen: dict[str, dict[str, Any]] = {}
        m = self.cached_manifest()
        for e in getattr(m, "entries_all", None) or m.entries:
            if prefix and not e.path.startswith(prefix + "/"):
                continue
            rest = e.path[len(prefix) + 1 :] if prefix else e.path
            head, sep, _ = rest.partition("/")
            key = head + ("/" if sep else "")
            if key not in seen:
                seen[key] = {
                    "name": key,
                    "size": e.size,
                    "dir": bool(sep),
                }
            elif sep:
                seen[key]["size"] += e.size
            if len(seen) >= limit:
                break
        return sorted(seen.values(), key=lambda x: (not x["dir"], x["name"]))

    def direct_snippet(self) -> str:
        """Shell lines a cluster node runs to download the source into "$DEST"."""
        raise SourceError(f"{self.s.kind} sources cannot be downloaded by the cluster")


class _FullManifest(Manifest):
    entries_all: list[ManifestEntry] = []


def _manifest(entries: list[ManifestEntry], truncated: bool) -> _FullManifest:
    m = Manifest.build(entries, truncated)
    return _FullManifest(
        **m.model_dump(), entries_all=sorted(entries, key=lambda e: e.path)
    )


# --------------------------------------------------------------------------- local


class LocalAdapter(SourceAdapter):
    """local_folder and local_file, confined to the allowed roots (default $HOME)."""

    def root(self) -> Path:
        p = Path(self.s.uri).expanduser()
        try:
            real = p.resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as e:
            raise SourceError(f"{self.s.uri} does not exist") from e
        if not any(real == r or r in real.parents for r in local_roots()):
            allowed = ", ".join(str(r) for r in local_roots())
            raise SourceError(f"{real} is outside the allowed folders ({allowed})")
        if self.s.kind == "local_file" and not real.is_file():
            raise SourceError(f"{real} is not a file")
        if self.s.kind == "local_folder" and not real.is_dir():
            raise SourceError(f"{real} is not a folder")
        return real

    def _inside(self, rel: str) -> Path:
        base = self.root()
        if base.is_file():
            return base
        target = (base / rel.lstrip("/")).resolve()
        if target != base and base not in target.parents:
            raise SourceError("path escapes the source folder")
        return target

    def manifest(self, limit: int = MANIFEST_LIMIT) -> _FullManifest:
        base = self.root()
        files = [base] if base.is_file() else None
        entries: list[ManifestEntry] = []
        truncated = False
        walk = (
            [(str(base.parent), [], [base.name])]
            if files
            else os.walk(base, followlinks=False)
        )
        top = base.parent if files else base
        for dirpath, dirnames, filenames in walk:
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for f in sorted(filenames):
                full = Path(dirpath) / f
                if full.is_symlink():
                    continue  # never follow links out of the source
                rel = str(full.relative_to(top))
                if not _filtered(self.s, rel):
                    continue
                st = full.stat()
                entries.append(
                    ManifestEntry(
                        path=rel, size=st.st_size, modified=str(int(st.st_mtime))
                    )
                )
                if len(entries) >= limit:
                    truncated = True
                    break
            if truncated:
                break
        return _manifest(entries, truncated)

    def preview(self, path: str = "", max_bytes: int = PREVIEW_BYTES) -> bytes:
        p = self._inside(path)
        if p.is_dir():
            raise SourceError("that is a folder")
        with open(p, "rb") as fh:
            return fh.read(max_bytes)

    def fetch(self, dest: Path) -> Path:
        base = self.root()
        dest.mkdir(parents=True, exist_ok=True)
        for e in self.manifest().entries_all:
            src = (base.parent if base.is_file() else base) / e.path
            out = dest / e.path
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
        return dest


# --------------------------------------------------------------------------- web


class WebAdapter(SourceAdapter):
    """A public URL (file or landing page). Listed as one file."""

    def _name(self) -> str:
        tail = self.s.uri.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        return tail or "index.html"

    def _open(self, method: str = "GET"):
        req = urllib.request.Request(
            self.s.uri, method=method, headers={"User-Agent": "deep-research/sources"}
        )
        try:
            return urllib.request.urlopen(req, timeout=TIMEOUT)  # noqa: S310 (http/https only)
        except Exception as e:
            raise SourceError(f"{self.s.uri}: {e}") from e

    def manifest(self, limit: int = MANIFEST_LIMIT) -> _FullManifest:
        if not self.s.uri.startswith(("http://", "https://")):
            raise SourceError("web sources must be http:// or https:// URLs")
        try:
            r = self._open("HEAD")
        except SourceError:
            r = self._open("GET")  # some servers refuse HEAD
        with r:
            size = int(r.headers.get("Content-Length") or 0)
            mod = r.headers.get("Last-Modified") or r.headers.get("ETag") or ""
        return _manifest(
            [ManifestEntry(path=self._name(), size=size, modified=mod)], False
        )

    def preview(self, path: str = "", max_bytes: int = PREVIEW_BYTES) -> bytes:
        with self._open() as r:
            return r.read(max_bytes)

    def fetch(self, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        with self._open() as r, open(dest / self._name(), "wb") as fh:
            shutil.copyfileobj(r, fh)
        return dest

    def direct_snippet(self) -> str:
        return (
            f'curl -fsSL --retry 3 -o "$DEST"/{shlex.quote(self._name())} '
            f"{shlex.quote(self.s.uri)}\n"
        )


# --------------------------------------------------------------------------- gcs


class GCSAdapter(SourceAdapter):
    """gs://bucket/prefix via `gcloud storage` and Application Default Credentials."""

    def _base(self) -> tuple[str, str]:
        if not self.s.uri.startswith("gs://"):
            raise SourceError("GCS sources look like gs://bucket/prefix")
        rest = self.s.uri[5:]
        bucket, _, prefix = rest.partition("/")
        return bucket, prefix.strip("/")

    def manifest(self, limit: int = MANIFEST_LIMIT) -> _FullManifest:
        bucket, prefix = self._base()
        pattern = f"gs://{bucket}/{prefix + '/' if prefix else ''}**"
        out = _run(
            [
                "gcloud",
                "storage",
                "objects",
                "list",
                pattern,
                f"--limit={limit + 1}",
                "--format=json(name,size,update_time)",
            ],
            timeout=120,
        )
        rows = json.loads(out or "[]")
        entries = []
        for r in rows[:limit]:
            rel = r["name"][len(prefix) + 1 :] if prefix else r["name"]
            if rel and not rel.endswith("/") and _filtered(self.s, rel):
                entries.append(
                    ManifestEntry(
                        path=rel,
                        size=int(r.get("size") or 0),
                        modified=r.get("update_time", ""),
                    )
                )
        return _manifest(entries, len(rows) > limit)

    def _obj(self, path: str) -> str:
        bucket, prefix = self._base()
        rel = path.strip("/")
        if ".." in rel.split("/"):
            raise SourceError("path escapes the source")
        return f"gs://{bucket}/{'/'.join(x for x in (prefix, rel) if x)}"

    def preview(self, path: str = "", max_bytes: int = PREVIEW_BYTES) -> bytes:
        out = _run(
            ["gcloud", "storage", "cat", f"--range=0-{max_bytes - 1}", self._obj(path)],
            timeout=120,
        )
        return out.encode()[:max_bytes]

    def fetch(self, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        _run(
            ["gcloud", "storage", "rsync", "-r", self._obj(""), str(dest)],
            timeout=3600,
        )
        return dest

    def direct_snippet(self) -> str:
        return f'gcloud storage rsync -r {shlex.quote(self._obj(""))} "$DEST"\n'


# --------------------------------------------------------------------------- s3


class S3Adapter(SourceAdapter):
    """s3://bucket/prefix through an rclone remote (auth_ref 'rclone:<remote>').

    CephRDS is S3-compatible; the laptop reaches it over the campus VPN, the cluster
    does not, so these default to relay staging.
    """

    def _remote(self) -> str:
        ref = self.s.auth_ref or ""
        if not ref.startswith("rclone:") or not ref[7:]:
            raise SourceError("S3 sources need auth_ref 'rclone:<remote name>'")
        return ref[7:]

    def _path(self, rel: str = "") -> str:
        if not self.s.uri.startswith("s3://"):
            raise SourceError("S3 sources look like s3://bucket/prefix")
        base = self.s.uri[5:].strip("/")
        rel = rel.strip("/")
        if ".." in rel.split("/"):
            raise SourceError("path escapes the source")
        return f"{self._remote()}:{'/'.join(x for x in (base, rel) if x)}"

    def manifest(self, limit: int = MANIFEST_LIMIT) -> _FullManifest:
        out = _run(
            ["rclone", "lsjson", "-R", "--files-only", "--no-mimetype", self._path()],
            timeout=180,
        )
        rows = json.loads(out or "[]")
        entries = [
            ManifestEntry(
                path=r["Path"],
                size=int(r.get("Size") or 0),
                modified=r.get("ModTime", ""),
            )
            for r in rows
            if _filtered(self.s, r["Path"])
        ]
        return _manifest(entries[:limit], len(entries) > limit)

    def preview(self, path: str = "", max_bytes: int = PREVIEW_BYTES) -> bytes:
        out = _run(["rclone", "cat", "--count", str(max_bytes), self._path(path)])
        return out.encode()[:max_bytes]

    def fetch(self, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        _run(["rclone", "copy", self._path(), str(dest)], timeout=3600)
        return dest

    def direct_snippet(self) -> str:
        return f'rclone copy {shlex.quote(self._path())} "$DEST"\n'


# --------------------------------------------------------------------------- internal


class InternalAdapter(SourceAdapter):
    """A past report (session id) or notebook (id) from the history DB, as Markdown."""

    def _text(self) -> tuple[str, str]:
        from deepresearch.core.config import user_db_path
        from deepresearch.dashboard.store import DashboardStore

        db = self.s.options.get("db_path") or user_db_path
        ref = int(str(self.s.uri).split(":")[-1])
        if self.s.kind == "report":
            import sqlite3

            with sqlite3.connect(db) as c:
                r = c.execute(
                    "SELECT prompt, result FROM sessions WHERE id=?", (ref,)
                ).fetchone()
            if not r or not r[1]:
                raise SourceError(f"report #{ref} has no text")
            return f"report_{ref}.md", f"# {r[0]}\n\n{r[1]}"
        nb = DashboardStore(db).get_notebook(ref)
        if not nb:
            raise SourceError(f"notebook #{ref} not found")
        return f"notebook_{ref}.md", f"# {nb['title']}\n\n{nb['content']}"

    def manifest(self, limit: int = MANIFEST_LIMIT) -> _FullManifest:
        name, text = self._text()
        return _manifest(
            [
                ManifestEntry(
                    path=name, size=len(text.encode()), modified=str(hash(text))
                )
            ],
            False,
        )

    def preview(self, path: str = "", max_bytes: int = PREVIEW_BYTES) -> bytes:
        return self._text()[1].encode()[:max_bytes]

    def fetch(self, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        name, text = self._text()
        (dest / name).write_text(text)
        return dest


ADAPTERS: dict[str, type[SourceAdapter]] = {
    "local_folder": LocalAdapter,
    "local_file": LocalAdapter,
    "web": WebAdapter,
    "gcs": GCSAdapter,
    "s3": S3Adapter,
    "report": InternalAdapter,
    "notebook": InternalAdapter,
}


def adapter_for(source: DataSource) -> SourceAdapter:
    if source.kind == "public_bucket":
        from deepresearch.sources.public import PublicBucketAdapter

        return PublicBucketAdapter(source)
    return ADAPTERS[source.kind](source)

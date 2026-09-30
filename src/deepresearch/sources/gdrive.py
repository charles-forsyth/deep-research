"""Google Drive sources: a Drive folder, or hand-picked Docs/Sheets/files.

Everything goes through an rclone Drive remote that is already signed in on this
machine (auth_ref "rclone:<remote>"), so no Google secret ever enters this program.
Google-native files are exported on the way out: Docs as Markdown, Sheets as CSV,
Slides and Drawings as PDF, so research runs, Ask and Lab jobs can read them.

URIs:
  gdrive://folder/<ID>           a folder or a shared drive (by ID), recursively
  gdrive://files/<ID>,<ID>,...   picked files; options.files keeps their names and
                                 parents so they can be found again without downloading

A cluster node has no Drive login, so these are always relayed (this machine fetches
and uploads them).
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from deepresearch.sources.adapters import (
    MANIFEST_LIMIT,
    PREVIEW_BYTES,
    SourceAdapter,
    SourceError,
    _filtered,
    _FullManifest,
    _manifest,
    _rclone_filters,
    _run,
    _run_bytes,
    safe_join,
)
from deepresearch.sources.model import DataSource, ManifestEntry

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
# what each Google-native type is exported as (anything else native is not readable)
EXPORT_EXT = {
    "application/vnd.google-apps.document": "md",
    "application/vnd.google-apps.spreadsheet": "csv",
    "application/vnd.google-apps.presentation": "pdf",
    "application/vnd.google-apps.drawing": "pdf",
}
EXPORT_FLAGS = [
    "--drive-export-formats",
    "md,csv,pdf",
    "--drive-skip-shortcuts",  # a shortcut can point at a whole other tree
    "--drive-skip-dangling-shortcuts",
]
FILES_MAX = 200  # picked files per source
PREVIEW_DOWNLOAD_MAX = 20 * 1024**2  # a non-native file is downloaded to preview it
ID_RE = re.compile(r"[A-Za-z0-9_-]{10,100}")


def is_native(mime: str) -> bool:
    return mime.startswith("application/vnd.google-apps.")


def readable(mime: str) -> bool:
    """Can this Drive item be fetched as a file (folders and forms cannot)?"""
    return not is_native(mime) or mime in EXPORT_EXT


def export_name(name: str, mime: str) -> str:
    """A safe local file name for the item: "/" and ":" become full-width look-alikes
    (Drive titles often hold "1:1 Agenda" or "A/B test"), native files get their
    export extension."""
    clean = name.replace("/", "\uff0f").replace(":", "\uff1a").replace("\\", "\uff3c")
    clean = clean.strip() or "untitled"
    if clean.startswith("."):
        clean = "_" + clean  # never a hidden file
    ext = EXPORT_EXT.get(mime)
    return f"{clean}.{ext}" if ext else clean


def quote(value: str) -> str:
    """A string literal for the Drive query language."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def query(remote: str, q: str, timeout: int = 90) -> list[dict[str, Any]]:
    """Run a Drive search through rclone (all pages); returns Drive file records."""
    out = _run(["rclone", "backend", "query", f"{remote}:", q], timeout=timeout)
    rows = json.loads(out or "null") or []
    return [r for r in rows if isinstance(r, dict)]


def parse_uri(uri: str) -> tuple[str, list[str]]:
    m = re.fullmatch(r"gdrive://(folder|files)/(.+)", uri.strip())
    if not m:
        raise SourceError(
            "Google Drive sources look like gdrive://folder/<ID> or gdrive://files/<ID>,..."
        )
    ids = [x for x in m.group(2).split(",") if x]
    if not ids or not all(ID_RE.fullmatch(x) for x in ids):
        raise SourceError("that is not a Google Drive file or folder ID")
    if m.group(1) == "folder" and len(ids) != 1:
        raise SourceError("a Drive folder source holds one folder")
    return m.group(1), ids


class DriveAdapter(SourceAdapter):
    def __init__(self, source: DataSource):
        super().__init__(source)
        self.mode, self.ids = parse_uri(source.uri)

    def _remote(self) -> str:
        ref = self.s.auth_ref or ""
        if not ref.startswith("rclone:") or not ref[7:]:
            raise SourceError("Google Drive sources need auth_ref 'rclone:<remote>'")
        return ref[7:]

    # -- picked files -------------------------------------------------------
    def _picked(self) -> list[dict[str, Any]]:
        meta = {
            str(f.get("id")): f
            for f in self.s.options.get("files") or []
            if isinstance(f, dict)
        }
        missing = [i for i in self.ids if i not in meta]
        if missing:
            raise SourceError(
                "add Drive files from the dashboard's file browser (it records their "
                "names so they can be found again)"
            )
        if len(self.ids) > FILES_MAX:
            raise SourceError(f"a Drive source holds up to {FILES_MAX} picked files")
        return [meta[i] for i in self.ids]

    def _locate(self) -> dict[str, dict[str, Any]]:
        """Current Drive records for the picked files, by ID.

        Drive search cannot look a file up by ID, so each file is found in its parent
        folder (survives renames) or, failing that, by its name (survives moves).
        """
        remote = self._remote()
        picked = self._picked()
        found: dict[str, dict[str, Any]] = {}
        parents = sorted({p for f in picked for p in f.get("parents") or [] if p})
        for p in parents:
            if not ID_RE.fullmatch(p) and p != "root":
                continue
            try:
                rows = query(remote, f"{quote(p)} in parents and trashed=false")
            except SourceError:
                continue  # a parent we cannot list (shared with me): try by name
            for r in rows:
                if r.get("id") in self.ids:
                    found[r["id"]] = r
        todo = [f for f in picked if f["id"] not in found]
        for i in range(0, len(todo), 20):
            chunk = todo[i : i + 20]
            q = " or ".join(f"name = {quote(str(f.get('name') or ''))}" for f in chunk)
            for r in query(remote, f"({q}) and trashed=false"):
                if r.get("id") in self.ids:
                    found[r["id"]] = r
        lost = [str(f.get("name") or f["id"]) for f in picked if f["id"] not in found]
        if lost:
            raise SourceError(
                f"not found in Drive any more (deleted, trashed, unshared, or renamed "
                f"and moved): {', '.join(lost[:5])}"
            )
        return found

    def _entries(self) -> list[tuple[ManifestEntry, str]]:
        """(entry, drive id) per picked file; duplicate names get the ID prefixed."""
        found = self._locate()
        out: list[tuple[ManifestEntry, str]] = []
        seen: dict[str, int] = {}
        recs = [found[i] for i in self.ids]
        for r in recs:
            n = export_name(r.get("name") or r["id"], r.get("mimeType") or "")
            seen[n] = seen.get(n, 0) + 1
        for r in recs:
            mime = r.get("mimeType") or ""
            if not readable(mime):
                raise SourceError(f"'{r.get('name')}' ({mime}) cannot be exported")
            n = export_name(r.get("name") or r["id"], mime)
            path = f"{r['id'][:8]}/{n}" if seen[n] > 1 else n
            out.append(
                (
                    ManifestEntry(
                        path=path,
                        size=int(r.get("size") or 0),
                        modified=r.get("modifiedTime") or "",
                    ),
                    r["id"],
                )
            )
        return out

    # -- SourceAdapter ------------------------------------------------------
    def manifest(self, limit: int = MANIFEST_LIMIT) -> _FullManifest:
        if self.mode == "files":
            entries = [e for e, _ in self._entries() if _filtered(self.s, e.path)]
            return _manifest(entries[:limit], len(entries) > limit)
        out = _run(
            [
                "rclone",
                "lsjson",
                "-R",
                "--files-only",
                *EXPORT_FLAGS,
                "--drive-root-folder-id",
                self.ids[0],
                f"{self._remote()}:",
            ],
            timeout=300,
        )
        rows = json.loads(out or "[]")
        entries = [
            ManifestEntry(
                path=r["Path"],
                size=max(int(r.get("Size") or 0), 0),  # -1 = native, size unknown
                modified=r.get("ModTime", ""),
            )
            for r in rows
            if _filtered(self.s, r["Path"])
        ]
        return _manifest(entries[:limit], len(entries) > limit)

    def _copyid(self, file_id: str, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        before = set(dest_dir.iterdir())
        _run(
            [
                "rclone",
                "backend",
                "copyid",
                f"{self._remote()}:",
                file_id,
                str(dest_dir) + "/",
                *EXPORT_FLAGS,
            ],
            timeout=1800,
        )
        new = [p for p in dest_dir.iterdir() if p not in before]
        if not new:
            raise SourceError(f"Drive returned nothing for file {file_id}")
        return new[0]

    def preview(self, path: str = "", max_bytes: int = PREVIEW_BYTES) -> bytes:
        rel = path.strip("/")
        if ".." in rel.split("/"):
            raise SourceError("path escapes the source")
        if self.mode == "folder":
            return _run_bytes(
                [
                    "rclone",
                    "cat",
                    "--count",
                    str(max_bytes),
                    *EXPORT_FLAGS,
                    "--drive-root-folder-id",
                    self.ids[0],
                    f"{self._remote()}:{rel}",
                ],
                timeout=120,
            )[:max_bytes]
        for e, fid in self._entries():
            if e.path == rel or (not rel and e is not None):
                if e.size > PREVIEW_DOWNLOAD_MAX:
                    raise SourceError("too big to preview")
                with tempfile.TemporaryDirectory(prefix="dr-drive-") as tmp:
                    with open(self._copyid(fid, Path(tmp)), "rb") as fh:
                        return fh.read(max_bytes)
        raise SourceError(f"{rel} is not in this source")

    def fetch(self, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        if self.mode == "folder":
            _run(
                [
                    "rclone",
                    "copy",
                    *EXPORT_FLAGS,
                    *_rclone_filters(self.s),
                    "--drive-root-folder-id",
                    self.ids[0],
                    f"{self._remote()}:",
                    str(dest),
                ],
                timeout=3600,
            )
            return dest
        for e, fid in self._entries():
            if not _filtered(self.s, e.path):
                continue
            with tempfile.TemporaryDirectory(prefix="dr-drive-") as tmp:
                got = self._copyid(fid, Path(tmp))
                out = safe_join(dest, e.path)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(got), out)
        return dest

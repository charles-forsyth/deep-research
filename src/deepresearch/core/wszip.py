"""Export and import a workspace as a zip (v0.43.0).

Export (read-only for the workspace):
    <name>-<date>.drws.zip
      manifest.json      format, app version, workspace name/description, created_at,
                         counts, and sha256 + size for every other file
      history.db         consistent SQLite copy (online backup API)
      lab/run_<n>/...    fetched Lab outputs, logs and plans
      audio/...          only with include_audio (large; otherwise remade on request)
      uploads/...        only with include_uploads
Personal fields are stripped from the exported DB copy (never from the workspace):
session `pid`, source `auth_ref`, source `options.store` / `store_hash` / `db_path`
(Gemini index ids belong to your API key), and audio rows when audio is left out.
Nothing machine-wide is ever included: no .env, no API key, no lab_targets.json.

Import (a zip from anyone, so treated as untrusted):
  - always creates a NEW workspace (never merges into or overwrites an existing one)
  - every member name is checked: relative, no "..", no absolute paths, no symlinks or
    devices, only under history.db / lab/ / audio/ / uploads/ / manifest.json
  - size limits (default 5 GB unpacked, 200k files) and a compression-ratio check
  - every file is verified against the manifest's sha256; extra or missing files fail
  - history.db must open, pass `PRAGMA integrity_check`, and have a `sessions` table
  - unpacked into a hidden temp folder and moved into place only when all checks pass
  - Lab runs that were in flight are turned into drafts, running reports into crashed,
    and audio paths are pointed at the new workspace's folder
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from deepresearch.core import workspace as W

FORMAT = "deep-research-workspace"
FORMAT_VERSION = 1
MAX_UNPACKED = 5 * 1024**3
MAX_FILES = 200_000
MAX_RATIO = 200  # unpacked/compressed; zip bombs go far beyond this
ALLOWED_TOP = ("history.db", "manifest.json", "lab", "audio", "uploads")
ACTIVE = (
    "planning",
    "submitting",
    "smoke",
    "queued",
    "running",
    "fetching",
    "analyzing",
)


class ZipError(ValueError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _counts(db: str) -> dict:
    out = {}
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for key, sql in (
            ("reports", "SELECT COUNT(*) FROM sessions WHERE parent_id IS NULL"),
            ("sessions", "SELECT COUNT(*) FROM sessions"),
            ("projects", "SELECT COUNT(*) FROM projects"),
            ("lab_runs", "SELECT COUNT(*) FROM lab_runs"),
            ("sources", "SELECT COUNT(*) FROM data_sources"),
            ("notebooks", "SELECT COUNT(*) FROM notebooks"),
            ("annotations", "SELECT COUNT(*) FROM annotations"),
        ):
            try:
                out[key] = int(c.execute(sql).fetchone()[0])
            except sqlite3.Error:
                out[key] = 0
    finally:
        c.close()
    return out


def _has(c: sqlite3.Connection, table: str) -> bool:
    return bool(
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _scrub(db: str, keep_audio: bool) -> None:
    """Remove machine- and key-specific fields from an exported DB copy."""
    c = sqlite3.connect(db)
    try:
        if _has(c, "sessions"):
            c.execute("UPDATE sessions SET pid = NULL")
        if _has(c, "data_sources"):
            for sid, opts in c.execute(
                "SELECT id, options FROM data_sources"
            ).fetchall():
                try:
                    o = json.loads(opts or "{}")
                except ValueError:
                    o = {}
                for k in ("store", "store_hash", "db_path"):
                    o.pop(k, None)
                c.execute(
                    "UPDATE data_sources SET options=?, auth_ref='' WHERE id=?",
                    (json.dumps(o), sid),
                )
        if _has(c, "audio_exports") and not keep_audio:
            c.execute("DELETE FROM audio_exports")
        c.commit()
        c.execute("VACUUM")
    finally:
        c.close()


def export(slug: str, dest: str | Path, include_audio: bool = False,
           include_uploads: bool = False) -> dict:  # fmt: skip
    """Write the workspace to a zip file. Returns the manifest."""
    ws = W.get(slug)
    if not Path(ws.db_path).exists():
        raise ZipError(f"workspace {ws.slug!r} has no database yet")
    dest = Path(dest)
    # a folder (existing, or any path not ending in .zip) gets a dated file name
    if dest.is_dir() or not dest.name.lower().endswith(".zip"):
        dest = dest / f"{ws.slug}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.drws.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="drws-export-") as tmp:
        db = Path(tmp) / "history.db"
        W.sqlite_copy(ws.db_path, str(db))
        _scrub(str(db), keep_audio=include_audio)
        members: list[tuple[str, Path]] = [("history.db", db)]
        subs = (
            ["lab"]
            + (["audio"] if include_audio else [])
            + (["uploads"] if include_uploads else [])
        )
        for sub in subs:
            root = ws.root / sub
            if not root.is_dir():
                continue
            for f in sorted(root.rglob("*")):
                if f.is_symlink() or not f.is_file():
                    continue  # never follow or ship links
                rel = f.relative_to(ws.root).as_posix()
                members.append((rel, f))
        files = {}
        for rel, f in members:
            files[rel] = {"sha256": _sha256(f), "size": f.stat().st_size}
        from deepresearch import __version__

        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "app_version": __version__,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "workspace": {
                "id": ws.slug,
                "name": ws.name,
                "description": ws.description,
                "color": ws.color,
            },  # fmt: skip
            "counts": _counts(str(db)),
            "includes": {"audio": include_audio, "uploads": include_uploads},
            "files": files,
        }
        part = dest.with_name(dest.name + ".part")
        with zipfile.ZipFile(
            part, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        ) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
            for rel, f in members:
                z.write(f, rel)
        part.replace(dest)
    manifest["path"] = str(dest)
    manifest["bytes"] = dest.stat().st_size
    return manifest


def _safe_name(name: str) -> str:
    """A member name that is safe to unpack, or raise."""
    if not name or "\x00" in name or "\\" in name:
        raise ZipError(f"unsafe file name in zip: {name!r}")
    p = PurePosixPath(name)
    if p.is_absolute() or any(part in ("..", "") for part in p.parts if part != "."):
        raise ZipError(f"unsafe path in zip: {name!r}")
    if len(p.parts) > 1 and p.parts[0] not in ALLOWED_TOP:
        raise ZipError(f"unexpected folder in zip: {name!r}")
    if len(p.parts) == 1 and p.parts[0] not in ("history.db", "manifest.json"):
        raise ZipError(f"unexpected file in zip: {name!r}")
    return p.as_posix()


def inspect(src: str | Path) -> dict:
    """Read and check a workspace zip's manifest and member list (nothing unpacked)."""
    src = Path(src)
    if not zipfile.is_zipfile(src):
        raise ZipError("not a zip file")
    with zipfile.ZipFile(src) as z:
        try:
            manifest = json.loads(z.read("manifest.json"))
        except KeyError:
            raise ZipError(
                "not a deep-research workspace zip (no manifest.json)"
            ) from None
        except ValueError:
            raise ZipError("manifest.json is not valid JSON") from None
        if manifest.get("format") != FORMAT:
            raise ZipError("not a deep-research workspace zip (wrong format)")
        if int(manifest.get("format_version") or 0) > FORMAT_VERSION:
            raise ZipError(
                "this zip was made by a newer deep-research; update before importing"
            )
        total, count = 0, 0
        names = set()
        for info in z.infolist():
            name = _safe_name(info.filename)
            if info.is_dir():
                continue
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind and kind != stat.S_IFREG:  # 0 = no Unix mode recorded (plain file)
                raise ZipError(f"links and special files are not allowed: {name!r}")
            total += info.file_size
            count += 1
            if (
                info.compress_size
                and info.file_size / max(1, info.compress_size) > MAX_RATIO
            ):
                raise ZipError(f"suspicious compression ratio for {name!r}")
            names.add(name)
        if count > MAX_FILES:
            raise ZipError(f"too many files ({count})")
        if total > MAX_UNPACKED:
            raise ZipError(f"too large when unpacked ({total / 1e9:.1f} GB)")
        files = manifest.get("files") or {}
        if not isinstance(files, dict) or "history.db" not in files:
            raise ZipError("manifest lists no history.db")
        listed = set(files)
        present = names - {"manifest.json"}
        if listed != present:
            extra, missing = sorted(present - listed)[:5], sorted(listed - present)[:5]
            raise ZipError(
                f"zip contents do not match its manifest (extra {extra}, missing {missing})"
            )
        for n in listed:
            _safe_name(n)
    return {**manifest, "unpacked_bytes": total, "file_count": count}


def import_zip(
    src: str | Path, name: str | None = None, slug: str | None = None
) -> W.Workspace:
    """Create a new workspace from a zip. Never touches an existing workspace."""
    man = inspect(src)
    wsinfo = man.get("workspace") or {}
    name = (name or str(wsinfo.get("name") or "Imported")).strip()[:80] or "Imported"
    base_slug = W.validate_slug(slug) if slug else W.slugify(name)
    s, i = base_slug, 2
    while W.exists(s) or (W.spaces_dir() / s).exists() or s == W.MAIN:
        if slug:
            raise ZipError(f"a workspace named {s!r} already exists")
        s = f"{base_slug[:36]}-{i}"
        i += 1
    W.spaces_dir().mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".import-{s}-", dir=W.spaces_dir()))
    try:
        with zipfile.ZipFile(src) as z:
            for info in z.infolist():
                if info.is_dir() or info.filename == "manifest.json":
                    continue
                rel = _safe_name(info.filename)
                out = stage / rel
                # resolve() guards against anything the name checks missed
                if not out.resolve().is_relative_to(stage.resolve()):
                    raise ZipError(f"unsafe path in zip: {rel!r}")
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as fin, open(out, "wb") as fout:
                    shutil.copyfileobj(fin, fout, 1 << 20)
                meta = man["files"][rel]
                if out.stat().st_size != int(meta.get("size", -1)) or _sha256(
                    out
                ) != meta.get("sha256"):
                    raise ZipError(
                        f"checksum mismatch for {rel!r} (damaged or altered zip)"
                    )
        db = stage / "history.db"
        try:
            c = sqlite3.connect(db)
            ok = c.execute("PRAGMA integrity_check").fetchone()[0]
            has_sessions = _has(c, "sessions")
            c.close()
        except sqlite3.Error as e:
            raise ZipError(f"history.db is not a valid database: {e}") from e
        if ok != "ok" or not has_sessions:
            raise ZipError("history.db failed its integrity check")
        for sub in ("lab", "audio", "uploads", "logs"):
            (stage / sub).mkdir(exist_ok=True)
        ws = W.Workspace(
            slug=s,
            name=name,
            color=str(wsinfo.get("color") or "violet")
            if str(wsinfo.get("color") or "") in W.COLORS
            else "violet",
            created_at=datetime.now().isoformat(timespec="seconds"),
            description=str(wsinfo.get("description") or "")[:500],
            extra={
                "imported_from": str(Path(src).name),
                "imported_at": datetime.now().isoformat(timespec="seconds"),
                "source_app_version": str(man.get("app_version") or ""),
            },  # fmt: skip
        )
        (stage / "workspace.json").write_text(json.dumps({
            "name": ws.name, "color": ws.color, "archived": False,
            "created_at": ws.created_at, "description": ws.description, **ws.extra,
        }, indent=2))  # fmt: skip
        dest = W.spaces_dir() / s
        if dest.exists():
            raise ZipError(f"a workspace named {s!r} appeared meanwhile")
        os.replace(stage, dest)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    _settle(ws)
    W.init_db(ws)
    return W.get(s)


def _settle(ws: W.Workspace) -> None:
    """Make an imported DB safe to open here: nothing looks like it is running."""
    c = sqlite3.connect(ws.db_path)
    try:
        if _has(c, "sessions"):
            c.execute("UPDATE sessions SET pid = NULL")
            c.execute("UPDATE sessions SET status='crashed' WHERE status='running'")
        if _has(c, "lab_runs"):
            marks = ",".join("?" * len(ACTIVE))
            c.execute(
                f"UPDATE lab_runs SET status='draft', job_id=NULL, slurm_state=NULL, "
                f"stage='Imported while it was running; review, then submit' "
                f"WHERE status IN ({marks})",
                ACTIVE,
            )
        if _has(c, "audio_exports"):
            for rid, p in c.execute("SELECT id, path FROM audio_exports").fetchall():
                fname = Path(p or "").name
                newp = ws.audio_dir / fname
                if fname and newp.exists():
                    c.execute(
                        "UPDATE audio_exports SET path=? WHERE id=?", (str(newp), rid)
                    )
                else:
                    c.execute("DELETE FROM audio_exports WHERE id=?", (rid,))
        c.commit()
    finally:
        c.close()

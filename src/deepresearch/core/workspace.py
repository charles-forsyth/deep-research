"""Workspaces: separate libraries of reports, projects, notes, Lab runs and sources.

Main is the library that existed before workspaces (v0.40.0). Its files stay exactly where
they always were, in ~/.config/deepresearch/ (history.db, lab/, audio/, uploads/, logs/),
so nothing is moved or migrated and an older version still reads it. Every other
workspace lives in its own folder:

    ~/.config/deepresearch/workspaces/<slug>/
        workspace.json   name, colour, archived, created_at
        history.db       its own reports, projects, notes, Lab runs, data sources, audio rows
        lab/ audio/ uploads/ logs/

Shared by all workspaces (one per machine): the Gemini key (.env), the cluster targets
(lab_targets.json), cluster catalogs and the Lab's learned lessons (lab_pitfalls.json).

Which workspace a process uses: `--workspace` on the command line, else the
DR_WORKSPACE environment variable, else Main. The dashboard picks it per request (the
X-DR-Workspace header set by the switcher) and passes it to every research run it starts,
so a run always writes to the workspace it began in.

Deleting a workspace never removes files: its folder moves to workspaces/.trash/. Main
cannot be deleted, renamed or archived.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from deepresearch.core.config import xdg_config_home

MAIN = "main"
ENV_VAR = "DR_WORKSPACE"
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
COLORS = ("slate", "teal", "violet", "amber", "rose", "green")


def base_dir() -> Path:
    """~/.config/deepresearch (read at call time so tests can point XDG elsewhere)."""
    return Path(os.getenv("XDG_CONFIG_HOME") or xdg_config_home) / "deepresearch"


def spaces_dir() -> Path:
    return base_dir() / "workspaces"


class WorkspaceError(ValueError):
    pass


@dataclass
class Workspace:
    slug: str
    name: str
    color: str = "slate"
    archived: bool = False
    created_at: str = ""
    description: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def is_main(self) -> bool:
        return self.slug == MAIN

    @property
    def root(self) -> Path:
        return base_dir() if self.is_main else spaces_dir() / self.slug

    @property
    def db_path(self) -> str:
        return str(self.root / "history.db")

    @property
    def lab_dir(self) -> Path:
        return self.root / "lab"

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def uploads_dir(self) -> Path:
        return self.root / "uploads"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def remote_prefix(self) -> str:
        """Sub-folder for this workspace's Lab runs on the cluster ('' for Main, whose
        runs keep their original <remote_root>/run_<id> folders)."""
        return "" if self.is_main else f"ws-{self.slug}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("extra", None)
        d.update(is_main=self.is_main, root=str(self.root))
        return d


def _meta_path(slug: str) -> Path:
    # Main's metadata lives beside the other workspaces so Main's own folder is not touched
    return spaces_dir() / (f"{MAIN}.json" if slug == MAIN else f"{slug}/workspace.json")


def _read_meta(slug: str) -> dict:
    p = _meta_path(slug)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def _write_meta(ws: Workspace) -> None:
    p = _meta_path(ws.slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    d = {
        "name": ws.name,
        "color": ws.color,
        "archived": ws.archived,
        "created_at": ws.created_at,
        "description": ws.description,
        **ws.extra,
    }
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(p)


def _from_meta(slug: str) -> Workspace:
    m = _read_meta(slug)
    known = {"name", "color", "archived", "created_at", "description"}
    return Workspace(
        slug=slug,
        name=str(m.get("name") or ("Main" if slug == MAIN else slug)),
        color=str(m.get("color") or "slate"),
        archived=bool(m.get("archived")) and slug != MAIN,
        created_at=str(m.get("created_at") or ""),
        description=str(m.get("description") or ""),
        extra={k: v for k, v in m.items() if k not in known},
    )


def validate_slug(slug: str) -> str:
    s = (slug or "").strip().lower()
    if not SLUG_RE.match(s):
        raise WorkspaceError(
            "workspace ids are 1-40 lower-case letters, digits and dashes "
            f"(not starting or ending with a dash): {slug!r}"
        )
    return s


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40].strip("-")
    return s or "workspace"


def exists(slug: str) -> bool:
    if slug == MAIN:
        return True
    return (spaces_dir() / slug / "workspace.json").exists()


def get(slug: str | None = None) -> Workspace:
    """The workspace `slug` (default: the current one). Raises if it does not exist."""
    s = current_slug() if slug is None else validate_slug(slug)
    if not exists(s):
        raise WorkspaceError(
            f"no workspace named {s!r} (see `deep-research workspace list`)"
        )
    return _from_meta(s)


def list_all(include_archived: bool = True) -> list[Workspace]:
    out = [_from_meta(MAIN)]
    d = spaces_dir()
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if (
                p.is_dir()
                and not p.name.startswith(".")
                and (p / "workspace.json").exists()
            ):
                if SLUG_RE.match(p.name) and p.name != MAIN:
                    out.append(_from_meta(p.name))
    return [w for w in out if include_archived or not w.archived]


def current_slug() -> str:
    s = (os.getenv(ENV_VAR) or "").strip().lower()
    return s or MAIN


def use(slug: str) -> Workspace:
    """Make `slug` this process's workspace (the CLI's --workspace). Children inherit it."""
    ws = get(slug)
    if ws.archived:
        raise WorkspaceError(f"workspace {ws.slug!r} is archived; unarchive it first")
    os.environ[ENV_VAR] = ws.slug
    return ws


def create(
    name: str, slug: str | None = None, color: str = "teal", description: str = ""
) -> Workspace:
    """A new, empty workspace. Never touches an existing folder."""
    s = validate_slug(slug or slugify(name))
    if s == MAIN:
        raise WorkspaceError("'main' is reserved for the original library")
    if s.startswith("."):
        raise WorkspaceError("invalid workspace id")
    root = spaces_dir() / s
    if root.exists():
        raise WorkspaceError(f"a workspace folder named {s!r} already exists")
    ws = Workspace(
        slug=s,
        name=(name or s).strip()[:80] or s,
        color=color if color in COLORS else "teal",
        created_at=datetime.now().isoformat(timespec="seconds"),
        description=(description or "").strip()[:500],
    )
    root.mkdir(parents=True)
    for sub in ("lab", "audio", "uploads", "logs"):
        (root / sub).mkdir()
    _write_meta(ws)
    init_db(ws)
    return ws


def init_db(ws: Workspace) -> None:
    """Create every table a workspace uses, so a fresh workspace works for the CLI too."""
    from deepresearch.storage.database import DatabaseSchema

    DatabaseSchema.init_db(ws.db_path)


def update(slug: str, name: str | None = None, color: str | None = None,
           archived: bool | None = None, description: str | None = None) -> Workspace:  # fmt: skip
    ws = get(slug)
    if name is not None:
        n = name.strip()[:80]
        if not n:
            raise WorkspaceError("a workspace needs a name")
        ws.name = n
    if color is not None:
        if color not in COLORS:
            raise WorkspaceError(f"colour must be one of {', '.join(COLORS)}")
        ws.color = color
    if description is not None:
        ws.description = description.strip()[:500]
    if archived is not None:
        if ws.is_main and archived:
            raise WorkspaceError("Main cannot be archived")
        ws.archived = bool(archived)
    _write_meta(ws)
    return ws


def trash(slug: str) -> Path:
    """Move a workspace's folder to workspaces/.trash/ (never deleted). Returns where."""
    ws = get(slug)
    if ws.is_main:
        raise WorkspaceError("Main cannot be deleted")
    dest_dir = spaces_dir() / ".trash"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{ws.slug}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    shutil.move(str(ws.root), str(dest))
    return dest


def sqlite_copy(src: str, dst: str) -> None:
    """Consistent copy of a live SQLite DB (online backup API; safe while in use)."""
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def duplicate(slug: str, name: str, new_slug: str | None = None) -> Workspace:
    """A full copy of a workspace (DB, Lab outputs, audio) under a new id. The source is
    only read. Cluster folders are per workspace, so the copy's Lab runs are new runs
    as far as the cluster is concerned (their outputs are already copied here)."""
    src = get(slug)
    ws = create(name, new_slug, color="teal", description=f"Copy of {src.name}")
    try:
        sqlite_copy(src.db_path, ws.db_path) if Path(src.db_path).exists() else None
        for sub in ("lab", "audio"):
            a = src.root / sub
            if a.is_dir():
                shutil.rmtree(ws.root / sub)
                shutil.copytree(a, ws.root / sub, symlinks=False)
        rewrite_audio_paths(ws, src)
    except Exception:
        # leave no half-made workspace behind: move it to the trash, keep the source
        trash(ws.slug)
        raise
    return ws


def rewrite_audio_paths(ws: Workspace, src: Workspace) -> None:
    """audio_exports.path is absolute; point copied rows at the copy's audio folder."""
    with sqlite3.connect(ws.db_path) as c:
        has = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audio_exports'"
        ).fetchone()
        if not has:
            return
        old, new = str(src.audio_dir), str(ws.audio_dir)
        for rid, p in c.execute("SELECT id, path FROM audio_exports").fetchall():
            if p and p.startswith(old):
                c.execute(
                    "UPDATE audio_exports SET path=? WHERE id=?",
                    (new + p[len(old) :], rid),
                )


def db_path() -> str:
    """The current workspace's history DB (process-wide: CLI and research runs)."""
    return get().db_path

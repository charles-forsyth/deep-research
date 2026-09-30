"""`deep-research workspace ...`: list, create, duplicate, rename, archive, delete."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from deepresearch.core import workspace as W


def _json(args) -> bool:
    return bool(getattr(args, "json", False))


def _emit(args, obj) -> None:
    from deepresearch.cli.jsonout import emit

    emit(obj)


def stats(ws: W.Workspace) -> dict:
    """Counts and size for a workspace (read-only; missing DB = empty)."""
    out = {"reports": 0, "projects": 0, "lab_runs": 0, "sources": 0, "bytes": 0}
    p = Path(ws.db_path)
    if p.exists():
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
            for key, sql in (
                ("reports", "SELECT COUNT(*) FROM sessions WHERE parent_id IS NULL"),
                ("projects", "SELECT COUNT(*) FROM projects WHERE archived = 0"),
                ("lab_runs", "SELECT COUNT(*) FROM lab_runs"),
                ("sources", "SELECT COUNT(*) FROM data_sources WHERE temporary = 0"),
            ):
                try:
                    out[key] = int(c.execute(sql).fetchone()[0])
                except sqlite3.Error:
                    pass
            c.close()
        except sqlite3.Error:
            pass
    total = 0
    for sub in ("history.db", "lab", "audio", "uploads"):
        q = ws.root / sub
        if q.is_file():
            total += q.stat().st_size
        elif q.is_dir():
            total += sum(f.stat().st_size for f in q.rglob("*") if f.is_file())
    out["bytes"] = total
    return out


def handle(args) -> int:
    cmd = getattr(args, "ws_command", None) or "list"
    try:
        if cmd == "list":
            rows = []
            cur = W.current_slug()
            for ws in W.list_all(include_archived=getattr(args, "all", False)):
                rows.append({**ws.to_dict(), **stats(ws), "current": ws.slug == cur})
            if _json(args):
                _emit(args, {"workspaces": rows})
                return 0
            for r in rows:
                mark = "*" if r["current"] else " "
                arch = " (archived)" if r["archived"] else ""
                print(
                    f"{mark} {r['slug']:<20} {r['name']}{arch}  -  {r['reports']} reports, "
                    f"{r['projects']} projects, {r['lab_runs']} Lab runs, "
                    f"{r['bytes'] / 1e6:.1f} MB"
                )
            return 0
        if cmd == "create":
            ws = W.create(args.name, args.slug, description=args.description)
            return _done(args, ws, f"Created workspace {ws.slug!r} at {ws.root}")
        if cmd == "duplicate":
            ws = W.duplicate(args.source, args.name, args.slug)
            return _done(
                args, ws, f"Copied {args.source!r} to {ws.slug!r} at {ws.root}"
            )
        if cmd == "rename":
            ws = W.update(args.id, name=args.name)
            return _done(args, ws, f"Renamed {ws.slug!r} to {ws.name!r}")
        if cmd in ("archive", "unarchive"):
            ws = W.update(args.id, archived=cmd == "archive")
            return _done(args, ws, f"{cmd.capitalize()}d {ws.slug!r}")
        if cmd == "delete":
            ws = W.get(args.id)
            if ws.is_main:
                raise W.WorkspaceError("Main cannot be deleted")
            if not args.yes:
                if not sys.stdin.isatty():
                    raise W.WorkspaceError("add --yes to delete without a prompt")
                ans = input(
                    f"Move workspace {ws.slug!r} ({ws.name}) to the trash? Type its id: "
                )
                if ans.strip() != ws.slug:
                    print("Not deleted.")
                    return 1
            dest = W.trash(ws.slug)
            if _json(args):
                _emit(args, {"deleted": ws.slug, "moved_to": str(dest)})
            else:
                print(f"Moved {ws.slug!r} to {dest} (nothing was erased)")
            return 0
    except W.WorkspaceError as e:
        if _json(args):
            _emit(args, {"error": str(e)})
        else:
            print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    print(f"[ERROR] unknown workspace command {cmd!r}", file=sys.stderr)
    return 2


def _done(args, ws: W.Workspace, msg: str) -> int:
    if _json(args):
        _emit(args, {"workspace": {**ws.to_dict(), **stats(ws)}})
    else:
        print(msg)
    return 0

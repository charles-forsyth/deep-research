"""`deep-research projects ...` (v0.47.0): list, show, create, add, remove, export.

Uses the dashboard's own API code in-process (no server needed, no HTTP), so the CLI
and the dashboard can never disagree about what a project holds. The workspace comes
from `-W/--workspace` or DR_WORKSPACE like every other command. Read commands never
write; `create`, `add` and `remove` change only project membership (never a report).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _json(args) -> bool:
    return bool(getattr(args, "json", False))


def _emit(obj: Any) -> None:
    from deepresearch.cli.jsonout import emit

    emit(obj)


def _api():
    """An Api bound to the current workspace, without starting watchers or servers."""
    from deepresearch.core import workspace as W
    from deepresearch.dashboard import server as srv
    from deepresearch.dashboard.lab import Lab

    ws = W.get()
    lab = Lab(
        ws.db_path,
        lambda: None,
        srv.STATE_DIR,
        results_dir=ws.lab_dir,
        workspace=ws.slug,
    )
    lab.ensure_watcher = lambda: None  # type: ignore[method-assign]
    lab.auto_review = False
    api = srv.Api(ws.db_path, spawn=lambda *a: None, lab=lab)
    return api


def _call(api, method: str, path: str, body: Any = None, query: dict | None = None):
    code, out = api.dispatch(method, path, query or {}, body)
    return out


def _find(api, ref: str) -> dict:
    """A project by id or (case-insensitive, unique prefix of) title."""
    from deepresearch.dashboard.server import ApiError

    rows = _call(api, "GET", "/api/projects", query={"archived": ["1"]})["projects"]
    if ref.isdigit():
        for p in rows:
            if p["id"] == int(ref):
                return p
        raise ApiError(404, f"no project #{ref}")
    low = ref.lower().strip()
    exact = [p for p in rows if p["title"].lower() == low]
    if exact:
        return exact[0]
    pre = [p for p in rows if p["title"].lower().startswith(low)]
    if len(pre) == 1:
        return pre[0]
    if not pre:
        raise ApiError(404, f"no project matches {ref!r}")
    raise ApiError(
        409,
        f"{ref!r} matches {len(pre)} projects: "
        + ", ".join(f"#{p['id']} {p['title']}" for p in pre[:5]),
    )


def _print_list(rows: list[dict], inbox: int) -> None:
    if not rows:
        print('No projects yet. Create one: deep-research projects create "Title"')
    for p in rows:
        c = p.get("counts") or {}
        flags = " (archived)" if p.get("archived") else ""
        print(
            f"#{p['id']:<4} {p['title']}{flags}\n"
            f"      {c.get('session', 0)} reports, {c.get('source', 0)} sources, "
            f"{c.get('notebook', 0)} notebooks, {p.get('protection_level') or 'P2'}"
        )
    print(f"\nInbox: {inbox} report(s) in no project.")


def _print_show(d: dict) -> None:
    p = d["project"]
    print(
        f"#{p['id']} {p['title']}  [{d.get('effective_level') or p.get('protection_level')}]"
    )
    if p.get("description"):
        print(f"  {p['description']}")
    print(f"\nReports ({len(d['reports'])}):")
    for r in d["reports"]:
        home = " home" if r.get("is_home") else ""
        print(
            f"  #{r['id']:<5} {r['status']:<10}{home:5} {(r['prompt'] or '').splitlines()[0][:100]}"
        )
    if d["sources"]:
        print(f"\nData sources ({len(d['sources'])}):")
        for s in d["sources"]:
            print(
                f"  {s['name']:<32} {s['kind']:<8} {s['status'] or '':<12} {s['uri'][:70]}"
            )
    cb = d.get("claims") or {}
    if cb.get("total"):
        c = cb["counts"]
        print(
            f"\nClaims tested ({cb['total']}): {c['confirmed']} confirmed, {c['refuted']} "
            f"refuted, {c['inconclusive']} inconclusive, {c['pending']} pending, "
            f"{c['broken']} broken"
        )
        for cl in cb["claims"]:
            print(
                f"  {cl['outcome'].upper():<12} Lab #{cl['run_id']:<4} {cl['question'][:95]}"
            )
    if d["lab_runs"]:
        print(
            f"\nLab runs: {len(d['lab_runs'])} (see `deep-research projects show {p['id']} --json`)"
        )
    if d["notebooks"]:
        print(f"Notebooks: {', '.join(n['title'] for n in d['notebooks'])}")
    if d.get("summary_stale"):
        print("\nAI summary is out of date (regenerate it in the dashboard).")


EXPORT_EXT = {"md": "md", "json": "json", "bib": "bib", "csv": "csv", "zip": "zip"}


def handle(args) -> int:
    from deepresearch.dashboard.server import ApiError, RawResponse

    cmd = getattr(args, "pj_command", None) or "list"
    try:
        api = _api()
        if cmd == "list":
            q = {"archived": ["1"]} if getattr(args, "all", False) else {}
            out = _call(api, "GET", "/api/projects", query=q)
            if _json(args):
                _emit(out)
            else:
                _print_list(out["projects"], out["inbox"])
            return 0
        if cmd == "show":
            p = _find(api, args.project)
            d = _call(api, "GET", f"/api/projects/{p['id']}")
            if _json(args):
                _emit(d)
            else:
                _print_show(d)
            return 0
        if cmd == "create":
            body: dict[str, Any] = {"title": args.title}
            if args.description:
                body["description"] = args.description
            if args.level:
                body["protection_level"] = args.level
            if args.report:
                body["sessions"] = [int(x) for x in args.report]
            if args.source:
                body["sources"] = list(args.source)
            out = _call(api, "POST", "/api/projects", body)
            if _json(args):
                _emit(out)
            else:
                print(f"Created project #{out['id']} {out['title']}")
            return 0
        if cmd in ("add", "remove"):
            p = _find(api, args.project)
            done: list[str] = []
            method = "POST" if cmd == "add" else "DELETE"
            path = f"/api/projects/{p['id']}/items"
            for sid in args.report or []:
                _call(api, method, path, {"kind": "session", "ids": [int(sid)]})
                done.append(f"report #{sid}")
            for name in args.source or []:
                if not api.sources.get(name):
                    raise ApiError(404, f"no data source {name!r}")
                _call(api, method, path, {"kind": "source", "ids": [name]})
                done.append(f"source {name}")
            if not done:
                raise ApiError(
                    400, "nothing to do: give --report ID and/or --source NAME"
                )
            d = _call(api, "GET", f"/api/projects/{p['id']}")
            if _json(args):
                _emit(
                    {
                        "project": d["project"],
                        "added" if cmd == "add" else "removed": done,
                    }
                )
            else:
                verb = "Added" if cmd == "add" else "Removed"
                print(
                    f"{verb} {', '.join(done)} {'to' if cmd == 'add' else 'from'} #{p['id']} {p['title']}"
                )
            return 0
        if cmd == "export":
            p = _find(api, args.project)
            fmt = args.format
            out = _call(
                api, "GET", f"/api/projects/{p['id']}/export", query={"format": [fmt]}
            )
            if isinstance(out, RawResponse):
                data, fname = out.data, out.filename or f"project-{p['id']}.{fmt}"
            else:
                content = out["content"]
                data = (
                    json.dumps(content, indent=2, default=str)
                    if not isinstance(content, str)
                    else content
                ).encode()
                fname = out["filename"]
            if str(args.output) == "-":
                if fmt == "zip":
                    raise ApiError(400, "a zip cannot go to stdout; give -o FILE")
                sys.stdout.write(data.decode())
                return 0
            raw = str(args.output or "")
            dest = Path(raw) if raw else Path.cwd() / fname
            # a folder: one that exists, or a path written with a trailing slash
            if dest.is_dir() or raw.endswith(("/", "\\")):
                dest = dest / fname
            if dest.exists() and dest.is_dir():
                raise ApiError(400, f"{dest} is a folder")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            if _json(args):
                _emit({"path": str(dest), "bytes": len(data), "format": fmt})
            else:
                print(f"Wrote {dest} ({len(data) / 1024:.1f} KB)")
            return 0
        raise ApiError(400, f"unknown projects command {cmd!r}")
    except ApiError as e:
        if _json(args):
            _emit({"error": e.message})
        else:
            print(f"Error: {e.message}", file=sys.stderr)
        return 1


def add_parser(subparsers, add_json) -> None:
    p = subparsers.add_parser(
        "projects",
        help="List, show, create, add to and export projects",
        description=(
            "Projects group reports, data sources, notebooks and Lab runs (one per grant, "
            "paper or thesis). Same data as the dashboard's Projects; add --json for "
            "scripts. PROJECT is an id or a title (a unique prefix is enough)."
        ),
    )
    sub = p.add_subparsers(dest="pj_command")
    q = sub.add_parser(
        "list", help="All projects with their counts, and the inbox size"
    )
    q.add_argument("--all", action="store_true", help="Include archived projects")
    add_json(q)
    q = sub.add_parser(
        "show", help="One project: reports, sources, claims tested, Lab runs"
    )
    q.add_argument("project")
    add_json(q)
    q = sub.add_parser("create", help="Create a project")
    q.add_argument("title")
    q.add_argument("--description", default="")
    q.add_argument(
        "--level",
        choices=["P1", "P2", "P3", "P4"],
        help="Protection level (label only)",
    )
    q.add_argument(
        "--report", action="append", help="Report id to include (repeatable)"
    )
    q.add_argument(
        "--source", action="append", help="Data source name to include (repeatable)"
    )
    add_json(q)
    for name, verb in (("add", "Add"), ("remove", "Remove")):
        q = sub.add_parser(
            name, help=f"{verb} reports or data sources (never deletes them)"
        )
        q.add_argument("project")
        q.add_argument("--report", action="append", help="Report id (repeatable)")
        q.add_argument(
            "--source", action="append", help="Data source name (repeatable)"
        )
        add_json(q)
    q = sub.add_parser(
        "export", help="Export: md dossier, json, bib, csv citations, zip package"
    )
    q.add_argument("project")
    q.add_argument("--format", "-f", choices=sorted(EXPORT_EXT), default="md")
    q.add_argument(
        "-o",
        "--output",
        help="File or folder (default: here; '-' = stdout, not for zip)",
    )
    add_json(q)

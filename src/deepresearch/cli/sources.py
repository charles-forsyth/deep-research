"""`deep-research sources ...`: manage data sources from the command line."""

from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.table import Table

from deepresearch.sources import KINDS, LEVELS, DataSource, SourceRegistry
from deepresearch.sources.adapters import SourceError, adapter_for, local_roots
from deepresearch.sources.model import STAGING
from deepresearch.sources.service import check

console = Console(width=120)


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return str(n)


def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "sources",
        help="Manage data sources (web, GCS, S3/CephRDS, local folders, reports)",
        description=(
            "Register the places your data lives once, then use them in research, "
            "follow-ups and Lab runs. Only references are stored: bucket access uses "
            "gcloud (GCS) and rclone remotes (S3/CephRDS), never saved secrets. Local "
            "sources must sit under your home folder (or DR_LOCAL_ROOTS)."
        ),
    )
    sp = p.add_subparsers(dest="sources_cmd")

    a = sp.add_parser("add", help="Register a source and test it")
    a.add_argument("name", help="short name: lowercase letters, digits, dashes")
    a.add_argument(
        "uri", help="https://..., gs://bucket/prefix, s3://bucket/prefix, or a path"
    )
    a.add_argument("--kind", choices=KINDS, help="guessed from the URI when omitted")
    a.add_argument("--title", default="")
    a.add_argument("--description", default="")
    a.add_argument("--tag", action="append", default=[], help="repeatable")
    a.add_argument("--auth", default="", help="credential reference, e.g. rclone:ceph")
    a.add_argument(
        "--level",
        default="",
        choices=["", *LEVELS],
        help="protection level (label only)",
    )
    a.add_argument(
        "--staging", default="auto", choices=STAGING, help="how Lab jobs get it"
    )
    a.add_argument("--include", action="append", default=[], help="glob, repeatable")
    a.add_argument("--exclude", action="append", default=[], help="glob, repeatable")
    a.add_argument("--no-test", action="store_true", help="save without reaching it")

    sp.add_parser("list", help="List sources")
    for cmd, hlp in (
        ("show", "Show a source and what used it"),
        ("test", "Reach a source and refresh its manifest"),
        ("rm", "Delete a source (the data itself is never touched)"),
    ):
        x = sp.add_parser(cmd, help=hlp)
        x.add_argument("name")
    b = sp.add_parser("browse", help="List files in a source")
    b.add_argument("name")
    b.add_argument("path", nargs="?", default="")
    pv = sp.add_parser("preview", help="Print the start of a file in a source")
    pv.add_argument("name")
    pv.add_argument("path", nargs="?", default="")
    pv.add_argument("--bytes", type=int, default=4000)
    for x in sp.choices.values():
        x.add_argument("--json", action="store_true", help="machine-readable output")


def guess_kind(uri: str) -> str:
    if uri.startswith(("http://", "https://")):
        return "web"
    if uri.startswith("gs://"):
        return "gcs"
    if uri.startswith("s3://"):
        return "s3"
    if uri.startswith("report:"):
        return "report"
    if uri.startswith("notebook:"):
        return "notebook"
    import os

    return "local_file" if os.path.isfile(os.path.expanduser(uri)) else "local_folder"


def handle(args: argparse.Namespace, reg: SourceRegistry | None = None) -> int:
    reg = reg or SourceRegistry()
    cmd = getattr(args, "sources_cmd", None)
    as_json = getattr(args, "json", False)
    if cmd == "add":
        kind = args.kind or guess_kind(args.uri)
        uri = args.uri
        if kind.startswith("local_"):
            import os

            uri = os.path.abspath(os.path.expanduser(uri))
        opts = {}
        if args.include:
            opts["include"] = args.include
        if args.exclude:
            opts["exclude"] = args.exclude
        s = DataSource(
            name=args.name,
            kind=kind,
            uri=uri,
            title=args.title,
            description=args.description,
            tags=args.tag,
            auth_ref=args.auth,
            protection_level=args.level or ("P1" if kind == "web" else "P2"),
            staging=args.staging,
            options=opts,
        )
        s = reg.add(s)
        if not args.no_test:
            s = check(reg, s)
        _print_one(s, as_json)
        return 0 if s.status in ("ok", "unchecked") else 2
    if cmd == "list":
        rows = reg.list()
        if as_json:
            print(json.dumps([r.public() for r in rows], indent=2, default=str))
            return 0
        t = Table(title=f"Data sources ({len(rows)})")
        for col in (
            "name",
            "kind",
            "status",
            "files",
            "size",
            "staging",
            "level",
            "uri",
        ):
            t.add_column(col)
        for r in rows:
            m = r.manifest
            t.add_row(
                r.name,
                r.kind,
                r.status,
                (str(m.file_count) + ("+" if m.truncated else "")) if m else "-",
                _size(m.total_bytes) if m else "-",
                r.effective_staging,
                r.protection_level,
                r.uri,
            )
        console.print(t)
        if not rows:
            console.print(
                "Add one: deep-research sources add NAME gs://bucket/prefix  "
                f"(local roots: {', '.join(str(x) for x in local_roots())})"
            )
        return 0
    if cmd in ("show", "test", "rm", "browse", "preview"):
        try:
            s = reg.require(args.name)
        except KeyError as e:
            console.print(f"[red]{e.args[0]}[/red]")
            return 1
        if cmd == "rm":
            reg.delete(s.name)
            console.print(
                f"Deleted source '{s.name}' (the data itself was not touched)"
            )
            return 0
        if cmd == "test":
            s = check(reg, s)
            _print_one(s, as_json)
            return 0 if s.status == "ok" else 2
        if cmd == "show":
            _print_one(s, as_json, uses=reg.uses(s))
            return 0
        try:
            a = adapter_for(s)
            if cmd == "browse":
                items = a.list(args.path)
                if as_json:
                    print(json.dumps(items, indent=2))
                else:
                    for i in items:
                        console.print(f"{_size(i['size']):>10}  {i['name']}")
            else:
                sys.stdout.write(
                    a.preview(args.path, args.bytes).decode("utf-8", "replace")
                )
                sys.stdout.write("\n")
        except SourceError as e:
            console.print(f"[red]{e}[/red]")
            return 2
        return 0
    console.print(
        "Usage: deep-research sources {add,list,show,test,browse,preview,rm} ..."
    )
    return 1


def _print_one(s: DataSource, as_json: bool, uses: list | None = None) -> None:
    d = s.public()
    if uses is not None:
        d["used_by"] = uses
    if as_json:
        if d.get("manifest"):
            d["manifest"]["entries"] = d["manifest"]["entries"][:50]
        print(json.dumps(d, indent=2, default=str))
        return
    m = s.manifest
    colour = {"ok": "green", "unreachable": "red"}.get(s.status, "yellow")
    console.print(
        f"[bold]{s.name}[/bold]  {s.kind}  [{colour}]{s.status}[/{colour}]  {s.uri}"
    )
    if s.last_error:
        console.print(f"  [red]{s.last_error}[/red]")
    if m:
        console.print(
            f"  {m.file_count}{'+' if m.truncated else ''} files, {_size(m.total_bytes)}, "
            f"formats: {', '.join(f'{k} {v}' for k, v in list(m.formats.items())[:6])}"
        )
    console.print(
        f"  Lab staging: {s.effective_staging}, job variable ${s.env_var}, "
        f"level {s.protection_level}" + (f", auth {s.auth_ref}" if s.auth_ref else "")
    )
    for u in (uses or [])[:10]:
        console.print(f"  used by {u['used_by_kind']} #{u['used_by_id']} ({u['role']})")

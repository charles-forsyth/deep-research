"""`deep-research sources ...`: manage data sources from the command line."""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.table import Table

from deepresearch.sources import KINDS, LEVELS, DataSource, SourceRegistry
from deepresearch.sources.adapters import SourceError, adapter_for, local_roots
from deepresearch.sources.model import STAGING
from deepresearch.sources.service import check
from deepresearch.cli.jsonout import emit

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
    dc = sp.add_parser(
        "discover",
        help="Search free open data catalogs (Data.gov, Zenodo, Hugging Face, AWS Open "
        "Data, Google Cloud public datasets, Earth Engine)",
    )
    dc.add_argument("query")
    dc.add_argument(
        "--catalog", action="append",
        choices=["datagov", "zenodo", "huggingface", "aws", "gcp", "earthengine"],
        help="limit to a catalog (repeatable; default all)",
    )  # fmt: skip
    dc.add_argument("--limit", type=int, default=6, help="results per catalog")
    ix = sp.add_parser(
        "index",
        help="Build (or rebuild) the saved Gemini search index research runs reuse",
    )
    ix.add_argument("name")
    ix.add_argument("--drop", action="store_true", help="delete the index instead")
    b = sp.add_parser("browse", help="List files in a source")
    b.add_argument("name")
    b.add_argument("path", nargs="?", default="")
    pv = sp.add_parser("preview", help="Print the start of a file in a source")
    pv.add_argument("name")
    pv.add_argument("path", nargs="?", default="")
    pv.add_argument("--bytes", type=int, default=4000)
    for x in sp.choices.values():
        x.add_argument("--json", action="store_true", help="machine-readable output")


def local_uri(uri: str) -> str:
    """An absolute local path for a local source, checked up front.

    Relative paths are read from the home folder (not wherever the dashboard or CLI
    happens to run). Anything that looks like another URL scheme, or a path outside
    the allowed folders, is refused instead of being saved as a broken folder.
    """
    import os
    import re
    from pathlib import Path

    from deepresearch.sources.adapters import local_roots

    raw = uri.strip()
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", raw) and not re.match(
        r"^[a-zA-Z]:[\\/]", raw
    ):
        raise ValueError(
            f"'{raw.split(':', 1)[0]}:' addresses are not supported; use https://, "
            "gs://, s3://, report:N, notebook:N, or a folder or file path"
        )
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = Path.home() / p
    real = p.resolve()
    roots = local_roots()
    if not any(real == r or r in real.parents for r in roots):
        raise ValueError(
            f"{real} is outside the folders local sources may use "
            f"({', '.join(str(r) for r in roots)})"
        )
    return str(real)


def guess_kind(uri: str, auth: str = "") -> str:
    """Buckets without credentials are public (read anonymously, never billed)."""
    import re

    if re.match(
        r"^https://([^./]+\.s3[.-]([a-z0-9-]+\.)?amazonaws\.com|storage\.googleapis\.com)"
        r"/?[^?]*/$|^https://[^./]+\.s3[.-]([a-z0-9-]+\.)?amazonaws\.com/?$",
        uri,
    ):
        return "public_bucket"
    if uri.startswith(("http://", "https://")):
        return "web"
    if uri.startswith("gs://"):
        return "gcs" if auth else "public_bucket"
    if uri.startswith("s3://"):
        return "s3" if auth else "public_bucket"
    if uri.startswith("report:"):
        return "report"
    if uri.startswith("notebook:"):
        return "notebook"
    import os
    from pathlib import Path

    p = Path(os.path.expanduser(uri))
    if not p.is_absolute():
        p = Path.home() / p
    return "local_file" if p.is_file() else "local_folder"


def handle(args: argparse.Namespace, reg: SourceRegistry | None = None) -> int:
    reg = reg or SourceRegistry()
    cmd = getattr(args, "sources_cmd", None)
    as_json = getattr(args, "json", False) is True
    if cmd == "add":
        kind = args.kind or guess_kind(args.uri, args.auth or "")
        uri = args.uri
        if kind.startswith("local_"):
            try:
                uri = local_uri(uri)
            except ValueError as e:
                print(f"[ERROR] {e}")
                if as_json:
                    emit({"error": str(e)})
                return 2
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
    if cmd == "discover":
        from deepresearch.sources.discover import discover

        out = discover(args.query, args.catalog, args.limit)
        if as_json:
            emit(out)
            return 0 if out["results"] or not out["errors"] else 2
        for c, err in out["errors"].items():
            console.print(f"[yellow]{c}: {err}[/yellow]")
        for r in out["results"]:
            console.print(
                f"[bold]{r['title']}[/bold]  [dim]{r['catalog']} | "
                f"{r.get('license') or 'license not stated'}[/dim]"
            )
            if r.get("description"):
                console.print(f"  {r['description'][:220]}")
            console.print(f"  [cyan]{r.get('page') or ''}[/cyan]")
            for f in r["files"][:4]:
                console.print(f"    {f.get('format') or '?':>7}  {f['url']}")
            for b in r.get("buckets", [])[:4]:
                console.print(
                    f"   bucket  {b['uri']}  [dim]{b.get('title') or ''}[/dim]"
                )
            if r.get("note"):
                console.print(f"  [dim]{r['note']}[/dim]")
        if out["results"]:
            console.print(
                "\nAdd one: deep-research sources add NAME <file url or bucket>   "
                "(public buckets are read anonymously; check the license first)"
            )
        return 0 if out["results"] or not out["errors"] else 2
    if cmd == "list":
        rows = reg.list()
        if as_json:
            emit([r.public() for r in rows])
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
    if cmd in ("show", "test", "rm", "browse", "preview", "index"):
        try:
            s = reg.require(args.name)
        except KeyError as e:
            console.print(f"[red]{e.args[0]}[/red]")
            if as_json:
                emit({"error": str(e.args[0])})
            return 1
        if cmd == "index":
            from deepresearch.sources.index import build_index, drop_index

            client = _genai_client()
            try:
                if args.drop:
                    s = drop_index(reg, s, client)
                    console.print(f"Index for '{s.name}' deleted")
                else:
                    s = build_index(reg, s, client, log=console.print)
            except Exception as e:
                console.print(f"[red]{e}[/red]")
                if as_json:
                    emit({"error": str(e)})
                return 2
            _print_one(s, as_json)
            return 0
        if cmd == "rm":
            index_error = None
            if s.options.get("store"):
                from deepresearch.sources.index import drop_index

                try:
                    drop_index(reg, s, _genai_client())
                except Exception as e:
                    index_error = str(e)
                    console.print(f"[yellow]Could not delete its index: {e}[/yellow]")
            reg.delete(s.name)
            console.print(
                f"Deleted source '{s.name}' (the data itself was not touched)"
            )
            if as_json:
                emit(
                    {
                        "deleted": s.name,
                        "data_touched": False,
                        "index_error": index_error,
                    }
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
                    emit(items)
                else:
                    for i in items:
                        console.print(f"{_size(i['size']):>10}  {i['name']}")
            elif as_json:
                raw = a.preview(args.path, args.bytes)
                emit(
                    {
                        "name": s.name,
                        "path": args.path,
                        "bytes": len(raw),
                        "text": raw.decode("utf-8", "replace"),
                    }
                )
            else:
                sys.stdout.write(
                    a.preview(args.path, args.bytes).decode("utf-8", "replace")
                )
                sys.stdout.write("\n")
        except SourceError as e:
            console.print(f"[red]{e}[/red]")
            if as_json:
                emit({"error": str(e)})
            return 2
        return 0
    console.print(
        "Usage: deep-research sources "
        "{add,list,show,test,index,discover,browse,preview,rm} ..."
    )
    return 1


def _genai_client():
    from google import genai

    from deepresearch.core.config import DeepResearchConfig

    return genai.Client(api_key=DeepResearchConfig().api_key)


def _print_one(s: DataSource, as_json: bool, uses: list | None = None) -> None:
    d = s.public()
    if uses is not None:
        d["used_by"] = uses
    if as_json:
        if d.get("manifest"):
            d["manifest"]["entries"] = d["manifest"]["entries"][:50]
        emit(d)
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

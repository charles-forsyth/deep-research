import sys
import json
import argparse
from pydantic import ValidationError

from deepresearch import __version__
from deepresearch.cli.commands import (
    handle_research,
    handle_start,
    handle_followup,
    handle_list,
    handle_show,
    handle_delete,
    handle_cleanup,
    handle_tree,
    handle_auth,
    handle_estimate,
)
from deepresearch.cli.jsonout import json_flag as _json_flag


def get_version():
    return __version__


DESCRIPTION = """
Gemini Deep Research Agent CLI
==============================
Conduct autonomous, multi-step research with the Gemini Deep Research agent.
Supports web search, local file ingestion, streaming thoughts, recursive
research, and follow-up questions.

A bare prompt runs `research`:  %(prog)s "History of the internet"
"""

EPILOG = """
Examples:
---------
1. Basic web research (streaming):
   %(prog)s research "History of the internet" --stream

2. Research with local files:
   %(prog)s research "Summarize this contract" --upload ./contract.pdf --stream

3. Formatted output and export (format is chosen by file extension):
   %(prog)s research "Compare GPU prices" --format "Markdown table" --output prices.md
   %(prog)s research "List top 5 cloud providers" --output market_data.json

4. Recursive research (check the cost first):
   %(prog)s estimate "State of solid-state batteries" --depth 2 --breadth 3
   %(prog)s research "State of solid-state batteries" --depth 2 --breadth 3

5. Headless research (fire and forget):
   %(prog)s start "Detailed analysis of quantum computing"
   %(prog)s list
   %(prog)s show 1

6. Semantic search over past research:
   %(prog)s search "What did I research about quantum error correction?"

7. Follow-up question on session #1:
   %(prog)s followup 1 "Can you explain the error correction?"

8. Web dashboard (research workstation in the browser):
   %(prog)s dashboard --start           # http://<host>:7420
   %(prog)s dashboard --status
   %(prog)s dashboard --stop

9. Manage history:
   %(prog)s tree 1                      # session #1 and its child tasks
   %(prog)s show 1 --recursive --save report.html
   %(prog)s delete 1

Configuration:
--------------
GEMINI_API_KEY is read from ./.env first, then ~/.config/deepresearch/.env
(or $XDG_CONFIG_HOME/deepresearch/.env). `%(prog)s auth login` writes the
key to that user file. Session history lives in history.db in the same folder.
"""

ID_HELP = "Session ID (integer, from `list`) or Interaction ID"
MAX_DEPTH = 5  # same limits as the dashboard (POST /api/research)
MAX_BREADTH = 10
DEPTH_HELP = (
    "Recursion depth, 1-5; 1 = a single research task, no recursion "
    "(default: %(default)s)"
)
BREADTH_HELP = (
    "Max follow-up child tasks per recursion level, 1-10. Total tasks grow as "
    "1 + B + B^2 + ... for depth levels (default: %(default)s)"
)
UPLOAD_HELP = (
    "Local files or folders to upload into a temporary File Search Store "
    "(deleted when the task finishes)"
)
STORES_HELP = (
    "Names of existing File Search Stores to search (e.g. fileSearchStores/abc123)"
)
SOURCE_HELP = (
    "A registered data source to include (repeatable; see `deep-research sources "
    "list`). Its files are fetched and searched like --upload"
)
FORMAT_HELP = 'Extra output instructions for the report, e.g. "Markdown table"'
OUTPUT_HELP = (
    "Save the report to a file. .json is parsed and pretty-printed "
    "(raw text goes to <file>.raw if it is not valid JSON); .csv takes the CSV "
    "code block; anything else is saved as plain text"
)


def _add_research_options(p: argparse.ArgumentParser) -> None:
    """Options shared by `research` and `start`."""
    p.add_argument("prompt", help="The research prompt or question")
    p.add_argument("--stores", nargs="+", help=STORES_HELP)
    p.add_argument("--upload", nargs="+", help=UPLOAD_HELP)
    p.add_argument(
        "--source",
        action="append",
        metavar="NAME",
        help=SOURCE_HELP,
    )
    p.add_argument("--format", help=FORMAT_HELP)
    p.add_argument("--output", help=OUTPUT_HELP)
    p.add_argument("--depth", type=int, default=1, help=DEPTH_HELP)
    p.add_argument("--breadth", type=int, default=3, help=BREADTH_HELP)
    p.add_argument(
        "--plan-id",
        metavar="INTERACTION",
        help=(
            "Run as the continuation of an approved research plan (the plan "
            "interaction id from the dashboard's Plan first)"
        ),
    )
    p.add_argument(
        "--max",
        action="store_true",
        help=(
            "Use Google's Deep Research Max agent: more searches and reading, slower, "
            "about 2-3x the cost of a standard run"
        ),
    )


JSON_HELP = (
    "Machine-readable output: exactly one JSON document on stdout (logs and "
    'progress go to stderr); failures print {"error": ...} and exit non-zero'
)


def _add_json(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help=JSON_HELP)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deep-research",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {get_version()}"
    )

    parser.add_argument(
        "-W",
        "--workspace",
        metavar="ID",
        help="Use this workspace (a separate library of reports, projects, notes, Lab "
        "runs and data sources). Default: $DR_WORKSPACE, else main. See `deep-research "
        "workspace list`.",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    parser_research = subparsers.add_parser(
        "research",
        help="Run a research task in the foreground",
        description="Run a research task in the foreground and print the report.",
    )
    _add_research_options(parser_research)
    parser_research.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress logs, output only the final report",
    )
    parser_research.add_argument(
        "--stream",
        action="store_true",
        help="Stream the agent's thought process (ignored when --depth > 1)",
    )
    parser_research.add_argument("--adopt-session", type=int, help=argparse.SUPPRESS)

    parser_search = subparsers.add_parser(
        "search",
        help="Semantic search over past research sessions",
        description=(
            "Embed the query, find the most similar completed sessions in the "
            "local history, and synthesize a cited answer from them. The first "
            "run embeds any sessions that have no embedding yet."
        ),
    )
    parser_search.add_argument("query", help="The question to search past research for")
    parser_search.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Number of best-matching sessions to synthesize from (default: %(default)s)",
    )

    parser_start = subparsers.add_parser(
        "start",
        help="Run a research task in the background",
        description=(
            "Start a research task as a detached background process. Logs go to "
            "~/.config/deepresearch/logs/session_<id>.log; check progress with "
            "`list` and read the result with `show`."
        ),
    )
    _add_research_options(parser_start)

    parser_followup = subparsers.add_parser(
        "followup",
        help="Ask a follow-up question on a previous session",
        description="Ask a follow-up question in the context of a previous session.",
    )
    parser_followup.add_argument("id", help=ID_HELP)
    parser_followup.add_argument("prompt", help="The follow-up question")
    parser_followup.add_argument(
        "--source",
        action="append",
        metavar="NAME",
        help="A data source whose text is given with the question (repeatable)",
    )

    parser_repair = subparsers.add_parser(
        "repair",
        help="Restore reports saved with only their last part (before v0.38.2)",
        description=(
            "Long reports arrive in several parts; before v0.38.2 only the last part was "
            "saved. While Google still keeps the interaction, this restores the full text "
            "(appended follow-ups are kept). Dry run unless --apply."
        ),
    )
    parser_repair.add_argument(
        "ids", nargs="*", type=int, help="Only these session ids"
    )
    parser_repair.add_argument(
        "--apply",
        action="store_true",
        help="Write the repaired text (default: report only)",
    )
    parser_repair.add_argument(
        "--resynthesize",
        action="store_true",
        help="With --apply: also rebuild recursive reports whose synthesis started "
        "from a cut main report (one Flash call each, a few cents)",
    )
    parser_repair.add_argument(
        "--json", action="store_true", help="Machine-readable output"
    )

    parser_list = subparsers.add_parser("list", help="List recent research sessions")
    parser_list.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of sessions to show (default: %(default)s)",
    )

    parser_show = subparsers.add_parser(
        "show", help="Show the report and details of a previous session"
    )
    parser_show.add_argument("id", help=ID_HELP)
    parser_show.add_argument(
        "--save",
        metavar="FILE",
        help="Also save the output; .html keeps the colors, any other extension is plain text",
    )
    parser_show.add_argument(
        "--recursive", action="store_true", help="Include all child session reports"
    )

    parser_delete = subparsers.add_parser(
        "delete",
        help="Delete a session from local history",
        description=(
            "Delete a session from the local history database, with its "
            "sub-reports, notes, audio and Lab runs. There is no confirmation "
            "prompt. Refused while one of its Lab runs is still on the cluster."
        ),
    )
    parser_delete.add_argument("id", help=ID_HELP)

    parser_cleanup = subparsers.add_parser(
        "cleanup",
        help="Delete temporary File Search Stores left on this API key",
        description=(
            "Delete temporary File Search Stores (uploads from past runs, and "
            "unnamed leftovers from older versions) with their documents. Named "
            "stores, such as data source indexes, are kept. --all deletes ALL "
            "File Search Stores on this API key, including stores you pass with "
            "--stores. Asks for confirmation unless --force is given."
        ),
    )
    parser_cleanup.add_argument(
        "--force", action="store_true", help="Delete without confirmation"
    )
    parser_cleanup.add_argument(
        "--all",
        action="store_true",
        help="Also delete named stores (data source indexes, your own stores)",
    )

    parser_tree = subparsers.add_parser(
        "tree",
        help="Show sessions and their recursive child tasks as a tree",
    )
    parser_tree.add_argument(
        "id",
        nargs="?",
        help="Root session ID (integer). Omit to show the 10 most recent trees",
    )

    parser_auth = subparsers.add_parser(
        "auth",
        help="Save or remove the Gemini API key",
        description=(
            "login: prompt for a Gemini API key and write it to "
            "~/.config/deepresearch/.env (overwrites that file). "
            "logout: delete that file. A ./.env in the current directory still "
            "takes precedence."
        ),
    )
    parser_auth.add_argument(
        "action", choices=["login", "logout"], help="Action to perform"
    )

    parser_cluster = subparsers.add_parser(
        "cluster",
        help="Sign the Lab in to the Ursa Major cluster service (bifrost)",
        description=(
            "login: open the browser to sign in to the hosted bifrost MCP server as "
            "deep-research's own program client; the token is kept in "
            "~/.config/deepresearch/bifrost-token.json (mode 600). logout: revoke and "
            "delete it. status: who is signed in, with which tiers and caps."
        ),
    )
    parser_cluster.add_argument(
        "action", choices=["login", "logout", "status"], help="Action to perform"
    )
    parser_cluster.add_argument("--json", action="store_true", help="JSON output")

    parser_nexus = subparsers.add_parser(
        "nexus",
        help="Sign in to Nexus (read-only) for project links",
        description=(
            "login: open the browser to sign in to the Nexus MCP server as "
            "deep-research's own read-only program client (nexus-deep-research); the "
            "token is kept in ~/.config/deepresearch/nexus-token.json (mode 600). "
            "logout: revoke and delete it. status: who is signed in."
        ),
    )
    parser_nexus.add_argument(
        "action", choices=["login", "logout", "status"], help="Action to perform"
    )
    parser_nexus.add_argument("--json", action="store_true", help="JSON output")

    parser_estimate = subparsers.add_parser(
        "estimate",
        help="Estimate the cost of a research task (no API calls)",
        description=(
            "Rough cost estimate from the recursion shape and upload size, using "
            "fixed per-task token averages. Makes no API calls."
        ),
    )
    parser_estimate.add_argument("prompt", help="The research prompt or question")
    parser_estimate.add_argument("--depth", type=int, default=1, help=DEPTH_HELP)
    parser_estimate.add_argument("--breadth", type=int, default=3, help=BREADTH_HELP)
    parser_estimate.add_argument(
        "--max", action="store_true", help="Estimate a Deep Research Max run"
    )
    parser_estimate.add_argument(
        "--upload", nargs="+", help="Files or folders you plan to upload"
    )

    from deepresearch.cli.projects import add_parser as add_projects_parser

    add_projects_parser(subparsers, _add_json)

    parser_ws = subparsers.add_parser(
        "workspace",
        help="List, create, rename, archive or delete workspaces",
        description=(
            "Workspaces are separate libraries: each has its own reports, projects, "
            "notes, notebooks, Lab runs, data sources and audio. 'main' is the original "
            "library in ~/.config/deepresearch and is never moved. Others live in "
            "~/.config/deepresearch/workspaces/<id>/. The Gemini key, cluster settings "
            "and the Lab's learned lessons are shared. Pick one for any command with "
            "--workspace ID (or DR_WORKSPACE=ID)."
        ),
    )
    ws_sub = parser_ws.add_subparsers(dest="ws_command")
    p = ws_sub.add_parser("list", help="List workspaces with their size and counts")
    p.add_argument("--all", action="store_true", help="Include archived workspaces")
    _add_json(p)
    p = ws_sub.add_parser("create", help="Create a new, empty workspace")
    p.add_argument("name", help="Display name, e.g. 'Demo'")
    p.add_argument("--id", dest="slug", help="Id (default: from the name)")
    p.add_argument("--description", default="")
    _add_json(p)
    p = ws_sub.add_parser("duplicate", help="Copy a whole workspace under a new name")
    p.add_argument("source", help="Workspace id to copy (only read)")
    p.add_argument("name", help="Name of the copy")
    p.add_argument("--id", dest="slug")
    _add_json(p)
    p = ws_sub.add_parser("rename", help="Change a workspace's display name")
    p.add_argument("id")
    p.add_argument("name")
    _add_json(p)
    for verb, h in (("archive", "Hide a workspace from the switcher (keeps it)"),
                    ("unarchive", "Show an archived workspace again")):  # fmt: skip
        p = ws_sub.add_parser(verb, help=h)
        p.add_argument("id")
        _add_json(p)
    p = ws_sub.add_parser(
        "copy",
        help="Copy projects or reports (with sub-reports, notes, Lab runs and outputs, "
        "data sources) from one workspace into another",
    )
    p.add_argument(
        "--from", dest="src", default="main", help="Source workspace (default main)"
    )
    p.add_argument("--to", dest="dst", required=True, help="Target workspace")
    p.add_argument("--project", type=int, action="append", default=[], metavar="ID")
    p.add_argument("--report", type=int, action="append", default=[], metavar="ID")
    p.add_argument(
        "--dry-run", action="store_true", help="Only show what would be copied"
    )
    _add_json(p)
    p = ws_sub.add_parser("export", help="Save a workspace as a .zip to share or keep")
    p.add_argument(
        "id", nargs="?", help="Workspace to export (default: the current one)"
    )
    p.add_argument("-o", "--output", default=".", help="File or folder (default: here)")
    p.add_argument(
        "--audio", action="store_true", help="Include generated audio (large)"
    )
    p.add_argument("--uploads", action="store_true", help="Include uploaded files")
    _add_json(p)
    p = ws_sub.add_parser(
        "import", help="Create a new workspace from a .zip (never overwrites one)"
    )
    p.add_argument("zip", help="Path to a workspace .zip")
    p.add_argument("--name", help="Name for the new workspace (default: from the zip)")
    p.add_argument("--id", dest="slug", help="Id for the new workspace")
    p.add_argument(
        "--check", action="store_true", help="Only check the zip; import nothing"
    )
    _add_json(p)
    p = ws_sub.add_parser(
        "delete",
        help="Move a workspace to workspaces/.trash (never main; nothing is erased)",
    )
    p.add_argument("id")
    p.add_argument("--yes", action="store_true", help="Do not ask to confirm")
    _add_json(p)
    _add_json(parser_ws)

    parser_dash = subparsers.add_parser(
        "dashboard",
        help="Run the web dashboard (research workstation) in the background",
        description=(
            "Start, stop or restart the web dashboard: a browser workstation "
            "for launching research, watching live logs, reading and annotating "
            "reports, a notebook for collecting and editing findings, semantic "
            "search, follow-ups, and export. It has no login, so it listens on "
            "127.0.0.1 (this machine only) by default. Anyone who can reach the "
            "port can read your research and files and spend your API credits; "
            "--host 0.0.0.0 --allow-remote shares it on a network you trust."
        ),
    )
    action = parser_dash.add_mutually_exclusive_group()
    action.add_argument("--start", action="store_true", help="Start in the background")
    action.add_argument(
        "--stop", action="store_true", help="Stop the running dashboard"
    )
    action.add_argument(
        "--restart",
        action="store_true",
        help="Stop and start again (keeps the previous host/port unless given)",
    )
    action.add_argument(
        "--status", action="store_true", help="Show whether it is running"
    )
    action.add_argument(
        "--foreground",
        action="store_true",
        help="Run in this terminal instead of detaching (Ctrl-C to stop)",
    )
    parser_dash.add_argument(
        "--host", default=None, help="Bind address (default: 127.0.0.1)"
    )
    parser_dash.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow a non-loopback --host (no login: anyone who can reach it can "
        "use it)",
    )
    parser_dash.add_argument(
        "--port", type=int, default=None, help="Port (default: 7420)"
    )

    for p in (
        parser_research,
        parser_search,
        parser_start,
        parser_followup,
        parser_list,
        parser_show,
        parser_delete,
        parser_cleanup,
        parser_tree,
        parser_auth,
        parser_estimate,
        parser_dash,
    ):
        _add_json(p)

    from deepresearch.cli.sources import add_parser as add_sources_parser

    add_sources_parser(subparsers)  # every `sources` subcommand already has --json

    return parser


def handle_dashboard(args) -> int:
    if _json_flag(args):
        return _dashboard_json(args)
    return _dashboard(args)


def _dashboard_json(args) -> int:
    """Run the dashboard action (its messages go to stderr), then report state."""
    from deepresearch.cli.jsonout import emit
    from deepresearch.dashboard import daemon

    if args.foreground:
        emit({"error": "--foreground cannot be combined with --json"})
        return 2
    code = _dashboard(args)
    state = daemon.read_state()
    health = daemon._probe(state["host"], int(state["port"])) if state else None
    emit(
        {
            "exit_code": code,
            "running": bool(state),
            "healthy": bool(health),
            "pid": state["pid"] if state else None,
            "host": state["host"] if state else None,
            "port": state["port"] if state else None,
            "allow_remote": bool(state.get("allow_remote")) if state else False,
            "version": (health or {}).get("version"),
            "urls": daemon._urls(state["host"], int(state["port"])) if state else [],
        }
    )
    return code


def _dashboard(args) -> int:
    from deepresearch.dashboard import daemon

    if args.stop:
        return daemon.stop()
    remote = getattr(args, "allow_remote", False)
    if args.restart:
        return daemon.restart(args.host, args.port, remote)
    if args.foreground:
        from deepresearch.dashboard.server import serve

        host = args.host or daemon.DEFAULT_HOST
        if not daemon.is_loopback(host) and not remote:
            print(
                f"[ERROR] Refusing to listen on {host} without --allow-remote "
                "(the dashboard has no login)."
            )
            return 2
        serve(host, args.port or daemon.DEFAULT_PORT, local_only=not remote)
        return 0
    if args.start:
        return daemon.start(
            args.host or daemon.DEFAULT_HOST, args.port or daemon.DEFAULT_PORT, remote
        )
    return daemon.status()


def main():
    parser = build_parser()

    known_commands = {
        "research",
        "search",
        "start",
        "followup",
        "repair",
        "list",
        "show",
        "delete",
        "cleanup",
        "tree",
        "auth",
        "cluster",
        "nexus",
        "estimate",
        "dashboard",
        "sources",
        "workspace",
        "projects",
        "-W",
        "--workspace",
        "-h",
        "--help",
        "-v",
        "--version",
    }

    # a leading --workspace/-W (with its value) comes before the command
    first = 1
    while len(sys.argv) > first and sys.argv[first] in ("-W", "--workspace"):
        first += 2
    if len(sys.argv) > first and sys.argv[first].startswith("--workspace="):
        first += 1
    if len(sys.argv) > first and sys.argv[first] not in known_commands:
        sys.argv.insert(first, "research")

    args = parser.parse_args()

    if getattr(args, "workspace", None):
        from deepresearch.core import workspace

        try:
            workspace.use(args.workspace)
        except workspace.WorkspaceError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            if _json_flag(args):
                print(json.dumps({"error": str(e)}))
            sys.exit(2)

    if not args.command:
        parser.print_help()
        return

    # K7: the same limits as the dashboard, so a typo can't start a huge run
    if args.command in ("research", "start", "estimate"):
        d, b = getattr(args, "depth", 1), getattr(args, "breadth", 3)
        if not (1 <= d <= MAX_DEPTH and 1 <= b <= MAX_BREADTH):
            msg = f"depth must be 1-{MAX_DEPTH} and breadth 1-{MAX_BREADTH}"
            print(f"[ERROR] {msg}", file=sys.stderr)
            if _json_flag(args):
                print(json.dumps({"error": msg}))
            sys.exit(2)

    if _json_flag(args):
        from deepresearch.cli.jsonout import json_mode

        with json_mode(True):
            _dispatch(parser, args, as_json=True)
        return
    _dispatch(parser, args, as_json=False)


def _dispatch(parser: argparse.ArgumentParser, args, as_json: bool) -> None:
    from deepresearch.cli.jsonout import emit

    try:
        if args.command == "start":
            handle_start(args)
        elif args.command == "research":
            handle_research(args)
        elif args.command == "search":
            from deepresearch.cli.commands import handle_search

            handle_search(args)
        elif args.command == "followup":
            handle_followup(args)
        elif args.command == "repair":
            from deepresearch.cli.commands import handle_repair

            handle_repair(args)
        elif args.command == "list":
            handle_list(args)
        elif args.command == "show":
            handle_show(args)
        elif args.command == "delete":
            handle_delete(args)
        elif args.command == "cleanup":
            handle_cleanup(args)
        elif args.command == "tree":
            handle_tree(args)
        elif args.command == "auth":
            handle_auth(args)
        elif args.command == "nexus":
            from deepresearch.cli.cluster import handle_nexus

            sys.exit(handle_nexus(args))
        elif args.command == "cluster":
            from deepresearch.cli.cluster import handle as handle_cluster

            sys.exit(handle_cluster(args))
        elif args.command == "estimate":
            handle_estimate(args)
        elif args.command == "workspace":
            from deepresearch.cli.workspaces import handle as handle_ws

            code = handle_ws(args)
            if code:
                sys.exit(code)
        elif args.command == "projects":
            from deepresearch.cli.projects import handle as handle_projects

            code = handle_projects(args)
            if code:
                sys.exit(code)
        elif args.command == "sources":
            from deepresearch.cli.sources import handle as handle_sources

            code = handle_sources(args)
            if code:
                sys.exit(code)
        elif args.command == "dashboard":
            code = handle_dashboard(args)
            if code:
                sys.exit(code)
        else:
            parser.print_help()

    except ValidationError as e:
        print(f"[ERROR] Input Validation Failed:\n{e}")
        if as_json:
            emit({"error": f"Input validation failed: {e}"})
            sys.exit(2)
    except ValueError as e:
        print(f"[CONFIG ERROR] {e}")
        if as_json:
            emit({"error": f"Config error: {e}"})
            sys.exit(2)
    except Exception as e:
        print(f"[CRITICAL ERROR] {e}")
        if as_json:
            emit({"error": str(e)})
            sys.exit(1)


if __name__ == "__main__":
    main()

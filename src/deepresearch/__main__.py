import sys
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
DEPTH_HELP = (
    "Recursion depth; 1 = a single research task, no recursion (default: %(default)s)"
)
BREADTH_HELP = (
    "Max follow-up child tasks per recursion level. Total tasks grow as "
    "1 + B + B^2 + ... for depth levels (default: %(default)s)"
)
UPLOAD_HELP = (
    "Local files or folders to upload into a temporary File Search Store "
    "(deleted when the task finishes)"
)
STORES_HELP = (
    "Names of existing File Search Stores to search (e.g. fileSearchStores/abc123)"
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
    p.add_argument("--format", help=FORMAT_HELP)
    p.add_argument("--output", help=OUTPUT_HELP)
    p.add_argument("--depth", type=int, default=1, help=DEPTH_HELP)
    p.add_argument("--breadth", type=int, default=3, help=BREADTH_HELP)


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
            "Delete a session from the local history database. There is no "
            "confirmation prompt. Child sessions are not deleted."
        ),
    )
    parser_delete.add_argument("id", help=ID_HELP)

    parser_cleanup = subparsers.add_parser(
        "cleanup",
        help="Delete ALL File Search Stores on this API key",
        description=(
            "Delete ALL File Search Stores on this API key, with their "
            "documents. That includes stores you pass with --stores, "
            "not only temporary ones left behind by crashed uploads. Asks for "
            "confirmation unless --force is given."
        ),
    )
    parser_cleanup.add_argument(
        "--force", action="store_true", help="Delete without confirmation"
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
        "--upload", nargs="+", help="Files or folders you plan to upload"
    )

    parser_dash = subparsers.add_parser(
        "dashboard",
        help="Run the web dashboard (research workstation) in the background",
        description=(
            "Start, stop or restart the web dashboard: a browser workstation "
            "for launching research, watching live logs, reading and annotating "
            "reports, a notebook for collecting and editing findings, semantic "
            "search, follow-ups, and export. It has no login: it binds "
            "0.0.0.0 by default, so anyone who can reach the port can use it "
            "and spend your API credits. Use --host 127.0.0.1 to keep it local."
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
        "--host", default=None, help="Bind address (default: 0.0.0.0)"
    )
    parser_dash.add_argument(
        "--port", type=int, default=None, help="Port (default: 7420)"
    )

    return parser


def handle_dashboard(args) -> int:
    from deepresearch.dashboard import daemon

    if args.stop:
        return daemon.stop()
    if args.restart:
        return daemon.restart(args.host, args.port)
    if args.foreground:
        from deepresearch.dashboard.server import serve

        serve(args.host or daemon.DEFAULT_HOST, args.port or daemon.DEFAULT_PORT)
        return 0
    if args.start:
        return daemon.start(
            args.host or daemon.DEFAULT_HOST, args.port or daemon.DEFAULT_PORT
        )
    return daemon.status()


def main():
    parser = build_parser()

    known_commands = {
        "research",
        "search",
        "start",
        "followup",
        "list",
        "show",
        "delete",
        "cleanup",
        "tree",
        "auth",
        "estimate",
        "dashboard",
        "-h",
        "--help",
        "-v",
        "--version",
    }

    if len(sys.argv) > 1 and sys.argv[1] not in known_commands:
        sys.argv.insert(1, "research")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

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
        elif args.command == "estimate":
            handle_estimate(args)
        elif args.command == "dashboard":
            code = handle_dashboard(args)
            if code:
                sys.exit(code)
        else:
            parser.print_help()

    except ValidationError as e:
        print(f"[ERROR] Input Validation Failed:\n{e}")
    except ValueError as e:
        print(f"[CONFIG ERROR] {e}")
    except Exception as e:
        print(f"[CRITICAL ERROR] {e}")


if __name__ == "__main__":
    main()

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


def main():
    desc = """
Gemini Deep Research Agent CLI
==============================
A powerful tool to conduct autonomous, multi-step research using Gemini 3 Pro.
Support web search, local file ingestion, streaming thoughts, and follow-ups.
    """

    epilog = """
Examples:
---------
1. Basic Web Research (Streaming):
   %(prog)s research "History of the internet" --stream
   %(prog)s search "History of the internet" --stream  # 'search' is an alias

2. Research with Local Files (Smart Context):
   %(prog)s research "Summarize this contract" --upload ./contract.pdf --stream

3. Formatted Output & Export:
   %(prog)s search "Compare GPU prices" --format "Markdown table" --output prices.md
   %(prog)s search "List top 5 cloud providers" --output market_data.json

4. Headless Research (Fire & Forget):
   %(prog)s start "Detailed analysis of quantum computing"
   # ... process detaches ...
   %(prog)s list
   %(prog)s show 1

5. Follow-up Question:
   %(prog)s followup 1 "Can you explain the error correction?"

6. Manage History:
   %(prog)s list
   %(prog)s show 1

Configuration:
--------------
Set GEMINI_API_KEY in a local .env file or at ~/.config/deepresearch/.env
    """

    parser = argparse.ArgumentParser(
        description=desc,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {get_version()}"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    parser_research = subparsers.add_parser(
        "research", aliases=["search"], help="Start a new research task (alias: search)"
    )
    parser_research.add_argument("prompt", help="The research prompt or question")
    parser_research.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress logs, output only final report",
    )
    parser_research.add_argument(
        "--stream", action="store_true", help="Stream the agent's thought process"
    )
    parser_research.add_argument(
        "--stores", nargs="+", help="Existing Cloud File Search Store names"
    )
    parser_research.add_argument(
        "--upload", nargs="+", help="Local file/folder paths to upload"
    )
    parser_research.add_argument("--format", help="Specific output instructions")
    parser_research.add_argument("--output", help="Save report to file")
    parser_research.add_argument(
        "--depth", type=int, default=1, help="Recursive research depth"
    )
    parser_research.add_argument(
        "--breadth", type=int, default=3, help="Max child tasks per recursion level"
    )
    parser_research.add_argument("--adopt-session", type=int, help=argparse.SUPPRESS)

    parser_start = subparsers.add_parser(
        "start", help="Start a research task in the background"
    )
    parser_start.add_argument("prompt", help="The research prompt or question")
    parser_start.add_argument(
        "--upload", nargs="+", help="Local file/folder paths to upload"
    )
    parser_start.add_argument("--format", help="Specific output instructions")
    parser_start.add_argument("--output", help="Save report to file")
    parser_start.add_argument(
        "--depth", type=int, default=1, help="Recursive research depth"
    )
    parser_start.add_argument(
        "--breadth", type=int, default=3, help="Max child tasks per recursion level"
    )

    parser_followup = subparsers.add_parser("followup", help="Ask a follow-up question")
    parser_followup.add_argument(
        "id", help="The Interaction ID from a previous research task"
    )
    parser_followup.add_argument("prompt", help="The follow-up question")

    parser_list = subparsers.add_parser("list", help="List recent research sessions")
    parser_list.add_argument(
        "--limit", type=int, default=10, help="Number of sessions to show"
    )

    parser_show = subparsers.add_parser(
        "show", help="Show details of a previous session"
    )
    parser_show.add_argument("id", help="Session ID (integer) or Interaction ID")
    parser_show.add_argument(
        "--save", help="Save the colorful report to HTML or Text file"
    )
    parser_show.add_argument(
        "--recursive", action="store_true", help="Include all child session reports"
    )

    parser_delete = subparsers.add_parser(
        "delete", help="Delete a session from history"
    )
    parser_delete.add_argument("id", help="Session ID (integer) or Interaction ID")

    parser_cleanup = subparsers.add_parser(
        "cleanup", help="Delete stale cloud resources (GC)"
    )
    parser_cleanup.add_argument(
        "--force", action="store_true", help="Delete without confirmation"
    )

    parser_tree = subparsers.add_parser("tree", help="Visualize session hierarchy")
    parser_tree.add_argument("id", nargs="?", help="Root Session ID (optional)")

    parser_auth = subparsers.add_parser("auth", help="Manage authentication")
    parser_auth.add_argument(
        "action", choices=["login", "logout"], help="Action to perform"
    )

    parser_estimate = subparsers.add_parser(
        "estimate", help="Estimate cost of a research task"
    )
    parser_estimate.add_argument("prompt", help="The research prompt or question")
    parser_estimate.add_argument("--depth", type=int, default=1, help="Recursive depth")
    parser_estimate.add_argument(
        "--breadth", type=int, default=3, help="Recursive breadth"
    )
    parser_estimate.add_argument("--upload", nargs="+", help="Files to upload")

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
        elif args.command in ("research", "search"):
            handle_research(args)
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

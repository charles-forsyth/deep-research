import sys
import os
import sqlite3
import subprocess
from rich.console import Console
from rich.table import Table
from rich.tree import Tree
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.terminal_theme import MONOKAI
from google import genai

from deepresearch.core.config import (
    DeepResearchConfig,
    user_config_path,
    xdg_config_home,
)
from deepresearch.core.session import SessionManager
from deepresearch.core.agent import DeepResearchAgent
from deepresearch.cli.base import ResearchRequest, FollowUpRequest

console = Console(width=120)


def detach_process(args_list: list[str], log_path: str) -> int:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as log_file:
        # Find the entrypoint package or script
        # Using sys.argv[0] is usually the deep-research script
        cmd = [sys.executable, "-u", sys.argv[0]] + args_list

        import typing

        kwargs: typing.Dict[str, typing.Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            cmd, stdout=log_file, stderr=log_file, stdin=subprocess.DEVNULL, **kwargs
        )
        return proc.pid


def handle_research(args):
    request = ResearchRequest(
        prompt=args.prompt,
        stores=args.stores,
        stream=args.stream,
        output_format=args.format,
        upload_paths=args.upload,
        output_file=args.output,
        adopt_session_id=args.adopt_session,
        depth=args.depth,
        breadth=args.breadth,
    )
    agent = DeepResearchAgent(quiet=args.quiet)

    if request.depth > 1:
        if request.stream and not args.quiet:
            print(
                "[INFO] Recursive research does not support streaming to stdout. Switching to polling mode."
            )
        agent.start_recursive_research(request)
    elif request.stream:
        agent.start_research_stream(request)
    else:
        agent.start_research_poll(request)


def handle_search(args):
    import json
    import math

    config = DeepResearchConfig()
    client = genai.Client(api_key=config.api_key)
    mgr = SessionManager()

    # 1. Backfill if needed
    unembedded = mgr.get_completed_sessions_without_embeddings()
    if unembedded:
        console.print(
            f"[bold yellow][INFO] Generating vector embeddings for {len(unembedded)} past sessions. This only happens once per new session...[/]"
        )
        for row in unembedded:
            try:
                # Truncate slightly to avoid huge token limits, though 004 handles up to 2k-10k usually
                text_to_embed = (
                    f"Objective: {row['prompt']}\n\nResult:\n{row['result']}"
                )
                text_to_embed = text_to_embed[:15000]
                resp = client.models.embed_content(
                    model="gemini-embedding-001", contents=text_to_embed
                )
                mgr.update_embedding(row["id"], json.dumps(resp.embeddings[0].values))
            except Exception as e:
                console.print(f"[red]Failed to embed session {row['id']}: {e}[/]")

    console.print(f"[bold cyan][INFO] Searching knowledge graph for:[/] {args.query}")
    try:
        query_resp = client.models.embed_content(
            model="gemini-embedding-001", contents=args.query
        )
        query_vec = query_resp.embeddings[0].values
    except Exception as e:
        console.print(f"[bold red][ERROR] Failed to embed query:[/] {e}")
        return

    all_docs = mgr.get_all_embeddings()
    if not all_docs:
        console.print(
            "[yellow]No completed research sessions found in the database to search.[/]"
        )
        return

    def cosine_sim(v1, v2):
        dot = sum(a * b for a, b in zip(v1, v2))
        mag1 = math.sqrt(sum(a * a for a in v1))
        mag2 = math.sqrt(sum(b * b for b in v2))
        return dot / (mag1 * mag2) if mag1 and mag2 else 0

    scored = []
    for doc in all_docs:
        try:
            doc_vec = json.loads(doc["embedding"])
            score = cosine_sim(query_vec, doc_vec)
            scored.append((score, doc))
        except Exception:
            pass

    scored.sort(key=lambda x: x[0], reverse=True)
    top_k = scored[: args.limit]

    if not top_k:
        console.print("[yellow]No relevant matches found.[/]")
        return

    context = ""
    console.print("\n[bold green]Top Matches Found:[/]")
    for score, doc in top_k:
        console.print(
            f"  - Session [bold]#{doc['id']}[/] (Similarity: {score:.2f}) - {doc['prompt'][:60]}..."
        )
        context += f"--- SESSION {doc['id']} (Relevance Score: {score:.2f}) ---\nPROMPT: {doc['prompt']}\nRESULT:\n{doc['result']}\n\n"

    console.print(
        "\n[bold cyan][INFO] Synthesizing final answer from past research...[/]"
    )
    prompt = f"""User Question: {args.query}

Search Results from Past Research:
{context}

INSTRUCTIONS:
1. Answer the User Question using ONLY the information provided in the "Search Results from Past Research".
2. You MUST cite the Session ID (e.g., "[Session #12]") for every fact you provide.
3. If the answer cannot be found in the provided past research, clearly state that you don't have enough local data and suggest the user run a new deep research on the topic."""

    try:
        response = client.models.generate_content(
            model=config.followup_model, contents=prompt
        )
        console.print("\n")
        console.print(
            Panel(Markdown(response.text), title="[bold]Semantic Search Result[/]")
        )
    except Exception as e:
        console.print(f"[bold red][ERROR] Synthesis failed:[/] {e}")


def handle_start(args):
    mgr = SessionManager()
    sid = mgr.create_session("pending_start", args.prompt, args.upload)

    child_args = ["research", args.prompt, "--adopt-session", str(sid)]
    if args.upload:
        child_args += ["--upload"] + args.upload
    if args.format:
        child_args += ["--format", args.format]
    if args.output:
        child_args += ["--output", args.output]

    child_args += ["--depth", str(args.depth)]
    child_args += ["--breadth", str(args.breadth)]

    log_file = os.path.join(
        xdg_config_home, "deepresearch", "logs", f"session_{sid}.log"
    )
    pid = detach_process(child_args, log_file)
    mgr.update_session_pid(sid, pid)

    print(f"[INFO] Research started in background! (Session ID: {sid}, PID: {pid})")
    print(f"[INFO] Logs: {log_file}")
    print("[INFO] Check status with: deep-research list")


def handle_followup(args):
    interaction_id = args.id
    if args.id.isdigit():
        mgr = SessionManager()
        session = mgr.get_session(args.id)
        if session and session["interaction_id"]:
            print(
                f"[INFO] Resuming Session #{args.id} (Interaction: {session['interaction_id']})"
            )
            interaction_id = session["interaction_id"]
        else:
            print(f"[ERROR] Session #{args.id} not found or invalid.")
            return

    request = FollowUpRequest(interaction_id=interaction_id, prompt=args.prompt)
    agent = DeepResearchAgent()
    agent.follow_up(request)


def handle_list(args):
    mgr = SessionManager()
    sessions = mgr.list_sessions(args.limit)

    table = Table(title="Recent Research Sessions", box=None)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("Date", style="dim")
    table.add_column("Prompt", style="bold")

    for s in sessions:
        prompt = s["prompt"].replace("\n", " ")
        status_style = (
            "green"
            if s["status"] == "completed"
            else "yellow"
            if s["status"] == "running"
            else "red"
        )
        status_text = f"[{status_style}]{s['status']}[/{status_style}]"
        table.add_row(str(s["id"]), status_text, s["created_at"][:19], prompt)

    console.print(table)


def handle_show(args):
    mgr = SessionManager()

    def get_full_recursive_report(root_id, level=1):
        session = mgr.get_session(root_id)
        if not session:
            return ""

        indent_hash = "#" * min(level, 6)
        title = session["prompt"].replace("\n", " ")

        report = f"{indent_hash} Session #{session['id']} (Depth {session['depth']})\n"
        report += f"**Objective:** {title}\n"
        report += f"**Status:** {session['status']}\n\n"

        if session["result"]:
            report += session["result"]
        else:
            report += "*(No content)*"

        report += "\n\n---\n\n"

        children = mgr.get_children(root_id)
        for child in children:
            report += get_full_recursive_report(child["id"], level + 1)

        return report

    if args.recursive:
        full_content = get_full_recursive_report(args.id)
        if not full_content:
            console.print(f"[bold red]Session {args.id} not found.[/]")
        else:
            console.print(Markdown(full_content))
            if args.save:
                if args.save.lower().endswith(".html"):
                    save_console = Console(record=True)
                    save_console.print(Markdown(full_content))
                    save_console.save_html(args.save, theme=MONOKAI)
                else:
                    with open(args.save, "w") as f:
                        f.write(full_content)
                console.print(f"[bold green]Recursive report saved to {args.save}[/]")
        return

    session = mgr.get_session(args.id)
    show_console = Console(record=True) if args.save else console

    if not session:
        show_console.print(f"[bold red][ERROR] Session '{args.id}' not found.[/]")
    else:
        show_console.print(
            Panel(
                f"[bold]Interaction ID:[/bold] {session['interaction_id']}\n"
                f"[bold]Date:[/bold] {session['created_at']}\n"
                f"[bold]Status:[/bold] {session['status']}\n"
                f"[bold]Files:[/bold] {session['files']}",
                title=f"Session #{session['id']}",
                subtitle="Metadata",
            )
        )

        show_console.rule("[bold cyan]Prompt[/]")
        show_console.print(f"[bold]{session['prompt']}[/]\n")

        show_console.rule("[bold green]Result[/]")
        if session["result"]:
            show_console.print(Markdown(session["result"]))
        else:
            show_console.print("[italic dim](No result stored)[/]")

    if args.save:
        if args.save.lower().endswith(".html"):
            show_console.save_html(args.save, theme=MONOKAI)
        else:
            show_console.save_text(args.save)
        console.print(f"[bold green][INFO][/] Report saved to {args.save}")


def handle_delete(args):
    mgr = SessionManager()
    success = mgr.delete_session(args.id)
    if success:
        console.print(f"[bold green][INFO][/] Session '{args.id}' deleted.")
    else:
        console.print(f"[bold red][ERROR][/] Session '{args.id}' not found.")


def handle_cleanup(args):
    config = DeepResearchConfig()
    client = genai.Client(api_key=config.api_key)

    console.print("[bold cyan][INFO][/] Scanning for File Search Stores...")
    try:
        stores = list(client.file_search_stores.list())
    except Exception as e:
        console.print(f"[bold red][ERROR][/] Failed to list stores: {e}")
        return

    if not stores:
        console.print("[bold green]No active stores found. System is clean![/]")
        return

    table = Table(title=f"Found {len(stores)} Active Cloud Stores")
    table.add_column("Name (ID)", style="cyan")
    table.add_column("Create Time", style="dim")

    for s in stores:
        created = getattr(s, "create_time", "Unknown")
        table.add_row(s.name, str(created))

    console.print(table)
    console.print(
        "[bold yellow]WARNING: This will delete ALL listed stores and their files.[/]"
    )

    if not args.force:
        confirm = Prompt.ask(
            f"Are you sure you want to delete {len(stores)} stores?",
            choices=["y", "n"],
            default="n",
        )
        if confirm.lower() != "y":
            console.print("[bold yellow]Aborted.[/]")
            return

    with console.status("Deleting stores...", spinner="dots"):
        for s in stores:
            try:
                if hasattr(client.file_search_stores, "documents"):
                    docs = list(client.file_search_stores.documents.list(parent=s.name))
                    if docs:
                        console.print(f"  Emptying {len(docs)} documents...")
                    for doc in docs:
                        try:
                            client.file_search_stores.documents.delete(
                                name=doc.name, config={"force": True}
                            )
                        except Exception as e:
                            console.print(
                                f"  [yellow]Failed to delete doc {doc.name}: {e}[/]"
                            )
            except Exception as e:
                console.print(f"  [yellow]Failed to list docs: {e}[/]")

            try:
                client.file_search_stores.delete(name=s.name)
                console.print(f"[green]Deleted:[/green] {s.name}")
            except Exception as e:
                console.print(f"[bold red]Failed to delete {s.name}:[/] {e}")

    console.print("[bold green]Cleanup Complete![/]")


def handle_tree(args):
    mgr = SessionManager()

    def build_tree(node_id, tree_node):
        children = mgr.get_children(node_id)
        for child in children:
            status_style = (
                "green"
                if child["status"] == "completed"
                else "red"
                if child["status"] in ("crashed", "failed")
                else "yellow"
            )
            prompt = child["prompt"].replace("\n", " ")
            if len(prompt) > 100:
                prompt = prompt[:97] + "..."

            label = f"#{child['id']} [{status_style}]{child['status']}[/] [dim]Depth {child['depth']}[/]\n[italic]{prompt}[/]"
            branch = tree_node.add(label)
            build_tree(child["id"], branch)

    if args.id:
        root = mgr.get_session(args.id)
        if not root:
            console.print(f"[bold red]Session {args.id} not found[/]")
            return
        root_label = (
            f"[bold cyan]Session #{root['id']}[/] [dim]Depth {root['depth']}[/]"
        )
        t = Tree(root_label)
        build_tree(root["id"], t)
        console.print(t)
    else:
        forest = Tree("[bold]Recent Research Trees[/]")
        with sqlite3.connect(mgr.db_path) as conn:
            conn.row_factory = sqlite3.Row
            roots = conn.execute(
                "SELECT * FROM sessions WHERE parent_id IS NULL ORDER BY updated_at DESC LIMIT 10"
            ).fetchall()

        for r in roots:
            status_style = (
                "green"
                if r["status"] == "completed"
                else "red"
                if r["status"] in ("crashed", "failed")
                else "yellow"
            )
            prompt = r["prompt"].replace("\n", " ")
            if len(prompt) > 100:
                prompt = prompt[:97] + "..."

            label = f"#{r['id']} [{status_style}]{r['status']}[/]\n[italic]{prompt}[/]"
            branch = forest.add(label)
            build_tree(r["id"], branch)
        console.print(forest)


def handle_auth(args):
    if args.action == "login":
        console.print(
            Panel(
                "Enter your Gemini API Key. It will be stored securely in `~/.config/deepresearch/.env`.",
                title="Authentication",
            )
        )
        key = Prompt.ask("API Key", password=True)
        if not key.startswith("AIza"):
            console.print(
                "[yellow]Warning: Key does not start with 'AIza'. It might be invalid.[/]"
            )

        os.makedirs(os.path.dirname(user_config_path), exist_ok=True)
        with open(user_config_path, "w") as f:
            f.write(f"GEMINI_API_KEY={key}\n")

        console.print(f"[bold green]Success![/] Key saved to {user_config_path}")

    elif args.action == "logout":
        if os.path.exists(user_config_path):
            os.remove(user_config_path)
            console.print("[green]Logged out. Config file deleted.[/]")
        else:
            console.print("[yellow]Not logged in.[/]")


def handle_estimate(args):
    COST_INPUT_1M = 2.00
    COST_OUTPUT_1M = 12.00
    AVG_INPUT_TOKENS = 60_000
    AVG_OUTPUT_TOKENS = 4_000

    file_tokens = 0
    if args.upload:
        for path in args.upload:
            try:
                if os.path.isdir(path):
                    for root, _, files in os.walk(path):
                        for f in files:
                            size = os.path.getsize(os.path.join(root, f))
                            file_tokens += size * 0.25
                else:
                    size = os.path.getsize(path)
                    file_tokens += size * 0.25
            except Exception:
                pass

    total_nodes = 0
    for d in range(args.depth):
        nodes_at_level = pow(args.breadth, d)
        total_nodes += nodes_at_level

    total_input = (total_nodes * AVG_INPUT_TOKENS) + (total_nodes * file_tokens)
    total_output = total_nodes * AVG_OUTPUT_TOKENS

    cost = (total_input / 1_000_000 * COST_INPUT_1M) + (
        total_output / 1_000_000 * COST_OUTPUT_1M
    )

    table = Table(title="Cost Estimate (Gemini 3 Pro)")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="bold yellow")

    table.add_row("Recursion Depth", str(args.depth))
    table.add_row("Breadth (Fan-out)", str(args.breadth))
    table.add_row("Total Agent Nodes", str(total_nodes))
    table.add_row("File Context", f"{file_tokens:,.0f} tokens")
    table.add_row("Est. Input Tokens", f"{total_input:,.0f}")
    table.add_row("Est. Output Tokens", f"{total_output:,.0f}")
    table.add_row("Estimated Cost", f"${cost:.2f}")

    console.print(table)
    console.print(
        "[dim]Pricing: $2.00/1M Input, $12.00/1M Output. Actuals may vary based on search grounding.[/]"
    )

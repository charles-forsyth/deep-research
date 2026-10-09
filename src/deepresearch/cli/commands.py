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
    user_db_path,
    xdg_config_home,
)
from deepresearch.core.session import SessionManager
from deepresearch.core.agent import DeepResearchAgent
from deepresearch.cli.base import ResearchRequest, FollowUpRequest
from deepresearch.cli.jsonout import emit, fail, session_dict
from deepresearch.cli.jsonout import json_flag as _json_flag

console = Console(width=120)


def _db() -> str:
    """The current workspace's history DB (Main: the module's user_db_path)."""
    from deepresearch.core.session import _workspace_db

    return _workspace_db() or user_db_path


def _logs_dir() -> str:
    from deepresearch.core import workspace

    if workspace.current_slug() == workspace.MAIN:
        return os.path.join(xdg_config_home, "deepresearch", "logs")
    return str(workspace.get().logs_dir)


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


def _source_uploads(args) -> list[str] | None:
    """--source: indexed sources join --stores (their saved index is reused; rebuilt
    if the content changed); the others are fetched into folders and uploaded."""
    names = getattr(args, "source", None) or []
    if not names:
        return args.upload
    from deepresearch.sources import SourceRegistry
    from deepresearch.sources.index import index_state, stores_for
    from deepresearch.sources.usage import research_uploads, resolve

    reg = SourceRegistry(_db())
    srcs = resolve(reg, names)
    client = None
    if any(index_state(s) != "none" for s in srcs):
        client = genai.Client(api_key=DeepResearchConfig().api_key)
    stores, rest = stores_for(reg, srcs, client)
    if stores:
        print(f"[INFO] data source indexes: {', '.join(stores)}")
        args.stores = (args.stores or []) + stores
    paths, notes = research_uploads(rest) if rest else ([], [])
    for n in notes:
        print(f"[INFO] data source {n}")
    return (args.upload or []) + paths or None


def _record_source_use(args, session_id) -> None:
    names = getattr(args, "source", None) or []
    if not names or not session_id:
        return
    from deepresearch.sources import SourceRegistry

    reg = SourceRegistry(_db())
    for n in names:
        s = reg.get(n)
        if s:
            reg.record_use(s, "session", int(session_id))


def handle_research(args):
    from datetime import datetime

    as_json = _json_flag(args)
    t0 = datetime.now().isoformat()
    try:
        uploads = _source_uploads(args)
    except Exception as e:
        print(f"[ERROR] {e}")
        if args.adopt_session:
            # the dashboard made this row and waits on it; say why it stopped
            SessionManager().fail_session_id(
                int(args.adopt_session), f"Could not prepare the data sources: {e}"
            )
        if as_json:
            fail(f"Could not prepare the data sources: {e}", 2)
        sys.exit(2)
    _record_source_use(args, args.adopt_session)
    started = None
    if getattr(args, "source", None) and not args.adopt_session:
        started = datetime.now().isoformat()
    request = ResearchRequest(
        prompt=args.prompt,
        stores=args.stores,
        stream=args.stream,
        output_format=args.format,
        upload_paths=uploads,
        output_file=args.output,
        adopt_session_id=args.adopt_session,
        depth=args.depth,
        breadth=args.breadth,
        agent="max" if getattr(args, "max", False) is True else None,
        previous_interaction_id=(
            pid
            if isinstance(pid := getattr(args, "plan_id", None), str) and pid
            else None
        ),
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
    if started:
        # a foreground run had no session id up front; record the sources against
        # the session it created (newest with this prompt, created after we started)
        row = SessionManager().find_session_since(args.prompt, started)
        if row:
            _record_source_use(args, row)
    if as_json:
        _emit_research_result(args, t0)


def _emit_research_result(args, since_iso: str) -> None:
    """--json for `research`: the finished session, or an error with its status."""
    mgr = SessionManager()
    sid = args.adopt_session or mgr.find_session_since(args.prompt, since_iso)
    row = mgr.get_session(str(sid)) if sid else None
    if not row:
        fail("Research did not create a session (see the log on stderr)", 1)
    d = session_dict(row, result=True)
    if d.get("status") != "completed":
        fail(f"Research ended with status '{d.get('status')}'", 1, session=d)
    emit(d)


def handle_search(args):
    import json
    import math

    as_json = _json_flag(args)

    config = DeepResearchConfig()
    client = genai.Client(api_key=config.api_key)
    mgr = SessionManager()

    # 1. Backfill if needed
    embedded = 0
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
                embedded += 1
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
        if as_json:
            fail(f"Failed to embed query: {e}", 1)
        return

    all_docs = mgr.get_all_embeddings()
    if not all_docs:
        console.print(
            "[yellow]No completed research sessions found in the database to search.[/]"
        )
        if as_json:
            emit(
                {
                    "query": args.query,
                    "matches": [],
                    "answer": None,
                    "embedded": embedded,
                }
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
        if as_json:
            emit(
                {
                    "query": args.query,
                    "matches": [],
                    "answer": None,
                    "embedded": embedded,
                }
            )
        return
    matches = [
        {"session_id": doc["id"], "score": round(score, 4), "prompt": doc["prompt"]}
        for score, doc in top_k
    ]

    context = ""
    console.print("\n[bold green]Top Matches Found:[/]")
    for score, doc in top_k:
        console.print(
            f"  - Session [bold]#{doc['id']}[/] (Similarity: {score:.2f}) - {doc['prompt'][:60]}..."
        )
        context += f"--- SESSION {doc['id']} (Relevance Score: {score:.2f}) ---\nPROMPT: {doc['prompt']}\nRESULT:\n{doc['result']}\n\n"

    if getattr(args, "no_answer", False) is True:
        # retrieval only: one embedding call, well under a second (the cited answer
        # is a Flash call over the full reports and takes 15-30 s)
        if as_json:
            emit(
                {
                    "query": args.query,
                    "matches": matches,
                    "answer": None,
                    "model": None,
                    "embedded": embedded,
                }
            )
        return

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
        if as_json:
            emit(
                {
                    "query": args.query,
                    "matches": matches,
                    "answer": response.text or "",
                    "model": config.followup_model,
                    "embedded": embedded,
                }
            )
            return
        console.print("\n")
        console.print(
            Panel(Markdown(response.text), title="[bold]Semantic Search Result[/]")
        )
    except Exception as e:
        console.print(f"[bold red][ERROR] Synthesis failed:[/] {e}")
        if as_json:
            fail(f"Synthesis failed: {e}", 1, query=args.query, matches=matches)


def handle_start(args):
    mgr = SessionManager()
    sid = mgr.create_session("pending_start", args.prompt, args.upload)

    child_args = ["research", args.prompt, "--adopt-session", str(sid)]
    if args.upload:
        child_args += ["--upload"] + args.upload
    if args.stores:
        child_args += ["--stores"] + args.stores
    for n in getattr(args, "source", None) or []:
        child_args += ["--source", n]
    if args.format:
        child_args += ["--format", args.format]
    if args.output:
        child_args += ["--output", args.output]

    child_args += ["--depth", str(args.depth)]
    child_args += ["--breadth", str(args.breadth)]
    if getattr(args, "max", False):
        child_args.append("--max")
    if getattr(args, "plan_id", None):
        child_args += ["--plan-id", args.plan_id]

    log_file = os.path.join(_logs_dir(), f"session_{sid}.log")
    pid = detach_process(child_args, log_file)
    mgr.update_session_pid(sid, pid)

    if _json_flag(args):
        emit({"session_id": sid, "pid": pid, "log": log_file, "status": "running"})
        return
    print(f"[INFO] Research started in background! (Session ID: {sid}, PID: {pid})")
    print(f"[INFO] Logs: {log_file}")
    print("[INFO] Check status with: deep-research list")


def handle_followup(args):
    as_json = _json_flag(args)
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
            if as_json:
                fail(f"Session #{args.id} not found or invalid.", 1)
            return

    prompt = args.prompt
    names = getattr(args, "source", None) or []
    if names:
        from deepresearch.sources import SourceRegistry
        from deepresearch.sources.usage import ask_prompt, resolve

        try:
            prompt = ask_prompt(args.prompt, resolve(SourceRegistry(_db()), names))
        except Exception as e:
            print(f"[ERROR] {e}")
            if as_json:
                fail(str(e), 2)
            return
    request = FollowUpRequest(
        interaction_id=interaction_id,
        prompt=prompt,
        display_prompt=args.prompt,
        sources=names or None,
    )
    agent = DeepResearchAgent()
    answer = agent.follow_up(request)
    if as_json:
        row = SessionManager().get_session(interaction_id)
        if not answer:
            fail("Follow-up returned no text (see the log on stderr)", 1)
        emit(
            {
                "session_id": row["id"] if row else None,
                "interaction_id": interaction_id,
                "prompt": args.prompt,
                "sources": names,
                "answer": answer,
            }
        )


def handle_list(args):
    mgr = SessionManager()
    sessions = mgr.list_sessions(args.limit, status=getattr(args, "status", None))
    if _json_flag(args):
        emit([session_dict(s, result=False) for s in sessions])
        return

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


def _tree_node(mgr: SessionManager, row, result: bool) -> dict:
    """A session and all its descendants as nested JSON data."""
    d = session_dict(row, result=result)
    d["children"] = [_tree_node(mgr, c, result) for c in mgr.get_children(row["id"])]
    return d


def _show_json(args, mgr: SessionManager) -> None:
    if args.save:
        fail("--save cannot be combined with --json", 2)
    session = mgr.get_session(args.id)
    if not session:
        fail(f"Session '{args.id}' not found.", 1)
    from deepresearch.sources.provenance import session_provenance

    d = session_dict(session, result=True)
    d["provenance"] = session_provenance(_db(), dict(session))
    if args.recursive:
        d["children"] = [
            _tree_node(mgr, c, result=True) for c in mgr.get_children(session["id"])
        ]
    emit(d)


def handle_show(args):
    mgr = SessionManager()
    if _json_flag(args):
        _show_json(args, mgr)
        return

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
        from deepresearch.sources.provenance import session_provenance

        prov = session_provenance(_db(), dict(session))
        used = ", ".join(
            f"{d['name']}@{(d['manifest_hash'] or '')[:8]}" for d in prov["sources"]
        )
        show_console.print(
            Panel(
                f"[bold]Interaction ID:[/bold] {session['interaction_id']}\n"
                f"[bold]Date:[/bold] {session['created_at']}\n"
                f"[bold]Status:[/bold] {session['status']}\n"
                f"[bold]Files:[/bold] {session['files']}\n"
                + (f"[bold]Data sources:[/bold] {used}\n" if used else "")
                + f"[bold]Inputs fingerprint:[/bold] {prov['fingerprint']}",
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


def handle_repair(args):
    """Restore reports that were saved with only their last part."""
    from deepresearch.core import repair

    cfg = DeepResearchConfig()
    client = genai.Client(api_key=cfg.api_key)
    db = SessionManager().db_path or _db()
    plans = repair.scan(db, client, ids=args.ids or None)
    fixed = repair.apply(db, plans) if args.apply else 0
    resynth: list[int] = []
    if args.apply and getattr(args, "resynthesize", False):
        agent = DeepResearchAgent(config=cfg, quiet=True)
        resynth = repair.resynthesize(
            db,
            plans,
            agent,
            log=(lambda m: None) if _json_flag(args) else console.print,
        )
    todo = [p for p in plans if p["action"] == "repair"]
    counts: dict[str, int] = {}
    for p in plans:
        counts[p["action"]] = counts.get(p["action"], 0) + 1
    if _json_flag(args):
        emit(
            {
                "applied": bool(args.apply),
                "repaired": fixed,
                "resynthesized": resynth,
                "counts": counts,
                "sessions": [
                    {
                        k: v
                        for k, v in p.items()
                        if k not in ("new", "full_main", "tail")
                    }
                    for p in plans
                ],
            }
        )
        return
    for p in todo:
        console.print(
            f"  #{p['id']}: {p['before']:,} -> {p['after']:,} characters"
            + (" (follow-ups kept)" if p.get("kept_followups") else "")
        )
    console.print(
        f"[bold]{len(todo)} report(s) {'repaired' if args.apply else 'need repair'}[/]; "
        + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        + ("" if args.apply or not todo else "\nRun again with --apply to write them.")
    )
    if resynth:
        console.print(f"Re-synthesized: {', '.join('#' + str(i) for i in resynth)}")


def handle_delete(args):
    """Delete a report and its whole tree, exactly as the dashboard does (K10).

    Sub-reports, notes, meta, launch meta, usage, audio, project memberships and the
    report's Lab runs go with it; refused while one of its Lab runs is on the cluster.
    """
    as_json = _json_flag(args)
    mgr = SessionManager()
    row = mgr.get_session(args.id)
    if not row:
        if as_json:
            fail(f"Session '{args.id}' not found.", 1, id=args.id, deleted=False)
        console.print(f"[bold red][ERROR][/] Session '{args.id}' not found.")
        return
    from deepresearch.cli.projects import _api
    from deepresearch.dashboard.server import ApiError

    api = _api(mgr.db_path)
    try:
        out = api.delete_session(str(row["id"]), {"recursive": ["1"]}, None)
    except ApiError as e:
        if as_json:
            fail(e.message, 1, id=args.id, deleted=False)
        console.print(f"[bold red][ERROR][/] {e.message}")
        return
    ids = out["deleted"]
    if as_json:
        emit({"id": args.id, "deleted": True, "ids": ids})
        return
    extra = f" and {len(ids) - 1} sub-report(s)" if len(ids) > 1 else ""
    console.print(f"[bold green][INFO][/] Session '{args.id}'{extra} deleted.")


def _store_info(s) -> dict:
    return {
        "name": s.name,
        "display_name": str(getattr(s, "display_name", "") or ""),
        "create_time": str(getattr(s, "create_time", "") or ""),
    }


def handle_cleanup(args):
    as_json = _json_flag(args)
    config = DeepResearchConfig()
    client = genai.Client(api_key=config.api_key)

    console.print("[bold cyan][INFO][/] Scanning for File Search Stores...")
    try:
        stores = list(client.file_search_stores.list())
    except Exception as e:
        console.print(f"[bold red][ERROR][/] Failed to list stores: {e}")
        if as_json:
            fail(f"Failed to list stores: {e}", 1)
        return

    from deepresearch.storage.files import is_disposable_store

    protected = _protected_stores()
    keep = (
        []
        if getattr(args, "all", False)
        else [s for s in stores if not is_disposable_store(s, protected)]
    )
    kept = {s.name for s in keep}
    stores = [s for s in stores if s.name not in kept]
    if keep:
        console.print(
            f"[bold cyan][INFO][/] Keeping {len(keep)} named store(s) "
            "(data source indexes or stores you created); use --all to include them:"
        )
        for s in keep:
            console.print(f"  {s.name}  {getattr(s, 'display_name', '') or ''}")

    if not stores:
        console.print("[bold green]No temporary stores found. System is clean![/]")
        if as_json:
            emit({"deleted": [], "failed": [], "kept": [_store_info(s) for s in keep]})
        return
    if as_json and not args.force:
        # --json never prompts: without --force it only reports what would go.
        emit(
            {
                "dry_run": True,
                "would_delete": [_store_info(s) for s in stores],
                "kept": [_store_info(s) for s in keep],
                "hint": "add --force to delete",
            }
        )
        return

    table = Table(title=f"Found {len(stores)} store(s) to delete")
    table.add_column("Name (ID)", style="cyan")
    table.add_column("Display name")
    table.add_column("Create Time", style="dim")

    for s in stores:
        created = getattr(s, "create_time", "Unknown")
        table.add_row(s.name, str(getattr(s, "display_name", "") or ""), str(created))

    console.print(table)
    console.print(
        "[bold yellow]WARNING: This will delete the listed stores and their files.[/]"
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

    deleted: list[str] = []
    failed: list[dict] = []
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
                deleted.append(s.name)
            except Exception as e:
                console.print(f"[bold red]Failed to delete {s.name}:[/] {e}")
                failed.append({"name": s.name, "error": str(e)})

    console.print("[bold green]Cleanup Complete![/]")
    if as_json:
        emit(
            {
                "deleted": deleted,
                "failed": failed,
                "kept": [_store_info(s) for s in keep],
            }
        )
        if failed:
            sys.exit(1)


def _protected_stores() -> set[str]:
    """Store names the data source registry points at (options.store)."""
    try:
        from deepresearch.sources import SourceRegistry

        return {
            str(s.options.get("store"))
            for s in SourceRegistry(_db()).list(include_temporary=True)
            if s.options.get("store")
        }
    except Exception:
        return set()


def _tree_json(args, mgr: SessionManager) -> None:
    if args.id:
        root = mgr.get_session(args.id)
        if not root:
            fail(f"Session {args.id} not found", 1)
        emit(_tree_node(mgr, root, result=False))
        return
    with sqlite3.connect(mgr.db_path) as conn:
        conn.row_factory = sqlite3.Row
        roots = conn.execute(
            "SELECT * FROM sessions WHERE parent_id IS NULL ORDER BY updated_at DESC LIMIT 10"
        ).fetchall()
    emit([_tree_node(mgr, r, result=False) for r in roots])


def handle_tree(args):
    mgr = SessionManager()
    if _json_flag(args):
        _tree_json(args, mgr)
        return

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


def set_env_value(path: str, name: str, value: str | None) -> bool:
    """Set (or with None remove) one NAME=value line in a .env file (K15).

    Every other line, comment and blank line is kept as it was. The file is
    rewritten through a temporary file in the same folder and renamed into place,
    so it is never half-written, and it is always left readable by the owner only.
    Returns True when a line for NAME existed before.
    """
    import re as _re
    import tempfile

    lines: list[str] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    pat = _re.compile(rf"^\s*(?:export\s+)?{_re.escape(name)}\s*=")
    existed = any(pat.match(ln) for ln in lines)
    out: list[str] = []
    placed = False
    for ln in lines:
        if pat.match(ln):
            if value is not None and not placed:
                out.append(f"{name}={value}")
                placed = True
            continue
        out.append(ln)
    if value is not None and not placed:
        out.append(f"{name}={value}")
    if value is None and not existed:
        return False
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + ("\n" if out else ""))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return existed


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

        set_env_value(user_config_path, "GEMINI_API_KEY", key)

        console.print(f"[bold green]Success![/] Key saved to {user_config_path}")
        if _json_flag(args):
            emit({"saved": user_config_path})

    elif args.action == "logout":
        existed = set_env_value(user_config_path, "GEMINI_API_KEY", None)
        if existed:
            console.print(
                "[green]Logged out. GEMINI_API_KEY removed; other settings kept.[/]"
            )
        else:
            console.print("[yellow]Not logged in.[/]")
        if _json_flag(args):
            emit({"logged_out": existed, "path": user_config_path})


def handle_estimate(args):
    from deepresearch.core import estimate as est_mod

    file_bytes = 0
    for path in args.upload or []:
        try:
            if os.path.isdir(path):
                for root, _, files in os.walk(path):
                    for f in files:
                        file_bytes += os.path.getsize(os.path.join(root, f))
            else:
                file_bytes += os.path.getsize(path)
        except Exception:
            pass
    e = est_mod.estimate(
        args.depth,
        args.breadth,
        file_bytes,
        "max" if getattr(args, "max", False) else None,
    )
    total_nodes, file_tokens = e["nodes"], e["file_tokens"]
    total_input, total_output, cost = (
        e["input_tokens"],
        e["output_tokens"],
        e["cost_usd"],
    )
    COST_INPUT_1M, COST_CACHED_1M = est_mod.COST_INPUT_1M, est_mod.COST_CACHED_1M
    COST_OUTPUT_1M = est_mod.COST_OUTPUT_1M

    if _json_flag(args):
        emit(
            {
                "prompt": args.prompt,
                "depth": args.depth,
                "breadth": args.breadth,
                "nodes": total_nodes,
                "file_tokens": round(file_tokens),
                "input_tokens": round(total_input),
                "output_tokens": round(total_output),
                "cost_usd": round(cost, 2),
                "agent": e["agent"],
                "searches": e["searches"],
                "search_usd": e["search_usd"],
                "cost_high_usd": e["cost_high_usd"],
                "pricing": {
                    "input_per_1m": COST_INPUT_1M,
                    "cached_per_1m": COST_CACHED_1M,
                    "output_per_1m": COST_OUTPUT_1M,
                    "search_per_1k": est_mod.COST_SEARCH_1K,
                },
                "note": "Rough estimate; tokens, plus up to cost_high_usd if the agent runs Google's full search count.",
            }
        )
        return

    table = Table(
        title=f"Cost Estimate (Gemini Deep Research{' Max' if e['agent'] == 'max' else ''})"
    )
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="bold yellow")

    table.add_row("Recursion Depth", str(args.depth))
    table.add_row("Breadth (Fan-out)", str(args.breadth))
    table.add_row("Total Agent Nodes", str(total_nodes))
    table.add_row("File Context", f"{file_tokens:,.0f} tokens")
    table.add_row("Est. Input Tokens", f"{total_input:,.0f}")
    table.add_row("Est. Output Tokens", f"{total_output:,.0f}")
    table.add_row("Estimated Cost", f"${cost:.2f} to ${e['cost_high_usd']:.2f}")

    console.print(table)
    console.print(
        "[dim]Tokens at $2.00/1M input, $12.00/1M output; the high figure adds Google "
        f"Search at $14/1K for up to {e['searches']} searches.[/]"
    )

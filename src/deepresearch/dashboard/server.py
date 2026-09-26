"""HTTP server for the Deep Research dashboard.

Stdlib only (ThreadingHTTPServer) so the CLI keeps its small dependency set.
Research jobs run as detached `deep-research research ... --adopt-session N`
processes, exactly like `deep-research start`, so they survive a dashboard
restart and show up in the CLI's `list`.
"""

import json
import mimetypes
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import traceback
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from deepresearch import __version__
from deepresearch.core.config import user_db_path, xdg_config_home
from deepresearch.core.session import SessionManager
from deepresearch.dashboard.features import VOICES, Features
from deepresearch.dashboard.store import DashboardStore

MAX_BODY = 25 * 1024 * 1024  # uploads are base64 in JSON
LOG_DIR = Path(xdg_config_home) / "deepresearch" / "logs"
UPLOAD_DIR = Path(xdg_config_home) / "deepresearch" / "uploads"
AUDIO_DIR = Path(xdg_config_home) / "deepresearch" / "audio"
STATIC = resources.files("deepresearch.dashboard") / "static"

# Estimate constants mirror `deep-research estimate` (cli/commands.py).
# Per agent run, from Google's Deep Research docs ("~250k input tokens (~50-70%
# cached), ~60k output") and matching measured runs (2026-09: $0.37-$2.04).
COST_INPUT_1M = 2.00
COST_CACHED_1M = 0.20
COST_OUTPUT_1M = 12.00
AVG_INPUT_TOKENS = 250_000
CACHED_FRACTION = 0.6
AVG_OUTPUT_TOKENS = 60_000


class RawResponse:
    """Binary payload (audio) instead of JSON."""

    def __init__(self, data: bytes, ctype: str, filename: str | None = None):
        self.data = data
        self.ctype = ctype
        self.filename = filename


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _cli_cmd() -> list[str]:
    """Command prefix that runs this same installed CLI (no runpy warning)."""
    from deepresearch.dashboard.daemon import CHILD_BOOT

    return [sys.executable, "-I", "-u", "-c", CHILD_BOOT]


def detach(args: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as log:
        proc = subprocess.Popen(
            _cli_cmd() + args,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={
                **os.environ,
                "PYTHONUNBUFFERED": "1",
                "COLUMNS": "120",
                "DR_LOG_TIMESTAMPS": "1",  # timeline needs times on log lines
            },
        )
    return proc.pid


def estimate(depth: int, breadth: int, file_bytes: int = 0) -> dict:
    file_tokens = file_bytes * 0.25
    nodes = sum(pow(breadth, d) for d in range(max(depth, 1)))
    total_in = nodes * AVG_INPUT_TOKENS + nodes * file_tokens
    total_out = nodes * AVG_OUTPUT_TOKENS
    cached = nodes * AVG_INPUT_TOKENS * CACHED_FRACTION
    cost = (
        (total_in - cached) / 1e6 * COST_INPUT_1M
        + cached / 1e6 * COST_CACHED_1M
        + total_out / 1e6 * COST_OUTPUT_1M
    )
    return {
        "nodes": nodes,
        "input_tokens": int(total_in),
        "output_tokens": int(total_out),
        "file_tokens": int(file_tokens),
        "cost_usd": round(cost, 2),
    }


def _safe_name(name: str) -> str:
    base = os.path.basename(name or "upload")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:120] or "upload"


class Api:
    """Route table + handlers. Kept separate from the HTTP plumbing for tests."""

    def __init__(self, db_path: str = user_db_path, spawn: Callable = detach):
        self.db_path = db_path
        self.sessions = SessionManager(db_path)
        self.store = DashboardStore(db_path)
        self.spawn = spawn
        self._embed_lock = threading.Lock()
        self.fx = Features(db_path, self._config, AUDIO_DIR)
        self._jobs: dict[int, dict] = {}
        self._job_seq = 0
        self._jobs_lock = threading.Lock()
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS run_meta (
                    session_id INTEGER PRIMARY KEY,
                    depth INTEGER, breadth INTEGER, estimate_usd REAL,
                    rerun_of INTEGER, launched_at TEXT
                );
                """
            )
        self.routes: list[tuple[str, re.Pattern, Callable]] = []
        r = self._route
        r("GET", r"/api/health", self.health)
        r("GET", r"/api/stats", self.stats)
        r("GET", r"/api/sessions", self.list_sessions)
        r("GET", r"/api/sessions/(\d+)", self.get_session)
        r("DELETE", r"/api/sessions/(\d+)", self.delete_session)
        r("PATCH", r"/api/sessions/(\d+)/meta", self.patch_meta)
        r("POST", r"/api/sessions/(\d+)/cancel", self.cancel_session)
        r("POST", r"/api/sessions/(\d+)/followup", self.followup)
        r("GET", r"/api/sessions/(\d+)/log", self.session_log)
        r("GET", r"/api/sessions/(\d+)/tree", self.session_tree)
        r("GET", r"/api/sessions/(\d+)/export", self.export_session)
        r("POST", r"/api/research", self.start_research)
        r("POST", r"/api/estimate", self.estimate)
        r("POST", r"/api/uploads", self.upload)
        r("POST", r"/api/search", self.search)
        r("GET", r"/api/annotations", self.list_annotations)
        r("POST", r"/api/annotations", self.create_annotation)
        r("PATCH", r"/api/annotations/(\d+)", self.update_annotation)
        r("DELETE", r"/api/annotations/(\d+)", self.delete_annotation)
        r("GET", r"/api/notebooks", self.list_notebooks)
        r("POST", r"/api/notebooks", self.create_notebook)
        r("GET", r"/api/notebooks/(\d+)", self.get_notebook)
        r("PUT", r"/api/notebooks/(\d+)", self.update_notebook)
        r("DELETE", r"/api/notebooks/(\d+)", self.delete_notebook)
        r("GET", r"/api/stores", self.list_stores)
        r("GET", r"/api/sessions/(\d+)/usage", self.session_usage)
        r("GET", r"/api/sessions/(\d+)/timeline", self.session_timeline)
        r("GET", r"/api/map", self.research_map)
        r("POST", r"/api/compare", self.compare)
        r("POST", r"/api/brief", self.brief)
        r("POST", r"/api/audio/estimate", self.audio_estimate)
        r("POST", r"/api/audio", self.audio_start)
        r("GET", r"/api/audio", self.audio_list)
        r("GET", r"/api/audio/jobs/(\d+)", self.audio_job)
        r("GET", r"/api/audio/(\d+)/file", self.audio_file)

    def _route(self, method: str, pattern: str, fn: Callable) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), fn))

    def dispatch(
        self, method: str, path: str, query: dict, body: Any
    ) -> tuple[int, Any]:
        for m, pat, fn in self.routes:
            match = pat.match(path)
            if match and m == method:
                return 200, fn(*match.groups(), query=query, body=body)
        if any(pat.match(path) for _, pat, _ in self.routes):
            raise ApiError(405, "Method not allowed")
        raise ApiError(404, "Not found")

    # ---- helpers ---------------------------------------------------------

    def _session(self, sid: str) -> dict:
        row = self.sessions.get_session(sid)
        if not row:
            raise ApiError(404, f"Session {sid} not found")
        return dict(row)

    def _refresh_liveness(self) -> None:
        # SessionManager.list_sessions marks dead 'running' rows as crashed.
        self.sessions.list_sessions(limit=10_000)

    def _config(self):
        from deepresearch.core.config import DeepResearchConfig

        try:
            return DeepResearchConfig()
        except Exception as e:
            raise ApiError(400, str(e)) from e

    # ---- handlers --------------------------------------------------------

    def health(self, query, body):
        key = bool(os.getenv("GEMINI_API_KEY"))
        return {"ok": True, "version": __version__, "api_key": key}

    def stats(self, query, body):
        self._refresh_liveness()
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            by_status = dict(
                conn.execute(
                    "SELECT status, COUNT(*) FROM sessions GROUP BY status"
                ).fetchall()
            )
            chars = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(result)), 0) FROM sessions"
            ).fetchone()[0]
            roots = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE parent_id IS NULL"
            ).fetchone()[0]
        return {
            "by_status": by_status,
            "total": sum(by_status.values()),
            "roots": roots,
            "result_chars": chars,
            "notebooks": len(self.store.list_notebooks()),
            "annotations": len(self.store.list_annotations()),
        }

    def list_sessions(self, query, body):
        self._refresh_liveness()
        q = (query.get("q") or [""])[0].strip() or None
        limit = int((query.get("limit") or ["500"])[0])
        return {"sessions": self.store.session_rows(q=q, limit=min(limit, 5000))}

    def get_session(self, sid, query, body):
        s = self._session(sid)
        s["files"] = json.loads(s.get("files") or "[]")
        s.pop("embedding", None)
        s["meta"] = self.store.get_meta(int(sid))
        s["children"] = [
            {"id": c["id"], "status": c["status"], "prompt": c["prompt"]}
            for c in self.sessions.get_children(int(sid))
        ]
        s["annotations"] = self.store.list_annotations(int(sid))
        s["log_available"] = (LOG_DIR / f"session_{sid}.log").exists()
        s["run"] = self._run_meta(int(sid))
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            s["reruns"] = [
                r[0]
                for r in conn.execute(
                    "SELECT session_id FROM run_meta WHERE rerun_of = ? ORDER BY session_id",
                    (int(sid),),
                )
            ]
        return s

    def _run_meta(self, sid: int) -> dict | None:
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM run_meta WHERE session_id = ?", (sid,)
            ).fetchone()
        return dict(row) if row else None

    def delete_session(self, sid, query, body):
        self._session(sid)
        ids = [int(sid)]
        if (query.get("recursive") or ["0"])[0] == "1":
            ids += self.store.descendants(int(sid))
        for i in ids:
            self.sessions.delete_session(str(i))
        self.store.purge_session_workspace(ids)
        return {"deleted": ids}

    def patch_meta(self, sid, query, body):
        self._session(sid)
        body = body or {}
        return self.store.set_meta(
            int(sid), starred=body.get("starred"), tags=body.get("tags")
        )

    def cancel_session(self, sid, query, body):
        s = self._session(sid)
        if s["status"] not in ("running",):
            raise ApiError(409, f"Session is {s['status']}, not running")
        notes = []
        iid = s.get("interaction_id") or ""
        if iid and not iid.startswith("pending"):
            try:
                from google import genai

                genai.Client(api_key=self._config().api_key).interactions.cancel(iid)
                notes.append("cloud interaction cancelled")
            except Exception as e:
                notes.append(f"cloud cancel failed: {e}")
        pid = s.get("pid")
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
                notes.append(f"process group {pid} terminated")
            except ProcessLookupError:
                notes.append("process already gone")
            except Exception as e:
                notes.append(f"kill failed: {e}")
        self.store.set_status(int(sid), "cancelled")
        return {"status": "cancelled", "notes": notes}

    def followup(self, sid, query, body):
        s = self._session(sid)
        prompt = ((body or {}).get("prompt") or "").strip()
        if not prompt:
            raise ApiError(400, "Follow-up prompt is empty")
        iid = s.get("interaction_id") or ""
        if not iid or iid.startswith("pending"):
            raise ApiError(409, "Session has no interaction to follow up on yet")
        from deepresearch.cli.base import FollowUpRequest
        from deepresearch.core.agent import DeepResearchAgent

        agent = DeepResearchAgent(config=self._config(), quiet=True)
        before = self._session(sid).get("result") or ""
        agent.follow_up(FollowUpRequest(interaction_id=iid, prompt=prompt))
        after = self._session(sid).get("result") or ""
        if after == before:
            raise ApiError(502, "Follow-up returned no text (see server log)")
        return {"result": after, "appended": after[len(before) :]}

    def session_log(self, sid, query, body):
        path = LOG_DIR / f"session_{sid}.log"
        if not path.exists():
            return {"text": "", "size": 0, "exists": False}
        offset = int((query.get("offset") or ["0"])[0])
        size = path.stat().st_size
        with open(path, "rb") as f:
            if offset <= 0 or offset > size:
                offset = max(0, size - 200_000)
            f.seek(offset)
            data = f.read()
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", data.decode("utf-8", "replace"))
        return {"text": text, "size": size, "offset": offset, "exists": True}

    def _tree(self, sid: int, seen: set) -> dict:
        s = self._session(str(sid))
        seen.add(sid)
        return {
            "id": s["id"],
            "status": s["status"],
            "depth": s.get("depth") or 1,
            "prompt": s["prompt"],
            "children": [
                self._tree(c["id"], seen)
                for c in self.sessions.get_children(sid)
                if c["id"] not in seen
            ],
        }

    def session_tree(self, sid, query, body):
        return self._tree(int(sid), set())

    def _recursive_markdown(self, sid: int, level: int, seen: set) -> str:
        s = self._session(str(sid))
        seen.add(sid)
        out = f"{'#' * min(level, 6)} Session #{s['id']}\n\n"
        out += f"**Objective:** {s['prompt']}\n\n**Status:** {s['status']}\n\n"
        out += (s.get("result") or "*(No content)*") + "\n\n---\n\n"
        for c in self.sessions.get_children(sid):
            if c["id"] not in seen:
                out += self._recursive_markdown(c["id"], level + 1, seen)
        return out

    def export_session(self, sid, query, body):
        s = self._session(sid)
        fmt = (query.get("format") or ["md"])[0]
        recursive = (query.get("recursive") or ["0"])[0] == "1"
        if recursive:
            md = self._recursive_markdown(int(sid), 1, set())
        else:
            md = f"# {s['prompt']}\n\n{s.get('result') or ''}"
        anns = self.store.list_annotations(int(sid))
        if fmt == "json":
            s.pop("embedding", None)
            s["annotations"] = anns
            if recursive:
                s["recursive_markdown"] = md
            return {"filename": f"session_{sid}.json", "content": s}
        if anns and (query.get("annotations") or ["1"])[0] == "1":
            md += "\n\n---\n\n## Annotations\n\n"
            for a in anns:
                md += f"> {a['quote']}\n\n"
                if a["note"]:
                    md += f"{a['note']}\n\n"
        return {"filename": f"session_{sid}.md", "content": md}

    def start_research(self, query, body):
        body = body or {}
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            raise ApiError(400, "Prompt is empty")
        depth = int(body.get("depth") or 1)
        breadth = int(body.get("breadth") or 3)
        if not (1 <= depth <= 5 and 1 <= breadth <= 10):
            raise ApiError(400, "depth must be 1-5 and breadth 1-10")
        uploads = [str(p) for p in body.get("uploads") or []]
        for p in uploads:
            if not Path(p).resolve().is_relative_to(UPLOAD_DIR.resolve()):
                raise ApiError(400, f"Upload path not allowed: {p}")
            if not Path(p).exists():
                raise ApiError(400, f"Upload missing: {p}")
        stores = [str(s).strip() for s in body.get("stores") or [] if str(s).strip()]
        fmt = (body.get("format") or "").strip()
        if not os.getenv("GEMINI_API_KEY"):
            raise ApiError(400, "GEMINI_API_KEY is not set for the dashboard process")

        sid = self.sessions.create_session("pending_start", prompt, uploads or None)
        args = ["research", prompt, "--adopt-session", str(sid)]
        if uploads:
            args += ["--upload", *uploads]
        if stores:
            args += ["--stores", *stores]
        if fmt:
            args += ["--format", fmt]
        args += ["--depth", str(depth), "--breadth", str(breadth)]
        if depth == 1:
            args.append("--stream")  # thought summaries land in the live log
        pid = self.spawn(args, LOG_DIR / f"session_{sid}.log")
        self.sessions.update_session_pid(sid, pid)
        rerun_of = body.get("rerun_of")
        size = sum(Path(p).stat().st_size for p in uploads if Path(p).exists())
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO run_meta VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    depth,
                    breadth,
                    estimate(depth, breadth, size)["cost_usd"],
                    int(rerun_of) if rerun_of else None,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
        return {"id": sid, "pid": pid}

    def estimate(self, query, body):
        body = body or {}
        size = 0
        for p in body.get("uploads") or []:
            try:
                size += Path(p).stat().st_size
            except OSError:
                pass
        return estimate(
            int(body.get("depth") or 1), int(body.get("breadth") or 3), size
        )

    def upload(self, query, body):
        import base64

        body = body or {}
        name = _safe_name(body.get("name", ""))
        try:
            data = base64.b64decode(body.get("data") or "", validate=True)
        except Exception as e:
            raise ApiError(400, f"Bad upload encoding: {e}") from e
        if not data:
            raise ApiError(400, "Empty file")
        from uuid import uuid4

        dest_dir = UPLOAD_DIR / uuid4().hex[:12]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        dest.write_bytes(data)
        return {"path": str(dest), "name": name, "size": len(data)}

    def search(self, query, body):
        """Semantic search, same approach as `deep-research search`."""
        import math

        body = body or {}
        q = (body.get("query") or "").strip()
        if not q:
            raise ApiError(400, "Query is empty")
        limit = max(1, min(int(body.get("limit") or 5), 20))
        synthesize = bool(body.get("synthesize", True))
        from google import genai

        cfg = self._config()
        client = genai.Client(api_key=cfg.api_key)
        embedded = 0
        with self._embed_lock:
            for row in self.sessions.get_completed_sessions_without_embeddings():
                try:
                    text = f"Objective: {row['prompt']}\n\nResult:\n{row['result']}"
                    resp = client.models.embed_content(
                        model="gemini-embedding-001", contents=text[:15000]
                    )
                    if not resp.embeddings:
                        continue
                    self.sessions.update_embedding(
                        row["id"], json.dumps(resp.embeddings[0].values)
                    )
                    embedded += 1
                except Exception:
                    continue
        qv = client.models.embed_content(model="gemini-embedding-001", contents=q)
        if not qv.embeddings or not qv.embeddings[0].values:
            raise ApiError(502, "Embedding API returned no vector for the query")
        qvec = qv.embeddings[0].values

        def cos(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            ma = math.sqrt(sum(x * x for x in a))
            mb = math.sqrt(sum(y * y for y in b))
            return dot / (ma * mb) if ma and mb else 0.0

        scored = []
        for doc in self.sessions.get_all_embeddings():
            try:
                scored.append((cos(qvec, json.loads(doc["embedding"])), doc))
            except Exception:
                continue
        scored.sort(key=lambda t: t[0], reverse=True)
        top = scored[:limit]
        matches = [
            {"id": d["id"], "score": round(sc, 4), "prompt": d["prompt"]}
            for sc, d in top
        ]
        answer = None
        if synthesize and top:
            ctx = "".join(
                f"--- SESSION {d['id']} (score {sc:.2f}) ---\nPROMPT: {d['prompt']}\n"
                f"RESULT:\n{d['result']}\n\n"
                for sc, d in top
            )
            prompt = (
                f"User Question: {q}\n\nSearch Results from Past Research:\n{ctx}\n"
                "INSTRUCTIONS:\n1. Answer using ONLY the search results above.\n"
                '2. Cite the Session ID (e.g. "[Session #12]") for every fact.\n'
                "3. If the answer is not in the results, say so plainly."
            )
            answer = client.models.generate_content(
                model=cfg.followup_model, contents=prompt
            ).text
        return {"matches": matches, "answer": answer, "embedded_now": embedded}

    def list_annotations(self, query, body):
        sid = (query.get("session_id") or [None])[0]
        return {"annotations": self.store.list_annotations(int(sid) if sid else None)}

    def create_annotation(self, query, body):
        body = body or {}
        sid = int(body.get("session_id") or 0)
        self._session(str(sid))
        quote = (body.get("quote") or "").strip()
        if not quote:
            raise ApiError(400, "Nothing selected to annotate")
        return self.store.create_annotation(
            sid,
            quote[:5000],
            int(body.get("occurrence") or 0),
            body.get("note") or "",
            body.get("color") or "amber",
        )

    def update_annotation(self, aid, query, body):
        body = body or {}
        a = self.store.update_annotation(int(aid), body.get("note"), body.get("color"))
        if not a:
            raise ApiError(404, "Annotation not found")
        return a

    def delete_annotation(self, aid, query, body):
        if not self.store.delete_annotation(int(aid)):
            raise ApiError(404, "Annotation not found")
        return {"deleted": int(aid)}

    def list_notebooks(self, query, body):
        return {"notebooks": self.store.list_notebooks()}

    def create_notebook(self, query, body):
        body = body or {}
        return self.store.create_notebook(
            body.get("title") or "Untitled", body.get("content") or ""
        )

    def get_notebook(self, nid, query, body):
        nb = self.store.get_notebook(int(nid))
        if not nb:
            raise ApiError(404, "Notebook not found")
        return nb

    def update_notebook(self, nid, query, body):
        body = body or {}
        nb = self.store.update_notebook(
            int(nid), body.get("title"), body.get("content")
        )
        if not nb:
            raise ApiError(404, "Notebook not found")
        return nb

    def delete_notebook(self, nid, query, body):
        if not self.store.delete_notebook(int(nid)):
            raise ApiError(404, "Notebook not found")
        return {"deleted": int(nid)}

    def list_stores(self, query, body):
        from google import genai

        client = genai.Client(api_key=self._config().api_key)
        try:
            stores = list(client.file_search_stores.list())
        except Exception as e:
            raise ApiError(502, f"Could not list stores: {e}") from e
        return {
            "stores": [
                {
                    "name": s.name,
                    "display_name": getattr(s, "display_name", None),
                    "create_time": str(getattr(s, "create_time", "") or ""),
                }
                for s in stores
            ]
        }

    # ---- v0.17: cost, timeline, map, compare, briefs, audio -----------------

    def session_usage(self, sid, query, body):
        s = self._session(sid)
        refresh = (query.get("refresh") or ["0"])[0] == "1"
        out = self.fx.usage(
            int(sid), s.get("interaction_id") or "", s["status"], refresh=refresh
        )
        out["estimate_usd"] = (self._run_meta(int(sid)) or {}).get("estimate_usd")
        return out

    def session_timeline(self, sid, query, body):
        root = self._session(sid)
        lanes = []

        def walk(node_id: int, seen: set) -> None:
            s = self._session(str(node_id))
            seen.add(node_id)
            lanes.append(
                {
                    "id": s["id"],
                    "depth": s.get("depth") or 1,
                    "parent_id": s.get("parent_id"),
                    "status": s["status"],
                    "prompt": s["prompt"],
                    "created_at": s["created_at"],
                    "updated_at": s["updated_at"],
                    "chars": len(s.get("result") or ""),
                }
            )
            for c in self.sessions.get_children(node_id):
                if c["id"] not in seen:
                    walk(c["id"], seen)

        walk(int(sid), set())
        events = []
        path = LOG_DIR / f"session_{sid}.log"
        if path.exists():
            text = re.sub(
                r"\x1b\[[0-9;]*[A-Za-z]", "", path.read_text("utf-8", "replace")
            )
            for line in text.splitlines():
                m = re.match(
                    r"^(?:\[(\d\d:\d\d:\d\d)\] )?\[(THOUGHT|INFO|ERROR|WARN)\] (.*)$",
                    line.strip(),
                )
                if m:
                    events.append(
                        {
                            "t": m.group(1),
                            "kind": m.group(2).lower(),
                            "text": m.group(3)[:400],
                        }
                    )
        return {
            "root": root["id"],
            "status": root["status"],
            "lanes": lanes,
            "events": events[-400:],
            "run": self._run_meta(int(sid)),
        }

    def research_map(self, query, body):
        return self.fx.research_map()

    def compare(self, query, body):
        body = body or {}
        a = self._session(str(body.get("a")))
        b = self._session(str(body.get("b")))
        try:
            return self.fx.compare(a, b, bool(body.get("summarize")))
        except Exception as e:
            raise ApiError(502, f"Compare failed: {e}") from e

    def _doc(self, kind: str, ref: int) -> tuple[str, str]:
        if kind == "session":
            s = self._session(str(ref))
            return s["prompt"], s.get("result") or ""
        if kind == "notebook":
            nb = self.store.get_notebook(ref)
            if not nb:
                raise ApiError(404, "Notebook not found")
            return nb["title"], nb["content"]
        raise ApiError(400, "kind must be session or notebook")

    def brief(self, query, body):
        body = body or {}
        title, content = self._doc(body.get("kind", ""), int(body.get("id") or 0))
        if not content.strip():
            raise ApiError(400, "Nothing to summarize")
        try:
            return self.fx.brief(title, content, body.get("style") or "brief")
        except ValueError as e:
            raise ApiError(400, str(e)) from e
        except Exception as e:
            raise ApiError(502, f"Brief failed: {e}") from e

    def audio_estimate(self, query, body):
        body = body or {}
        _, content = self._doc(body.get("kind", ""), int(body.get("id") or 0))
        mode = body.get("mode") or "full"
        text = self.fx.speakable(content)
        out = self.fx.estimate_audio(text, mode)
        out["voices"] = VOICES
        return out

    def audio_start(self, query, body):
        body = body or {}
        kind = body.get("kind", "")
        ref = int(body.get("id") or 0)
        title, content = self._doc(kind, ref)
        mode = body.get("mode") or "full"
        voice = body.get("voice") or "Charon"
        if mode not in ("full", "summary") or voice not in VOICES:
            raise ApiError(400, "bad mode or voice")
        if not content.strip():
            raise ApiError(400, "Nothing to read")
        with self._jobs_lock:
            self._job_seq += 1
            jid = self._job_seq
            self._jobs[jid] = {
                "id": jid,
                "status": "running",
                "result": None,
                "error": None,
            }

        def work():
            try:
                res = self.fx.make_audio(kind, ref, title, content, mode, voice)
                res.pop("path", None)
                self._jobs[jid].update(status="done", result=res)
            except Exception as e:
                traceback.print_exc()
                self._jobs[jid].update(status="error", error=str(e)[:300])

        threading.Thread(target=work, daemon=True).start()
        return {"job": jid}

    def audio_job(self, jid, query, body):
        job = self._jobs.get(int(jid))
        if not job:
            raise ApiError(404, "No such job")
        return job

    def audio_list(self, query, body):
        kind = (query.get("kind") or ["session"])[0]
        ref = int((query.get("id") or ["0"])[0])
        rows = self.fx.list_audio(kind, ref)
        for r in rows:
            r.pop("path", None)
        return {"audio": rows}

    def audio_file(self, aid, query, body):
        try:
            data, ctype, name = self.fx.audio_file(int(aid))
        except FileNotFoundError as e:
            raise ApiError(404, "Audio not found") from e
        return RawResponse(data, ctype, name)


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"deep-research-dashboard/{__version__}"

        def log_message(self, format: str, *args: Any) -> None:
            if os.getenv("DR_DASHBOARD_ACCESS_LOG"):
                super().log_message(format, *args)

        def _send(self, status: int, payload: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: int, obj: Any) -> None:
            self._send(
                status,
                json.dumps(obj, default=str).encode(),
                "application/json; charset=utf-8",
            )

        def _handle(self, method: str) -> None:
            url = urlparse(self.path)
            if not url.path.startswith("/api/"):
                if method != "GET":
                    return self._json(405, {"error": "Method not allowed"})
                return self._static(url.path)
            body = None
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(413, {"error": "Request too large"})
            if length:
                if method != "GET" and not (
                    self.headers.get("Content-Type", "").startswith("application/json")
                ):
                    # Blocks simple cross-site form posts (no CORS preflight).
                    return self._json(415, {"error": "Use application/json"})
                try:
                    body = json.loads(self.rfile.read(length) or b"null")
                except ValueError:
                    return self._json(400, {"error": "Invalid JSON"})
            try:
                status, obj = api.dispatch(method, url.path, parse_qs(url.query), body)
                if isinstance(obj, RawResponse):
                    return self._raw(obj)
                self._json(status, obj)
            except ApiError as e:
                self._json(e.status, {"error": e.message})
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def _raw(self, obj: "RawResponse") -> None:
            data, total = obj.data, len(obj.data)
            start, end, status = 0, total - 1, 200
            m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
            if m and total:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else total - 1
                else:  # suffix range: last N bytes
                    start = max(0, total - int(m.group(2) or 0))
                end = min(end, total - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.end_headers()
                    return
                status = 206
            chunk = data[start : end + 1]
            self.send_response(status)
            self.send_header("Content-Type", obj.ctype)
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            if obj.filename:
                disp = "attachment" if "download=1" in self.path else "inline"
                self.send_header(
                    "Content-Disposition", f'{disp}; filename="{obj.filename}"'
                )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(chunk)

        def _static(self, path: str) -> None:
            rel = path.lstrip("/") or "index.html"
            parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
            node = STATIC
            for p in parts:
                node = node / p
            if not node.is_file():
                node = STATIC / "index.html"
                parts = ["index.html"]
            data = node.read_bytes()
            ctype = mimetypes.guess_type(parts[-1])[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(HTTPStatus.OK, data, ctype)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PUT(self) -> None:
            self._handle("PUT")

        def do_PATCH(self) -> None:
            self._handle("PATCH")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

    return Handler


def serve(host: str, port: int, db_path: str = user_db_path) -> None:
    api = Api(db_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(api))
    httpd.daemon_threads = True
    print(f"[INFO] Deep Research dashboard {__version__} on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

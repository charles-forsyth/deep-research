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
import time
import traceback
import zipfile
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
from deepresearch.dashboard.lab import Lab, TargetError
from deepresearch.dashboard.project_api import ProjectApi
from deepresearch.dashboard.projects import ProjectStore
from deepresearch.dashboard.store import DashboardStore

MAX_BODY = 25 * 1024 * 1024  # uploads are base64 in JSON
MAX_IMPORT = 5 * 1024**3  # workspace zips are streamed, not JSON
LOG_DIR = Path(xdg_config_home) / "deepresearch" / "logs"
UPLOAD_DIR = Path(xdg_config_home) / "deepresearch" / "uploads"
AUDIO_DIR = Path(xdg_config_home) / "deepresearch" / "audio"
STATE_DIR = Path(xdg_config_home) / "deepresearch"
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

    def __init__(
        self,
        data: bytes,
        ctype: str,
        filename: str | None = None,
        inline: bool = True,
        sandbox: bool = False,
    ):
        self.data = data
        self.ctype = ctype
        self.filename = filename
        self.inline = inline
        self.sandbox = sandbox


# Types a browser shows without running anything.
SAFE_INLINE = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
    "video/mp4",
}


def _clean_msg(e: Any) -> str:
    """A pydantic error as a sentence (no 'Value error, ' prefix)."""
    msg = str(e.errors()[0].get("msg", e))
    return re.sub(r"^(Value error|Assertion failed), ", "", msg)


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
            cwd=str(LOG_DIR.parent),  # never pick up a stray ./.env
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


# Host names that only resolve on the local network or tailnet. A DNS-rebinding
# page is served from a public domain, so its requests carry that domain in Host.
LOCAL_SUFFIXES = (
    ".local",
    ".lan",
    ".home",
    ".home.arpa",
    ".localdomain",
    ".internal",
    ".ts.net",
)


def _split_host(value: str) -> str:
    """'Host' or Origin netloc -> lower-case host name without port or brackets."""
    v = (value or "").strip().lower()
    if v.startswith("["):  # [::1]:7420
        return v[1 : v.find("]")] if "]" in v else v
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def host_allowed(host_header: str) -> bool:
    import ipaddress

    host = _split_host(host_header).rstrip(".")
    if not host:
        return False
    extra = os.getenv("DR_ALLOWED_HOSTS", "")
    if host in {h.strip().lower() for h in extra.split(",") if h.strip()}:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return "." not in host or host.endswith(LOCAL_SUFFIXES)


def _safe_name(name: str) -> str:
    base = os.path.basename(name or "upload")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:120] or "upload"


class WorkspaceContext:
    """Everything a request needs from one workspace: its DB and the stores, Lab and
    features bound to it, plus its folders for logs, uploads and audio."""

    def __init__(self, slug: str, db_path: str, config, lab: Lab | None = None,
                 log_dir: Path | None = None, upload_dir: Path | None = None,
                 audio_dir: Path | None = None, results_dir: Path | None = None):  # fmt: skip
        from deepresearch.sources import SourceRegistry

        self.slug = slug
        self.db_path = db_path
        self.log_dir = log_dir
        self.upload_dir = upload_dir
        self.sessions = SessionManager(db_path)
        self.store = DashboardStore(db_path)
        self.fx = Features(db_path, config, audio_dir or AUDIO_DIR)
        self.lab = lab or Lab(
            db_path, config, STATE_DIR, results_dir=results_dir, workspace=slug
        )
        self.sources = SourceRegistry(db_path)
        self.projects = ProjectStore(db_path)
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


_CTX = threading.local()  # the workspace of the request this thread is serving

_CTX_ATTRS = ("db_path", "sessions", "store", "fx", "lab", "sources", "projects")


def _ctx_prop(name: str):
    def get(self):
        ctx = getattr(_CTX, "ctx", None) or self._main
        return getattr(ctx, name)

    def set_(self, value):
        # tests (and the scratch server) replace these on the Api; that means Main
        setattr(self._main, name, value)

    return property(get, set_)


class Api(ProjectApi):
    """Route table + handlers. Kept separate from the HTTP plumbing for tests.

    Workspaces: `self.db_path`, `self.sessions`, `self.store`, `self.fx`, `self.lab`,
    `self.sources` and `self.projects` resolve to the workspace of the request being
    served (header X-DR-Workspace; default Main), so every handler works unchanged in
    any workspace. Main's context is the one built from the constructor arguments.
    """

    db_path = _ctx_prop("db_path")  # type: ignore[assignment]
    sessions = _ctx_prop("sessions")  # type: ignore[assignment]
    store = _ctx_prop("store")  # type: ignore[assignment]
    fx = _ctx_prop("fx")  # type: ignore[assignment]
    lab = _ctx_prop("lab")  # type: ignore[assignment]
    sources = _ctx_prop("sources")  # type: ignore[assignment]
    projects = _ctx_prop("projects")  # type: ignore[assignment]

    def __init__(
        self,
        db_path: str = user_db_path,
        spawn: Callable = detach,
        lab: Lab | None = None,
        workspaces: bool = False,
    ):
        # workspaces=False (tests, scratch server): Main only, exactly as before
        self._main = WorkspaceContext("main", db_path, self._config, lab=lab)
        self._contexts: dict[str, WorkspaceContext] = {"main": self._main}
        self._ctx_lock = threading.Lock()
        self._workspaces = workspaces
        self.spawn = spawn
        self._embed_lock = threading.Lock()
        self._jobs: dict[int, dict] = {}
        self._job_seq = 0
        self._jobs_lock = threading.Lock()
        self.routes: list[tuple[str, re.Pattern, Callable]] = []
        r = self._route
        r("GET", r"/api/health", self.health)
        r("GET", r"/api/workspaces", self.ws_list)
        r("POST", r"/api/workspaces", self.ws_create)
        r("PATCH", r"/api/workspaces/([a-z0-9-]+)", self.ws_update)
        r("POST", r"/api/workspaces/([a-z0-9-]+)/duplicate", self.ws_duplicate)
        r("DELETE", r"/api/workspaces/([a-z0-9-]+)", self.ws_delete)
        r("POST", r"/api/workspaces/copy/plan", self.ws_copy_plan)
        r("POST", r"/api/workspaces/copy", self.ws_copy)
        r("GET", r"/api/workspaces/([a-z0-9-]+)/export", self.ws_export)
        r("GET", r"/api/stats", self.stats)
        r("GET", r"/api/sessions", self.list_sessions)
        r("GET", r"/api/sessions/(\d+)", self.get_session)
        r("DELETE", r"/api/sessions/(\d+)", self.delete_session)
        r("PATCH", r"/api/sessions/(\d+)/meta", self.patch_meta)
        r("POST", r"/api/sessions/(\d+)/cancel", self.cancel_session)
        r("POST", r"/api/sessions/(\d+)/cancel/retry", self.retry_cancel)
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
        r("GET", r"/api/lab/runs", self.lab_all_runs)
        r("GET", r"/api/lab/targets", self.lab_targets)
        r("GET", r"/api/lab/catalog", self.lab_catalog)
        r("POST", r"/api/lab/catalog/refresh", self.lab_catalog_refresh)
        r("GET", r"/api/lab/warm", self.lab_warm)
        r("POST", r"/api/lab/warm/start", self.lab_warm_start)
        r("POST", r"/api/lab/warm/stop", self.lab_warm_stop)
        r("GET", r"/api/lab/pitfalls", self.lab_pitfalls)
        r("POST", r"/api/lab/pitfalls", self.lab_pitfall_add)
        r("DELETE", r"/api/lab/pitfalls/([\w.-]+)", self.lab_pitfall_rm)
        r("GET", r"/api/sessions/(\d+)/lab", self.lab_list)
        r("POST", r"/api/sessions/(\d+)/lab/suggestions", self.lab_suggestions)
        r("POST", r"/api/sessions/(\d+)/lab", self.lab_create)
        r("GET", r"/api/lab/(\d+)", self.lab_get)
        r("PUT", r"/api/lab/(\d+)/plan", self.lab_edit)
        r("POST", r"/api/lab/(\d+)/submit", self.lab_submit)
        r("POST", r"/api/lab/(\d+)/cancel", self.lab_cancel)
        r("POST", r"/api/lab/(\d+)/rerun", self.lab_rerun)
        r("POST", r"/api/lab/(\d+)/replan", self.lab_replan)
        r("POST", r"/api/lab/(\d+)/fix", self.lab_fix)
        r("POST", r"/api/lab/(\d+)/review", self.lab_review)
        r("POST", r"/api/lab/(\d+)/undo-fix", self.lab_undo_fix)
        r("POST", r"/api/lab/(\d+)/undo-refine", self.lab_undo_refine)
        r("POST", r"/api/lab/(\d+)/laptop-fetch", self.lab_laptop_fetch)
        r("POST", r"/api/lab/(\d+)/fix-failed", self.lab_fix_failed)
        r("GET", r"/api/lab/(\d+)/log", self.lab_log)
        r("GET", r"/api/lab/(\d+)/file", self.lab_file)
        r("DELETE", r"/api/lab/(\d+)", self.lab_delete)
        r("GET", r"/api/sources/discover", self.sources_discover)
        r("GET", r"/api/browse/places", self.browse_places)
        r("GET", r"/api/browse/list", self.browse_list)
        r("GET", r"/api/browse/preview", self.browse_preview)
        r("POST", r"/api/browse/spec", self.browse_spec)
        r("GET", r"/api/sources", self.sources_list)
        r("POST", r"/api/sources", self.sources_add)
        r("GET", r"/api/sources/(\d+)", self.sources_get)
        r("PATCH", r"/api/sources/(\d+)", self.sources_patch)
        r("DELETE", r"/api/sources/(\d+)", self.sources_delete)
        r("POST", r"/api/sources/(\d+)/test", self.sources_test)
        r("POST", r"/api/sources/(\d+)/index", self.sources_index)
        r("DELETE", r"/api/sources/(\d+)/index", self.sources_index_drop)
        r("GET", r"/api/sources/(\d+)/browse", self.sources_browse)
        r("GET", r"/api/sources/(\d+)/preview", self.sources_preview)
        self.register_project_routes()

    def _route(self, method: str, pattern: str, fn: Callable) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), fn))

    # ---- workspaces ------------------------------------------------------
    def context(self, slug: str | None) -> WorkspaceContext:
        """The context for a workspace id (created on first use)."""
        s = (slug or "main").strip().lower() or "main"
        if s == "main" or not self._workspaces:
            if s != "main":
                raise ApiError(400, "Workspaces are not enabled on this server")
            return self._main
        with self._ctx_lock:
            ctx = self._contexts.get(s)
            if ctx:
                return ctx
            from deepresearch.core import workspace as W

            try:
                ws = W.get(s)
            except W.WorkspaceError as e:
                raise ApiError(404, str(e)) from e
            if ws.archived:
                raise ApiError(409, f"Workspace {s!r} is archived")
            W.init_db(ws)
            ctx = WorkspaceContext(
                ws.slug, ws.db_path, self._config,
                log_dir=ws.logs_dir, upload_dir=ws.uploads_dir,
                audio_dir=ws.audio_dir, results_dir=ws.lab_dir,
            )  # fmt: skip
            # share the cluster targets (one SSH connection, one warm node)
            ctx.lab.targets = self._main.lab.targets
            self._contexts[s] = ctx
            return ctx

    def start_watchers(self) -> None:
        """Resume Lab watching in every workspace with runs still in flight, and keep
        the always-on warm node alive when the target asks for it."""
        self._main.lab.ensure_watcher()
        try:
            self._main.lab.start_warm_keeper()
        except Exception:
            traceback.print_exc()
        if not self._workspaces:
            return
        from deepresearch.core import workspace as W

        for ws in W.list_all(include_archived=False):
            if ws.is_main or not Path(ws.db_path).exists():
                continue
            try:
                self.context(ws.slug).lab.ensure_watcher()
            except Exception:
                traceback.print_exc()

    @property
    def current_workspace(self) -> str:
        ctx = getattr(_CTX, "ctx", None) or self._main
        return ctx.slug

    def _log_dir(self) -> Path:
        ctx = getattr(_CTX, "ctx", None) or self._main
        return ctx.log_dir or LOG_DIR

    def _upload_dir(self) -> Path:
        ctx = getattr(_CTX, "ctx", None) or self._main
        return ctx.upload_dir or UPLOAD_DIR

    # ---- workspace API ------------------------------------------------------
    def _ws_enabled(self) -> None:
        if not self._workspaces:
            raise ApiError(400, "Workspaces are not enabled on this server")

    def ws_list(self, query, body):
        from deepresearch.cli.workspaces import stats
        from deepresearch.core import workspace as W

        if not self._workspaces:
            return {"enabled": False, "current": "main", "workspaces": []}
        out = []
        for ws in W.list_all(include_archived=True):
            active = 0
            ctx = self._contexts.get(ws.slug)
            if ctx:
                active = len(ctx.lab.active())
            out.append({**ws.to_dict(), **stats(ws), "active_lab_runs": active})
        return {"enabled": True, "current": self.current_workspace, "workspaces": out}

    def ws_create(self, query, body):
        from deepresearch.core import workspace as W

        self._ws_enabled()
        body = body or {}
        try:
            ws = W.create(
                str(body.get("name") or ""),
                body.get("id") or None,
                color=str(body.get("color") or "teal"),
                description=str(body.get("description") or ""),
            )
        except W.WorkspaceError as e:
            raise ApiError(400, str(e)) from e
        return ws.to_dict()

    def ws_update(self, slug, query, body):
        from deepresearch.core import workspace as W

        self._ws_enabled()
        body = body or {}
        try:
            ws = W.update(
                slug,
                name=body.get("name"),
                color=body.get("color"),
                archived=body.get("archived"),
                description=body.get("description"),
            )
        except W.WorkspaceError as e:
            raise ApiError(400, str(e)) from e
        if ws.archived:
            with self._ctx_lock:
                self._contexts.pop(ws.slug, None)
        return ws.to_dict()

    def ws_duplicate(self, slug, query, body):
        from deepresearch.core import workspace as W

        self._ws_enabled()
        body = body or {}
        try:
            ws = W.duplicate(slug, str(body.get("name") or ""), body.get("id") or None)
        except W.WorkspaceError as e:
            raise ApiError(400, str(e)) from e
        return ws.to_dict()

    def ws_delete(self, slug, query, body):
        from deepresearch.core import workspace as W

        self._ws_enabled()
        if (body or {}).get("confirm") != slug:
            raise ApiError(400, 'Send {"confirm": "<id>"} to delete a workspace')
        ctx = self._contexts.get(slug)
        if ctx and ctx.lab.active():
            raise ApiError(
                409, "This workspace has Lab runs in progress; cancel them first"
            )
        try:
            dest = W.trash(slug)
        except W.WorkspaceError as e:
            raise ApiError(400, str(e)) from e
        with self._ctx_lock:
            self._contexts.pop(slug, None)
        return {"deleted": slug, "moved_to": str(dest)}

    def _copy_args(self, body) -> tuple[str, str, list[int], list[int]]:
        body = body or {}
        src = str(body.get("from") or self.current_workspace)
        dst = str(body.get("to") or "")
        projects = [int(x) for x in body.get("projects") or []]
        reports = [int(x) for x in body.get("reports") or []]
        if not dst:
            raise ApiError(400, "Pick a workspace to copy into")
        return src, dst, projects, reports

    def ws_copy_plan(self, query, body):
        from deepresearch.core import workspace as W
        from deepresearch.core import wscopy

        self._ws_enabled()
        src, dst, projects, reports = self._copy_args(body)
        try:
            cp = wscopy.plan(W.get(src).db_path, projects, reports)
        except (W.WorkspaceError, wscopy.CopyError) as e:
            raise ApiError(400, str(e)) from e
        return {"from": src, "to": dst, "counts": cp.counts()}

    def ws_copy(self, query, body):
        from deepresearch.core import workspace as W
        from deepresearch.core import wscopy

        self._ws_enabled()
        src, dst, projects, reports = self._copy_args(body)
        try:
            return wscopy.copy(src, dst, projects, reports)
        except (W.WorkspaceError, wscopy.CopyError) as e:
            raise ApiError(400, str(e)) from e

    def ws_export(self, slug, query, body):
        """Download a workspace as a zip (built in a temp file, streamed back)."""
        import tempfile

        from deepresearch.core import workspace as W
        from deepresearch.core import wszip

        self._ws_enabled()
        audio = (query.get("audio") or ["0"])[0] == "1"
        with tempfile.TemporaryDirectory(prefix="drws-dl-") as tmp:
            try:
                man = wszip.export(slug, tmp, include_audio=audio)
            except (W.WorkspaceError, wszip.ZipError) as e:
                raise ApiError(400, str(e)) from e
            p = Path(man["path"])
            if p.stat().st_size > 2 * 1024**3:
                raise ApiError(
                    413,
                    "Too large to download here; use `deep-research workspace export`",
                )
            data = p.read_bytes()
        return RawResponse(data, "application/zip", p.name, inline=False)

    def ws_import_file(self, path: Path, name: str | None) -> dict:
        """Import an uploaded zip (the handler streams it to `path`)."""
        from deepresearch.core import workspace as W
        from deepresearch.core import wszip

        self._ws_enabled()
        try:
            ws = wszip.import_zip(path, name or None)
        except (W.WorkspaceError, wszip.ZipError, zipfile.BadZipFile) as e:
            raise ApiError(400, str(e)) from e
        return ws.to_dict()

    def dispatch(
        self, method: str, path: str, query: dict, body: Any,
        workspace: str | None = None,
    ) -> tuple[int, Any]:  # fmt: skip
        prev = getattr(_CTX, "ctx", None)
        _CTX.ctx = self.context(workspace)
        try:
            return self._dispatch(method, path, query, body)
        finally:
            _CTX.ctx = prev

    def _dispatch(
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

    def _key_valid(self) -> bool | None:
        """Check the key with Google once every 10 minutes (None = could not check)."""
        import time
        import urllib.error
        import urllib.request

        key = os.getenv("GEMINI_API_KEY") or ""
        if not key:
            return False
        cached = getattr(self, "_key_check", None)
        if cached and cached[0] == key and time.time() - cached[1] < 600:
            return cached[2]
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
            headers={"x-goog-api-key": key},
        )
        try:
            with urllib.request.urlopen(req, timeout=5):
                ok: bool | None = True
        except urllib.error.HTTPError as e:
            ok = False if e.code in (400, 401, 403) else None
        except Exception:
            ok = None  # offline: don't claim either way
        self._key_check = (key, time.time(), ok)
        return ok

    def health(self, query, body):
        key = bool(os.getenv("GEMINI_API_KEY"))
        out = {
            "ok": True,
            "version": __version__,
            "api_key": key,
            "workspace": self.current_workspace,
        }
        if (query.get("check") or ["0"])[0] == "1":
            out["api_key_valid"] = self._key_valid()
        return out

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
        rows = self.store.session_rows(q=q, limit=min(limit, 5000))
        members = self.projects.membership_map()
        for r in rows:
            r["projects"] = members.get(r["id"], []) if not r.get("parent_id") else []
            if r.get("status") == "running":
                st = self._stall(r)
                if st:
                    r["stalled"] = st["reason"]
        return {"sessions": rows}

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
        s["log_available"] = (self._log_dir() / f"session_{sid}.log").exists()
        s["stall"] = self._stall(s)
        s["cancel_unconfirmed"] = self.store.cancel_unconfirmed(int(sid))
        s["run"] = self._run_meta(int(sid))
        from deepresearch.sources.provenance import session_provenance

        s["provenance"] = session_provenance(self.db_path, s)
        s["projects"] = self.projects.projects_for("session", int(sid))
        home = self.projects.home_project(int(sid))
        s["project_defaults"] = self._defaults(home) if home else None
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            s["reruns"] = [
                r[0]
                for r in conn.execute(
                    "SELECT session_id FROM run_meta WHERE rerun_of = ? ORDER BY session_id",
                    (int(sid),),
                )
            ]
        return s

    STALL_MIN = 45  # a running report whose log has not grown for this long

    def _stall(self, s: dict) -> dict | None:
        """Is a 'running' session stuck? Its log stopped growing, or its process died.

        Deep Research at Google can sit "in_progress" with no output for hours (#287);
        the dashboard then shows "running" forever. This reports it so the page can offer
        Stop / Re-run instead of a spinner.
        """
        if s.get("status") != "running":
            return None
        log = self._log_dir() / f"session_{s['id']}.log"
        pid = s.get("pid")
        alive = True
        if pid:
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                alive = False
            except (PermissionError, ValueError, TypeError):
                pass
        idle = self._log_idle_min(log)
        if not alive:
            return {"reason": "process", "minutes": round(idle or 0),
                    "message": "The research process is gone but the session was never "
                               "finished. Stop it and re-run."}  # fmt: skip
        if idle is not None and idle >= self.STALL_MIN:
            return {"reason": "idle", "minutes": round(idle),
                    "message": f"No progress for {round(idle)} minutes. Deep Research "
                               "sometimes stalls at Google; stop it and re-run it."}  # fmt: skip
        return None

    @staticmethod
    def _log_idle_min(log) -> float | None:
        """Minutes since the log last showed NEW progress.

        File mtime is not enough: when Google's stream drops, the agent reconnects every
        ~10 minutes and the resumed stream replays the same thoughts, so the file keeps
        growing while nothing moves (#287: 13 identical replays). Progress = a [THOUGHT] or
        output line not seen earlier in the log; reconnect notices don't count.
        """
        try:
            if not log.exists():
                return None
            mtime = log.stat().st_mtime
            text = log.read_text("utf-8", "replace")[-400_000:]
        except OSError:
            return None
        stamp = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\] (.*)$")
        seen: set[str] = set()
        last_new: str | None = None
        for ln in text.splitlines():
            m = stamp.match(ln)
            if not m:
                continue
            body = m.group(4).strip()
            if not body or "Connection lost" in body or "Resuming from" in body:
                continue
            if body not in seen:
                seen.add(body)
                last_new = ":".join(m.groups()[:3])
        idle = (time.time() - mtime) / 60
        if last_new:
            # the newest new line's clock time, today (the log uses local HH:MM:SS)
            now = time.localtime()
            h, mi, se = (int(x) for x in last_new.split(":"))
            t = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, h, mi, se, 0, 0, -1))
            if t > time.time() + 60:
                t -= 86400  # it was yesterday
            idle = max(idle, (time.time() - t) / 60)
        return idle

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
        live = self.lab.active_for(ids)
        if live:
            # Deleting would drop the only record of a job still using the cluster.
            raise ApiError(
                409,
                "Cancel the lab runs still on the cluster first: "
                + ", ".join(f"#{r['id']} ({r['status']})" for r in live),
            )
        for i in ids:
            self.sessions.delete_session(str(i))
        audio = self.store.purge_session_workspace(ids)
        self.lab.purge_session(ids)
        self.projects.purge("session", ids)
        self._remove_audio_files(audio)
        return {"deleted": ids}

    def _remove_audio_files(self, paths: list[str]) -> None:
        """Delete audio files of deleted reports, only inside this library's audio folder."""
        root = Path(self.fx.audio_dir).resolve()
        for p in paths:
            try:
                f = Path(p).resolve()
                if f.is_relative_to(root) and f.is_file():
                    f.unlink()
            except OSError:
                pass

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
        # A recursive run starts each child as its own background task at Google;
        # killing the local process does not stop them, so cancel every one.
        rows = [s] + [
            dict(r)
            for c in self.store.descendants(int(sid))
            if (r := self.sessions.get_session(str(c))) and r["status"] == "running"
        ]
        unconfirmed = self._cloud_cancel(
            [(r, "cloud interaction" if r is s else f"child #{r['id']}") for r in rows],
            notes,
        )
        pid = s.get("pid")
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
                notes.append(f"process group {pid} terminated")
            except ProcessLookupError:
                notes.append("process already gone")
            except Exception as e:
                notes.append(f"kill failed: {e}")
        for row in rows:
            self.store.set_status(int(row["id"]), "cancelled")
        # The local process is gone either way, but say plainly when Google did not
        # confirm: that task may still be running (and billing) until it finishes.
        self.store.set_cancel_unconfirmed(int(sid), unconfirmed)
        return {
            "status": "cancelled",
            "notes": notes,
            "cancel_unconfirmed": unconfirmed,
        }

    def _cloud_cancel(
        self, rows: list[tuple[dict, str]], notes: list[str]
    ) -> list[dict]:
        """Ask Google to cancel each interaction; return the ones it did not confirm."""
        client = None
        failed: list[dict] = []
        for row, label in rows:
            iid = row.get("interaction_id") or ""
            if not iid or iid.startswith("pending"):
                continue
            try:
                if client is None:
                    from google import genai

                    client = genai.Client(api_key=self._config().api_key)
                client.interactions.cancel(iid)
                notes.append(f"{label} cancelled")
            except Exception as e:
                notes.append(f"{label} cancel failed: {e}")
                failed.append(
                    {"id": int(row["id"]), "interaction_id": iid, "error": str(e)[:300]}
                )
        return failed

    def retry_cancel(self, sid, query, body):
        """Retry cloud cancels that failed (K4). 409 when there is nothing to retry."""
        self._session(sid)
        pending = self.store.cancel_unconfirmed(int(sid))
        if not pending:
            raise ApiError(409, "Nothing to retry: every cancel was confirmed")
        notes: list[str] = []
        rows = [
            ({"id": p["id"], "interaction_id": p["interaction_id"]}, f"#{p['id']}")
            for p in pending
        ]
        left = self._cloud_cancel(rows, notes)
        self.store.set_cancel_unconfirmed(int(sid), left)
        return {"notes": notes, "cancel_unconfirmed": left}

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

        names = [str(n) for n in (body or {}).get("data_sources") or []]
        full = prompt
        if names:
            from deepresearch.sources.usage import ask_prompt, resolve

            try:
                srcs = resolve(self.sources, names)
                full = ask_prompt(prompt, srcs)
            except Exception as e:
                raise ApiError(400, str(e)) from e
        agent = DeepResearchAgent(
            config=self._config(), quiet=True, db_path=self.db_path
        )
        before = self._session(sid).get("result") or ""
        agent.follow_up(
            FollowUpRequest(
                interaction_id=iid,
                prompt=full,
                display_prompt=prompt,
                sources=names or None,
            )
        )
        for s in srcs if names else []:
            self.sources.record_use(s, "session", int(sid))
        after = self._session(sid).get("result") or ""
        if after == before:
            raise ApiError(502, "Follow-up returned no text (see server log)")
        return {"result": after, "appended": after[len(before) :]}

    def session_log(self, sid, query, body):
        path = self._log_dir() / f"session_{sid}.log"
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
        from deepresearch.sources.provenance import session_provenance

        prov = session_provenance(self.db_path, s)
        if fmt == "json":
            s.pop("embedding", None)
            s["annotations"] = anns
            s["provenance"] = prov
            if recursive:
                s["recursive_markdown"] = md
            return {"filename": f"session_{sid}.json", "content": s}
        if anns and (query.get("annotations") or ["1"])[0] == "1":
            md += "\n\n---\n\n## Annotations\n\n"
            for a in anns:
                md += f"> {a['quote']}\n\n"
                if a["note"]:
                    md += f"{a['note']}\n\n"
        md += (
            "\n\n---\n\n*Provenance: inputs fingerprint "
            f"`{prov['fingerprint']}`"
            + (
                "; data sources "
                + ", ".join(
                    f"{d['name']} ({d['uri']}, content {(d['manifest_hash'] or '')[:12]})"
                    for d in prov["sources"]
                )
                if prov["sources"]
                else ""
            )
            + ".*\n"
        )
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
            if not Path(p).resolve().is_relative_to(self._upload_dir().resolve()):
                raise ApiError(400, f"Upload path not allowed: {p}")
            if not Path(p).exists():
                raise ApiError(400, f"Upload missing: {p}")
        stores = [str(s).strip() for s in body.get("stores") or [] if str(s).strip()]
        ds = [str(n) for n in body.get("data_sources") or []]
        for n in ds:
            if self.sources.get(n) is None:
                raise ApiError(400, f"data source '{n}' does not exist")
        fmt = (body.get("format") or "").strip()
        project_id = body.get("project_id")
        if project_id and not self.projects.get(int(project_id)):
            raise ApiError(400, f"Project {project_id} not found")
        if not os.getenv("GEMINI_API_KEY"):
            raise ApiError(400, "GEMINI_API_KEY is not set for the dashboard process")

        sid = self.sessions.create_session("pending_start", prompt, uploads or None)
        if project_id:
            self.projects.add_item(int(project_id), "session", sid, home=True)
        args = ["research", prompt, "--adopt-session", str(sid)]
        if uploads:
            args += ["--upload", *uploads]
        if stores:
            args += ["--stores", *stores]
        for n in ds:
            args += ["--source", n]
        if fmt:
            args += ["--format", fmt]
        args += ["--depth", str(depth), "--breadth", str(breadth)]
        if depth == 1:
            args.append("--stream")  # thought summaries land in the live log
        if self.current_workspace != "main":
            # the run writes to the workspace it started in, whatever is shown later
            args = ["--workspace", self.current_workspace, *args]
        pid = self.spawn(args, self._log_dir() / f"session_{sid}.log")
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

        dest_dir = self._upload_dir() / uuid4().hex[:12]
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
        self.projects.purge("notebook", [int(nid)])
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

    # ---- data sources ------------------------------------------------------

    def _source(self, sid):
        s = self.sources.get(int(sid))
        if not s:
            raise ApiError(404, f"Source {sid} not found")
        return s

    def _source_view(self, s, full: bool = False, entries: int = 20) -> dict:
        from deepresearch.sources.index import index_state

        d = s.public()
        d["index_state"] = index_state(s)
        if d.get("manifest"):
            d["manifest"]["entries"] = d["manifest"]["entries"][
                : 200 if full else entries
            ]
        return d

    def sources_list(self, query, body):
        from deepresearch.sources.adapters import local_roots

        return {
            "sources": [self._source_view(s, entries=0) for s in self.sources.list()],
            "local_roots": [str(r) for r in local_roots()],
        }

    def sources_add(self, query, body):
        from pydantic import ValidationError

        from deepresearch.cli.sources import guess_kind, local_uri
        from deepresearch.sources import DataSource
        from deepresearch.sources.service import check

        b = dict(body or {})
        uri = str(b.get("uri") or "").strip()
        if not uri:
            raise ApiError(400, "Location is required")
        b["uri"] = uri
        b["kind"] = b.get("kind") or guess_kind(uri, str(b.get("auth_ref") or ""))
        if b["kind"].startswith("local_"):
            try:
                b["uri"] = local_uri(uri)
            except ValueError as e:
                raise ApiError(400, str(e)) from e
        b.setdefault("protection_level", "P1" if b["kind"] == "web" else "P2")
        keep = set(DataSource.model_fields) - {
            "id",
            "status",
            "last_checked",
            "last_error",
            "manifest",
            "created_at",
            "updated_at",
        }
        try:
            s = DataSource(**{k: v for k, v in b.items() if k in keep})
            s = self.sources.add(s)
        except ValidationError as e:
            raise ApiError(400, _clean_msg(e)) from e
        except ValueError as e:
            raise ApiError(409, str(e)) from e
        if b.get("test", True):
            s = check(self.sources, s)
        return self._source_view(s)

    def sources_get(self, sid, query, body):
        s = self._source(sid)
        d = self._source_view(s, full=True)
        d["used_by"] = self.sources.uses(s)
        return d

    def sources_patch(self, sid, query, body):
        from pydantic import ValidationError

        s = self._source(sid)
        editable = (
            "title",
            "description",
            "tags",
            "options",
            "auth_ref",
            "protection_level",
            "staging",
        )
        data = s.model_dump()
        data.update({k: v for k, v in (body or {}).items() if k in editable})
        if isinstance(data.get("options"), dict):
            # the saved index is managed by index/unindex, never by an edit form
            fresh = self._source(sid).options
            for k in ("store", "store_hash"):
                if k in fresh:
                    data["options"][k] = fresh[k]
                else:
                    data["options"].pop(k, None)
        try:
            from deepresearch.sources import DataSource

            s = self.sources.update(DataSource(**data))
        except ValidationError as e:
            raise ApiError(400, _clean_msg(e)) from e
        return self._source_view(s)

    def _genai(self):
        from google import genai

        if not hasattr(self, "_genai_client"):
            self._genai_client = genai.Client(api_key=self._config().api_key)
        return self._genai_client

    def sources_delete(self, sid, query, body):
        s = self._source(sid)
        if s.options.get("store"):
            from deepresearch.sources.index import drop_index

            try:
                drop_index(self.sources, s, self._genai())
            except Exception:
                pass  # the source goes anyway; `cleanup --all` can remove the store
        self.sources.delete(s.id)
        self.projects.purge("source", [int(s.id)])
        return {"deleted": s.name}

    def sources_discover(self, query, body):
        from deepresearch.sources.discover import discover

        q = ((query.get("q") or [""])[0]).strip()
        if len(q) < 2:
            raise ApiError(400, "Type at least 2 characters")
        cats = [c for c in (query.get("catalog") or []) if c]
        return discover(q[:200], cats or None, 6)

    def sources_index(self, sid, query, body):
        from deepresearch.sources.index import build_index

        try:
            s = build_index(
                self.sources, self._source(sid), self._genai(), log=lambda m: None
            )
        except Exception as e:
            raise ApiError(502, f"Could not build the index: {e}") from e
        return self._source_view(s)

    def sources_index_drop(self, sid, query, body):
        from deepresearch.sources.index import drop_index

        try:
            s = drop_index(self.sources, self._source(sid), self._genai())
        except Exception as e:
            raise ApiError(502, f"Could not delete the index: {e}") from e
        return self._source_view(s)

    def sources_test(self, sid, query, body):
        from deepresearch.sources.service import check

        return self._source_view(check(self.sources, self._source(sid)))

    # ---- v0.38: file browser for adding sources (SPEC section 13.8) -------------

    def browse_places(self, query, body):
        from deepresearch.sources import browse

        return {"places": browse.places()}

    def browse_list(self, query, body):
        from deepresearch.sources import browse
        from deepresearch.sources.adapters import SourceError

        q = {k: (v or [""])[0] for k, v in query.items()}
        try:
            out = browse.list_place(
                q.get("place", ""), q.get("path", ""), q.get("q", "")
            )
        except SourceError as e:
            raise ApiError(502, str(e)) from e
        return {"place": q.get("place", ""), "path": q.get("path", ""), **out}

    def browse_preview(self, query, body):
        from deepresearch.sources import browse
        from deepresearch.sources.adapters import SourceError

        q = {k: (v or [""])[0] for k, v in query.items()}
        try:
            size = int(q.get("size") or 0)
        except ValueError:
            size = 0
        try:
            data = browse.preview(q.get("place", ""), q.get("path", ""), 64_000, size)
        except SourceError as e:
            raise ApiError(502, str(e)) from e
        text = data.decode("utf-8", "replace")
        binary = text.count("\ufffd") > len(text) // 20
        return {"binary": binary, "text": "" if binary else text, "bytes": len(data)}

    def browse_spec(self, query, body):
        """The source a set of picked items becomes (the dialog then POSTs it to
        /api/sources after the user names it)."""
        from deepresearch.sources import browse
        from deepresearch.sources.adapters import SourceError

        b = body or {}
        items = [i for i in b.get("items") or [] if isinstance(i, dict)]
        try:
            spec = browse.source_spec(str(b.get("place") or ""), items)
        except SourceError as e:
            raise ApiError(400, str(e)) from e
        base, n = spec["name"], 2
        while self.sources.get_by_name(spec["name"]):
            spec["name"] = f"{base[:37]}-{n}"
            n += 1
        return spec

    def sources_browse(self, sid, query, body):
        from deepresearch.sources.adapters import SourceError, adapter_for

        path = (query.get("path") or [""])[0]
        try:
            return {"path": path, "items": adapter_for(self._source(sid)).list(path)}
        except SourceError as e:
            raise ApiError(502, str(e)) from e

    def sources_preview(self, sid, query, body):
        from deepresearch.sources.adapters import SourceError, adapter_for

        path = (query.get("path") or [""])[0]
        try:
            data = adapter_for(self._source(sid)).preview(path, 64_000)
        except SourceError as e:
            raise ApiError(502, str(e)) from e
        text = data.decode("utf-8", "replace")
        binary = text.count("\ufffd") > len(text) // 20
        return {
            "path": path,
            "binary": binary,
            "text": "" if binary else text,
            "bytes": len(data),
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
        path = self._log_dir() / f"session_{sid}.log"
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

    def _doc(self, kind: str, ref: int, with_lab: bool = False) -> tuple[str, str]:
        if kind == "session":
            s = self._session(str(ref))
            text = s.get("result") or ""
            if with_lab:
                # Lab results attach to the report (never edit it); summaries and
                # audio include them so the listener hears what the checks showed
                from deepresearch.dashboard.projects import lab_findings

                labs = lab_findings(self.lab.runs_for(int(ref)))
                if labs:
                    text += "\n\n## Lab results\n\n" + labs
            return s["prompt"], text
        if kind == "notebook":
            nb = self.store.get_notebook(ref)
            if not nb:
                raise ApiError(404, "Notebook not found")
            return nb["title"], nb["content"]
        raise ApiError(400, "kind must be session or notebook")

    def brief(self, query, body):
        body = body or {}
        title, content = self._doc(
            body.get("kind", ""), int(body.get("id") or 0), with_lab=True
        )
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
        mode = body.get("mode") or "full"
        _, content = self._doc(
            body.get("kind", ""), int(body.get("id") or 0), with_lab=mode == "summary"
        )
        text = self.fx.speakable(content)
        out = self.fx.estimate_audio(text, mode)
        out["voices"] = VOICES
        return out

    def audio_start(self, query, body):
        body = body or {}
        kind = body.get("kind", "")
        ref = int(body.get("id") or 0)
        mode = body.get("mode") or "full"
        title, content = self._doc(kind, ref, with_lab=mode == "summary")
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

    # ---- lab runs: real computations on an HPC cluster ------------------------

    def _lab_run(self, rid) -> dict:
        run = self.lab.get(int(rid))
        if not run:
            raise ApiError(404, "Lab run not found")
        return run

    def _lab_view(self, run: dict) -> dict:
        tgt = self.lab.target(run.get("target"))
        run["target_label"] = tgt.label if tgt else None
        from deepresearch.sources.provenance import lab_provenance

        run["provenance"] = lab_provenance(self.db_path, run)
        plan = run.get("plan") or {}
        if isinstance(plan, dict) and plan.get("review"):
            run["review_stale"] = self.lab.review_stale(plan)
        if isinstance(plan, dict) and run.get("status") == "draft":
            from deepresearch.dashboard.labfetch import blocked_urls

            run["blocked_urls"] = blocked_urls(plan)
        return run

    def lab_targets(self, query, body):
        return {
            "targets": [
                {
                    "name": t.name,
                    "label": t.label,
                    "partitions": t.partitions,
                    "default_partition": t.default_partition,
                }
                for t in self.lab.targets.values()
            ]
        }

    def lab_catalog(self, query, body):
        name = (query.get("target") or [None])[0]
        return self.lab.catalog(name, refresh=False)

    def lab_catalog_refresh(self, query, body):
        name = (body or {}).get("target")
        st = self.lab.catalog(name, refresh=True)
        if st.get("error") and not st.get("available"):
            raise ApiError(502, st["error"])
        return st

    def lab_warm(self, query, body):
        try:
            return self.lab.warm_status((query.get("target") or [None])[0])
        except TargetError as e:
            raise ApiError(502, str(e)) from e

    def lab_warm_start(self, query, body):
        try:
            return self.lab.warm_start((body or {}).get("target"))
        except ValueError as e:
            raise ApiError(400, str(e)) from e
        except TargetError as e:
            raise ApiError(502, str(e)) from e

    def lab_warm_stop(self, query, body):
        try:
            return self.lab.warm_stop((body or {}).get("target"))
        except ValueError as e:
            raise ApiError(400, str(e)) from e
        except TargetError as e:
            raise ApiError(502, str(e)) from e

    def lab_pitfalls(self, query, body):
        from deepresearch.dashboard import labguard

        return {
            "pitfalls": labguard.all_pitfalls(self.lab.state_dir),
            "rules": labguard.GENERAL_RULES,
        }

    def lab_pitfall_add(self, query, body):
        from deepresearch.dashboard import labguard

        body = body or {}
        match = body.get("match") or []
        if isinstance(match, str):
            match = [m.strip() for m in match.split(",")]
        try:
            return labguard.add_learned(
                self.lab.state_dir,
                str(body.get("text") or ""),
                [str(m) for m in match],
                str(body.get("source") or "added by hand"),
            )
        except ValueError as e:
            raise ApiError(400, str(e)) from e

    def lab_pitfall_rm(self, pid, query, body):
        from deepresearch.dashboard import labguard

        if not labguard.remove_learned(self.lab.state_dir, pid):
            raise ApiError(
                404, "No learned pitfall with that id (curated ones are in code)"
            )
        return {"ok": True}

    def lab_all_runs(self, query, body):
        self.lab.ensure_watcher()
        out = []
        for r in self.lab.all_runs():
            v = self._lab_view(r)
            plan = v.get("plan") or {}
            out.append(
                {
                    **{
                        k: v.get(k)
                        for k in (
                            "id",
                            "session_id",
                            "status",
                            "stage",
                            "error",
                            "scope",
                            "job_id",
                            "estimate_usd",
                            "ai_cost_usd",
                            "updated_at",
                            "created_at",
                            "target_label",
                            "data_sources",
                        )
                    },  # fmt: skip
                    # summary only: selections, write-ups and plans are fetched per run
                    "error": (v.get("error") or "")[:300] or None,
                    "plan": {
                        "title": plan.get("title"),
                        "resources": {
                            "partition": (plan.get("resources") or {}).get("partition")
                        },
                    },
                    "session_title": (r.get("session_prompt") or "")[:160],
                    # outcome only (no check details) for the list's badge
                    "assessment": {
                        "outcome": (v.get("assessment") or {}).get("outcome")
                    }
                    if v.get("assessment")
                    else None,
                }
            )
        return {"runs": out}

    def lab_list(self, sid, query, body):
        self._session(sid)
        self.lab.ensure_watcher()
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            sug = conn.execute(
                "SELECT data, cost_usd, created_at FROM lab_suggestions WHERE session_id = ?",
                (int(sid),),
            ).fetchone()
        return {
            "runs": [self._lab_view(r) for r in self.lab.runs_for(int(sid))],
            "suggestions": (
                {**json.loads(sug[0]), "cost_usd": sug[1], "created_at": sug[2]}
                if sug
                else None
            ),
            "configured": bool(self.lab.targets),
        }

    def lab_suggestions(self, sid, query, body):
        s = self._session(sid)
        if not (s.get("result") or "").strip():
            raise ApiError(400, "This session has no report yet")
        try:
            return self.lab.suggestions(
                int(sid),
                s["prompt"],
                s["result"],
                refresh=bool((body or {}).get("refresh")),
            )
        except Exception as e:
            raise ApiError(502, f"Suggestions failed: {e}") from e

    def lab_create(self, sid, query, body):
        s = self._session(sid)
        body = body or {}
        if not self.lab.targets:
            raise ApiError(400, "No compute target configured (lab_targets.json)")
        scope = body.get("scope") or "document"
        if scope not in ("selection", "document", "suggestion"):
            raise ApiError(400, "scope must be selection, document or suggestion")
        text = (body.get("selection") or "").strip() if scope == "selection" else ""
        if scope == "selection" and not text:
            raise ApiError(400, "Nothing selected")
        if scope != "selection":
            text = s.get("result") or ""
            if not text.strip():
                raise ApiError(400, "This session has no report yet")
        request = (body.get("request") or "").strip()[:4000]
        ds = [str(x) for x in body.get("data_sources") or []]
        target = body.get("target")
        home = self.projects.home_project(int(sid))
        if home:
            # project defaults: its cluster, and its partition as the planner's preference
            target = target or home.get("lab_target") or None
            part = home.get("lab_partition")
            if part and "partition" not in request.lower():
                request = (
                    request
                    + ("\n" if request else "")
                    + f"Project default partition: {part} (use it unless the job needs "
                    "hardware it lacks)."
                )[:4000]
        try:
            run = self.lab.create(
                int(sid),
                scope,
                text[:120000],
                request,
                target,
                data_sources=ds or None,
            )
        except ValueError as e:
            raise ApiError(400, str(e)) from e
        threading.Thread(
            target=self.lab.make_plan, args=(run["id"], s["prompt"]), daemon=True
        ).start()
        return self._lab_view(run)

    def lab_get(self, rid, query, body):
        return self._lab_view(self._lab_run(rid))

    def lab_edit(self, rid, query, body):
        self._lab_run(rid)
        plan = (body or {}).get("plan")
        if not isinstance(plan, dict):
            raise ApiError(400, "plan must be an object")
        try:
            return self._lab_view(self.lab.edit_plan(int(rid), plan))
        except ValueError as e:
            raise ApiError(409, str(e)) from e

    def lab_submit(self, rid, query, body):
        self._lab_run(rid)
        try:
            return self._lab_view(self.lab.submit(int(rid)))
        except ValueError as e:
            raise ApiError(409, str(e)) from e
        except TargetError as e:
            raise ApiError(502, str(e)) from e

    def lab_cancel(self, rid, query, body):
        self._lab_run(rid)
        try:
            return self._lab_view(self.lab.cancel(int(rid)))
        except ValueError as e:
            raise ApiError(409, str(e)) from e
        except TargetError as e:
            raise ApiError(502, str(e)) from e

    def lab_rerun(self, rid, query, body):
        run = self._lab_run(rid)
        plan = (body or {}).get("plan") or run.get("plan")
        if not isinstance(plan, dict):
            raise ApiError(409, "The original run has no plan to copy")
        new = self.lab.create(
            run["session_id"],
            run["scope"],
            run.get("selection") or "",
            run.get("request") or "",
            run.get("target"),
            rerun_of=run["id"],
            plan=plan,
        )
        return self._lab_view(new)

    def lab_review(self, rid, query, body):
        self._lab_run(rid)
        try:
            return self._lab_view(self.lab.review(int(rid)))
        except ValueError as e:
            raise ApiError(409, str(e)) from e

    def lab_fix(self, rid, query, body):
        self._lab_run(rid)
        try:
            run = self.lab.fix_plan(int(rid))
        except ValueError as e:
            raise ApiError(409, str(e)) from e
        view = self._lab_view(run)
        view["fix"] = run.get("fix")
        return view

    def lab_fix_failed(self, rid, query, body):
        self._lab_run(rid)
        try:
            run = self.lab.fix_failed(int(rid))
        except ValueError as e:
            raise ApiError(409, str(e)) from e
        view = self._lab_view(run)
        view["fix"] = run.get("fix")
        return view

    def lab_undo_fix(self, rid, query, body):
        self._lab_run(rid)
        try:
            return self._lab_view(self.lab.undo_fix(int(rid)))
        except ValueError as e:
            raise ApiError(409, str(e)) from e

    def lab_laptop_fetch(self, rid, query, body):
        self._lab_run(rid)
        urls = (body or {}).get("urls")
        if urls is not None and not (
            isinstance(urls, list) and all(isinstance(u, str) for u in urls)
        ):
            raise ApiError(400, "urls must be a list of strings")
        try:
            run = self.lab.laptop_fetch(int(rid), urls)
        except ValueError as e:
            raise ApiError(409, str(e)) from e
        view = self._lab_view(run)
        view["fix"] = run.get("fix")
        view["laptop_fetch"] = run.get("laptop_fetch")
        return view

    def lab_undo_refine(self, rid, query, body):
        self._lab_run(rid)
        try:
            return self._lab_view(self.lab.undo_refine(int(rid)))
        except ValueError as e:
            raise ApiError(409, str(e)) from e

    def lab_replan(self, rid, query, body):
        self._lab_run(rid)
        try:
            run = self.lab.replan(int(rid))
        except ValueError as e:
            raise ApiError(409, str(e)) from e
        s = self._session(run["session_id"])
        threading.Thread(
            target=self.lab.make_plan, args=(run["id"], s["prompt"]), daemon=True
        ).start()
        return self._lab_view(run)

    def lab_log(self, rid, query, body):
        self._lab_run(rid)
        offset = int((query.get("offset") or ["0"])[0])
        try:
            return self.lab.log(int(rid), offset)
        except TargetError as e:
            raise ApiError(502, str(e)) from e

    def lab_file(self, rid, query, body):
        self._lab_run(rid)
        rel = (query.get("path") or [""])[0]
        try:
            p = self.lab.file_path(int(rid), rel)
        except FileNotFoundError as e:
            raise ApiError(404, "File not found") from e
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or p.suffix.lower() in (
            ".csv",
            ".tsv",
            ".log",
            ".out",
            ".dat",
            ".xvg",
            ".json",
            ".md",
            ".sbatch",
            ".sh",
        ):
            ctype = "text/plain; charset=utf-8"
        # Files written by a Lab job are untrusted. Only plain images and text are
        # shown inline; SVG and HTML can carry scripts, so they (and anything else)
        # download instead of rendering on the dashboard's origin.
        inline = ctype.startswith("text/plain") or ctype in SAFE_INLINE
        return RawResponse(p.read_bytes(), ctype, p.name, inline=inline, sandbox=True)

    def lab_delete(self, rid, query, body):
        run = self._lab_run(rid)
        if run["status"] in (
            "submitting",
            "smoke",
            "queued",
            "running",
            "fetching",
            "analyzing",
        ):
            raise ApiError(409, "Cancel the run before deleting it")
        import shutil

        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute("DELETE FROM lab_runs WHERE id = ?", (int(rid),))
        shutil.rmtree(self.lab.results_dir / f"run_{int(rid)}", ignore_errors=True)
        return {"deleted": int(rid)}


def _loopback_peer(addr: str) -> bool:
    import ipaddress

    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:  # ::ffff:127.0.0.1
        ip = ip.ipv4_mapped
    return ip.is_loopback


def make_handler(api: Api, local_only: bool = False):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"deep-research-dashboard/{__version__}"

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # the browser went away; nothing to do

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
            if local_only and not _loopback_peer(self.client_address[0]):
                # Belt and braces for the loopback-only default: even if the socket
                # is reachable (a proxy, a mis-set --host), refuse other machines.
                return self._json(
                    403, {"error": "This dashboard only accepts this machine."}
                )
            if not host_allowed(self.headers.get("Host", "")):
                # DNS rebinding: a public site pointing its name at this machine.
                return self._json(
                    403,
                    {
                        "error": "Unrecognised host name. Open the dashboard by IP, "
                        "machine name or tailnet name, or add this name to "
                        "DR_ALLOWED_HOSTS."
                    },
                )
            if not url.path.startswith("/api/"):
                if method != "GET":
                    return self._json(405, {"error": "Method not allowed"})
                return self._static(url.path)
            body = None
            length = int(self.headers.get("Content-Length") or 0)
            if method == "POST" and url.path == "/api/workspaces/import":
                return self._import_upload(length, parse_qs(url.query))
            if length > MAX_BODY:
                return self._json(413, {"error": "Request too large"})
            if method != "GET":
                origin = self.headers.get("Origin")
                if origin and urlparse(origin).netloc.lower() != (
                    self.headers.get("Host", "").lower()
                ):
                    return self._json(403, {"error": "Cross-origin request refused"})
                if not self.headers.get("Content-Type", "").startswith(
                    "application/json"
                ):
                    # Every write must be JSON, with or without a body: browsers
                    # cannot send that cross-site without a CORS preflight, which
                    # this server never approves.
                    return self._json(415, {"error": "Use application/json"})
            if length:
                try:
                    body = json.loads(self.rfile.read(length) or b"null")
                except ValueError:
                    return self._json(400, {"error": "Invalid JSON"})
            try:
                q = parse_qs(url.query)
                # the switcher sends the header on fetch(); links (<img>, <audio>,
                # downloads) carry ?ws= because they cannot set headers
                ws = self.headers.get("X-DR-Workspace") or (q.get("ws") or [""])[0]
                status, obj = api.dispatch(method, url.path, q, body, workspace=ws)
                if isinstance(obj, RawResponse):
                    return self._raw(obj)
                self._json(status, obj)
            except ApiError as e:
                self._json(e.status, {"error": e.message})
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def _import_upload(self, length: int, q: dict) -> None:
            """A workspace zip, streamed to a temp file (larger than MAX_BODY allows).
            Same-origin rule as other writes; the zip is checked by wszip before use."""
            import tempfile

            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc.lower() != (
                self.headers.get("Host", "").lower()
            ):
                return self._json(403, {"error": "Cross-origin request refused"})
            if self.headers.get("Content-Type", "") != "application/zip":
                return self._json(415, {"error": "Send the file as application/zip"})
            if not 0 < length <= MAX_IMPORT:
                return self._json(413, {"error": "Zip is empty or too large"})
            with tempfile.NamedTemporaryFile(prefix="drws-up-", suffix=".zip") as f:
                left = length
                while left:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        return self._json(400, {"error": "Upload ended early"})
                    f.write(chunk)
                    left -= len(chunk)
                f.flush()
                try:
                    obj = api.ws_import_file(Path(f.name), (q.get("name") or [""])[0])
                except ApiError as e:
                    return self._json(e.status, {"error": e.message})
                except Exception as e:
                    traceback.print_exc()
                    return self._json(500, {"error": f"{type(e).__name__}: {e}"})
            self._json(200, obj)

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
                disp = (
                    "attachment"
                    if "download=1" in self.path or not obj.inline
                    else "inline"
                )
                safe_name = re.sub(r"[^\w.\- ]", "_", obj.filename)
                self.send_header(
                    "Content-Disposition", f'{disp}; filename="{safe_name}"'
                )
            if obj.sandbox:
                self.send_header(
                    "Content-Security-Policy", "sandbox; default-src 'none'"
                )
            self.send_header("X-Content-Type-Options", "nosniff")
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


def serve(
    host: str, port: int, db_path: str = user_db_path, local_only: bool = True
) -> None:
    # Also covers `dashboard --foreground` run from a folder with an old .env:
    # the user settings file wins for everything this process and its runs do.
    from deepresearch.core.config import service_env

    os.environ.update(service_env())
    api = Api(db_path, workspaces=True)
    api.start_watchers()  # pick up lab runs still active from before a restart
    httpd = ThreadingHTTPServer((host, port), make_handler(api, local_only=local_only))
    httpd.daemon_threads = True
    print(f"[INFO] Deep Research dashboard {__version__} on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

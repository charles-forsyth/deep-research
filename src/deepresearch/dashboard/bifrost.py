"""The hosted ursa-bifrost MCP server as a cluster backend (v0.53.0, R1).

bifrost (UCR-Research-Computing/ursa-bifrost) answers typed questions about Ursa Major
over MCP with Google sign-in: the catalog, partition state, static script checks,
deterministic job diagnosis, files under home/scratch. deep-research signs in as its own
program client, `bifrost-deep-research` (users.yaml on the server: tiers R1+A1, its own
call budget and its own day cap and ledger), never with Hermes' or anyone else's tokens.

R1 (v0.53.0) used it for reads; R2 (v0.55.0) also runs the Lab's Slurm jobs through it
(`BifrostJobs` below): submit with self-confirmation, one batched watcher call per round,
line-paged logs, fetch through job_results/results_link, cancel, and data sources relayed
through bifrost's staging uploads. Pilots still use the SSH warm worker until R3.

| Lab need | bifrost |
|---|---|
| catalog | resource `hpc://catalog` |
| stocked-out partitions | `cluster_status` (problems with stockout reasons) |
| pre-flight | `script_check` on the generated run.sbatch |
| failure diagnosis | `job_explain` findings, stored next to the old class |
| install-ladder notes | `files_read ~/deep-research-lab/envs/ladder.jsonl` |
| efficiency of finished jobs | `job_show` |

Transport: MCP Streamable HTTP, one JSON-RPC POST per call. The server is stateless
(`Stateless: true, JSONResponse: true`), so no session or SSE stream is needed and the
whole client is the standard library. Every result arrives as JSON with `data`, `source`,
`as_of`; text written by users or jobs comes inside `untrusted` fields and is only ever
shown or passed on as data.

Tokens live in `bifrost-token.json` in the state dir (mode 600). Access tokens last an
hour; refresh tokens rotate on every use, so a refresh runs under a lock and the new
pair is written before it is used (two threads refreshing at once would burn the token
and sign the dashboard out).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

DEFAULT_URL = "https://bifrost-mcp-125853442225.us-central1.run.app"
CLIENT_ID = "bifrost-deep-research"  # pre-registered in bifrost's users.yaml
REDIRECT = "http://127.0.0.1:{port}/callback"
TOKEN_FILE = "bifrost-token.json"
LOGIN_PORT = 47615


class BifrostError(RuntimeError):
    """A bifrost call failed (network, HTTP status or a tool error)."""


class NotSignedIn(BifrostError):
    """No usable token: run `deep-research cluster login`."""


def _now() -> float:
    return time.time()


class BifrostClient:
    """One signed-in program client. Thread-safe; cheap to share."""

    def __init__(
        self,
        state_dir: Path,
        url: str = DEFAULT_URL,
        client_id: str = CLIENT_ID,
        opener: Callable | None = None,
        timeout: float = 120.0,
        token_file: str = TOKEN_FILE,
        label: str = "bifrost",
    ):
        # v0.62.0: the same client signs in to Nexus (its own id, token file, label)
        self.token_file = token_file
        self.label = label
        self.signin_hint = (
            "not signed in to Nexus: run `deep-research nexus login`"
            if label == "nexus"
            else "not signed in to the cluster: run `deep-research cluster login`"
        )
        self.state_dir = Path(state_dir)
        self.url = url.rstrip("/")
        self.client_id = client_id
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._lock = threading.Lock()  # token file and refresh
        self._id = 0

    # ---- tokens --------------------------------------------------------------
    @property
    def token_path(self) -> Path:
        return self.state_dir / self.token_file

    def _load(self) -> dict | None:
        try:
            return json.loads(self.token_path.read_text())
        except (OSError, ValueError):
            return None

    def _save(self, tok: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.label}-token.", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(tok, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.token_path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def signed_in(self) -> bool:
        tok = self._load()
        return bool(tok and tok.get("refresh_token"))

    def _http(self, method: str, url: str, data: bytes | None = None,
              headers: dict | None = None) -> tuple[int, dict, bytes]:  # fmt: skip
        req = urllib.request.Request(
            url, data=data, method=method, headers=headers or {}
        )
        try:
            with self._open(req, timeout=self.timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read() or b""
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise BifrostError(f"cannot reach {self.label}: {e}") from e

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            tok = self._load()
            if not tok or not tok.get("refresh_token"):
                raise NotSignedIn(self.signin_hint)
            if not force_refresh and tok.get("_exp", 0) - _now() > 60:
                return tok["access_token"]
            status, _, body = self._http(
                "POST",
                self.url + "/token",
                urllib.parse.urlencode(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": tok["refresh_token"],
                        "client_id": tok.get("client_id") or self.client_id,
                    }
                ).encode(),
                {"Content-Type": "application/x-www-form-urlencoded"},
            )
            if status != 200:
                raise NotSignedIn(
                    "cluster sign-in expired: run `deep-research cluster login` "
                    f"(token refresh answered HTTP {status})"
                )
            new = json.loads(body)
            tok.update(new)  # refresh tokens rotate: keep the newest
            tok["_exp"] = _now() + int(new.get("expires_in", 3600))
            self._save(tok)
            return tok["access_token"]

    # ---- MCP ------------------------------------------------------------------
    def _rpc(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            self._id += 1
            rid = self._id
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        ).encode()
        status, headers, body = 0, dict[str, str](), b""
        for attempt in (0, 1):
            status, headers, body = self._http(
                "POST",
                self.url + "/mcp",
                payload,
                {
                    "Authorization": "Bearer "
                    + self.access_token(force_refresh=attempt > 0),
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            if status == 401 and attempt == 0:
                continue  # access token revoked or expired early: refresh once
            break
        if status == 401:
            raise NotSignedIn(
                "cluster sign-in was refused: run `deep-research cluster login`"
            )
        if status == 429:
            raise BifrostError(
                f"{self.label} rate limit reached; try again in a minute"
            )
        if status != 200:
            raise BifrostError(f"{self.label} {method}: HTTP {status} {body[:200]!r}")
        text = body.decode("utf-8", "replace")
        ctype = {k.lower(): v for k, v in headers.items()}.get("content-type", "")
        if "text/event-stream" in ctype:
            data = [
                ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")
            ]
            text = data[-1] if data else ""
        try:
            msg = json.loads(text)
        except ValueError as e:
            raise BifrostError(f"{self.label} {method}: unreadable answer") from e
        if "error" in msg:
            raise BifrostError(
                f"{self.label} {method}: {msg['error'].get('message', msg['error'])}"
            )
        return msg.get("result") or {}

    def call(self, tool: str, args: dict | None = None) -> Any:
        """One tool call; returns the envelope's `data`. Tool errors raise."""
        res = self._rpc("tools/call", {"name": tool, "arguments": args or {}})
        text = "".join(
            p.get("text", "")
            for p in res.get("content") or []
            if p.get("type") == "text"
        )
        if res.get("isError"):
            raise BifrostError(f"{tool}: {text.strip()[:500]}")
        try:
            env = json.loads(text) if text else {}
        except ValueError:
            return text
        if isinstance(env, dict) and "data" in env:
            return env["data"]
        return env

    def resource(self, uri: str) -> str:
        res = self._rpc("resources/read", {"uri": uri})
        parts = res.get("contents") or []
        return parts[0].get("text", "") if parts else ""

    def whoami(self) -> dict:
        status, _, body = self._http(
            "GET",
            self.url + "/whoami",
            headers={"Authorization": "Bearer " + self.access_token()},
        )
        if status == 401:
            status, _, body = self._http(
                "GET",
                self.url + "/whoami",
                headers={
                    "Authorization": "Bearer " + self.access_token(force_refresh=True)
                },
            )
        if status != 200:
            raise NotSignedIn(f"whoami answered HTTP {status}")
        return json.loads(body)

    # ---- sign-in ---------------------------------------------------------------
    def login(self, open_browser: Callable[[str], Any] | None = None,
              port: int = LOGIN_PORT, wait_s: float = 300.0) -> dict:  # fmt: skip
        """Browser sign-in (OAuth code + PKCE) as the pre-registered program client."""
        meta_status, _, meta_body = self._http(
            "GET", self.url + "/.well-known/oauth-authorization-server"
        )
        if meta_status != 200:
            raise BifrostError(f"{self.label} metadata: HTTP {meta_status}")
        meta = json.loads(meta_body)
        redirect = REDIRECT.format(port=port)
        verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        state = secrets.token_urlsafe(16)
        url = (
            meta["authorization_endpoint"]
            + "?"
            + urllib.parse.urlencode(
                {
                    "response_type": "code",
                    "client_id": self.client_id,
                    "redirect_uri": redirect,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": state,
                    "resource": self.url + "/mcp",
                }
            )
        )
        got: dict = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                got.update({k: v[0] for k, v in q.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"deep-research is signed in. You can close this tab.")

            def log_message(self, format, *args):  # noqa: A002
                pass

        srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
        srv.timeout = 1
        try:
            if open_browser:
                open_browser(url)
            deadline = _now() + wait_s
            while not got and _now() < deadline:
                srv.handle_request()
        finally:
            srv.server_close()
        if got.get("state") != state or "code" not in got:
            raise BifrostError(
                f"sign-in failed: {got.get('error') or 'no code returned'}"
            )
        status, _, body = self._http(
            "POST",
            meta["token_endpoint"],
            urllib.parse.urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": got["code"],
                    "redirect_uri": redirect,
                    "client_id": self.client_id,
                    "code_verifier": verifier,
                }
            ).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status != 200:
            raise BifrostError(f"token exchange failed: HTTP {status} {body[:200]!r}")
        tok = json.loads(body)
        tok["client_id"] = self.client_id
        tok["_exp"] = _now() + int(tok.get("expires_in", 3600))
        with self._lock:
            self._save(tok)
        return self.whoami()

    def logout(self) -> bool:
        tok = self._load()
        if not tok:
            return False
        for t in (tok.get("access_token"), tok.get("refresh_token")):
            if t:
                try:
                    self._http(
                        "POST",
                        self.url + "/revoke",
                        urllib.parse.urlencode({"token": t}).encode(),
                        {"Content-Type": "application/x-www-form-urlencoded"},
                    )
                except BifrostError:
                    pass  # revoke is best effort; the local file goes regardless
        with self._lock:
            try:
                self.token_path.unlink()
            except FileNotFoundError:
                pass
        return True


# ---- what the Lab asks (thin, typed wrappers) ----------------------------------------

STOCKOUT_WORDS = (
    "RESOURCE_POOL_EXHAUSTED",
    "stockout",
    "ZONE_RESOURCE",
    "insufficient capacity",
)

# bifrost job_explain rule id -> the Lab's failure class (SPEC 20.12). The Lab keeps its
# own classifier for one release and stores both, so the two can be compared.
RULE_CLASS = {
    "container": "container",
    "timeout": "timeout",
    "oom": "oom",
    "oom-log": "oom",
    "memory-near-limit": "oom",
    "install-ladder": "install",
    "module-missing": "install",
    "missing-feature": "missing-feature",
    "glibc": "missing-feature",
    "script-error": "script",
}


def stockouts(client: BifrostClient) -> set[str]:
    """Partitions with nodes that failed to boot for lack of GCP capacity."""
    data = client.call("cluster_status") or {}
    out = set()
    for p in data.get("partitions") or []:
        probs = " ".join(p.get("problems") or [])
        if any(w.lower() in probs.lower() for w in STOCKOUT_WORDS):
            out.add(p.get("name"))
    return {p for p in out if p}


def recent_node_failures(client: BifrostClient, hours: int = 3) -> set[str]:
    """Partitions where one of our jobs lost its node in the last `hours` (requeued after
    a failed boot, or NODE_FAIL). GCP stockouts come and go; a partition that just failed
    to start a node is likely to fail the next one too."""
    rows = (
        client.call("jobs_list", {"since": f"now-{int(hours)}hours", "limit": 200})
        or []
    )
    out = set()
    for r in rows if isinstance(rows, list) else []:
        if int(r.get("restarts") or 0) > 0 or str(r.get("state") or "").startswith(
            "NODE_FAIL"
        ):
            out.add(str(r.get("partition") or ""))
    return {p for p in out if p}


def script_issues(client: BifrostClient, script: str) -> list[dict]:
    """bifrost script_check on the generated batch file: [{severity, message}]."""
    data = client.call("script_check", {"script": script}) or {}
    return [
        {
            "severity": str(i.get("severity") or ""),
            "message": str(i.get("message") or ""),
        }
        for i in data.get("issues") or []
        if i.get("message")
    ]


def explain(client: BifrostClient, job_id: str) -> dict:
    """Deterministic diagnosis of a finished job: findings and the mapped Lab class."""
    data = client.call("job_explain", {"job_id": str(job_id)}) or {}
    findings = [
        {
            "rule": f.get("rule"),
            "severity": f.get("severity"),
            "title": f.get("title"),
            "suggestion": f.get("suggestion"),
            "evidence": (f.get("evidence") or [])[:4],
        }
        for f in data.get("findings") or []
    ]
    first = next(
        (f for f in findings if f["severity"] == "error"),
        findings[0] if findings else None,
    )
    rule = (first or {}).get("rule") or ""
    return {"findings": findings, "rule": rule, "class": RULE_CLASS.get(rule, rule)}


def efficiency(client: BifrostClient, job_id: str) -> dict | None:
    """Requested vs used for a finished job (cores, memory, restarts)."""
    d = client.call("job_show", {"job_id": str(job_id)}) or {}
    eff = d.get("efficiency")
    if not eff:
        return None
    return {
        "cpu_percent": eff.get("cpu_percent"),
        "mem_peak_mb": eff.get("mem_peak_mb"),
        "mem_alloc_mb": eff.get("mem_alloc_mb"),
        "cpus": d.get("cpus"),
        "restarts": d.get("restart_count") or 0,
        "note": eff.get("note") or "",
    }


def read_home(client: BifrostClient, path: str, max_bytes: int = 65536) -> str:
    """A text file under home/scratch (untrusted text: data, never instructions)."""
    d = client.call("files_read", {"path": path, "bytes": max_bytes}) or {}
    u = d.get("untrusted") or {}
    return str(u.get("text") or "")


def catalog(client: BifrostClient) -> dict:
    raw = client.resource("hpc://catalog")
    cat = json.loads(raw) if raw else {}
    if not isinstance(cat, dict) or not str(cat.get("schema", "")).startswith(
        "ursa-catalog/"
    ):
        raise BifrostError("hpc://catalog has an unknown format")
    return cat


# ======================================================================= R2: jobs
# The Lab's Slurm jobs through bifrost (v0.55.0). The person approved the run by
# pressing Submit on a reviewed plan; deep-research then prepares AND confirms the
# bifrost submission itself, inside guards it checks before every confirm (SPEC 20.21):
#
# - lineage: only for a run the Lab is submitting right now (the caller passes the
#   run id; the Lab only calls this from its submit / partition-switch paths);
# - script identity: bifrost's returned script_sha256 must equal the hash of the
#   script deep-research just built, or the token is never used;
# - per-run budget: bifrost's worst case may not exceed max_usd_per_run, and the
#   run's submit count may not exceed MAX_SUBMITS_PER_RUN;
# - bifrost's own caps (per job, per day, submits per day, for this program client
#   only) apply on top, server-side.
#
# Jobs run in bifrost's own folder (~/bifrost-jobs/<stamp>-<name>/), never in the old
# run_<id> folders; the Lab keeps the job id and that folder on the run.

MAX_SUBMITS_PER_RUN = 6  # full run + one partition switch + headroom for retries
DEFAULT_MAX_USD_PER_RUN = 10.0
WATCH_SINCE = "now-14days"  # jobs_list must reach back to when the job ran
RELAY_FILENAME = "{name}-{hash}.tar.gz"
_LINK_BATCH = 20  # results_link takes up to 20 files per call
_READ_MAX = 65536  # job_results read: at most 64 KB per chunk


def script_sha(script: str) -> str:
    """bifrost's script_sha256 is the first 16 hex chars of sha256(script, NUL)."""
    return hashlib.sha256(script.encode() + b"\0").hexdigest()[:16]


class SubmitRefused(BifrostError):
    """bifrost (or a Lab guard) refused the job; nothing was submitted."""


class BifrostJobs:
    """Slurm job operations for the Lab over a signed-in BifrostClient."""

    def __init__(self, client: BifrostClient, max_usd_per_run: float = DEFAULT_MAX_USD_PER_RUN,
                 http_get: Callable | None = None, http_put: Callable | None = None):  # fmt: skip
        self.c = client
        self.max_usd_per_run = float(max_usd_per_run)
        self._get = http_get or _https_get
        self._put = http_put or _https_put

    # ---- submit ------------------------------------------------------------------
    def submit(self, script: str, job_name: str, inputs: list[str] | None = None,
               submits_so_far: int = 0) -> dict:  # fmt: skip
        """Prepare, check the guards, confirm. Returns {job_id, folder, worst_usd,
        plan_hash, warnings}. Raises SubmitRefused when nothing was submitted."""
        if submits_so_far >= MAX_SUBMITS_PER_RUN:
            raise SubmitRefused(
                f"this run already submitted {submits_so_far} jobs (limit "
                f"{MAX_SUBMITS_PER_RUN}); press Submit again to start over"
            )
        args: dict = {"script": script, "job_name": job_name}
        if inputs:
            args["inputs"] = list(inputs)
        try:
            plan = self.c.call("job_submit", args) or {}
        except NotSignedIn:
            raise
        except BifrostError as e:
            raise SubmitRefused(str(e).removeprefix("job_submit: ")) from e
        tok = str(plan.get("confirm_token") or "")
        if not tok:
            raise SubmitRefused("bifrost returned no confirm token")
        if str(plan.get("script_sha256") or "") != script_sha(script):
            # never confirm a plan for a script other than the one just built
            raise SubmitRefused(
                "bifrost's plan is for a different script than the Lab built; not submitted"
            )
        worst = float(plan.get("worst_case_usd") or 0)
        if worst > self.max_usd_per_run:
            raise SubmitRefused(
                f"worst case ${worst:.2f} is over the Lab's ${self.max_usd_per_run:.2f} "
                "per-run limit (lab_targets.json bifrost.max_usd_per_run); not submitted"
            )
        try:
            done = self.c.call("job_submit_confirm", {"confirm_token": tok}) or {}
        except NotSignedIn:
            raise
        except BifrostError as e:
            raise SubmitRefused(str(e).removeprefix("job_submit_confirm: ")) from e
        job = str(done.get("job_id") or "")
        if not job.isdigit():
            raise BifrostError(f"bifrost returned an unexpected job id {job!r}")
        return {
            "job_id": job,
            "folder": str(done.get("remote_dir") or plan.get("remote_dir") or ""),
            "worst_usd": worst,
            "plan_hash": str(plan.get("plan_hash") or ""),
            "partition": plan.get("partition"),
            "warnings": [str(w) for w in plan.get("warnings") or []][:10],
        }

    # ---- watch -------------------------------------------------------------------
    def states(self, job_ids: list[str]) -> dict[str, dict]:
        """One jobs_list call for many jobs: {job_id: row}. Missing ids are absent."""
        ids = sorted({str(j) for j in job_ids if str(j).isdigit()}, key=int)
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 100):
            batch = ids[i : i + 100]
            rows = (
                self.c.call(
                    "jobs_list", {"job_ids": batch, "since": WATCH_SINCE, "limit": 200}
                )
                or []
            )
            for r in rows if isinstance(rows, list) else []:
                jid = str(r.get("job_id") or "")
                if jid in batch:
                    out[jid] = r
        return out

    def read_text(self, job_id: str, rel: str, max_bytes: int = 2000) -> str:
        """The END of a small text file in the job folder ('' when missing)."""
        try:
            first = (
                self.c.call(
                    "job_results", {"job_id": str(job_id), "read": rel, "read_bytes": 1}
                )
                or {}
            )
        except BifrostError:
            return ""
        size = int(((first.get("chunk") or {}).get("file_bytes")) or 0)
        if size <= 0:
            return ""
        start = max(0, size - min(max_bytes, _READ_MAX))
        d = self.c.call(
            "job_results",
            {"job_id": str(job_id), "read": rel, "read_offset": start,
             "read_bytes": min(max_bytes, _READ_MAX)},
        ) or {}  # fmt: skip
        return str(((d.get("preview_untrusted") or {}).get("text")) or "")

    def stage(self, job_id: str) -> str:
        lines = self.read_text(job_id, "stage.txt", 2000).strip().splitlines()
        return lines[-1].strip() if lines else ""

    # ---- logs --------------------------------------------------------------------
    def log(self, job_id: str, start_line: int = 1, lines: int = 1000) -> dict:
        """Lines from start_line (1-based). {text, first_line, last_line, total_lines}."""
        d = self.c.call(
            "job_log_tail",
            {"job_id": str(job_id), "start_line": max(1, int(start_line)),
             "lines": max(1, min(int(lines), 1000))},
        ) or {}  # fmt: skip
        text = str(((d.get("untrusted") or {}).get("text")) or "")
        return {
            "text": text,
            "first_line": int(d.get("first_line") or 0),
            "last_line": int(d.get("last_line") or 0),
            "total_lines": int(d.get("total_lines") or 0),
        }

    def log_text(self, job_id: str, max_lines: int = 4000) -> str:
        """The last max_lines lines of the job log as one string (for the fixer)."""
        head = self.log(job_id, 1, 1)
        total = head["total_lines"]
        if total <= 0:
            return ""
        start = max(1, total - max_lines + 1)
        parts = []
        while start <= total:
            w = self.log(job_id, start, 1000)
            if not w["text"] or w["last_line"] < start:
                break
            parts.append(w["text"] if w["text"].endswith("\n") else w["text"] + "\n")
            start = w["last_line"] + 1
        return "".join(parts)

    # ---- cancel ------------------------------------------------------------------
    def cancel(self, job_id: str) -> None:
        """The person pressed Stop (or the Lab is moving the job): prepare + confirm."""
        plan = self.c.call("job_cancel", {"job_id": str(job_id)}) or {}
        tok = str(plan.get("confirm_token") or "")
        if not tok:
            raise BifrostError("bifrost returned no confirm token for the cancel")
        self.c.call("job_cancel_confirm", {"confirm_token": tok})

    # ---- fetch -------------------------------------------------------------------
    def list_files(self, job_id: str) -> list[dict]:
        files: list[dict] = []
        off = 0
        while True:
            d = (
                self.c.call(
                    "job_results", {"job_id": str(job_id), "offset": off, "limit": 1000}
                )
                or {}
            )
            files += [f for f in d.get("files") or [] if f.get("path")]
            off = int(d.get("next_offset") or 0)
            if not off or len(files) > 5000:
                return files

    def fetch(self, job_id: str, dest: Path, max_total: int, max_file: int,
              keep: Callable[[str], bool] | None = None) -> list[dict]:  # fmt: skip
        """Copy the job folder's files into dest (the same layout and limits as the
        SSH fetch). Small text files come through job_results read; everything else
        through signed results_link downloads. Returns [{path, size[, skipped]}]."""
        dest.mkdir(parents=True, exist_ok=True)
        root = dest.resolve()
        want, skipped, total = [], [], 0
        for f in self.list_files(job_id):
            path, size = str(f["path"]), int(f.get("bytes") or 0)
            if (
                path.startswith("/")
                or ".." in path.split("/")
                or (keep and not keep(path))
            ):
                continue
            if size > max_file or total + size > max_total:
                skipped.append({"path": path, "size": size, "skipped": True})
                continue
            want.append((path, size))
            total += size
        got: list[dict] = []
        links: list[str] = []
        for path, size in want:
            target = (dest / path).resolve()
            if not target.is_relative_to(root):
                continue
            if size <= _READ_MAX and _texty(path):
                try:
                    d = self.c.call(
                        "job_results",
                        {"job_id": str(job_id), "read": path, "read_bytes": _READ_MAX},
                    ) or {}  # fmt: skip
                except BifrostError as e:
                    if "binary" not in str(e):
                        raise
                    links.append(path)
                    continue
                text = str(((d.get("preview_untrusted") or {}).get("text")) or "")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
                got.append({"path": path, "size": size})
            else:
                links.append(path)
        for i in range(0, len(links), _LINK_BATCH):
            batch = links[i : i + _LINK_BATCH]
            d = (
                self.c.call("results_link", {"job_id": str(job_id), "files": batch})
                or {}
            )
            for ln in d.get("links") or []:
                path = str(ln.get("file") or "")
                target = (dest / path).resolve()
                if path not in batch or not target.is_relative_to(root):
                    continue
                data = self._get(str(ln.get("download_url") or ""), max_file)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                got.append({"path": path, "size": len(data)})
        return got + skipped

    # ---- data relay ----------------------------------------------------------------
    def staged(self) -> dict[str, dict]:
        """Uploads still in staging, by filename: {filename: {upload_id, bytes, ...}}."""
        try:
            rows = self.c.call("uploads_list") or []
        except BifrostError:
            return {}
        out: dict[str, dict] = {}
        items = rows.get("uploads") if isinstance(rows, dict) else rows
        for r in items or []:
            name = str(r.get("filename") or "")
            if name:
                out[name] = r
        return out

    def upload(self, filename: str, payload: bytes) -> str:
        """Stage one file (signed PUT); returns its upload id for job_submit inputs."""
        t = (
            self.c.call("upload_prepare", {"filename": filename, "bytes": len(payload)})
            or {}
        )
        url, uid = str(t.get("upload_url") or ""), str(t.get("upload_id") or "")
        if not url.startswith("https://") or not uid:
            raise BifrostError("bifrost returned no upload link")
        self._put(url, payload, dict(t.get("headers") or {}))
        return uid


_TEXT_EXT = (
    ".txt", ".log", ".json", ".csv", ".tsv", ".md", ".sbatch", ".sh", ".py", ".r",
    ".yaml", ".yml", ".out", ".err", ".dat", ".xyz", ".tex", ".html", ".xml", ".ini",
)  # fmt: skip


def _texty(path: str) -> bool:
    low = path.lower()
    return low.endswith(_TEXT_EXT) or "." not in low.rsplit("/", 1)[-1]


def _https_get(url: str, max_bytes: int) -> bytes:
    if not url.startswith("https://"):
        raise BifrostError("refusing a non-HTTPS download link")
    with urllib.request.urlopen(url, timeout=600) as r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise BifrostError("download larger than the per-file limit")
    return data


def _https_put(url: str, payload: bytes, headers: dict) -> None:
    if not url.startswith("https://"):
        raise BifrostError("refusing a non-HTTPS upload link")
    req = urllib.request.Request(url, data=payload, method="PUT", headers=headers)
    with urllib.request.urlopen(req, timeout=3600) as r:
        if r.status not in (200, 201):
            raise BifrostError(f"upload failed: HTTP {r.status}")

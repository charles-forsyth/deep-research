"""The hosted ursa-bifrost MCP server as a cluster backend (v0.53.0, R1).

bifrost (UCR-Research-Computing/ursa-bifrost) answers typed questions about Ursa Major
over MCP with Google sign-in: the catalog, partition state, static script checks,
deterministic job diagnosis, files under home/scratch. deep-research signs in as its own
program client, `bifrost-deep-research` (users.yaml on the server: tiers R1+A1, its own
call budget and its own day cap and ledger), never with Hermes' or anyone else's tokens.

R1 uses it for reads only; submit, watch and fetch still go over SSH (cluster.py):

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
    ):
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
        return self.state_dir / TOKEN_FILE

    def _load(self) -> dict | None:
        try:
            return json.loads(self.token_path.read_text())
        except (OSError, ValueError):
            return None

    def _save(self, tok: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".bifrost-token.", dir=self.state_dir)
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
            raise BifrostError(f"cannot reach bifrost: {e}") from e

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            tok = self._load()
            if not tok or not tok.get("refresh_token"):
                raise NotSignedIn(
                    "not signed in to the cluster: run `deep-research cluster login`"
                )
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
        status, headers, body = 0, {}, b""
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
            raise BifrostError("bifrost rate limit reached; try again in a minute")
        if status != 200:
            raise BifrostError(f"bifrost {method}: HTTP {status} {body[:200]!r}")
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
            raise BifrostError(f"bifrost {method}: unreadable answer") from e
        if "error" in msg:
            raise BifrostError(
                f"bifrost {method}: {msg['error'].get('message', msg['error'])}"
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
            raise BifrostError(f"bifrost metadata: HTTP {meta_status}")
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
                self.wfile.write(
                    b"deep-research is signed in to the cluster. You can close this tab."
                )

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

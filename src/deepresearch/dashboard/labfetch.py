"""Fetch on this laptop when a site blocks the cluster (v0.50.0).

Some sites refuse the cluster's addresses (loc.gov answers 429/403 to Ursa Major) but
serve a normal client fine. Run #48 worked only after the data was pulled on the laptop by
hand and attached as a local data source. This module does that as one step:

1. `blocked_urls(plan)`: download URLs whose pre-flight check failed on a compute node in
   a way another machine can fix (403, 429, 5xx, timeouts, refused connections). A 404 or
   a bad host is the plan's mistake, so it is not offered.
2. `fetch(urls, dest)`: fetch each on this machine, politely (one at a time, a pause
   between requests, a short backoff on 429), refusing private and loopback addresses
   (the URLs are AI-written), redirects to them, non-HTTP schemes and anything over the
   size caps. Files land in a folder under the home folder with `urls.json` (file -> URL,
   status, bytes, sha256, when) for provenance.
3. The Lab registers that folder as a `local_folder` data source, attaches it to the plan
   and asks the fixer to read the files from `$DS_<NAME>` instead of downloading.

Never runs anything on the cluster and never submits.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_URLS = 15
MAX_FILE_BYTES = 200 * 1024**2
MAX_TOTAL_BYTES = 1024**3
TIMEOUT_S = 120
PAUSE_S = 2.0
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) deep-research-lab/1.0 (research use; laptop fetch)"
)

# statuses another machine can get past: blocked, rate limited, server trouble, no answer
_RETRYABLE = re.compile(
    r"^HTTP (401|403|405|406|418|429|451|5\d\d)$|^HTTP 000$|timed? ?out|refused|reset|"
    r"curl: \((6|7|28|35|52|56)\)",
    re.I,
)


def blocked_urls(plan: dict) -> list[dict]:
    """URL checks that failed on the cluster in a way a laptop fetch can fix."""
    from deepresearch.dashboard.lab import script_urls

    live = set(script_urls(plan))
    done = set((plan.get("laptop_fetch") or {}).get("urls") or [])
    out = []
    for u in plan.get("url_checks") or []:
        if not isinstance(u, dict) or u.get("ok") is not False:
            continue
        url, status = str(u.get("url") or ""), str(u.get("status") or "")
        if url in live and url not in done and _RETRYABLE.search(status):
            out.append({"url": url, "status": status})
    return out[:MAX_URLS]


class FetchRefused(ValueError):
    pass


_NOT_PUBLIC = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),  # Tailscale IPv6
)


def _public_host(host: str) -> None:
    """Refuse names that resolve to private, loopback, link-local or reserved addresses."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise FetchRefused(f"cannot resolve {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            # carrier-grade NAT: Tailscale's tailnet lives here (the Core Pi, laptops)
            any(ip in n for n in _NOT_PUBLIC)
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise FetchRefused(f"{host} resolves to a non-public address ({ip})")


def check_url(url: str) -> None:
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise FetchRefused(f"only http(s) URLs can be fetched: {url[:120]}")
    _public_host(p.hostname)


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)  # a redirect may not lead into the LAN either
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SafeRedirects)

_EXT = {
    "application/json": ".json",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "text/html": ".html",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/zip": ".zip",
    "application/gzip": ".gz",
    "application/x-gzip": ".gz",
    "application/fits": ".fits",
}


def file_name(url: str, content_type: str = "") -> str:
    """A safe, stable file name: the last path part plus a short hash of the full URL
    (query strings differ while paths repeat, e.g. loc.gov searches)."""
    p = urllib.parse.urlsplit(url)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(p.path).name or p.hostname or "data")
    stem = stem.strip("._")[:60] or "data"
    h = hashlib.sha256(url.encode()).hexdigest()[:8]
    base, dot, ext = stem.rpartition(".")
    if dot and 1 <= len(ext) <= 5 and ext.isalnum():
        return f"{base[:50]}-{h}.{ext}"
    ext = _EXT.get(content_type.split(";")[0].strip().lower(), ".dat")
    return f"{stem}-{h}{ext}"


def fetch(urls: list[str], dest: Path, opener=None, sleep=time.sleep) -> dict[str, Any]:
    """Fetch each URL into dest. Returns {"files": [...], "failed": [...], "bytes": n}.
    One at a time with a pause; one retry after a backoff on 429/503."""
    op = opener or _OPENER
    dest.mkdir(parents=True, exist_ok=True)
    files: list[dict] = []
    failed: list[dict] = []
    total = 0
    for i, url in enumerate(urls[:MAX_URLS]):
        if i:
            sleep(PAUSE_S)
        try:
            check_url(url)
        except FetchRefused as e:
            failed.append({"url": url, "error": str(e)})
            continue
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with op.open(req, timeout=TIMEOUT_S) as r:
                    ctype = r.headers.get("Content-Type", "")
                    name = file_name(url, ctype)
                    h = hashlib.sha256()
                    n = 0
                    tmp = dest / f".{name}.part"
                    with open(tmp, "wb") as fh:
                        while True:
                            chunk = r.read(1 << 16)
                            if not chunk:
                                break
                            n += len(chunk)
                            if n > MAX_FILE_BYTES or total + n > MAX_TOTAL_BYTES:
                                raise FetchRefused(
                                    "over the size limit "
                                    f"({MAX_FILE_BYTES // 1024**2} MB per file, "
                                    f"{MAX_TOTAL_BYTES // 1024**3} GB in all)"
                                )
                            h.update(chunk)
                            fh.write(chunk)
                    tmp.replace(dest / name)
                    total += n
                    files.append(
                        {
                            "file": name,
                            "url": url,
                            "status": getattr(r, "status", 200),
                            "bytes": n,
                            "sha256": h.hexdigest(),
                            "content_type": ctype,
                            "fetched": datetime.now().isoformat(timespec="seconds"),
                        }
                    )
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt == 0:
                    sleep(min(int(e.headers.get("Retry-After") or 20), 60))
                    continue
                failed.append({"url": url, "error": f"HTTP {e.code}"})
                break
            except FetchRefused as e:
                failed.append({"url": url, "error": str(e)})
                break
            except Exception as e:  # timeout, reset, TLS
                failed.append({"url": url, "error": f"{type(e).__name__}: {e}"[:200]})
                break
        for p in dest.glob(".*.part"):
            p.unlink(missing_ok=True)
    (dest / "urls.json").write_text(
        json.dumps({"files": files, "failed": failed}, indent=1)
    )
    return {"files": files, "failed": failed, "bytes": total}


def fetch_root() -> Path:
    """Where laptop fetches go: inside an allowed local-source folder."""
    from deepresearch.sources.adapters import local_roots

    roots = local_roots()
    home = Path.home()
    base = home if home in roots else roots[0]
    return base / "research-data" / "lab-fetch"


def source_name(workspace: str, run_id: int) -> str:
    ws = re.sub(r"[^a-z0-9]+", "-", (workspace or "main").lower()).strip("-")[:12]
    return f"fetch-{ws}-run{run_id}"[:41]


def problems_for_fixer(env_var: str, files: list[dict]) -> list[str]:
    """What the fixer is told: read these files instead of downloading."""
    lines = [
        f"These URLs are blocked from the cluster (it gets 403/429), so they were "
        f"fetched on the laptop and staged as a data source. The job must read the files "
        f"from ${env_var} and must NOT download these URLs:"
    ]
    lines += [f"  ${env_var}/{f['file']}  <-  {f['url']}" for f in files]
    return [" ".join(lines[:1]) + "\n" + "\n".join(lines[1:])]


# A job's own log saying a site refused it: "HTTP 429 for 1890 riverside_orange" (run #42,
# loc.gov), "HTTPError: 403 Client Error: Forbidden for url: https://...", "urllib.error.
# HTTPError: HTTP Error 429: Too Many Requests". A bare 403/429 inside numbers or tables
# is not enough: the status must sit next to HTTP / Client Error / Forbidden / Too Many.
_LOG_BLOCK = re.compile(
    r"(?:HTTP(?: Error)?[ :]+(?P<a>401|403|429|451|50[234])\b"
    r"|\b(?P<b>403|429)\b[ :]*(?:Client Error|Forbidden|Too Many Requests))",
    re.I,
)
_URL_RE = re.compile(r"https?://[^\s'\"<>)\]]+")


def runtime_blocked(log: str, plan: dict) -> dict | None:
    """Did the job (or its pilot) hit a site that refuses the cluster? (v0.51.0, L8)

    Pre-flight only checks the fixed URLs in a plan; a job that builds its URLs while it
    runs (query loops, as run #42 did against loc.gov) is only seen in its log. Returns
    {"statuses", "count", "hosts", "urls", "lines"} when the log shows refusals, else None.
    `urls` are the plan's fixed URLs on a refusing host (fetchable here); when it is empty
    the job builds its URLs, and the fixer is told to pre-fetch or stage the data instead.
    """
    if not log:
        return None
    from deepresearch.dashboard.lab import script_urls

    hits = [m for m in _LOG_BLOCK.finditer(log)]
    if not hits:
        return None
    statuses = sorted({(m.group("a") or m.group("b")) for m in hits})
    lines: list[str] = []
    hosts: set[str] = set()
    for m in hits:
        a = log.rfind("\n", 0, m.start()) + 1
        b = log.find("\n", m.end())
        line = log[a : b if b >= 0 else len(log)].strip()
        if line and line not in lines and len(lines) < 5:
            lines.append(line[:240])
        for u in _URL_RE.findall(line):
            h = urllib.parse.urlsplit(u).hostname
            if h:
                hosts.add(h.lower())
    plan_urls = script_urls(plan)
    plan_hosts = {
        (urllib.parse.urlsplit(u).hostname or "").lower() for u in plan_urls
    } - {""}
    text = json.dumps({k: plan.get(k) for k in ("script", "inputs")})
    if not hosts:
        # the log names no URL (run #42): blame the hosts the script talks to
        for u in _URL_RE.findall(text):
            h = (urllib.parse.urlsplit(u).hostname or "").lower()
            if h:
                hosts.add(h)
    done = set((plan.get("laptop_fetch") or {}).get("urls") or [])
    urls = [
        u for u in plan_urls
        if (urllib.parse.urlsplit(u).hostname or "").lower() in hosts and u not in done
    ][:MAX_URLS]  # fmt: skip
    if not hosts and not plan_hosts:
        return None  # nothing in the plan talks to the web: not a site refusal
    return {
        "statuses": statuses,
        "count": len(hits),
        "hosts": sorted(hosts)[:6],
        "urls": urls,
        "lines": lines,
    }


def runtime_problem(info: dict) -> str:
    """What the fixer is told when a failed job was refused by a site at run time."""
    hosts = ", ".join(info.get("hosts") or []) or "a data site"
    return (
        f"The job was refused by {hosts} while it ran (HTTP "
        f"{'/'.join(info.get('statuses') or [])}, {info.get('count')} times): the site "
        "blocks or rate-limits the cluster's addresses, and backing off did not help. Do "
        "not just retry harder. Fetch fewer, larger responses (facets, bulk files, "
        "pagination with big page sizes) and cache them; or, if the plan can name the "
        "exact URLs, list them as fixed URLs in the script so they can be fetched on the "
        "laptop and staged as a data source. A result computed from refused requests is "
        "not a result: treat missing data as a failed check, never as zero."
    )

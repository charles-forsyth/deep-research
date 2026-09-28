"""Getting data sources to a Lab job on the cluster.

Two modes per source (DataSource.effective_staging):

relay   The machine running deep-research fetches the source (local files, CephRDS over
        the campus VPN, GCS, web) and uploads it to the cluster before sbatch. Needed
        for anything the cluster cannot reach itself.
direct  The job downloads it on the node in a "Staging data" stage (web, GCS; S3
        endpoints the cluster can reach through its own rclone remote).

Either way the data lands in <remote_root>/data/<name>-<manifest hash>/, is reused by
later runs with the same content, is made read-only, and the job gets DS_<NAME>
pointing at it plus sources.json for provenance.
"""

from __future__ import annotations

import json
import shlex
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from deepresearch.sources.adapters import SourceError, adapter_for
from deepresearch.sources.model import DataSource

RELAY_MAX_BYTES = (
    2 * 1024**3
)  # laptop -> cluster upload cap per source (override: options.max_relay_bytes)


def cache_key(s: DataSource) -> str:
    h = s.manifest.hash if s.manifest and s.manifest.hash else "nohash"
    return f"{s.name}-{h}"


def remote_dir(remote_root: str, s: DataSource) -> str:
    return f"{remote_root}/data/{cache_key(s)}"


def sources_json(sources: list[DataSource], remote_root: str) -> str:
    return json.dumps(
        [
            {
                "name": s.name,
                "kind": s.kind,
                "uri": s.uri,
                "staging": s.effective_staging,
                "manifest_hash": s.manifest.hash if s.manifest else "",
                "files": s.manifest.file_count if s.manifest else None,
                "bytes": s.manifest.total_bytes if s.manifest else None,
                "env_var": s.env_var,
                "path": remote_dir(remote_root, s),
            }
            for s in sources
        ],
        indent=2,
    )


def staging_block(sources: list[DataSource], remote_root: str) -> str:
    """Shell for run.sbatch: download direct sources, export DS_* for all of them."""
    if not sources:
        return ""
    out = ['stage "Staging data"']
    for s in sources:
        d = remote_dir(remote_root, s)
        var = s.env_var
        out.append(f"export {var}={d}")
        if s.effective_staging == "direct":
            snippet = adapter_for(s).direct_snippet()
            out.append(
                f"""mkdir -p "$(dirname "${var}")"
exec 8>"${var}.lock"; flock 8
if [ ! -f "${var}/.ready" ]; then
  rm -rf "${var}"; mkdir -p "${var}"
  DEST="${var}"
  {snippet.strip()}
  chmod -R a-w "${var}" 2>/dev/null || true
  touch "${var}/.ready" 2>/dev/null || {{ chmod u+w "${var}"; touch "${var}/.ready"; chmod u-w "${var}"; }}
else
  echo "[INFO] reusing staged {s.name}"
fi
flock -u 8"""
            )
        else:
            out.append(
                f'[ -f "${var}/.ready" ] || {{ echo "[ERROR] data source {s.name} was not '
                f'uploaded to ${var}"; exit 3; }}'
            )
        out.append(
            f'echo "[INFO] {s.name} -> ${var} ($(du -sh "${var}" 2>/dev/null | cut -f1))"'
        )
    return "\n".join(out) + "\n"


def relay_upload(target: Any, s: DataSource, log=print) -> str:
    """Fetch a relay source on this machine and upload it to the cluster cache.

    Skips the upload when the cluster already holds this exact content (same manifest
    hash). Returns the remote directory.
    """
    remote_root = getattr(target, "remote_root", "~/deep-research-lab")
    d = remote_dir(remote_root, s)
    have = target.run(f"test -f {d}/.ready && echo yes || echo no", timeout=60)
    if have.stdout.decode().strip() == "yes":
        log(f"[INFO] {s.name}: already on the cluster ({d})")
        return d
    cap = int(s.options.get("max_relay_bytes") or RELAY_MAX_BYTES)
    size = s.manifest.total_bytes if s.manifest else 0
    if size > cap:
        raise SourceError(
            f"{s.name} is {size / 1024**3:.1f} GB, over the relay limit of "
            f"{cap / 1024**3:.1f} GB (raise options.max_relay_bytes to allow it)"
        )
    with tempfile.TemporaryDirectory(prefix="dr-relay-") as tmp:
        local = adapter_for(s).fetch(Path(tmp) / "data")
        tar_path = Path(tmp) / "data.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            for p in sorted(local.rglob("*")):
                if p.is_file() and not p.is_symlink():
                    tar.add(p, arcname=str(p.relative_to(local)))
        payload = tar_path.read_bytes()
        log(f"[INFO] {s.name}: uploading {len(payload) / 1024**2:.1f} MB to {d}")
        target.sh(
            f"set -e; rm -rf {d}.part; mkdir -p {d}.part; tar xzf - -C {d}.part; "
            f"touch {d}.part/.ready; chmod -R a-w {d}.part; "
            f"rm -rf {d}; mv {d}.part {d}",
            stdin=payload,
            timeout=3600,
        )
    return d


def plan_data_note(sources: list[DataSource], remote_root: str) -> str:
    """What the planner is told about the sources a user picked."""
    if not sources:
        return ""
    lines = [
        "DATA SOURCES THE USER SELECTED (already staged read-only before your script "
        "runs; read them from the environment variable, never download them again):"
    ]
    for s in sources:
        m = s.manifest
        sample = ", ".join(e.path for e in (m.entries[:8] if m else []))
        fmts = ", ".join(
            f"{k} {v}" for k, v in list((m.formats if m else {}).items())[:6]
        )
        lines.append(
            f"- ${s.env_var} ({s.kind}: {s.uri}): "
            + (
                f"{m.file_count}{'+' if m.truncated else ''} files, "
                f"{m.total_bytes / 1024**2:.1f} MB; formats {fmts}; e.g. {sample}"
                if m
                else "contents not listed"
            )
            + (f". {s.description}" if s.description else "")
        )
    return "\n".join(lines) + "\n"


def quote(v: str) -> str:
    return shlex.quote(v)

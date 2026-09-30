"""Data source records and manifests."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

KINDS = (
    "web", "gcs", "s3", "public_bucket", "local_folder", "local_file", "report",
    "notebook", "gdrive",
)  # fmt: skip
LEVELS = ("P1", "P2", "P3", "P4")  # shown, never enforced (Chuck, 2026-09-28)
STAGING = ("auto", "relay", "direct")
ENTRY_CAP = 5000  # manifest entries kept in the DB so browsing needs no re-listing
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")


def default_staging(kind: str) -> str:
    """How a Lab job gets this kind of source when the plan does not say.

    relay: the machine running deep-research fetches the data and uploads it to the
    cluster (needed for local files and for CephRDS, which the cluster cannot reach
    without the campus VPN). direct: the cluster node downloads it itself.
    """
    return "direct" if kind in ("web", "gcs", "public_bucket") else "relay"


class ManifestEntry(BaseModel):
    path: str
    size: int = 0
    modified: str = ""


class Manifest(BaseModel):
    """What a source holds, as far as a bounded listing can tell."""

    file_count: int = 0
    total_bytes: int = 0
    truncated: bool = False  # listing stopped at the limit; counts are lower bounds
    entries: list[ManifestEntry] = Field(
        default_factory=list
    )  # up to ENTRY_CAP, for browsing
    formats: dict[str, int] = Field(default_factory=dict)  # extension -> count
    hash: str = ""

    @classmethod
    def build(cls, entries: list[ManifestEntry], truncated: bool = False) -> Manifest:
        entries = sorted(entries, key=lambda e: e.path)
        fmts: dict[str, int] = {}
        for e in entries:
            ext = e.path.rsplit(".", 1)[-1].lower() if "." in e.path[-8:] else "(none)"
            fmts[ext] = fmts.get(ext, 0) + 1
        digest = hashlib.sha256(
            json.dumps([[e.path, e.size, e.modified] for e in entries]).encode()
        ).hexdigest()[:16]
        return cls(
            file_count=len(entries),
            total_bytes=sum(e.size for e in entries),
            truncated=truncated,
            entries=entries[:ENTRY_CAP],
            formats=dict(sorted(fmts.items(), key=lambda kv: -kv[1])),
            hash=digest,
        )


class DataSource(BaseModel):
    id: int | None = None
    name: str
    title: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    kind: str
    uri: str
    options: dict[str, Any] = Field(default_factory=dict)
    auth_ref: str = ""  # e.g. "adc", "rclone:ceph"; a reference, never a secret
    protection_level: str = "P2"
    staging: str = "auto"
    temporary: bool = False
    status: str = "unchecked"
    last_checked: str = ""
    last_error: str = ""
    manifest: Manifest | None = None
    created_at: str = ""
    updated_at: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.fullmatch(v or ""):
            raise ValueError(
                "name must be 2-41 characters: lowercase letters, digits and dashes, "
                "starting with a letter or digit"
            )
        return v

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        return v

    @field_validator("protection_level")
    @classmethod
    def _level(cls, v: str) -> str:
        v = (v or "P2").upper()
        if v not in LEVELS:
            raise ValueError(f"protection level must be one of {', '.join(LEVELS)}")
        return v

    @field_validator("staging")
    @classmethod
    def _staging(cls, v: str) -> str:
        if v not in STAGING:
            raise ValueError(f"staging must be one of {', '.join(STAGING)}")
        return v

    @property
    def env_var(self) -> str:
        """Variable a Lab job reads to find its staged copy, e.g. DS_NOAA_GHCN."""
        return "DS_" + self.name.upper().replace("-", "_")

    @property
    def effective_staging(self) -> str:
        return default_staging(self.kind) if self.staging == "auto" else self.staging

    def public(self) -> dict[str, Any]:
        """JSON-safe view for the API and CLI."""
        d = self.model_dump()
        d["env_var"] = self.env_var
        d["effective_staging"] = self.effective_staging
        return d

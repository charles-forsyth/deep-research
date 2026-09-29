"""Public cloud buckets read anonymously over HTTPS: free, no credentials, no billing.

`s3://bucket/prefix` (AWS Open Data) and `gs://bucket/prefix` (Google Cloud public
datasets) are listed and read through the providers' public REST endpoints with no
account. Nothing here can create a charge: requests are unsigned, so a requester-pays
bucket answers with an error instead of billing anyone. Downloads use plain HTTPS, so a
cluster node needs only curl (no rclone, gcloud or aws CLI).
"""

from __future__ import annotations

import json
import re
import shlex
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from deepresearch.sources.adapters import (
    MANIFEST_LIMIT,
    PREVIEW_BYTES,
    SourceAdapter,
    SourceError,
    _filtered,
    _FullManifest,
    _manifest,
    safe_join,
    safe_rel,
)
from deepresearch.sources.model import ManifestEntry

UA = "deep-research (anonymous public data)"
TIMEOUT = 30
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
FETCH_MAX_FILES = 2000
MAX_PAGES = 50  # 1,000 keys per page: a filter scans at most 50,000 keys, then stops


def _http(url: str, max_bytes: int | None = None) -> bytes:
    headers = {"User-Agent": UA}
    if max_bytes:
        headers["Range"] = f"bytes=0-{max_bytes - 1}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310
            return r.read(max_bytes) if max_bytes else r.read()
    except urllib.error.HTTPError as e:
        body = e.read(2000).decode("utf-8", "replace")
        if "RequesterPays" in body or "requester pays" in body.lower():
            raise SourceError(
                "this bucket is requester-pays (reading it costs money); not supported"
            ) from e
        if e.code in (401, 403):
            raise SourceError(
                "this bucket is not public (anonymous access denied); use an s3 or gcs "
                "source with credentials instead"
            ) from e
        if e.code == 404:
            raise SourceError("not found (check the bucket name and prefix)") from e
        raise SourceError(f"HTTP {e.code}: {body[:200]}") from e
    except urllib.error.URLError as e:
        raise SourceError(f"could not reach the bucket: {e.reason}") from e


def parse_uri(uri: str) -> tuple[str, str, str]:
    """('s3'|'gs', bucket, prefix). Accepts s3://, gs:// and https bucket URLs."""
    m = re.match(r"^(s3|gs)://([^/]+)/?(.*)$", uri)
    if m:
        return m.group(1), m.group(2), m.group(3).strip("/")
    m = re.match(
        r"^https://([^./]+)\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com/?(.*)$", uri
    )
    if m:
        return "s3", m.group(1), m.group(2).strip("/")
    m = re.match(r"^https://storage\.googleapis\.com/([^/]+)/?(.*)$", uri)
    if m:
        return "gs", m.group(1), m.group(2).strip("/")
    raise SourceError(
        "public buckets look like s3://bucket/prefix or gs://bucket/prefix"
    )


class PublicBucketAdapter(SourceAdapter):
    def _parts(self) -> tuple[str, str, str]:
        return parse_uri(self.s.uri)

    def _rel(self, path: str) -> str:
        rel = path.strip("/")
        if ".." in rel.split("/"):
            raise SourceError("path escapes the source")
        return rel

    def object_url(self, key: str) -> str:
        scheme, bucket, _ = self._parts()
        q = urllib.parse.quote(key)
        if scheme == "s3":
            region = self.s.options.get("region")
            host = (
                f"{bucket}.s3.{region}.amazonaws.com"
                if region
                else f"{bucket}.s3.amazonaws.com"
            )
            return f"https://{host}/{q}"
        return f"https://storage.googleapis.com/{bucket}/{q}"

    def _key(self, path: str) -> str:
        _, _, prefix = self._parts()
        return "/".join(x for x in (prefix, self._rel(path)) if x)

    # -- listing ------------------------------------------------------------
    def _list_s3(self, bucket: str, prefix: str, limit: int) -> tuple[list, bool]:
        rows: list[tuple[str, int, str]] = []
        token = None
        pages = 0
        host = self.object_url("").split("/")[2]
        while len(rows) <= limit:
            q = {"list-type": "2", "max-keys": "1000"}
            if prefix:
                q["prefix"] = prefix + "/"
            if self._top_only():
                q["delimiter"] = "/"
            if token:
                q["continuation-token"] = token
            xml = _http(f"https://{host}/?" + urllib.parse.urlencode(q))
            try:
                root = ET.fromstring(xml)
            except ET.ParseError as e:
                raise SourceError(
                    "the bucket did not return a listing (is it an S3 bucket?)"
                ) from e
            for c in root.findall(f"{S3_NS}Contents"):
                row = (
                    c.findtext(f"{S3_NS}Key") or "",
                    int(c.findtext(f"{S3_NS}Size") or 0),
                    c.findtext(f"{S3_NS}LastModified") or "",
                )
                if self._keep(row[0], prefix):
                    rows.append(row)
            pages += 1
            token = root.findtext(f"{S3_NS}NextContinuationToken")
            if root.findtext(f"{S3_NS}IsTruncated") != "true" or not token:
                return rows, len(rows) > limit
            if pages >= MAX_PAGES:
                return rows, True
        return rows, True

    def _list_gs(self, bucket: str, prefix: str, limit: int) -> tuple[list, bool]:
        rows: list[tuple[str, int, str]] = []
        token = None
        pages = 0
        while len(rows) <= limit:
            q = {
                "maxResults": "1000",
                "fields": "items(name,size,updated),nextPageToken",
            }
            if prefix:
                q["prefix"] = prefix + "/"
            if self._top_only():
                q["delimiter"] = "/"
            if token:
                q["pageToken"] = token
            raw = _http(
                f"https://storage.googleapis.com/storage/v1/b/{bucket}/o?"
                + urllib.parse.urlencode(q)
            )
            try:
                d = json.loads(raw)
            except ValueError as e:
                raise SourceError("the bucket did not return a listing") from e
            for it in d.get("items") or []:
                if self._keep(it["name"], prefix):
                    rows.append(
                        (it["name"], int(it.get("size") or 0), it.get("updated", ""))
                    )
            pages += 1
            token = d.get("nextPageToken")
            if not token:
                return rows, len(rows) > limit
            if pages >= MAX_PAGES:
                return rows, True
        return rows, True

    def _top_only(self) -> bool:
        """Include globs with no "/" name files at the top of the prefix; list only that
        level instead of scanning the whole bucket (public buckets can hold millions
        of objects)."""
        inc = self.s.options.get("include") or []
        return bool(inc) and all("/" not in p and "**" not in p for p in inc)

    def _keep(self, key: str, prefix: str) -> bool:
        rel = key[len(prefix) + 1 :] if prefix else key
        if not rel or rel.endswith("/") or safe_rel(rel) != rel:
            return False  # folder markers and keys that are not plain relative paths
        return _filtered(self.s, rel)

    def manifest(self, limit: int = MANIFEST_LIMIT) -> _FullManifest:
        scheme, bucket, prefix = self._parts()
        rows, more = (self._list_s3 if scheme == "s3" else self._list_gs)(
            bucket, prefix, limit
        )
        entries = [
            ManifestEntry(
                path=key[len(prefix) + 1 :] if prefix else key, size=size, modified=mod
            )
            for key, size, mod in rows[:limit]
        ]
        return _manifest(entries, more or len(rows) > limit)

    def preview(self, path: str = "", max_bytes: int = PREVIEW_BYTES) -> bytes:
        if not path:
            raise SourceError("pick a file in the bucket to preview")
        return _http(self.object_url(self._key(path)), max_bytes)

    def _files(self) -> list[ManifestEntry]:
        m = self.s.manifest
        if m is None or m.truncated:
            m = self.manifest(FETCH_MAX_FILES)
            if m.truncated:
                raise SourceError(
                    f"{self.s.name} has more than {FETCH_MAX_FILES} files; narrow it with "
                    "a prefix or --include before fetching"
                )
        return list(m.entries)

    def fetch(self, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        files = self._files()

        def get(e: ManifestEntry) -> None:
            out = safe_join(dest, e.path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(_http(self.object_url(self._key(e.path))))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(get, files))
        return dest

    def direct_snippet(self) -> str:
        """curl every file of the stored manifest on the node (no cloud CLIs needed)."""
        files = self._files()
        lines = []
        for e in files:
            clean = safe_rel(e.path)
            if clean is None:
                raise SourceError(
                    f"refusing unsafe file name from the bucket: {e.path!r}"
                )
            rel = shlex.quote(clean)
            url = shlex.quote(self.object_url(self._key(clean)))
            # --create-dirs makes the parent folders; no word-splitting on spaces.
            lines.append(f'curl -fsSL --retry 3 --create-dirs -o "$DEST"/{rel} {url}')
        return "\n".join(lines) + "\n"


def describe(uri: str) -> dict[str, Any]:
    scheme, bucket, prefix = parse_uri(uri)
    return {"scheme": scheme, "bucket": bucket, "prefix": prefix}

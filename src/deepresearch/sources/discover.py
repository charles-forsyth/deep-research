"""Open dataset discovery: search public catalogs and turn a hit into a data source.

Google Dataset Search has no API, so discovery queries catalogs that do:

- Data.gov (US federal, state and local open data), the v4 Catalog API. It needs an
  api.data.gov key: DATA_GOV_API_KEY, or the shared DEMO_KEY (low rate limit).
- Zenodo (research data with DOIs), public API, no key.
- Hugging Face Hub datasets, public API, no key.

- AWS Open Data, Google Cloud public buckets and Earth Engine (cloud_catalogs.py); only
  free, anonymous-read data is offered.

Each hit carries direct download links (`files`, added as `web` sources) or public
buckets (`buckets`, added as `public_bucket` sources, read anonymously). Nothing is
downloaded during search.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

UA = "deep-research dataset discovery"
TIMEOUT = 20
CATALOGS = ("datagov", "zenodo", "huggingface", "aws", "gcp", "earthengine")


def _get(url: str, headers: dict[str, str] | None = None) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 (fixed hosts)
        return json.loads(r.read().decode("utf-8", "replace"))


def _clip(s: Any, n: int = 400) -> str:
    s = re.sub(r"<[^>]+>", " ", str(s or ""))
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)  # markdown links -> their text
    s = re.sub(r"\s+", " ", html.unescape(s)).strip()
    return s[:n] + ("..." if len(s) > n else "")


def search_datagov(q: str, limit: int = 8) -> list[dict[str, Any]]:
    key = os.getenv("DATA_GOV_API_KEY") or "DEMO_KEY"
    url = "https://api.gsa.gov/technology/datagov/v4/search?" + urllib.parse.urlencode(
        {"q": q, "per_page": limit}
    )
    out = []
    for r in _get(url, {"X-Api-Key": key}).get("results", [])[:limit]:
        dcat = r.get("dcat") or {}
        files = [
            {
                "title": d.get("title") or d.get("format") or "",
                "url": d.get("downloadURL"),
                "format": _fmt(d.get("format"), d.get("mediaType")),
            }
            for d in dcat.get("distribution") or []
            if d.get("downloadURL")
        ]
        out.append(
            {
                "catalog": "datagov",
                "id": r.get("slug") or r.get("identifier"),
                "title": _clip(r.get("title"), 200),
                "description": _clip(r.get("description") or dcat.get("description")),
                "publisher": (r.get("organization") or {}).get("title")
                if isinstance(r.get("organization"), dict)
                else r.get("publisher"),
                "license": dcat.get("license"),
                "page": f"https://catalog.data.gov/dataset/{r.get('slug')}"
                if r.get("slug")
                else dcat.get("landingPage"),
                "files": files[:10],
            }
        )
    return out


def search_zenodo(q: str, limit: int = 8) -> list[dict[str, Any]]:
    url = "https://zenodo.org/api/records?" + urllib.parse.urlencode(
        {"q": q, "size": limit, "type": "dataset"}
    )
    out = []
    for h in _get(url).get("hits", {}).get("hits", [])[:limit]:
        m = h.get("metadata") or {}
        files = [
            {
                "title": f.get("key"),
                "url": (f.get("links") or {}).get("self"),
                "format": (f.get("key") or "").rsplit(".", 1)[-1],
                "size": f.get("size"),
            }
            for f in h.get("files") or []
            if (f.get("links") or {}).get("self")
        ]
        out.append(
            {
                "catalog": "zenodo",
                "id": str(h.get("id")),
                "title": _clip(m.get("title"), 200),
                "description": _clip(m.get("description")),
                "publisher": ", ".join(
                    c.get("name", "") for c in (m.get("creators") or [])[:3]
                ),
                "license": (m.get("license") or {}).get("id"),
                "page": h.get("doi_url") or (h.get("links") or {}).get("self_html"),
                "files": files[:10],
            }
        )
    return out


def search_huggingface(q: str, limit: int = 8) -> list[dict[str, Any]]:
    url = "https://huggingface.co/api/datasets?" + urllib.parse.urlencode(
        {"search": q, "limit": limit}
    )
    out = []
    hits = [d for d in _get(url)[:limit] if not d.get("private") and not d.get("gated")]

    def detail(d: dict[str, Any]) -> dict[str, Any]:
        # the list call omits file names; one small lookup per hit adds them
        try:
            return {
                **d,
                **_get(f"https://huggingface.co/api/datasets/{d.get('id', '')}"),
            }
        except Exception:
            return d

    with ThreadPoolExecutor(max_workers=max(1, len(hits))) as pool:
        hits = list(pool.map(detail, hits))
    for d in hits:
        rid = d.get("id", "")
        files = [
            {
                "title": s.get("rfilename"),
                "url": f"https://huggingface.co/datasets/{rid}/resolve/main/"
                + urllib.parse.quote(s.get("rfilename", "")),
                "format": (s.get("rfilename") or "").rsplit(".", 1)[-1],
            }
            for s in d.get("siblings") or []
            if re.search(
                r"\.(csv|tsv|json|jsonl|parquet|txt|zip|gz)$", s.get("rfilename", "")
            )
        ]
        lic = next(
            (t.split(":", 1)[1] for t in d.get("tags", []) if t.startswith("license:")),
            None,
        )
        out.append(
            {
                "catalog": "huggingface",
                "id": rid,
                "title": rid,
                "description": _clip(
                    (d.get("cardData") or {}).get("pretty_name") or d.get("description")
                ),
                "publisher": d.get("author"),
                "license": lic,
                "page": f"https://huggingface.co/datasets/{rid}",
                "files": files[:10],
            }
        )
    return out


def _cloud(name: str):
    def run(q: str, limit: int = 8) -> list[dict[str, Any]]:
        from deepresearch.sources import cloud_catalogs as cc

        return {
            "aws": cc.search_aws,
            "gcp": cc.search_gcp,
            "earthengine": cc.search_earthengine,
        }[name](q, limit)

    return run


SEARCHERS = {
    "datagov": search_datagov,
    "zenodo": search_zenodo,
    "huggingface": search_huggingface,
    "aws": _cloud("aws"),
    "gcp": _cloud("gcp"),
    "earthengine": _cloud("earthengine"),
}


_MIME_NAMES = {
    "vnd.ms-excel": "Excel",
    "vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel",
    "plain": "Text",
    "octet-stream": "Binary",
    "geo+json": "GeoJSON",
    "geopackage+sqlite3": "GeoPackage",
    "vnd.google-earth.kml+xml": "KML",
    "x-netcdf": "NetCDF",
    "netcdf": "NetCDF",
    "zip": "ZIP",
}


def _fmt(fmt: Any, media_type: Any) -> str:
    """A short format label: the catalog's own name if it has one ("Excel"), else a
    readable name for the MIME type ("vnd.ms-excel" -> "Excel", "text/csv" -> "CSV")."""
    name = str(fmt or "").strip()
    if name and "/" not in name:
        return name
    sub = str(media_type or name or "").split(";")[0].split("/")[-1].strip().lower()
    if not sub:
        return ""
    return _MIME_NAMES.get(sub) or sub.replace("x-", "").split("+")[0].upper()


def discover(
    q: str, catalogs: list[str] | None = None, limit: int = 8
) -> dict[str, Any]:
    """Search catalogs in parallel. A failing catalog is reported, not fatal."""
    cats = [c for c in (catalogs or list(CATALOGS)) if c in SEARCHERS]
    results: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(cats) or 1) as pool:
        futs = {c: pool.submit(SEARCHERS[c], q, limit) for c in cats}
        for c, f in futs.items():
            try:
                results.extend(f.result())
            except Exception as e:  # network, rate limit, schema change
                errors[c] = str(e)[:300]
    return {"query": q, "results": results, "errors": errors}


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "dataset")[:40].strip("-") or "dataset"

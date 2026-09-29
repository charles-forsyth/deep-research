"""Free cloud catalogs for discovery: AWS Open Data, Google Cloud public buckets, Earth
Engine. Only sources that cost nothing to read are offered (Chuck, 2026-09-28):

- AWS: the Registry of Open Data (registry.opendata.aws/index.ndjson). Datasets whose
  buckets are requester-pays, need an AWS account, or are controlled-access are dropped.
- Google Cloud: a curated list of public buckets that answer anonymous reads (checked
  2026-09-28). Requester-pays and non-public Google buckets are not listed.
- Earth Engine: the public STAC catalog. Earth Engine data is used inside Earth Engine
  (free for noncommercial research with a registered account), so hits are
  information-only: they link to the catalog page and are not added as sources.

The catalog indexes are downloaded once and cached for a day.
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from deepresearch.sources.discover import _clip, _get

CACHE_TTL = 24 * 3600
AWS_INDEX = "https://registry.opendata.aws/index.ndjson"
EE_ROOT = "https://storage.googleapis.com/earthengine-stac/catalog/catalog.json"

# Google Cloud public datasets readable anonymously (no account, no billing).
GCP_PUBLIC = [
    ("gs://gcp-public-data-landsat", "Landsat 4-9 scenes (USGS/NASA)",
     "Landsat Collection scenes as GeoTIFF; index.csv.gz lists every scene.",
     ["satellite", "imagery", "earth observation", "landsat"]),
    ("gs://gcp-public-data-sentinel-2", "Sentinel-2 multispectral imagery (ESA Copernicus)",
     "Sentinel-2 L1C and L2A granules; index.csv.gz lists every granule.",
     ["satellite", "imagery", "earth observation", "sentinel"]),
    ("gs://gcp-public-data-goes-16", "GOES-16 weather satellite (NOAA)",
     "GOES-East ABI, GLM and other instrument products in NetCDF.",
     ["weather", "satellite", "noaa", "goes", "atmosphere"]),
    ("gs://gcp-public-data-goes-18", "GOES-18 weather satellite (NOAA)",
     "GOES-West ABI and derived products in NetCDF.",
     ["weather", "satellite", "noaa", "goes", "atmosphere"]),
    ("gs://gcp-public-data-nexrad-l2", "NEXRAD Level II weather radar (NOAA)",
     "Level II radar volumes from the US WSR-88D network, 1991 to present.",
     ["weather", "radar", "noaa", "precipitation", "storms"]),
    ("gs://gcp-public-data-arco-era5", "ERA5 reanalysis, analysis-ready (ECMWF, Google)",
     "ERA5 global atmospheric reanalysis as analysis-ready Zarr.",
     ["climate", "reanalysis", "weather", "era5", "temperature", "atmosphere"]),
    ("gs://weatherbench2", "WeatherBench 2 (Google Research)",
     "Benchmark datasets and results for data-driven weather forecasting.",
     ["weather", "forecast", "machine learning", "benchmark", "era5"]),
    ("gs://high-resolution-rapid-refresh", "HRRR weather model output (NOAA)",
     "High-Resolution Rapid Refresh 3 km hourly forecasts over the US.",
     ["weather", "forecast", "noaa", "model"]),
    ("gs://global-forecast-system", "GFS weather model output (NOAA)",
     "Global Forecast System model runs.",
     ["weather", "forecast", "noaa", "model", "global"]),
    ("gs://cmip6", "CMIP6 climate model output (Pangeo)",
     "Coupled Model Intercomparison Project Phase 6 output as Zarr.",
     ["climate", "model", "projection", "cmip6", "temperature"]),
    ("gs://gcp-public-data--broad-references", "Genome reference files (Broad Institute)",
     "Reference genomes and resources (hg19, hg38 and others) for genomics pipelines.",
     ["genomics", "genome", "reference", "bioinformatics", "dna"]),
    ("gs://gcp-public-data--gnomad", "gnomAD genome aggregation data (Broad Institute)",
     "Population allele frequencies from gnomAD releases.",
     ["genomics", "variants", "population", "bioinformatics"]),
    ("gs://public-datasets-deepmind-alphafold-v4", "AlphaFold protein structures v4 (DeepMind, EMBL-EBI)",
     "Predicted protein structures for about 214 million proteins.",
     ["protein", "structure", "biology", "alphafold", "bioinformatics"]),
    ("gs://earthengine-public", "Earth Engine public exports (Google)",
     "Public files exported from Earth Engine datasets.",
     ["earth observation", "geospatial", "satellite"]),
]  # fmt: skip


def _cache_dir() -> Path:
    base = os.getenv("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    d = Path(base) / "deepresearch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(p: Path, text: str) -> None:
    """Write via a temp file and rename, so a reader never sees half a file."""
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, p)


def _cached_text(name: str, url: str) -> str:
    p = _cache_dir() / name
    if p.exists() and time.time() - p.stat().st_mtime < CACHE_TTL:
        return p.read_text("utf-8")
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "deep-research discovery"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 (fixed host)
        text = r.read().decode("utf-8", "replace")
    _atomic_write(p, text)
    return text


def _score(q: str, title: Any, desc: Any = "", tags: Any = "") -> int:
    """Rank by where the words match: title and tags count far more than the
    description, so a long description that mentions a word in passing ranks low."""
    words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) > 1]
    if not words:
        return 0
    head = f"{title or ''} {tags or ''}".lower()
    body = str(desc or "").lower()
    if not all(w in head or w in body for w in words):
        return 0  # every word must appear somewhere
    score = sum(10 if w in head else 1 for w in words)
    if q.lower() in head:
        score += 20
    return score


def _free_s3(res: dict[str, Any]) -> bool:
    return (
        res.get("Type") == "S3 Bucket"
        and not res.get("RequesterPays")
        and not res.get("AccountRequired")
        and not res.get("ControlledAccess")
    )


def search_aws(q: str, limit: int = 8) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in _cached_text("aws-open-data.ndjson", AWS_INDEX).splitlines()
        if line.strip()
    ]
    scored = []
    for d in rows:
        if d.get("Deprecated"):
            continue
        free = [r for r in d.get("Resources") or [] if _free_s3(r)]
        if not free:
            continue  # requester-pays, account or controlled access: not free
        sc = _score(
            q, d.get("Name"), d.get("Description"), " ".join(d.get("Tags") or [])
        )
        if sc:
            scored.append((sc, d, free))
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, d, free in scored[:limit]:
        buckets = []
        for r in free[:5]:
            arn = r.get("ARN", "")
            path = arn.split(":::", 1)[-1] if ":::" in arn else ""
            if path:
                buckets.append(
                    {
                        "title": _clip(r.get("Description") or path, 120),
                        "uri": f"s3://{path.strip('/')}",
                        "region": r.get("Region"),
                    }
                )
        lic = _clip(d.get("License"), 160)
        out.append(
            {
                "catalog": "aws",
                "id": d.get("Slug"),
                "title": _clip(d.get("Name"), 200),
                "description": _clip(d.get("Description")),
                "publisher": _clip(d.get("ManagedBy"), 80),
                "license": lic,
                "page": f"https://registry.opendata.aws/{d.get('Slug')}/",
                "files": [],
                "buckets": buckets,
            }
        )
    return out


def search_gcp(q: str, limit: int = 8) -> list[dict[str, Any]]:
    scored = [
        (_score(q, t, desc, " ".join(tags)), uri, t, desc)
        for uri, t, desc, tags in GCP_PUBLIC
    ]
    scored = [x for x in scored if x[0]]
    scored.sort(key=lambda x: -x[0])
    return [
        {
            "catalog": "gcp",
            "id": uri,
            "title": t,
            "description": desc,
            "publisher": "Google Cloud public datasets",
            "license": "see the dataset's terms (free to read)",
            "page": f"https://console.cloud.google.com/storage/browser/{uri[5:]}",
            "files": [],
            "buckets": [{"title": uri, "uri": uri, "region": None}],
        }
        for _, uri, t, desc in scored[:limit]
    ]


def _ee_collections() -> list[dict[str, Any]]:
    """Every Earth Engine dataset (id, title, description, keywords), cached a day."""
    p = _cache_dir() / "earth-engine-index.json"
    if p.exists() and time.time() - p.stat().st_mtime < CACHE_TTL:
        return json.loads(p.read_text("utf-8"))
    root = _get(EE_ROOT)
    kids = [ln["href"] for ln in root.get("links", []) if ln.get("rel") == "child"]

    def provider(url: str) -> list[dict[str, Any]]:
        try:
            cat = _get(url)
        except Exception:
            return []
        return [
            {"id": ln.get("title"), "href": ln.get("href")}
            for ln in cat.get("links", [])
            if ln.get("rel") == "child"
        ]

    with ThreadPoolExecutor(max_workers=16) as pool:
        items = [i for group in pool.map(provider, kids) for i in group]

    def detail(it: dict[str, Any]) -> dict[str, Any] | None:
        try:
            d = _get(it["href"])
        except Exception:
            return None
        return {
            "id": d.get("id") or it["id"],
            "title": d.get("title") or it["id"],
            "description": _clip(d.get("description"), 500),
            "keywords": d.get("keywords") or [],
            "license": d.get("license"),
            "providers": [p.get("name") for p in d.get("providers") or []][:3],
        }

    with ThreadPoolExecutor(max_workers=32) as pool:
        rows = [r for r in pool.map(detail, items) if r]
    _atomic_write(p, json.dumps(rows))
    return rows


def search_earthengine(q: str, limit: int = 8) -> list[dict[str, Any]]:
    scored = []
    for d in _ee_collections():
        sc = _score(
            q, f"{d['id']} {d['title']}", d["description"], " ".join(d["keywords"])
        )
        if sc:
            scored.append((sc, d))
    scored.sort(key=lambda x: -x[0])
    return [
        {
            "catalog": "earthengine",
            "id": d["id"],
            "title": _clip(d["title"], 200),
            "description": d["description"],
            "publisher": ", ".join(p for p in d["providers"] if p),
            "license": d.get("license"),
            "page": "https://developers.google.com/earth-engine/datasets/catalog/"
            + str(d["id"]).replace("/", "_"),
            "files": [],
            "buckets": [],
            "note": "Used inside Earth Engine (free for noncommercial research with a "
            "registered account); not added as a source.",
        }
        for _, d in scored[:limit]
    ]

"""Free public buckets (AWS Open Data, Google Cloud public data) and the cloud catalogs.
HTTP is mocked; nothing here reaches the network."""

import io
import json
import urllib.error

import pytest

from deepresearch.cli.sources import guess_kind
from deepresearch.sources import DataSource
from deepresearch.sources import cloud_catalogs as cc
from deepresearch.sources import public as pb
from deepresearch.sources.adapters import SourceError, adapter_for
from deepresearch.sources.staging import staging_block

S3_PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>b</Name>
<IsTruncated>false</IsTruncated>
<Contents><Key>data/a.csv</Key><Size>10</Size><LastModified>2026-01-01</LastModified></Contents>
<Contents><Key>data/sub/b.txt</Key><Size>5</Size><LastModified>2026-01-02</LastModified></Contents>
<Contents><Key>data/</Key><Size>0</Size><LastModified>x</LastModified></Contents>
</ListBucketResult>"""


def _src(uri, **opts):
    return DataSource(name="pub", kind="public_bucket", uri=uri, options=opts)


def test_s3_listing_preview_and_urls(monkeypatch):
    seen = []

    def http(url, max_bytes=None):
        seen.append((url, max_bytes))
        return S3_PAGE.encode() if "list-type" in url else b"hello,world"

    monkeypatch.setattr(pb, "_http", http)
    a = adapter_for(_src("s3://b/data"))
    m = a.manifest()
    assert [e.path for e in m.entries] == ["a.csv", "sub/b.txt"] and m.total_bytes == 15
    assert "prefix=data%2F" in seen[0][0] and seen[0][0].startswith(
        "https://b.s3.amazonaws.com/"
    )
    assert a.preview("a.csv", 5) == b"hello,world"
    assert seen[-1] == ("https://b.s3.amazonaws.com/data/a.csv", 5)
    regional = adapter_for(_src("s3://b", region="us-west-2"))
    assert regional.object_url("k") == "https://b.s3.us-west-2.amazonaws.com/k"


def test_gcs_listing_paginates(monkeypatch):
    pages = {
        None: {"items": [{"name": "p/x.nc", "size": "3"}], "nextPageToken": "t2"},
        "t2": {"items": [{"name": "p/y.nc", "size": "4"}]},
    }

    def http(url, max_bytes=None):
        tok = url.split("pageToken=")[1].split("&")[0] if "pageToken=" in url else None
        return json.dumps(pages[tok]).encode()

    monkeypatch.setattr(pb, "_http", http)
    m = adapter_for(_src("gs://bk/p")).manifest()
    assert [e.path for e in m.entries] == ["x.nc", "y.nc"] and not m.truncated


def test_top_level_include_lists_one_level(monkeypatch):
    urls = []
    monkeypatch.setattr(
        pb, "_http", lambda u, max_bytes=None: urls.append(u) or S3_PAGE.encode()
    )
    adapter_for(_src("s3://b", include=["*.txt"])).manifest()
    assert "delimiter=%2F" in urls[0]
    urls.clear()
    adapter_for(_src("s3://b", include=["data/*.csv"])).manifest()
    assert "delimiter" not in urls[0]


def _http_error(code, body):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body.encode()))


@pytest.mark.parametrize(
    "code,body,msg",
    [
        (
            400,
            "Bucket is a requester pays bucket but no user project provided.",
            "requester-pays",
        ),
        (403, "<Code>AccessDenied</Code>", "not public"),
        (
            401,
            "Anonymous caller does not have storage.objects.list access",
            "not public",
        ),
        (404, "NoSuchBucket", "not found"),
    ],
)
def test_paid_or_private_buckets_are_refused(monkeypatch, code, body, msg):
    def boom(req, timeout=None):
        raise _http_error(code, body)

    monkeypatch.setattr(pb.urllib.request, "urlopen", boom)
    with pytest.raises(SourceError, match=msg):
        adapter_for(_src("gs://x")).manifest()


def test_requests_are_unsigned(monkeypatch):
    """No credentials ever go out, so a public-bucket request cannot bill anyone."""
    captured = {}

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=None):
            return b"{}"

    def urlopen(req, timeout=None):
        captured.update({k.lower(): v for k, v in req.header_items()})
        return R()

    monkeypatch.setattr(pb.urllib.request, "urlopen", urlopen)
    pb._http("https://storage.googleapis.com/storage/v1/b/x/o")
    assert "authorization" not in captured and "x-goog-user-project" not in captured


def test_direct_staging_uses_curl_only():
    s = _src("s3://noaa-ghcn-pds")
    from deepresearch.sources.model import Manifest, ManifestEntry

    s.manifest = Manifest.build([ManifestEntry(path="ghcnd-states.txt", size=1)])
    b = staging_block([s], "~/drl")
    assert (
        "curl -fsSL --retry 3 --create-dirs -o" in b
        and "https://noaa-ghcn-pds.s3.amazonaws.com/ghcnd-states.txt" in b
    )
    assert "rclone" not in b and "gcloud" not in b and "aws " not in b
    assert s.effective_staging == "direct"


def test_guess_kind_public_vs_credentialed():
    assert guess_kind("s3://noaa-ghcn-pds") == "public_bucket"
    assert guess_kind("gs://gcp-public-data-landsat") == "public_bucket"
    assert guess_kind("s3://forsythc-hdd-bucket/x", "rclone:ceph") == "s3"
    assert guess_kind("gs://ucr-omnet-core-coldline-backup", "gcloud") == "gcs"
    assert guess_kind("https://noaa-ghcn-pds.s3.amazonaws.com/") == "public_bucket"
    assert guess_kind("https://x.org/data.csv") == "web"
    assert pb.parse_uri("https://storage.googleapis.com/bk/p/q") == ("gs", "bk", "p/q")


AWS_ROWS = [
    {"Name": "NOAA GHCN", "Slug": "noaa-ghcn", "Description": "daily weather", "Tags": ["weather"],
     "License": "CC0", "Resources": [{"Type": "S3 Bucket", "ARN": "arn:aws:s3:::noaa-ghcn-pds", "Region": "us-east-1"}]},
    {"Name": "Paid weather", "Slug": "paid", "Description": "weather", "Tags": [],
     "Resources": [{"Type": "S3 Bucket", "ARN": "arn:aws:s3:::paid", "RequesterPays": True}]},
    {"Name": "Controlled weather", "Slug": "ctl", "Description": "weather", "Tags": [],
     "Resources": [{"Type": "S3 Bucket", "ARN": "arn:aws:s3:::ctl", "ControlledAccess": "https://x"}]},
    {"Name": "Old weather", "Slug": "old", "Deprecated": True, "Description": "weather",
     "Resources": [{"Type": "S3 Bucket", "ARN": "arn:aws:s3:::old"}]},
]  # fmt: skip


def test_aws_catalog_offers_only_free_buckets(monkeypatch):
    monkeypatch.setattr(
        cc, "_cached_text", lambda n, u: "\n".join(json.dumps(r) for r in AWS_ROWS)
    )
    out = cc.search_aws("weather")
    assert [r["id"] for r in out] == ["noaa-ghcn"]
    assert out[0]["buckets"] == [
        {"title": "noaa-ghcn-pds", "uri": "s3://noaa-ghcn-pds", "region": "us-east-1"}
    ]


def test_gcp_and_earthengine_catalogs(monkeypatch, tmp_path):
    assert (
        cc.search_gcp("weather radar")[0]["buckets"][0]["uri"]
        == "gs://gcp-public-data-nexrad-l2"
    )
    assert cc.search_gcp("zzzz-nothing") == []
    monkeypatch.setattr(
        cc,
        "_ee_collections",
        lambda: [
            {"id": "COPERNICUS/S5P/OFFL/L3_O3", "title": "Sentinel-5P OFFL O3", "description": "ozone",
             "keywords": ["o3"], "license": "proprietary", "providers": ["ESA"]},
        ],
    )  # fmt: skip
    ee = cc.search_earthengine("ozone")
    assert ee[0]["page"].endswith("/COPERNICUS_S5P_OFFL_L3_O3")
    assert (
        ee[0]["buckets"] == [] and ee[0]["files"] == [] and "not added" in ee[0]["note"]
    )


def test_cloud_catalogs_are_wired_into_discover(monkeypatch):
    from deepresearch.sources import discover as dc

    monkeypatch.setattr(
        cc,
        "search_aws",
        lambda q, n: [{"catalog": "aws", "title": "t", "files": [], "buckets": []}],
    )
    monkeypatch.setattr(cc, "search_gcp", lambda q, n: [])
    monkeypatch.setattr(cc, "search_earthengine", lambda q, n: [])
    out = dc.discover("x", ["aws", "gcp", "earthengine"])
    assert out["errors"] == {} and out["results"][0]["catalog"] == "aws"
    assert (
        set(dc.CATALOGS) >= {"aws", "gcp", "earthengine"}
        and "kaggle" not in dc.CATALOGS
    )

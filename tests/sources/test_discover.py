"""Open dataset discovery (catalog HTTP mocked)."""

import json

from deepresearch.sources import discover as dc

DATAGOV = {
    "results": [
        {
            "slug": "aqs-ozone-2008",
            "title": "AQS ambient observations: 2008 ozone",
            "description": "<p>Hourly ozone</p>",
            "organization": {"title": "EPA"},
            "dcat": {
                "license": "https://edg.epa.gov/EPA_Data_License.html",
                "distribution": [
                    {
                        "downloadURL": "https://x.gov/o3.csv",
                        "mediaType": "text/csv",
                        "title": "CSV",
                    },
                    {"title": "no link"},
                ],
            },
        }
    ]
}
ZENODO = {
    "hits": {
        "hits": [
            {
                "id": 42,
                "doi_url": "https://doi.org/10.5281/zenodo.42",
                "metadata": {
                    "title": "Smoke O3",
                    "description": "2003&ndash;2022 at 10&deg; W",
                    "creators": [{"name": "A"}, {"name": "B"}],
                    "license": {"id": "cc-by-4.0"},
                },
                "files": [
                    {
                        "key": "o3.rds",
                        "size": 9,
                        "links": {"self": "https://zenodo.org/f/o3.rds"},
                    }
                ],
            }
        ]
    }
}
HF_LIST = [{"id": "u/air", "author": "u"}, {"id": "u/secret", "gated": "auto"}]
HF_DETAIL = {
    "id": "u/air",
    "tags": ["license:mit"],
    "siblings": [{"rfilename": "README.md"}, {"rfilename": "data/train.csv"}],
}


def fake_get(url, headers=None):
    if "datagov" in url:
        assert headers and headers.get("X-Api-Key")
        return DATAGOV
    if "zenodo" in url:
        return ZENODO
    if url.startswith("https://huggingface.co/api/datasets?"):
        return HF_LIST
    if url.endswith("/u/air"):
        return HF_DETAIL
    raise AssertionError(url)


def test_discover_normalizes_all_catalogs(monkeypatch):
    monkeypatch.setattr(dc, "_get", fake_get)
    out = dc.discover("ozone")
    assert out["errors"] == {}
    by = {r["catalog"]: r for r in out["results"]}
    g = by["datagov"]
    assert g["page"] == "https://catalog.data.gov/dataset/aqs-ozone-2008"
    assert g["description"] == "Hourly ozone" and g["publisher"] == "EPA"
    assert [f["url"] for f in g["files"]] == ["https://x.gov/o3.csv"]
    z = by["zenodo"]
    assert z["license"] == "cc-by-4.0" and z["files"][0]["url"].endswith("o3.rds")
    assert z["description"] == "2003\u20132022 at 10\u00b0 W"  # entities decoded
    h = by["huggingface"]
    assert h["license"] == "mit"
    assert [f["url"] for f in h["files"]] == [
        "https://huggingface.co/datasets/u/air/resolve/main/data/train.csv"
    ]
    assert (
        len([r for r in out["results"] if r["catalog"] == "huggingface"]) == 1
    )  # gated skipped


def test_a_failing_catalog_is_reported_not_fatal(monkeypatch):
    def flaky(url, headers=None):
        if "zenodo" in url:
            raise OSError("503 Service Unavailable")
        return fake_get(url, headers)

    monkeypatch.setattr(dc, "_get", flaky)
    out = dc.discover("ozone", ["datagov", "zenodo"])
    assert (
        "503" in out["errors"]["zenodo"] and out["results"][0]["catalog"] == "datagov"
    )


def test_datagov_uses_configured_key(monkeypatch):
    seen = {}

    def get(url, headers=None):
        seen.update(headers or {})
        return {"results": []}

    monkeypatch.setattr(dc, "_get", get)
    monkeypatch.setenv("DATA_GOV_API_KEY", "mykey")
    dc.search_datagov("x")
    assert seen["X-Api-Key"] == "mykey"


def test_cli_and_api_discover(monkeypatch, capsys, tmp_path):
    from deepresearch.__main__ import main
    from deepresearch.dashboard import server as srv

    monkeypatch.setattr(dc, "_get", fake_get)
    monkeypatch.setattr(
        "sys.argv",
        ["dr", "sources", "discover", "ozone", "--json",
         "--catalog", "datagov", "--catalog", "zenodo", "--catalog", "huggingface"],
    )  # fmt: skip
    try:
        main()
    except SystemExit as e:
        assert e.code in (0, None)
    out = json.loads(capsys.readouterr().out)
    assert {r["catalog"] for r in out["results"]} == {
        "datagov",
        "zenodo",
        "huggingface",
    }

    api = srv.Api(str(tmp_path / "h.db"), spawn=lambda *a: 1)
    res = api.sources_discover({"q": ["ozone"], "catalog": ["zenodo"]}, None)
    assert [r["catalog"] for r in res["results"]] == ["zenodo"]
    try:
        api.sources_discover({"q": ["o"]}, None)
        raise AssertionError("short query accepted")
    except srv.ApiError as e:
        assert e.status == 400


def test_slug():
    assert dc.slug("AQS ambient: 2008 Ozone!") == "aqs-ambient-2008-ozone"
    assert dc.slug("***") == "dataset"


def test_clip_strips_markdown_links():
    assert (
        dc._clip("[CC BY 4.0](https://x) and [HERE](https://y)") == "CC BY 4.0 and HERE"
    )

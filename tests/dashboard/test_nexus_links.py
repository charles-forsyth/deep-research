"""v0.62.0: Nexus project links (read-only). Fixtures are real nexus_search /
nexus_labs_show reply shapes (2026-10-03), trimmed."""

from __future__ import annotations

import json

import pytest

from deepresearch.dashboard import nexus as nx
from tests.dashboard.test_server import app  # noqa: F401  (fixture)

SEARCH = [
    {"name": "Christian Shelton", "netid": "cshelton", "_type": "Researcher"},
    {"summary": "Responded to ticket RITM0335701 for Kristian Shelton", "id": "i1", "_type": "Interaction"},
    {"summary": "Follow up with Matthew Barth", "id": "t1", "_type": "Task"},
    {"description": "Research lab led by Boris Baer", "name": "Boris Baer Lab (borisbar)", "id": "u1", "_type": "Lab"},
    {"agency": "National Science Foundation (NSF)", "c_number": "2502990", "title": "NSF CC* Research Networking", "id": "g1", "_type": "Grant"},
    {"project_id": "ucr-ursa-major-godzik-lab", "name": "UCR-Ursa-Major-Godzik-Lab", "id": "p1", "_type": "GCPProject"},
    {"summary": "Christidis Lab AI Research", "name": "ucr-ursa-major-christidis-lab", "id": "r1", "_type": "ResearchProject"},
]  # fmt: skip
LAB_SHOW = {
    "unit": {"description": "Research lab led by Boris Baer", "name": "Boris Baer Lab (borisbar)", "_type": "Lab"},
    "connections_by_type": {"PI_OF": 1, "LEADS": 1, "MEMBER_OF": 18, "PARTICIPATED_IN": 3},
    "connections": [
        {"via": {"type": "PARTICIPATED_IN"}, "neighbor": {"_type": "Interaction", "display": "SECRET meeting notes about sensor datasets"}},
        {"via": {"type": "LEADS"}, "neighbor": {"_type": "Interaction", "display": "SECRET led-by interaction"}},
        {"via": {"type": "LEADS"}, "neighbor": {"_type": "Researcher", "display": "Boris Baer"}},
        {"via": {"type": "PI_OF"}, "neighbor": {"_type": "Researcher", "display": "Boris Baer"}},
        {"via": {"type": "MEMBER_OF"}, "neighbor": {"_type": "Researcher", "display": "Ana Paulino"}},
        {"via": {"type": "OPERATES"}, "neighbor": {"_type": "GCPProject", "display": "ucr-ursa-major-baer-lab"}},
    ],
}  # fmt: skip


class Fake:
    url = "https://nexus.test"

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def call(self, tool, args=None):
        self.calls.append((tool, args))
        return json.dumps(self.answers[tool])  # Nexus tools answer JSON text

    def signed_in(self):
        return True


def test_search_keeps_only_linkable_entities_and_never_private_text():
    c = Fake({"nexus_search": SEARCH})
    rows = nx.search(c, "x")
    assert c.calls == [("nexus_search", {"term": "x", "limit": 25})]
    assert [(r["kind"], r["id"]) for r in rows] == [
        ("lab", "Boris Baer Lab (borisbar)"),
        ("grant", "2502990"),
        ("gcp", "ucr-ursa-major-godzik-lab"),
        ("project", "ucr-ursa-major-christidis-lab"),
    ]
    flat = json.dumps(rows)
    assert (
        "RITM0335701" not in flat
        and "Matthew Barth" not in flat
        and "cshelton" not in flat
    )
    assert rows[1]["name"] == "NSF CC* Research Networking (2502990)"


def test_show_lists_pi_members_and_links_but_skips_interactions():
    nx._cache.clear()
    c = Fake({"nexus_labs_show": LAB_SHOW})
    d = nx.show(c, "lab", "Boris Baer Lab (borisbar)")
    assert c.calls == [("nexus_labs_show", {"name": "Boris Baer Lab (borisbar)"})]
    assert d["name"] == "Boris Baer Lab (borisbar)"
    assert "PI: Boris Baer" in d["lines"] and "Members: 18" in d["lines"]
    assert "Linked: ucr-ursa-major-baer-lab" in d["lines"]
    assert "SECRET" not in json.dumps(d)
    nx.show(c, "lab", "Boris Baer Lab (borisbar)")
    assert len(c.calls) == 1  # cached
    with pytest.raises(ValueError):
        nx.show(c, "person", "x")


def test_grant_show_reads_the_grant_by_c_number():
    nx._cache.clear()
    g = {"grant": {"title": "CC* Data Storage", "agency": "NSF", "start_date": "2024-02-01", "end_date": "2026-01-31", "c_number": "2346636"}, "connections": []}  # fmt: skip
    c = Fake({"nexus_grants_show": g})
    d = nx.show(c, "grant", "2346636")
    assert c.calls[0] == ("nexus_grants_show", {"c_number": "2346636"})
    assert "Sponsor: NSF" in d["lines"] and "Ends: 2026-01-31" in d["lines"]


def test_api_routes_need_sign_in_and_validate(app, monkeypatch):  # noqa: F811
    api = app["api"]
    c = Fake({"nexus_search": SEARCH, "nexus_labs_show": LAB_SHOW})
    c.signed_in = lambda: False
    api._nexus_client = c
    assert app["call"]("GET", "/api/nexus/status")[1] == {"signed_in": False}
    st, body = app["call"]("GET", "/api/nexus/search?q=baer")
    assert st == 409 and "nexus login" in body["error"]
    c.signed_in = lambda: True
    st, body = app["call"]("GET", "/api/nexus/search?q=baer")
    assert st == 200 and len(body["results"]) == 4
    assert app["call"]("GET", "/api/nexus/search?q=b")[1] == {"results": []}
    nx._cache.clear()
    st, d = app["call"](
        "GET", "/api/nexus/show?kind=lab&id=Boris%20Baer%20Lab%20(borisbar)"
    )
    assert st == 200 and "PI: Boris Baer" in d["lines"]
    assert app["call"]("GET", "/api/nexus/show?kind=person&id=x")[0] == 400
    assert app["call"]("GET", "/api/nexus/show?kind=lab&id=")[0] == 400


def test_nexus_is_a_known_command_so_it_never_launches_research():
    import inspect

    import deepresearch.__main__ as m

    assert '"nexus",' in inspect.getsource(m.main)

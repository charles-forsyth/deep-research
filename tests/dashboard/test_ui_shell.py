"""v0.54.0 (U1) shell: report titles, the cluster sign-in endpoint, and the action registry."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from deepresearch.dashboard.store import report_title
from tests.dashboard.test_server import _seed, app  # noqa: F401  (fixture)

STATIC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "deepresearch"
    / "dashboard"
    / "static"
)


@pytest.mark.parametrize(
    ("md", "want"),
    [
        ("# Ceph at Scale\n\nBody", "Ceph at Scale"),
        ("\n\n## **Bold** title  \ntext", "Bold title"),
        ("Intro line\n# Late heading", None),
        ("```\n# not a heading\n```", None),
        ("", None),
        (None, None),
        ("# " + "x" * 300, "x" * 160),
        ("# Title with [a link](https://example.com)", "Title with a link"),
    ],
)
def test_report_title(md, want):
    assert report_title(md) == want


def test_session_rows_carry_the_report_title(app):  # noqa: F811
    a = _seed(app["api"], "what is ceph", "# Ceph, Explained\n\nText.")
    b = _seed(app["api"], "no heading here", "Plain text report.")
    st, out = app["call"]("GET", "/api/sessions")
    assert st == 200
    rows = {s["id"]: s for s in out["sessions"]}
    assert rows[a]["title"] == "Ceph, Explained"
    assert rows[b]["title"] is None
    assert "head" not in rows[a]  # the raw report prefix never leaves the server
    assert app["call"]("GET", f"/api/sessions/{a}")[1]["title"] == "Ceph, Explained"


def test_cluster_status_without_bifrost(app):  # noqa: F811
    st, out = app["call"]("GET", "/api/cluster/status")
    assert st == 200 and out["signed_in"] is False and out["configured"] is False


def test_cluster_status_signed_in_never_leaks_tokens(app):  # noqa: F811
    class FakeBifrost:
        calls = 0

        def whoami(self):
            FakeBifrost.calls += 1
            return {
                "email": "pi@ucr.edu",
                "program": "bifrost-deep-research",
                "tiers": ["read"],
                "own_caps": {"max_cost_usd_per_day": 75},
                "access_token": "SECRET",
            }

    app["api"].lab.bifrost = FakeBifrost()
    _, out = app["call"]("GET", "/api/cluster/status")
    _, again = app["call"]("GET", "/api/cluster/status")
    assert out["signed_in"] and out["configured"]
    assert out["email"] == "pi@ucr.edu" and out["program"] == "bifrost-deep-research"
    assert "SECRET" not in json.dumps(out)
    assert again == out and FakeBifrost.calls == 1  # cached for 5 minutes


# ---- static checks on the action registry (REQ-DASH-13) -------------------------------


def _registry_ids():
    src = (STATIC / "actions.js").read_text()
    return re.findall(r'\{\s*id:\s*"([a-z0-9-]+)"', src)


def test_registry_ids_are_unique_and_all_handled():
    ids = _registry_ids()
    assert len(ids) >= 20 and len(ids) == len(set(ids))
    js = (STATIC / "app.js").read_text()
    acts = (STATIC / "actions.js").read_text()
    # report actions are handled in the reader's handler table; app-wide ones in actions.js
    end = js.index('ACT.bind("report"')
    reader = js[
        js.rindex("const handlers = {", 0, end) : js.index(
            "const avail = ", js.rindex("const handlers = {", 0, end)
        )
    ]
    report_ids = re.findall(r'\{\s*id:\s*"([a-z0-9-]+)"[^}]*scope:\s*"report"', acts)
    assert len(report_ids) >= 15
    missing = [
        i
        for i in report_ids
        if not re.search(rf'(^|[\s{{,])"?{re.escape(i)}"?\s*:', reader, re.M)
    ]
    assert not missing, f"report actions with no handler: {missing}"


def test_every_old_report_toolbar_action_is_still_registered():
    """Each button the pre-0.54 report toolbar showed must still exist as an action."""
    ids = set(_registry_ids())
    for old in [
        "star",
        "copy",
        "to-nb",
        "find",
        "listen",
        "ws-copy",
        "tree",
        "rerun",
        "delete",
        "export-md",
        "export-html",
        "export-json",
        "print",
        "brief",
        "compare",
        "project",
        "lab",
        "ask",
        "audio-full",
        "audio-summary",
        "info",
    ]:
        assert old in ids, old


def test_palette_lists_page_actions():
    js = (STATIC / "app.js").read_text()
    i = js.index("function openPalette")
    rows = js[
        js.rindex("function paletteRows", 0, i)
        if "function paletteRows" in js[:i]
        else i - 1500 : i
    ]
    assert "ACT.paletteRows()" in rows, (
        "the command palette must draw page actions from the registry"
    )
    acts = (STATIC / "actions.js").read_text()
    assert "paletteRows()" in acts and "this.ctx" in acts


def test_shell_has_no_permanent_status_bar_or_telemetry():
    html = (STATIC / "index.html").read_text()
    assert 'class="statusbar"' not in html
    assert (
        'id="telemetry"' not in html or "hidden" in html.split('id="telemetry"')[1][:40]
    )

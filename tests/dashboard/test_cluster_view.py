"""v0.57.0: the Lab's Cluster view (bifrost panels, cached) and the report's cluster jobs
on the Live log tab."""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from pathlib import Path

from deepresearch.dashboard import clusterview as cv
from tests.dashboard.test_server import app  # noqa: F401  (fixture)

STATIC = Path(cv.__file__).parent / "static"


class Client:
    def __init__(self, answers=None, delay=0.0):
        self.calls: list[tuple[str, dict]] = []
        self.answers = answers or {}
        self.delay = delay

    def call(self, tool, args=None):
        self.calls.append((tool, args))
        if self.delay:
            time.sleep(self.delay)
        a = self.answers.get(tool, {"ok": tool})
        if isinstance(a, Exception):
            raise a
        return a


def test_panel_is_cached_for_its_ttl_and_refreshed_in_the_background():
    now = [1000.0]
    v = cv.ClusterView(now=lambda: now[0])
    c = Client({"cluster_status": {"nodes_powered_up": 1}})
    a = v.get("now", c)
    assert a["data"] == {"nodes_powered_up": 1} and len(c.calls) == 1
    b = v.get("now", c)  # within the TTL: no second read
    assert b["data"] == a["data"] and len(c.calls) == 1
    now[0] += cv.PANELS["now"][2] + 1
    c.answers["cluster_status"] = {"nodes_powered_up": 2}
    v.get("now", c, wait=0)  # stale: answered from the cache, refreshed behind it
    for _ in range(50):
        if len(c.calls) == 2 and not v._busy:
            break
        time.sleep(0.01)
    assert len(c.calls) == 2 and v.get("now", c)["data"] == {"nodes_powered_up": 2}


def test_slow_first_read_answers_loading_and_reads_once():
    v = cv.ClusterView()
    c = Client({"storage_usage": {"home_bytes": 5}}, delay=0.3)
    first = v.get("storage", c, wait=0.01)
    again = v.get("storage", c, wait=0.01)  # the read in flight is not started twice
    assert first == {"loading": True} and again == {"loading": True}
    time.sleep(0.4)
    assert v.get("storage", c)["data"] == {"home_bytes": 5}
    assert [t for t, _ in c.calls] == ["storage_usage"]


def test_failed_read_keeps_the_last_answer_and_says_so():
    now = [0.0]
    v = cv.ClusterView(now=lambda: now[0])
    c = Client({"my_usage": {"rows": [1]}})
    v.get("usage", c)
    now[0] += 10_000
    c.answers["my_usage"] = RuntimeError("cannot reach bifrost")
    v.get("usage", c, wait=0)
    for _ in range(50):
        if not v._busy:
            break
        time.sleep(0.01)
    r = v.get("usage", c)
    assert r["data"] == {"rows": [1]} and "cannot reach" in r["error"]


def test_panels_only_call_read_tools():
    reads = {"cluster_status", "jobs_list", "my_usage", "waste_report", "storage_usage"}
    assert {tool for tool, _, _ in cv.PANELS.values()} <= reads


def test_lab_summary_names_every_job_and_counts_outcomes():
    now = datetime(2026, 10, 3, 12, 0)
    runs = [
        {"id": 7, "session_id": 3, "status": "completed", "created_at": "2026-10-02T10:00:00",
         "plan": {"title": "Poultry"}, "estimate_usd": 0.4, "ai_cost_usd": 0.05,
         "submitted_at": "x", "job_id": "501", "assessment": {"outcome": "confirmed"},
         "cluster_jobs": [{"job_id": "499", "why": "pilot round 1"}, {"job_id": "501", "why": "full"}]},
        {"id": 8, "session_id": 3, "status": "smoke", "created_at": "2026-10-03T09:00:00",
         "plan": {"title": "Eggs"}, "smoke": {"job_id": "505"}, "stage": "Pilot"},
        {"id": 2, "session_id": 1, "status": "failed", "created_at": "2026-08-01T00:00:00",
         "job_id": "40", "assessment": {"outcome": "broken"}},
    ]  # fmt: skip
    s = cv.lab_summary(runs, days=30, now=now)
    assert (
        s["jobs"]["499"]["why"] == "pilot round 1" and s["jobs"]["501"]["run_id"] == 7
    )
    assert s["jobs"]["505"]["why"] == "pilot" and s["jobs"]["40"]["run_id"] == 2
    assert s["runs"] == 2 and s["outcome"] == {"confirmed": 1}  # the August run is out
    assert [x["run_id"] for x in s["live"]] == [8]
    assert s["estimate_usd"] == 0.4 and s["ai_usd"] == 0.05


def test_cluster_routes(app):  # noqa: F811
    st, out = app["call"]("GET", "/api/cluster/panel/now")
    assert st == 200 and out == {"signed_in": False}
    st, _ = app["call"]("GET", "/api/cluster/panel/nope")
    assert st == 404
    c = Client({"cluster_status": {"jobs_running": 3}})
    app["api"].lab.bifrost = c
    cv.VIEW._cache.clear()
    st, out = app["call"]("GET", "/api/cluster/panel/now")
    assert st == 200 and out["data"] == {"jobs_running": 3}
    st, lab = app["call"]("GET", "/api/cluster/lab?days=7")
    assert st == 200 and lab["days"] == 7 and "jobs" in lab


def test_live_log_tab_shows_the_reports_cluster_jobs():
    js = (STATIC / "app.js").read_text()
    i = js.index('} else if (S.rtab === "log") {')
    block = js[i : js.index("\n  }\n}", i)]
    assert 'id="lablive"' in block and "LABLIVE.start(s)" in block
    assert "LABLIVE.stop()" in js[js.index("function stopLog()") :][:200]
    cl = (STATIC / "cluster.js").read_text()
    assert "/api/sessions/${s.id}/lab" in cl and "cluster_jobs" in cl


def test_cluster_view_is_reachable_everywhere():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    assert 'data-nav="cluster"' in html and '<script src="/cluster.js">' in html
    assert html.index("/cluster.js") < html.index("/app.js")
    assert re.search(r"cluster:\s*\(v\)\s*=>\s*CLV\.render\(v\)", js)
    assert '{ id: "cluster", scope: "app"' in (STATIC / "actions.js").read_text()
    assert "openCluster()" in (STATIC / "lab.js").read_text()


def test_cluster_js_builds_html_only_through_esc():
    cl = (STATIC / "cluster.js").read_text()
    # every interpolated cluster string in markup goes through esc() or a number helper
    for name in ("j.name", "x.path", "f.mount", "x.key", "p.name", "j.partition"):
        for m in re.finditer(r"\$\{([^}]*" + re.escape(name) + r"[^}]*)\}", cl):
            assert "esc(" in m.group(1) or "clip(" in m.group(1), m.group(0)


def test_view_threads_do_not_pile_up():
    v = cv.ClusterView()
    c = Client({"jobs_list": []}, delay=0.05)
    ts = [threading.Thread(target=v.get, args=("jobs", c)) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(c.calls) == 1


def test_cluster_page_lists_jobs_that_held_too_many_cores():
    cl = (STATIC / "cluster.js").read_text()
    assert "oversized(wd, labJobs)" in cl and "CLV.oversized(wd, labJobs)" in cl
    i = cl.index("  oversized(wd, labJobs) {")
    block = cl[i : cl.index("\n  },\n", i)]
    assert 'x.kind === "low-cpu"' in block and "* 1.5" in block
    for m in re.finditer(
        r"\$\{([^}]*(?:x\.partition|l\.title|x\.job_id)[^}]*)\}", block
    ):
        assert "esc(" in m.group(1) or "clip(" in m.group(1), m.group(0)

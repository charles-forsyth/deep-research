"""Dashboard API tests: real HTTP server on an ephemeral port, temp SQLite DB."""

import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from deepresearch.dashboard import server as srv
from deepresearch.dashboard.server import Api, estimate, make_handler


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(srv, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    spawned = []

    def fake_spawn(args, log_path):
        spawned.append((args, log_path))
        return 4242

    api = Api(str(tmp_path / "history.db"), spawn=fake_spawn)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(method, path, body=None, ctype="application/json", headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method)
        if method != "GET" and ctype:
            req.add_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return r.status, (
                    json.loads(raw)
                    if r.headers.get_content_type() == "application/json"
                    else raw
                )
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    yield {
        "call": call,
        "api": api,
        "spawned": spawned,
        "tmp": tmp_path,
        "port": httpd.server_address[1],
    }
    httpd.shutdown()
    httpd.server_close()


def _seed(
    api,
    prompt="Quantum error correction",
    result="Surface codes are good. Surface codes scale.",
):
    sid = api.sessions.create_session(f"iid_{prompt[:5]}", prompt)
    api.sessions.update_session(f"iid_{prompt[:5]}", "completed", result)
    return sid


def test_index_and_static_served(app):
    status, body = app["call"]("GET", "/")
    assert status == 200 and b"Deep Research" in body
    status, body = app["call"]("GET", "/app.js")
    assert status == 200 and b"boot" in body
    status, body = app["call"]("GET", "/vendor/purify.min.js")
    assert status == 200 and b"DOMPurify" in body


def test_static_path_traversal_falls_back_to_index(app):
    status, body = app["call"]("GET", "/../../../../etc/passwd")
    assert status == 200 and b"<!doctype html>" in body.lower()
    assert b"root:" not in body


def test_health_and_stats(app):
    _seed(app["api"])
    status, h = app["call"]("GET", "/api/health")
    assert status == 200 and h["ok"] and h["api_key"] is True
    status, st = app["call"]("GET", "/api/stats")
    assert st["total"] == 1 and st["by_status"]["completed"] == 1


def test_sessions_list_filter_and_detail(app):
    a = _seed(app["api"], "Quantum error correction")
    _seed(app["api"], "Solid state batteries", "Lithium metal anodes.")
    status, data = app["call"]("GET", "/api/sessions?q=anodes")
    assert [s["prompt"] for s in data["sessions"]] == ["Solid state batteries"]
    status, s = app["call"]("GET", f"/api/sessions/{a}")
    assert status == 200 and s["result"].startswith("Surface")
    assert s["meta"] == {"starred": False, "tags": []}
    assert "embedding" not in s
    assert app["call"]("GET", "/api/sessions/999")[0] == 404


def test_meta_star_and_tags(app):
    sid = _seed(app["api"])
    status, m = app["call"](
        "PATCH",
        f"/api/sessions/{sid}/meta",
        {"starred": True, "tags": ["qec", " ", "physics"]},
    )
    assert m == {"starred": True, "tags": ["qec", "physics"]}
    _, data = app["call"]("GET", "/api/sessions")
    assert data["sessions"][0]["starred"] is True


def test_annotation_lifecycle_and_export(app):
    sid = _seed(app["api"])
    status, a = app["call"](
        "POST",
        "/api/annotations",
        {"session_id": sid, "quote": "Surface codes", "occurrence": 1, "color": "cyan"},
    )
    assert status == 200 and a["occurrence"] == 1 and a["color"] == "cyan"
    _, a2 = app["call"]("PATCH", f"/api/annotations/{a['id']}", {"note": "key claim"})
    assert a2["note"] == "key claim"
    _, exp = app["call"]("GET", f"/api/sessions/{sid}/export?format=md")
    assert "## Annotations" in exp["content"] and "key claim" in exp["content"]
    _, j = app["call"]("GET", f"/api/sessions/{sid}/export?format=json")
    assert j["content"]["annotations"][0]["note"] == "key claim"
    assert app["call"]("DELETE", f"/api/annotations/{a['id']}")[0] == 200
    assert (
        app["call"]("POST", "/api/annotations", {"session_id": sid, "quote": " "})[0]
        == 400
    )


def test_bad_color_is_normalised(app):
    sid = _seed(app["api"])
    _, a = app["call"](
        "POST",
        "/api/annotations",
        {"session_id": sid, "quote": "x", "color": "<script>"},
    )
    assert a["color"] == "amber"


def test_notebook_crud(app):
    status, nb = app["call"]("POST", "/api/notebooks", {"title": "Findings"})
    assert status == 200 and nb["content"] == ""
    _, nb2 = app["call"]("PUT", f"/api/notebooks/{nb['id']}", {"content": "> quote"})
    assert nb2["content"] == "> quote" and nb2["title"] == "Findings"
    _, lst = app["call"]("GET", "/api/notebooks")
    assert lst["notebooks"][0]["chars"] == 7
    assert app["call"]("DELETE", f"/api/notebooks/{nb['id']}")[0] == 200
    assert app["call"]("GET", f"/api/notebooks/{nb['id']}")[0] == 404


def test_start_research_spawns_detached_cli(app):
    status, r = app["call"](
        "POST",
        "/api/research",
        {
            "prompt": "History of lidar",
            "depth": 1,
            "breadth": 3,
            "format": "table",
            "stores": ["fileSearchStores/abc"],
        },
    )
    assert status == 200 and r["pid"] == 4242
    args, log_path = app["spawned"][0]
    assert args[:4] == ["research", "History of lidar", "--adopt-session", str(r["id"])]
    assert (
        "--stream" in args
        and args[args.index("--stores") + 1] == "fileSearchStores/abc"
    )
    assert log_path.name == f"session_{r['id']}.log"
    s = app["api"].sessions.get_session(str(r["id"]))
    assert s["pid"] == 4242 and s["status"] == "running"


def test_start_research_recursive_has_no_stream(app):
    app["call"]("POST", "/api/research", {"prompt": "x", "depth": 2, "breadth": 2})
    args, _ = app["spawned"][0]
    assert "--stream" not in args and args[args.index("--depth") + 1] == "2"


def test_start_research_validation(app, monkeypatch):
    call = app["call"]
    assert call("POST", "/api/research", {"prompt": ""})[0] == 400
    assert call("POST", "/api/research", {"prompt": "x", "depth": 9})[0] == 400
    assert (
        call("POST", "/api/research", {"prompt": "x", "uploads": ["/etc/passwd"]})[0]
        == 400
    )
    monkeypatch.delenv("GEMINI_API_KEY")
    status, err = call("POST", "/api/research", {"prompt": "x"})
    assert status == 400 and "GEMINI_API_KEY" in err["error"]
    assert app["spawned"] == []


def test_upload_then_research_with_upload(app):
    data = base64.b64encode(b"hello world").decode()
    status, up = app["call"](
        "POST", "/api/uploads", {"name": "../../evil name.txt", "data": data}
    )
    assert status == 200 and up["name"] == "evil_name.txt" and up["size"] == 11
    assert str(app["tmp"] / "uploads") in up["path"]
    _, r = app["call"](
        "POST", "/api/research", {"prompt": "summarize", "uploads": [up["path"]]}
    )
    args, _ = app["spawned"][0]
    assert args[args.index("--upload") + 1] == up["path"]


def test_json_content_type_required_for_writes(app):
    status, err = app["call"](
        "POST", "/api/notebooks", {"title": "x"}, ctype="text/plain"
    )
    assert status == 415


def test_delete_recursive_purges_children_and_annotations(app):
    api = app["api"]
    root = _seed(api, "root")
    child = api.sessions.create_session("iid_child", "child", parent_id=root, depth=2)
    grand = api.sessions.create_session("iid_grand", "grand", parent_id=child, depth=3)
    app["call"]("POST", "/api/annotations", {"session_id": root, "quote": "Surface"})
    status, r = app["call"]("DELETE", f"/api/sessions/{root}?recursive=1")
    assert sorted(r["deleted"]) == sorted([root, child, grand])
    assert api.store.list_annotations() == []
    assert api.sessions.get_session(str(grand)) is None


def test_tree_and_recursive_export(app):
    api = app["api"]
    root = _seed(api, "root topic", "root body")
    child = api.sessions.create_session("iid_c", "child topic", parent_id=root, depth=2)
    api.sessions.update_session("iid_c", "completed", "child body")
    _, tree = app["call"]("GET", f"/api/sessions/{root}/tree")
    assert tree["children"][0]["id"] == child
    _, exp = app["call"]("GET", f"/api/sessions/{root}/export?format=md&recursive=1")
    assert "root body" in exp["content"] and "child body" in exp["content"]


def test_log_tail_strips_ansi(app):
    sid = _seed(app["api"])
    log_dir = app["tmp"] / "logs"
    log_dir.mkdir()
    (log_dir / f"session_{sid}.log").write_text("\x1b[1;36m[INFO]\x1b[0m hello\n")
    _, r = app["call"]("GET", f"/api/sessions/{sid}/log")
    assert r["text"] == "[INFO] hello\n" and r["exists"]
    _, r2 = app["call"]("GET", f"/api/sessions/{sid}/log?offset={r['size']}")
    assert r2["text"] == ""


def test_cancel_only_running(app):
    sid = _seed(app["api"])
    assert app["call"]("POST", f"/api/sessions/{sid}/cancel")[0] == 409


def test_followup_requires_prompt(app):
    sid = _seed(app["api"])
    assert (
        app["call"]("POST", f"/api/sessions/{sid}/followup", {"prompt": ""})[0] == 400
    )


def test_unknown_route_and_method(app):
    assert app["call"]("GET", "/api/nope")[0] == 404
    assert app["call"]("PUT", "/api/stats", {})[0] == 405


def test_estimate_matches_cli_model():
    e = estimate(depth=2, breadth=3)
    assert e["nodes"] == 4  # 1 + 3
    cached = 4 * 250_000 * 0.6
    expected = (4 * 250_000 - cached) / 1e6 * 2 + cached / 1e6 * 0.2
    expected += 4 * 60_000 / 1e6 * 12
    assert e["cost_usd"] == round(expected, 2)
    assert 0.8 < estimate(1, 3)["cost_usd"] < 1.2  # ~$0.95 per agent run


def _running(api):
    return api.sessions.create_session("pending_start", "still running", pid=None)


def test_bodyless_cross_site_post_cannot_cancel(app):
    """K2: a page on another site could cancel runs with an empty form post."""
    api = app["api"]
    sid = _running(api)
    status, _ = app["call"](
        "POST",
        f"/api/sessions/{sid}/cancel",
        ctype="application/x-www-form-urlencoded",
    )
    assert status == 415
    status, _ = app["call"]("POST", f"/api/sessions/{sid}/cancel", ctype=None)
    assert status == 415
    assert api.sessions.get_session(str(sid))["status"] == "running"


def test_foreign_origin_write_refused(app):
    api = app["api"]
    sid = _running(api)
    status, err = app["call"](
        "POST",
        f"/api/sessions/{sid}/cancel",
        headers={"Origin": "http://evil.example"},
    )
    assert status == 403 and "Cross-origin" in err["error"]
    assert api.sessions.get_session(str(sid))["status"] == "running"


def test_same_origin_json_cancel_still_works(app):
    api = app["api"]
    sid = _running(api)
    status, r = app["call"](
        "POST",
        f"/api/sessions/{sid}/cancel",
        headers={"Origin": f"http://127.0.0.1:{app['port']}"},
    )
    assert status == 200 and r["status"] == "cancelled"


def test_dns_rebinding_host_refused(app):
    status, err = app["call"](
        "GET", "/api/health", headers={"Host": "evil.example.com"}
    )
    assert status == 403 and "DR_ALLOWED_HOSTS" in err["error"]
    status, _ = app["call"]("GET", "/", headers={"Host": "attacker.com:7420"})
    assert status == 403


@pytest.mark.parametrize(
    "host",
    [
        "localhost:7420",
        "127.0.0.1:7420",
        "192.168.1.50:7420",
        "[::1]:7420",
        "[fe80::1]:7420",
        "chuck-laptop:7420",
        "chuck-laptop.tail9eb9b1.ts.net:7420",
        "chuck-laptop.tail9eb9b1.ts.net.:7420",
        "desk.local",
        "desk.home.arpa",
    ],
)
def test_local_host_names_allowed(host):
    assert srv.host_allowed(host)


def test_allowed_hosts_env(monkeypatch):
    assert not srv.host_allowed("research.example.org")
    monkeypatch.setenv("DR_ALLOWED_HOSTS", "research.example.org, other.example")
    assert srv.host_allowed("research.example.org:443")
    assert not srv.host_allowed("")

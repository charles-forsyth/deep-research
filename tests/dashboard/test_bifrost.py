"""The bifrost MCP client and the Lab's R1 reads (v0.53.0).

A fake bifrost runs on a local port and speaks the same HTTP the hosted server does
(`/token`, `/mcp` JSON-RPC with JSON answers, `/whoami`, `/revoke`), so the real client
code runs end to end: token refresh and rotation, the 401 retry, tool errors, and the
Lab's use of stockouts, script_check, job_explain, job_show and files_read."""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from deepresearch.dashboard import bifrost as bf


class Fake:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.refreshes = 0
        self.valid = {"at-1"}
        self.refresh_ok = {"rt-1"}
        self.revoked: list[str] = []
        self.answers: dict = {}
        self.errors: dict = {}
        self.force_401 = 0


def make_server(fake: Fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _auth(self):
            tok = self.headers.get("Authorization", "")[7:]
            if fake.force_401:
                fake.force_401 -= 1
                return False
            return tok in fake.valid

        def do_GET(self):
            if self.path == "/whoami":
                if not self._auth():
                    return self._json(401, {"error": "invalid_token"})
                return self._json(200, {"email": "chuck@ucr.edu", "tiers": ["R1", "A1"],
                                        "program": "bifrost-deep-research",
                                        "own_caps": {"max_cost_usd_per_day": 75}})  # fmt: skip
            self._json(404, {})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            if self.path == "/token":
                f = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                fake.refreshes += 1
                time.sleep(0.05)  # widen the window for a refresh race
                if f.get("refresh_token") not in fake.refresh_ok:
                    return self._json(400, {"error": "invalid_grant"})
                fake.refresh_ok.discard(f["refresh_token"])  # rotation: single use
                k = fake.refreshes + 1
                fake.valid = {f"at-{k}"}
                fake.refresh_ok.add(f"rt-{k}")
                return self._json(200, {"access_token": f"at-{k}", "refresh_token": f"rt-{k}",
                                        "expires_in": 3600, "token_type": "Bearer"})  # fmt: skip
            if self.path == "/revoke":
                fake.revoked.append(parse_qs(raw.decode()).get("token", [""])[0])
                self.send_response(200)
                self.end_headers()
                return
            if self.path == "/mcp":
                if not self._auth():
                    return self._json(401, {"error": "invalid_token"})
                msg = json.loads(raw)
                p = msg["params"]
                if msg["method"] == "resources/read":
                    fake.calls.append(("resource", p))
                    text = json.dumps(fake.answers.get(p["uri"], {}))
                    return self._json(200, {"jsonrpc": "2.0", "id": msg["id"],
                                            "result": {"contents": [{"uri": p["uri"], "text": text}]}})  # fmt: skip
                name, args = p["name"], p.get("arguments") or {}
                fake.calls.append((name, args))
                if name in fake.errors:
                    res = {
                        "content": [{"type": "text", "text": fake.errors[name]}],
                        "isError": True,
                    }
                else:
                    ans = fake.answers.get(name, {})
                    ans = ans(args) if callable(ans) else ans
                    env = {"data": ans, "source": {"backend": "fake"}, "as_of": "now"}
                    res = {"content": [{"type": "text", "text": json.dumps(env)}]}
                return self._json(
                    200, {"jsonrpc": "2.0", "id": msg["id"], "result": res}
                )
            self._json(404, {})

    return H


@pytest.fixture
def server(tmp_path):
    fake = Fake()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_server(fake))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    (tmp_path / bf.TOKEN_FILE).write_text(
        json.dumps(
            {
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "client_id": bf.CLIENT_ID,
                "_exp": time.time() + 3000,
            }
        )  # fmt: skip
    )
    yield fake, bf.BifrostClient(tmp_path, url=url), tmp_path
    httpd.shutdown()
    httpd.server_close()


def test_call_returns_the_envelope_data(server):
    fake, c, _ = server
    fake.answers["partitions"] = [{"name": "computehigh"}]
    assert c.call("partitions") == [{"name": "computehigh"}]
    assert fake.calls == [("partitions", {})]


def test_tool_errors_raise_with_the_message(server):
    fake, c, _ = server
    fake.errors["job_show"] = "job 9 not found"
    with pytest.raises(bf.BifrostError, match="job 9 not found"):
        c.call("job_show", {"job_id": "9"})


def test_expired_access_token_is_refreshed_and_the_rotated_pair_saved(server):
    fake, c, sd = server
    tok = json.loads((sd / bf.TOKEN_FILE).read_text())
    tok["_exp"] = time.time() - 1
    (sd / bf.TOKEN_FILE).write_text(json.dumps(tok))
    fake.answers["partitions"] = []
    c.call("partitions")
    saved = json.loads((sd / bf.TOKEN_FILE).read_text())
    assert (saved["access_token"], saved["refresh_token"]) == ("at-2", "rt-2")
    assert oct(os.stat(sd / bf.TOKEN_FILE).st_mode & 0o777) == "0o600"


def test_a_401_refreshes_once_and_retries(server):
    fake, c, _ = server
    fake.force_401 = 1
    fake.answers["partitions"] = ["ok"]
    assert c.call("partitions") == ["ok"] and fake.refreshes == 1


def test_parallel_refreshes_use_the_refresh_token_once(server):
    """Refresh tokens rotate: two threads refreshing at once would burn the token."""
    fake, c, sd = server
    tok = json.loads((sd / bf.TOKEN_FILE).read_text())
    tok["_exp"] = time.time() - 1
    (sd / bf.TOKEN_FILE).write_text(json.dumps(tok))
    errs = []

    def go():
        try:
            c.access_token()
        except Exception as e:
            errs.append(e)

    ts = [threading.Thread(target=go) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs and fake.refreshes == 1


def test_no_token_means_not_signed_in(tmp_path):
    c = bf.BifrostClient(tmp_path, url="http://127.0.0.1:9")
    assert not c.signed_in()
    with pytest.raises(bf.NotSignedIn, match="cluster login"):
        c.call("partitions")


def test_refused_refresh_says_sign_in_again(server):
    fake, c, sd = server
    tok = json.loads((sd / bf.TOKEN_FILE).read_text())
    tok.update(_exp=time.time() - 1, refresh_token="rt-dead")
    (sd / bf.TOKEN_FILE).write_text(json.dumps(tok))
    with pytest.raises(bf.NotSignedIn, match="cluster login"):
        c.call("partitions")


def test_logout_revokes_and_deletes(server):
    fake, c, sd = server
    assert c.logout() is True
    assert not (sd / bf.TOKEN_FILE).exists() and set(fake.revoked) == {"at-1", "rt-1"}
    assert c.logout() is False


def test_stockouts_from_cluster_status(server):
    fake, c, _ = server
    fake.answers["cluster_status"] = {"partitions": [
        {"name": "computehigh", "problems": ["c3-1: DOWN+CLOUD (RESOURCE_POOL_EXHAUSTED in us-central1-a)"]},
        {"name": "standard", "problems": ["c2d-3: DRAIN (Kill task failed)"]},
        {"name": "lab", "problems": []},
    ]}  # fmt: skip
    assert bf.stockouts(c) == {"computehigh"}


def test_explain_maps_rules_to_lab_classes(server):
    fake, c, _ = server
    fake.answers["job_explain"] = {"findings": [
        {"rule": "memory-near-limit", "severity": "warning", "title": "near"},
        {"rule": "oom", "severity": "error", "title": "Out of memory", "suggestion": "ask for more"},
    ]}  # fmt: skip
    d = bf.explain(c, "77")
    assert (d["rule"], d["class"]) == ("oom", "oom") and len(d["findings"]) == 2
    fake.answers["job_explain"] = {
        "findings": [{"rule": "bad-arguments", "severity": "error"}]
    }
    assert bf.explain(c, "78")["class"] == "bad-arguments"


def test_catalog_checks_the_schema(server):
    fake, c, _ = server
    fake.answers["hpc://catalog"] = {"schema": "ursa-catalog/1", "partitions": []}
    assert bf.catalog(c)["schema"] == "ursa-catalog/1"
    fake.answers["hpc://catalog"] = {"hello": 1}
    with pytest.raises(bf.BifrostError, match="unknown format"):
        bf.catalog(c)


# ---- the Lab -----------------------------------------------------------------------


class FakeTarget:
    kind = "fake"
    name = "ursa"
    label = "Ursa"
    default_partition = "computehigh"
    partitions = {"computehigh": {"cpus": 22, "mem_gb": 85, "usd_per_hour": 1.87}}
    cfg = {"bifrost": {}}
    warm = {"partition": "computehigh"}
    remote_root = "~/deep-research-lab"
    catalog = None
    catalog_path = ""

    def describe(self):
        return "Ursa"

    def job_dir(self, run_id):
        return f"~/deep-research-lab/run_{run_id}"

    def run(self, cmd, timeout=None):
        raise AssertionError("SSH used while bifrost is signed in: " + cmd)

    def sh(self, cmd, timeout=None):
        raise AssertionError("SSH used while bifrost is signed in: " + cmd)


@pytest.fixture
def lab(server, tmp_path):
    from deepresearch.dashboard.lab import Lab

    fake, c, _ = server
    t = FakeTarget()
    lb = Lab(
        str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"ursa": t}, bifrost=c
    )
    lb.ensure_watcher = lambda: None
    return lb, fake, t


def test_stockout_check_goes_through_bifrost_not_ssh(lab):
    from deepresearch.dashboard import lab as labm

    lb, fake, t = lab
    fake.answers["cluster_status"] = {
        "partitions": [{"name": "computehigh", "problems": ["x (stockout)"]}]
    }
    assert labm.stocked_out_partitions(t, lb.bifrost) == {"computehigh"}


def test_bifrost_script_check_errors_become_preflight_warnings(lab):
    lb, fake, t = lab
    fake.answers["script_check"] = lambda a: {"issues": [
        {"severity": "error", "message": 'module "gromac" not found'},
        {"severity": "warning", "message": "counts every core"},
    ], "seen": len(a["script"])}  # fmt: skip
    plan = {"script": "python run.py\n", "resources": {"partition": "computehigh"}}
    w = lb.cluster_check_warnings(t, plan)
    assert w == ['Cluster check (bifrost): module "gromac" not found']
    sent = [a for n, a in fake.calls if n == "script_check"][0]["script"]
    assert sent.startswith("#!") and "#SBATCH" in sent and "python run.py" in sent


def test_bifrost_outage_never_blocks_planning(lab):
    lb, fake, t = lab
    fake.errors["script_check"] = "server busy"
    assert lb.cluster_check_warnings(t, {"script": "x", "resources": {}}) == []
    lb.bifrost = None
    assert lb.cluster_check_warnings(t, {"script": "x", "resources": {}}) == []


def test_ladder_notes_read_through_files_read(lab):
    lb, fake, t = lab
    line = json.dumps({"key": "k1", "rung": "isolated-venv", "run": "run_9"})

    def files_read(a):
        if a.get("bytes") == 1:
            return {"chunk": {"file_bytes": 200}}
        return {"untrusted": {"text": line + "\n"}}

    fake.answers["files_read"] = files_read
    assert "run_9: isolated-venv" in lb.env_notes(t)


def test_finished_job_gets_efficiency_and_diagnosis(lab):
    lb, fake, t = lab
    with lb._conn() as conn:
        conn.execute(
            "INSERT INTO lab_runs (session_id, scope, status, job_id, target) VALUES (1, 'document', 'failed', '470', 'ursa')"
        )
        conn.commit()
    fake.answers["job_show"] = {"cpus": 2, "restart_count": 1,
                                "efficiency": {"cpu_percent": 48.0, "mem_peak_mb": 900, "mem_alloc_mb": 7914}}  # fmt: skip
    fake.answers["job_explain"] = {
        "findings": [{"rule": "timeout", "severity": "error", "title": "Hit the limit"}]
    }
    run = lb.get(1)
    lb._record_cluster_facts(run, "failed")
    got = lb.get(1)["cluster"]
    assert (
        got["efficiency"]["cpu_percent"] == 48.0 and got["efficiency"]["restarts"] == 1
    )
    assert got["diagnosis"]["class"] == "timeout" and got["job_id"] == "470"


def test_completed_job_gets_efficiency_only_and_warm_tasks_are_skipped(lab):
    lb, fake, t = lab
    with lb._conn() as conn:
        conn.execute(
            "INSERT INTO lab_runs (session_id, scope, status, job_id) VALUES (1, 'document', 'completed', '471')"
        )
        conn.execute(
            "INSERT INTO lab_runs (session_id, scope, status, job_id) VALUES (1, 'document', 'completed', 'warm:full-5')"
        )
        conn.commit()
    fake.answers["job_show"] = {"efficiency": {"cpu_percent": 90.0}}
    lb._record_cluster_facts(lb.get(1), "completed")
    lb._record_cluster_facts(lb.get(2), "completed")
    assert "diagnosis" not in lb.get(1)["cluster"]
    assert lb.get(2)["cluster"] is None
    assert [n for n, _ in fake.calls] == ["job_show"]


def test_without_a_bifrost_block_the_lab_stays_on_ssh(tmp_path):
    from deepresearch.dashboard.lab import _bifrost_for

    class T:
        cfg: dict = {}

    (tmp_path / bf.TOKEN_FILE).write_text(json.dumps({"refresh_token": "x"}))
    assert _bifrost_for({"a": T()}, tmp_path) is None
    T.cfg = {"bifrost": {"url": "https://example.invalid"}}
    c = _bifrost_for({"a": T()}, tmp_path)
    assert c is not None and c.url == "https://example.invalid"
    (tmp_path / bf.TOKEN_FILE).unlink()
    assert _bifrost_for({"a": T()}, tmp_path) is None  # configured but not signed in


def test_catalog_comes_from_the_resource_when_signed_in(tmp_path, server):
    from deepresearch.dashboard.cluster import SlurmSSHTarget
    from deepresearch.dashboard.lab import Lab

    fake, c, _ = server
    fake.answers["hpc://catalog"] = {
        "schema": "ursa-catalog/1",
        "generated": "g1",
        "partitions": [],
    }
    t = SlurmSSHTarget({"name": "ursa", "catalog_path": "/apps/docs/catalog.json", "bifrost": {},
                        "partitions": {"computehigh": {"cpus": 22}}})  # fmt: skip
    t.sh = lambda *a, **k: (_ for _ in ()).throw(AssertionError("ssh used"))  # type: ignore[method-assign]
    Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"ursa": t}, bifrost=c)
    assert t.load_catalog(tmp_path, refresh=True)["generated"] == "g1"
    assert Path(tmp_path / "catalog-ursa.json").exists()


def test_cluster_cli_status_and_logout(server, capsys, monkeypatch):
    from types import SimpleNamespace

    from deepresearch.cli.cluster import handle

    fake, c, sd = server
    assert handle(SimpleNamespace(action="status", json=True), client=c) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["signed_in"] and out["program"] == "bifrost-deep-research"
    assert handle(SimpleNamespace(action="logout", json=False), client=c) == 0
    assert "Signed out" in capsys.readouterr().out
    assert handle(SimpleNamespace(action="status", json=False), client=c) == 0
    assert "Not signed in" in capsys.readouterr().out

"""Background lifecycle for the dashboard: --start / --stop / --restart / --status."""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from deepresearch.core.config import xdg_config_home

STATE_DIR = Path(xdg_config_home) / "deepresearch"
PID_FILE = STATE_DIR / "dashboard.pid"
LOG_FILE = STATE_DIR / "logs" / "dashboard.log"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 7420


# Children run with `python -I`: -c otherwise puts the cwd first on sys.path,
# so starting from a source checkout would import that (possibly stale) copy.
CHILD_BOOT = (
    "import sys; from deepresearch import main; sys.argv[0] = 'deep-research'; main()"
)


def _alive(pid: int) -> bool:
    try:  # reap it first if it is our own child, else a zombie looks alive
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_state() -> dict | None:
    try:
        state = json.loads(PID_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or not _alive(int(state.get("pid", 0))):
        return None
    return state


def _probe(host: str, port: int) -> dict | None:
    target = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    try:
        with urllib.request.urlopen(
            f"http://{target}:{port}/api/health", timeout=1.5
        ) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _urls(host: str, port: int) -> list[str]:
    if host not in ("0.0.0.0", "::", ""):
        return [f"http://{host}:{port}"]
    urls = [f"http://localhost:{port}"]
    try:
        out = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=2
        ).stdout.split()
        urls += [f"http://{ip}:{port}" for ip in out if ":" not in ip]
    except Exception:
        pass
    return urls


def start(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    state = read_state()
    if state:
        print(
            f"[INFO] Dashboard already running (PID {state['pid']}) on "
            f"{state['host']}:{state['port']}. Use --restart to restart it."
        )
        return 0
    if _probe(host, port):
        print(f"[ERROR] Something is already answering on port {port}.")
        return 1

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-I",  # isolated: never import a stray ./deepresearch from the cwd
        "-u",
        "-c",
        CHILD_BOOT,
        "dashboard",
        "--foreground",
        "--host",
        host,
        "--port",
        str(port),
    ]
    with open(LOG_FILE, "a") as log:
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    PID_FILE.write_text(json.dumps({"pid": proc.pid, "host": host, "port": port}))

    for _ in range(40):
        if proc.poll() is not None:
            PID_FILE.unlink(missing_ok=True)
            print(f"[ERROR] Dashboard exited during startup (code {proc.returncode}).")
            print(f"        See {LOG_FILE}")
            return 1
        if _probe(host, port):
            print(f"[INFO] Dashboard started (PID {proc.pid}).")
            for u in _urls(host, port):
                print(f"       {u}")
            print(f"[INFO] Log: {LOG_FILE}")
            return 0
        time.sleep(0.25)
    print(f"[WARN] Dashboard PID {proc.pid} started but is not answering yet.")
    print(f"       See {LOG_FILE}")
    return 1


def stop() -> int:
    state = read_state()
    if not state:
        PID_FILE.unlink(missing_ok=True)
        print("[INFO] Dashboard is not running.")
        return 0
    pid = int(state["pid"])
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if not _alive(pid):
            break
        time.sleep(0.125)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    PID_FILE.unlink(missing_ok=True)
    print(f"[INFO] Dashboard stopped (PID {pid}).")
    return 0


def restart(host: str | None = None, port: int | None = None) -> int:
    state = read_state() or {}
    host = host or state.get("host") or DEFAULT_HOST
    port = port or state.get("port") or DEFAULT_PORT
    stop()
    time.sleep(0.3)
    return start(host, int(port))


def status() -> int:
    state = read_state()
    if not state:
        print(
            "[INFO] Dashboard is not running. Start it with: deep-research dashboard --start"
        )
        return 3
    health = _probe(state["host"], int(state["port"]))
    print(
        f"[INFO] Dashboard running (PID {state['pid']}) on "
        f"{state['host']}:{state['port']} - "
        f"{'healthy' if health else 'NOT answering'}"
    )
    for u in _urls(state["host"], int(state["port"])):
        print(f"       {u}")
    return 0 if health else 1

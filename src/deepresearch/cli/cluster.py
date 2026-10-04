"""`deep-research cluster login|logout|status` (v0.53.0): the Lab's bifrost sign-in."""

from __future__ import annotations

import json
import os
import webbrowser
from pathlib import Path

from deepresearch.dashboard import bifrost as bf


def _state_dir() -> Path:
    base = os.getenv(
        "XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config")
    )
    return Path(base) / "deepresearch"


def _url(state_dir: Path) -> str:
    try:
        data = json.loads((state_dir / "lab_targets.json").read_text())
    except (OSError, ValueError):
        return bf.DEFAULT_URL
    for t in data.get("targets", []):
        cfg = t.get("bifrost")
        if isinstance(cfg, dict) and cfg.get("url"):
            return str(cfg["url"])
    return bf.DEFAULT_URL


def handle(args, client: bf.BifrostClient | None = None) -> int:
    sd = _state_dir()
    c = client or bf.BifrostClient(sd, url=_url(sd))
    out: dict
    try:
        if args.action == "login":
            print("Opening your browser to sign in to the cluster service (bifrost)...")
            who = c.login(open_browser=webbrowser.open)
            out = {"signed_in": True, **who}
        elif args.action == "logout":
            out = {"signed_in": False, "removed": c.logout()}
        else:
            out = {"signed_in": c.signed_in(), "url": c.url}
            if out["signed_in"]:
                out.update(c.whoami())
    except bf.NotSignedIn as e:
        out = {"signed_in": False, "error": str(e)}
    except bf.BifrostError as e:
        out = {"error": str(e)}
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2))
    elif out.get("error"):
        print(f"[ERROR] {out['error']}")
    elif args.action == "logout":
        print("Signed out." if out.get("removed") else "Was not signed in.")
    elif out.get("signed_in"):
        caps = out.get("own_caps") or {}
        print(
            f"Signed in to {c.url} as {out.get('email')} "
            f"(program {out.get('program') or 'none'}; tiers {', '.join(out.get('tiers') or [])}"
            + (
                f"; ${caps.get('max_cost_usd_per_day')}/day, "
                f"{caps.get('max_submits_per_day')} submits/day"
                if caps
                else ""
            )
            + ")"
        )
    else:
        print("Not signed in. Run `deep-research cluster login`.")
    return 1 if out.get("error") else 0


def handle_nexus(args, client: bf.BifrostClient | None = None) -> int:
    """`deep-research nexus login|logout|status` (v0.62.0): read-only Nexus sign-in."""
    from deepresearch.dashboard import nexus as nx

    c = client or nx.client(_state_dir())
    out: dict
    try:
        if args.action == "login":
            print("Opening your browser to sign in to Nexus (read-only)...")
            who = c.login(open_browser=webbrowser.open)
            out = {"signed_in": True, **who}
        elif args.action == "logout":
            out = {"signed_in": False, "removed": c.logout()}
        else:
            out = {"signed_in": c.signed_in(), "url": c.url}
            if out["signed_in"]:
                out.update(c.whoami())
    except bf.NotSignedIn as e:
        out = {"signed_in": False, "error": str(e)}
    except bf.BifrostError as e:
        out = {"error": str(e)}
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2))
    elif out.get("error"):
        print(f"[ERROR] {out['error']}")
    elif args.action == "logout":
        print("Signed out." if out.get("removed") else "Was not signed in.")
    elif out.get("signed_in"):
        print(
            f"Signed in to Nexus as {out.get('email')} (role {out.get('role', 'read')})."
        )
    else:
        print("Not signed in. Run `deep-research nexus login`.")
    return 1 if out.get("error") else 0

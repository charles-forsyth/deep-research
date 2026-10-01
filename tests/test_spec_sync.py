"""docs/SPEC.md stays in sync with the code (v0.50.1).

The spec drifted for ~25 releases (module map, model calls, settings) without anything
noticing. These checks fail CI when a route, an environment variable, a module or a CLI
subcommand exists in the code but nowhere in the spec. They check presence only; the
prose is still reviewed by hand.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "deepresearch"
SPEC = (ROOT / "docs" / "SPEC.md").read_text()


def _py():
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def _route_in_spec(method: str, path: str) -> bool:
    """A route counts as documented if its method and literal path parts appear together,
    allowing any placeholder ({id}, {sid}, ...) and combined forms like GET/PATCH."""
    parts = re.split(r"\([^)]*\)", path.replace("\\.", "."))
    body = r"\{[^}]*\}".join(re.escape(p) for p in parts)
    rx = re.compile(
        r"(?:^|[\s`/])(?:[A-Z]+/)*" + method + r"(?:/[A-Z]+)*\s+" + body + r"(?![\w/-])"
    )
    return bool(rx.search(SPEC))


def test_every_api_route_is_in_the_spec():
    server = (SRC / "dashboard" / "server.py").read_text()
    routes = re.findall(r'r\("(GET|POST|PUT|PATCH|DELETE)",\s*r"([^"]+)"', server)
    assert len(routes) > 50
    missing = [f"{m} {p}" for m, p in routes if not _route_in_spec(m, p)]
    assert not missing, "routes missing from docs/SPEC.md: " + ", ".join(missing)


def test_every_environment_variable_is_in_the_spec():
    names = set()
    for p in _py():
        t = p.read_text()
        names |= set(re.findall(r'os\.(?:getenv|environ\.get)\(\s*"([A-Z0-9_]+)"', t))
        names |= set(re.findall(r'os\.environ\[\s*"([A-Z0-9_]+)"\s*\]', t))
    ignore = {"HOME", "PATH", "USER", "TMPDIR"}
    missing = sorted(n for n in names - ignore if f"`{n}`" not in SPEC)
    assert not missing, "environment variables missing from docs/SPEC.md: " + ", ".join(
        missing
    )


def test_every_module_is_in_the_module_map():
    i = SPEC.index("### 5.1 Module map")
    table = SPEC[i : SPEC.index("### 5.2", i)]
    missing = []
    for p in _py():
        rel = p.relative_to(SRC).as_posix()
        if p.name == "__init__.py" and rel != "__init__.py":
            continue
        if rel.startswith("sources/"):
            ok = f"`{p.stem}`" in table or p.name == "__init__.py"
        else:
            ok = f"`{rel}`" in table or f"`deepresearch/{rel}`" in table
        if not ok:
            missing.append(rel)
    assert not missing, "modules missing from SPEC 5.1: " + ", ".join(missing)


def test_every_static_file_is_in_the_module_map():
    i = SPEC.index("### 5.1 Module map")
    table = SPEC[i : SPEC.index("### 5.2", i)]
    static = SRC / "dashboard" / "static"
    missing = [
        f.name for f in static.iterdir()
        if f.suffix in (".js", ".css", ".html") and "vendor" not in f.name
        and not f.name.startswith(("marked", "purify"))
        and f"`{f.name}`" not in table
    ]  # fmt: skip
    assert not missing, "static files missing from SPEC 5.1: " + ", ".join(missing)


def test_every_cli_command_is_in_the_spec():
    main = (SRC / "__main__.py").read_text()
    m = re.search(r"known_commands\s*=\s*\{([^}]*)\}", main, re.S)
    assert m
    cmds = [c for c in re.findall(r'"([a-z][a-z-]+)"', m.group(1))]
    missing = [c for c in cmds if not re.search(r"`" + re.escape(c) + r"[ `|\[]", SPEC)]
    assert not missing, "CLI commands missing from docs/SPEC.md: " + ", ".join(missing)


def test_header_version_matches_the_package():
    pv = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    hv = re.search(r"Applies to \| deep-research v(\S+)", SPEC)
    assert pv and hv and pv.group(1) == hv.group(1)

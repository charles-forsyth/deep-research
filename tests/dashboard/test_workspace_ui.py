"""v0.41/0.42: the workspace switcher is wired into every request and every link."""

import re
from importlib import resources

STATIC = resources.files("deepresearch.dashboard") / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text()


def test_every_api_call_names_the_workspace():
    app = _read("app.js")
    assert '"X-DR-Workspace": WS.get()' in app
    # the only direct fetch() is inside api(); workspaces.js has one more, the streamed
    # zip import, which sends no workspace header on purpose (it creates a new one)
    assert app.count("fetch(") == 1
    assert _read("workspaces.js").count("fetch(") == 1


def test_links_that_cannot_send_headers_carry_ws():
    js = "".join(_read(f) for f in ("app.js", "features.js", "lab.js", "projects.js"))
    for m in re.finditer(r"[`\"'](/api/[^`\"']*)", js):
        start = max(0, m.start() - 60)
        before = js[start : m.start()]
        is_link = "api(" not in before[-8:]
        if is_link:
            assert "WS.q(" in before[-20:], f"link without ?ws=: {m.group(1)}"


def test_open_tabs_are_kept_per_workspace():
    app = _read("app.js")
    assert "localStorage.setItem(WS.key(LS_TABS)" in app
    assert "localStorage.getItem(WS.key(LS_TABS))" in app


def test_switcher_is_in_the_top_bar_and_loaded():
    html = _read("index.html")
    assert 'id="ws-switch"' in html and "/workspaces.js" in html
    # WSUI is defined before app.js's boot() awaits WSUI.init()
    assert html.index("/workspaces.js") < html.index("/app.js")
    assert "await WSUI.init()" in _read("app.js")


def test_tint_is_subtle():
    css = _read("app.css")
    rule = re.search(
        r"body\[data-ws\]:not\(\[data-ws=\"\"\]\) \.topbar \{([^}]*)\}", css
    )
    assert rule, "workspace tint rule missing"
    # only the border line and a faint shadow change; no background fill or banner
    assert "background" not in rule.group(1)


def test_topbar_status_chips_shrink_instead_of_pushing_buttons_off():
    css = _read("app.css")
    block = css[css.index(".telemetry {") : css.index("}", css.index(".telemetry {"))]
    assert (
        "min-width: 0" in block
        and "overflow: hidden" in block
        and "flex-wrap: wrap" in block
    )
    assert ".top-actions { flex: none; }" in css

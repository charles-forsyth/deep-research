"""v0.38.1: the dashboard must never be wider than a phone screen.

The app shell is a one-column grid; without `minmax(0, 1fr)` the column grows to its
widest child (the top bar, 430 px) and `overflow: hidden` on #app/body then cuts the
right side of every page off on a 390 px phone. A headless-browser audit found this;
this test keeps the rule from being removed.
"""

import re
from importlib import resources

CSS = (resources.files("deepresearch.dashboard") / "static" / "app.css").read_text()


def _rule(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert m, f"{selector} rule missing"
    return m.group(1)


def test_app_column_is_capped_at_the_screen_width():
    assert "grid-template-columns: minmax(0, 1fr)" in _rule("#app")
    assert "min-width: 0" in _rule("#app > *")


def test_viewport_meta_is_present():
    html = (
        resources.files("deepresearch.dashboard") / "static" / "index.html"
    ).read_text()
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in html


def test_phone_rules_exist_for_tables_and_forms():
    assert "@media (max-width: 600px)" in CSS
    for needle in (
        ".runs-table td:nth-child(3)",
        ".src-table",
        ".proj-stats",
        ".row {",
    ):
        assert needle in CSS.split("@media (max-width: 600px)", 1)[1], needle

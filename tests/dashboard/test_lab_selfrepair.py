"""Lab self-repair (v0.44.0): one retry on an unusable AI reply, control characters from
JSON escapes restored, failure classes for TLS/network/API changes, curated pitfalls."""

import json

import pytest

from deepresearch.dashboard import lab as labm
from deepresearch.dashboard import labguard
from deepresearch.dashboard.lab import EmptyReply, Lab, restore_control_chars

PLAN = {
    "title": "t",
    "question": "q",
    "resources": {
        "partition": "standard",
        "nodes": 1,
        "time_limit": "00:10:00",
        "gpus": 0,
    },
    "install": {"modules": [], "conda": [], "pip": []},
    "script": "echo hi > outputs/r.txt",
}


@pytest.fixture
def lab(tmp_path):
    lb = Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={})
    lb.ensure_watcher = lambda: None
    return lb


def _replies(lab, replies):
    calls = []

    def ask(prompt, search):
        calls.append((prompt, search))
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r, 0.001

    lab._ask = ask
    return calls


def _block(obj):
    return "```json\n" + json.dumps(obj) + "\n```"


def test_ask_plan_retries_once_after_broken_json_and_says_why(lab):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    calls = _replies(
        lab,
        [
            '```json\n{"plan": {"script": "x"\n```',
            _block({"plan": PLAN, "changes": ["c"]}),
        ],
    )
    new, out, why = lab._ask_plan(run["id"], "PROMPT", search=True)
    assert new["script"] == PLAN["script"] and out["changes"] == ["c"] and why == ""
    assert len(calls) == 2
    assert "could not be used" in calls[1][0] and "did not parse" in calls[1][0]
    assert calls[0][1] is True and calls[1][1] is False  # the retry skips web search


def test_ask_plan_retries_after_incomplete_plan_and_empty_reply(lab):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    calls = _replies(
        lab, [EmptyReply("MAX_TOKENS", 0.0), _block({"plan": {"script": "x"}})]
    )
    new, out, why = lab._ask_plan(run["id"], "P")
    assert new is None and "no complete plan" in why and len(calls) == 2
    assert "empty (MAX_TOKENS)" in calls[1][0]


def test_ask_plan_restores_latex_backslashes(lab):
    run = lab.create(1, "document", "x", plan=dict(PLAN))
    bad = dict(
        PLAN,
        script="plt.ylabel(r'$\\langle m \rangle$')\nplt.title(r'$\x0crac{a}{b}$')",
    )
    _replies(lab, [_block({"plan": bad})])
    new, _, _ = lab._ask_plan(run["id"], "P")
    assert "\r" not in new["script"] and "\x0c" not in new["script"]
    assert "\\rangle" in new["script"] and "\\frac" in new["script"]


def test_restore_control_chars_keeps_crlf_line_ends():
    s, n = restore_control_chars("a\r\nb \rangle\x08x")
    assert s == "a\r\nb \\rangle\\bx" and n == 2


def test_fix_failed_survives_one_bad_reply(lab, monkeypatch):
    class T:
        kind = name = "fake"
        label = "Fake"
        catalog = None
        partitions = {"standard": {"usd_per_hour": 1.0}}
        default_partition = "standard"

        def log(self, run_id, offset):
            return "Traceback\nNameError: name 'np' is not defined\n", 0

        def describe(self):
            return "Fake"

    lab.targets = {"fake": T()}
    run = lab.create(1, "document", "x", plan=dict(PLAN), target="fake")
    lab._update(run["id"], status="failed", stage="Failed", exit_code="1:0")
    monkeypatch.setattr(lab, "_fresh_catalog", lambda tgt: None)
    fixed = dict(PLAN, script="import numpy as np\necho hi > outputs/r.txt")
    _replies(
        lab,
        [
            "sorry, here is the plan: {broken",
            _block({"plan": fixed, "changes": ["import numpy (NameError np)"]}),
        ],
    )
    new = lab.fix_failed(run["id"])
    assert new["plan"]["fix_changes"] == ["import numpy (NameError np)"]


@pytest.mark.parametrize(
    "log,cls",
    [
        (
            "urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
            "tls",
        ),
        ("urllib.error.HTTPError: HTTP Error 403: Forbidden", "network"),
        (
            "File astropy/table/row.py, line 52\n  out = x\nKeyError: 'quarter'",
            "api-change",
        ),
        (
            "AttributeError: 'LightCurve' object has no attribute 'flatten2'",
            "api-change",
        ),
        ("  File run.py, line 548\nSyntaxError: unterminated string literal", "syntax"),
        ("Traceback\nKeyError: 'x'", "script"),
    ],
)
def test_new_failure_classes(log, cls):
    assert labm.classify_failure(log)[0] == cls


def test_curated_pitfalls_reach_prompts_for_matching_plans(tmp_path):
    block = labguard.prompt_block(
        "import lightkurve; urllib.request.urlopen(url)", tmp_path
    )
    assert "SSL_CERT_FILE" in block and "'mission'" in block
    assert "loc.gov answers slowly" in labguard.prompt_block(
        "https://www.loc.gov/collections/chronicling-america/", tmp_path
    )
    assert "SSL_CERT_FILE" not in labguard.prompt_block("lammps granular", tmp_path)

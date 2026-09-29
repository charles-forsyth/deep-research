"""Lab guards: pitfalls in prompts, science warnings, verdicts, learned lessons."""

import json

from deepresearch.dashboard import labguard as g
from deepresearch.dashboard.lab import validate_plan
from tests.dashboard.test_lab import PLAN, lab  # noqa: F401  (fixture)

# Shapes taken from real AI drafts (runs #49, #50, #51 on 2026-09-29).
HARDCODED = """cat << 'EOF' > run_sim.py
import numpy as np
v = np.zeros(3)
with open("outputs/analysis_report.txt", "w") as f:
    f.write(f"measured {v.mean():.2f}\\n")
    f.write("   - Baseline (No Fan): Extreme vertical stratification (dT head-ankle = 3.68 C).\\n")
    f.write("   Core 1 to burn cycles in a poll loop. As shown in the simulation, WCRT scales\\n")
EOF
python3 run_sim.py
"""
TYPED_REF = """cat << 'EOF' > generate_cases.py
import numpy as np
y_ghia = np.array([1.0000, 0.9766, 0.9688, 0.9609, 0.9531, 0.8516, 0.7344, 0.6172,
    0.5000, 0.4531, 0.2813, 0.1719, 0.1016, 0.0703, 0.0625, 0.0547, 0.0000])
EOF
python generate_cases.py
"""
CLEAN = """python - <<'EOF'
import json
res = {"wcrt": 1.0}
print("Running scenario A (tau = 0.0) for Type I error validation...")
open("outputs/summary.txt", "w").write(f"WCRT {res['wcrt']:.2f} us\\n")
grid = [5, 15, 30, 60, 100, 150, 200, 250, 300]
EOF
"""


def test_hardcoded_conclusions_are_flagged():
    hits = g.hardcoded_findings(HARDCODED)
    assert any("3.68 C" in h for h in hits)
    assert any("As shown in the simulation" in h for h in hits)
    assert not any("measured" in h for h in hits)  # f-strings are computed


def test_typed_reference_table_is_flagged():
    hits = g.typed_table_findings(TYPED_REF)
    assert hits and "y_ghia" in hits[0]


def test_clean_script_passes():
    assert g.science_warnings({"script": CLEAN}) == []


def test_validate_plan_includes_science_warnings():
    class T:
        partitions = {"standard": {}}
        default_partition = "standard"
        catalog = None

    w = validate_plan(T(), {**PLAN, "script": TYPED_REF})  # type: ignore[arg-type]
    assert any("Reference data is typed" in x for x in w)


def test_prompt_block_matches_software_words():
    b = g.prompt_block("Run SU2 on a lid driven cavity", None)
    assert "INC_NAVIER_STOKES" in b
    assert "computed by the job" in b  # general rules always present
    assert "OR-Tools" not in b
    assert "OR-Tools" in g.prompt_block("solve with ortools cp-sat", None)
    # a word inside another word does not match ("su2" in "consu2me" is not a word)
    assert "INC_NAVIER" not in g.prompt_block("consu2me nothing", None)


def test_learned_pitfalls_roundtrip(tmp_path):
    e = g.add_learned(
        tmp_path, "gmx needs -ntmpi 1 on one node", ["gromacs", "gmx"], "run #9"
    )
    again = g.add_learned(tmp_path, "gmx needs -ntmpi 1 on one node", ["gmx"], "dup")
    assert again["id"] == e["id"]
    assert "ntmpi" in g.prompt_block("GROMACS benchmark", tmp_path)
    assert g.remove_learned(tmp_path, e["id"])
    assert not g.remove_learned(tmp_path, e["id"])


def test_verdict_reading(tmp_path):
    assert g.read_verdict(tmp_path) is None
    (tmp_path / "outputs").mkdir()
    f = tmp_path / "outputs" / "verdict.json"
    f.write_text(json.dumps({"checks": [{"name": "Re100 u_min", "pass": False}]}))
    assert g.read_verdict(tmp_path)["pass"] is False
    f.write_text("{not json")
    assert g.read_verdict(tmp_path)["pass"] is None


def test_plan_prompt_carries_lessons(lab):  # noqa: F811
    sid = 1
    run = lab.create(sid, "document", "Use SU2 to run the cavity", "")
    seen = {}

    def ask(prompt, search):
        seen["p"] = prompt
        return "```json\n" + json.dumps(PLAN) + "\n```", 0.01

    lab._ask = ask
    lab.make_plan(run["id"], "Textbook CFD")
    assert "restart_flow.csv" in seen["p"]
    assert "LAB_SMOKE=1" in seen["p"]


def test_failed_verdict_marks_stage_and_fix_teaches(lab, tmp_path):  # noqa: F811
    failed = lab.create(1, "document", "x", "", plan={**PLAN, "title": "a"})
    lab._update(failed["id"], status="failed", error="IndexError in skyline parse")
    fixed_plan = {
        **PLAN,
        "software": [{"name": "treetime"}],
        "fix_changes": ["read skyline.tsv with comment='#'"],
    }
    run = lab.create(1, "document", "x", "", rerun_of=failed["id"], plan=fixed_plan)
    lab.replies.append("**Result** ok")
    lab.fake.fetch_extra = True
    # job finished; outputs include a failing verdict
    lab._update(run["id"], status="fetching", job_id="1", slurm_state="COMPLETED")
    orig_fetch = lab.fake.fetch

    def fetch(run_id, dest):
        files = orig_fetch(run_id, dest)
        (dest / "outputs" / "verdict.json").write_text(
            json.dumps({"checks": [{"name": "Ghia", "pass": False}], "pass": False})
        )
        return files

    lab.fake.fetch = fetch
    lab._finish(lab.get(run["id"]), lab.fake)
    done = lab.get(run["id"])
    assert done["status"] == "completed"
    assert "known-answer check FAILED" in done["stage"]
    assert done["verdict"]["pass"] is False
    learned = g.load_learned(lab.state_dir)
    assert learned and "comment='#'" in learned[0]["text"]
    assert "treetime" in learned[0]["match"]


def test_weak_reference_checks():
    from deepresearch.dashboard import labguard

    bad = [
        'curl -s https://x/paper.html -o inputs/p.html\nif ! grep -q "1.53" inputs/p.html; then exit 3; fi',
        "curl -o r.md https://x/README.md\ncontent = open('r.md').read()\nassert '3.2' in content",
        'curl -o "$REF_FILE" https://x/b.html\ngrep -qi "benchmark" "${REF_FILE}"',
    ]
    for s in bad:
        assert labguard.weak_reference_checks(s), s
    ok = [
        "if 'cd' in col_lower: pass",
        "plt.style.use('x' if 'x' in plt.style.available else 'default')",
        "grep -q GRANULAR <(lmp -h)",
    ]
    for s in ok:
        assert not labguard.weak_reference_checks(s), s
    w = labguard.science_warnings({"script": bad[0]})
    assert any("reference check only tests" in x for x in w)


def test_undefined_names_and_heredoc_unset_vars():
    from deepresearch.dashboard import labguard

    s = "python3 - <<'EOF'\nimport json\nx = 1\nprint(f'{x} {d3_fp32_mlups}')\nEOF\n"
    assert labguard.undefined_names(s) == ["d3_fp32_mlups (line 4)"]
    ok = "python3 - <<'EOF'\nimport numpy as np\ndef f(a):\n    return len(a)\nfor i in range(3):\n    print(f(np.zeros(i)))\nEOF\n"
    assert labguard.undefined_names(ok) == []
    s2 = 'WALL=3\npython3 - << EOF\nw = float("$WALL")\nplt.ylabel("$C_D")\nn = "${LAB_SMOKE:-0}"\nEOF\n'
    assert labguard.heredoc_unset_vars(s2) == ["$C_D (line 4)"]
    assert labguard.heredoc_unset_vars(s2.replace("<< EOF", "<< 'EOF'")) == []
    w = labguard.science_warnings({"script": s2})
    assert any("unquoted heredoc" in x for x in w)


def test_escape_heredoc_unset_vars():
    from deepresearch.dashboard import labguard

    s = "X=1\npython3 - << EOF\nx=$X\nlabel='$C_D'\ny='${LAB_SMOKE:-0}'\nEOF\n"
    new, esc = labguard.escape_heredoc_unset_vars(s)
    assert esc == ["C_D"] and "'\\$C_D'" in new and "x=$X" in new
    assert labguard.heredoc_unset_vars(new) == []
    quoted = s.replace("<< EOF", "<< 'EOF'")
    assert labguard.escape_heredoc_unset_vars(quoted) == (quoted, [])

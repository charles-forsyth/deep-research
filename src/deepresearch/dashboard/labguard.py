"""Lab guards: lessons from past runs (pitfalls) and checks on the science in a plan.

Two parts, both deterministic (no model calls):

- Pitfalls: short, software-specific lessons that go into the planning and fixing prompts
  when the plan or request mentions that software. Curated entries live here; learned
  entries are added when an AI fix of a failed run leads to a completed run, or by hand
  (`POST /api/lab/pitfalls`), and are kept in `<state_dir>/lab_pitfalls.json`.
- Science guards: pre-flight warnings for results written into the script instead of
  computed (run drafts #49/#50, 2026-09-29) and reference data typed from memory (#51:
  Ghia et al. values with the wrong sign). Warnings only; the reviewer decides.
"""

from __future__ import annotations

import ast
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- pitfalls

# match: lower-case words; an entry applies when any appears as a word in the text.
CURATED: list[dict[str, Any]] = [
    {
        "id": "ortools-layered",
        "match": ["ortools", "or-tools", "cp-sat", "cpsat"],
        "text": "OR-Tools pip wheels segfault (exit 139) on the first Solve() when layered on "
        "python-sci (bundled abseil/protobuf clash). List ortools under install.pip; the "
        "harness builds an isolated venv for it. Use NewFixedSizeIntervalVar (NewFixedInterval "
        "was removed).",
        "source": "run #38, #31",
    },
    {
        "id": "script-venv",
        "match": ["venv", "virtualenv", "uv pip"],
        "text": "Never build a venv in the script: it hides the Python module's packages "
        "(ModuleNotFoundError: matplotlib, runs #28 #30). Extra packages go in install.pip.",
        "source": "runs #28, #30",
    },
    {
        "id": "treetime-skyline",
        "match": ["treetime", "augur", "nextstrain"],
        "text": "TreeTime skyline.tsv starts with two '#' lines: read it with comment='#', "
        "header=None and explicit names. Rate uncertainty needs --clock-std-dev or "
        "--covariation, and a negative rate estimate means too few dated tips survived "
        "filtering; normalise every GenBank date format before filtering.",
        "source": "runs #23, #24, #25",
    },
    {
        "id": "vllm-version",
        "match": ["vllm"],
        "text": "The GPU driver limits vLLM: check the installed version's flags with "
        "`vllm serve --help` (or api_server --help) inside the job before using them; "
        "--kv-transfer-config does not exist in 0.6.x.",
        "source": "run #6",
    },
    {
        "id": "su2",
        "match": ["su2", "su2_cfd"],
        "text": "SU2 8.2: `module load openmpi su2/8.2.0` (one line is fine). For "
        "incompressible benchmarks use SOLVER= INC_NAVIER_STOKES with INC_* settings, not the "
        "compressible solver. With OUTPUT_FILES= (RESTART, CSV) the volume data is "
        "restart_flow.csv (columns PointID,x,y,Pressure,Velocity_x,...); there is no flow.csv. "
        "Mesh files need NELEM before NPOIN. Give mpirun </dev/null inside loops that read "
        "stdin. SPECIFIED_INLET_PROFILE: an unsteady run (TIME_DOMAIN= YES) reads "
        "<INLET_FILENAME stem>_00000.dat, not the name given; write both. Format: NMARK=, "
        "MARKER_TAG=, NROW=, NCOL=6, then x y T |U| nx ny per inlet node (run SU2_CFD once "
        "without the file and read the example_* template it writes). There is no "
        "VISC_NUM_METHOD_FLOW or SPATIAL_ORDER_FLOW option in 8.2. Unsteady force "
        "coefficients: set HISTORY_OUTPUT= ( ITER, RMS_RES, AERO_COEFF ) and "
        "REF_AREA= <D x depth>, and drop the start-up transient (impulsive start gives "
        "C_D near 30 in the first steps) before taking max/min: use the last few "
        "shedding periods only; a symmetric start needs several seconds of physical time "
        "at Re=100 before shedding is periodic (run #87: 4 s, C_L still one-signed). Use "
        "MUSCL_FLOW= YES and SLOPE_LIMITER_FLOW= for second order. SU2 also stops at "
        "MAX_TIME (default 1 s of physical time) whatever TIME_ITER says: set MAX_TIME= "
        "<total time> (run #87/#92 stopped at t=1 s, 201 of 2000 steps). SU2 stops on the first unknown option and prints 'Did you mean "
        "X?': take its suggestion.",
        "source": "draft #51, job 225, run #62/#74/#87",
    },
    {
        "id": "lammps-granular",
        "match": [
            "lammps",
            "lmp",
            "granular",
            "dem",
            "pair_style gran",
            "granular flow",
        ],
        "text": "The site LAMMPS modules (lammps/20250722.4 and -cuda) are built with KSPACE "
        "MANYBODY MISC MOLECULE REPLICA RIGID only: no GRANULAR (pair gran/*, fix pour, "
        "fix wall/gran), no DEM. For granular work install conda-forge lammps PINNED to "
        '2023.08.02 (install.conda: ["lammps=2023.08.02"]): it has GRANULAR and runs on '
        "the cluster's glibc 2.28; the 2024/2025 conda-forge builds need glibc 2.29+ and "
        "fail to start. Verify with `lmp -h | grep -q GRANULAR`. In 2D (dimension 2) fix "
        "pour needs gravity along -y (fix grav gravity 1.0 vector 0 -1 0); `lattice ... "
        "units box` is not valid syntax (lattice takes a scale; `region`/`create_atoms` take "
        "units box). thermo_style resets thermo_modify: put `thermo_modify lost ignore` AFTER "
        "thermo_style, or grains leaving an open boundary stop the run with 'Lost atoms'. "
        "But `lost ignore` also hides a system that falls apart: in run #89 the atom count "
        "fell to 6 (fewer than the 41 substrate atoms), both friction cases then gave "
        "identical output, and the verdict still passed. Print the final atom count and "
        "fail when it is far below substrate + grains poured.",
        "source": "runs #57/#81/#84, checked on a compute node 2026-09-29",
    },
    {
        "id": "gmsh-python",
        "match": ["gmsh", "import gmsh"],
        "text": "Python gmsh: pip wheels need libGLU.so.1, which compute nodes lack (OSError at "
        "import). On conda-forge `gmsh` is only the C++ library and program; the Python module "
        'is `python-gmsh`. Use install.conda: ["python-gmsh"] (the Lab maps gmsh to it).',
        "source": "runs #77/#86, checked on a compute node 2026-09-29",
    },
    {
        "id": "glibc-too-new",
        "match": ["glibc", "conda", "conda-forge", "pixi", "version `glibc"],
        "text": "Compute nodes run Rocky 8 (glibc 2.28). Recent conda-forge binaries may need "
        'glibc 2.29-2.38 and fail with "version `GLIBC_2.xx\' not found". Pin an older '
        "build of that package, or use the module or a container.",
        "source": "run #57 probe, 2026-09-29",
    },
    {
        "id": "local-containers",
        "match": ["apptainer", "singularity", ".sif", "container", "nvcc", "cuda"],
        "text": "Images already on the cluster (/apps/containers/*.sif) are listed by path "
        "under install.apptainer and used in place ($IMG_<NAME>); only docker:// or "
        "library:// references are pulled. The CUDA toolkit (nvcc) is in "
        "/apps/containers/cuda-12.4-devel.sif, not a module: `apptainer exec --nv "
        "$IMG_CUDA_12_4_DEVEL nvcc ...`.",
        "source": "run #61",
    },
    {
        "id": "lbm-stability",
        "match": ["lattice boltzmann", "lbm", "d2q9", "d3q19", "bgk", "mrt"],
        "text": "BGK lattice Boltzmann goes unstable as tau -> 0.5 (tau = 3 nu_lat + 0.5): "
        "keep tau >= 0.55 by refining the grid rather than lowering nu (plain BGK with "
        "Zou-He boundaries blew up at tau=0.535, Ny=83, Ma=0.02; Ny=165, tau=0.57 was "
        "stable and gave C_D within 0.4% of 5.5795), cap u_lat <= 0.1, and guard density "
        "divisions; report divergence as a failure, never catch it. Pressure probes at a "
        "body surface must sit on the first FLUID node: bounce-back solid nodes have rho "
        "fixed at 1, so Delta p read there is 0.",
        "source": "run #70, reproduced on a compute node 2026-09-29",
    },
    {
        "id": "freertos-host",
        "match": ["freertos", "heap_4", "heap_4.c"],
        "text": "To run FreeRTOS heap_N.c on a host, compile it unmodified as its own "
        "translation unit with shim FreeRTOS.h/task.h that define PRIVILEGED_DATA, "
        "PRIVILEGED_FUNCTION, BaseType_t, portMAX_DELAY and stub vTaskSuspendAll/"
        "xTaskResumeAll; pin the kernel release tag in the download URL. Its statics are "
        "global, so run one configuration per process (xargs -P), not OpenMP threads.",
        "source": "draft #48, job 222",
    },
    {
        "id": "simpy-cores",
        "match": ["simpy", "simso", "multiprocessor", "spinlock"],
        "text": "In SimPy scheduling models, one CPU runs one handler at a time: model each "
        "core as a Resource(capacity=1) and take locks inside it, or handlers overlap and "
        "contention is overstated.",
        "source": "draft #49, job 223",
    },
    {
        "id": "numba-cfd",
        "match": ["numba", "finite volume", "navier-stokes", "boussinesq", "cfd"],
        "text": "Hand-written CFD: central differences for advection blow up at cell Peclet "
        "numbers above 2; use upwind (or a TVD scheme), check the CFL number, and report "
        "residual/divergence so a diverged run cannot look like a result.",
        "source": "draft #50, job 224",
    },
    {
        "id": "pythermalcomfort",
        "match": ["pythermalcomfort", "pmv", "ppd", "thermal comfort"],
        "text": "pythermalcomfort 3.x/4.x: pmv_ppd_iso(tdb, tr, vr, rh, met, clo, "
        "model='7730-2005') returns an object (res.pmv, res.ppd), not a dict; "
        "cooling_effect(...).ce gives the ASHRAE 55 SET cooling effect and is 0 below "
        "0.1 m/s.",
        "source": "draft #50",
    },
    {
        "id": "mlperf-tiny-urls",
        "match": ["mlperf", "tflite", "tflm", "tensorflow lite micro"],
        "text": "MLPerf Tiny model URLs are case-sensitive (pretrainedResnet, not "
        "pretrainedResNet); curl -sfI every download URL in the Checking inputs stage.",
        "source": "run #28",
    },
    {
        "id": "compute-only",
        "match": [],
        "text": "Jobs run on compute nodes only; never assume the login node.",
        "source": "Chuck, 2026-09-29",
    },
]

# Rules that go into every planning prompt (from drafts #48-#51, 2026-09-29).
GENERAL_RULES = [
    "Every number in a summary, report or plot title must be computed by the job from its "
    "outputs. Never write conclusions, findings or expected numbers into the script as "
    "fixed text; build report text from variables.",
    "Reference data: for a TABLE of values (a profile, a curve), download a machine-readable "
    "data file (CSV/DAT/JSON) from a citable URL listed in inputs and parse the numbers. For "
    "a FEW scalar reference values (a drag coefficient, an exponent, an analytic threshold), "
    "type them with the citation in a comment; do not download a paper, abstract page or "
    "README just to grep a word or a number out of it (that check passes on unrelated pages "
    "and fails on 404s). Never type long tables from memory.",
    "Do not guess the output file names of external tools: after running a tool, list its "
    "output folder and read what exists; fail with a clear message if the expected file is "
    "missing.",
    "Where a known answer exists (a benchmark, an analytic limit, a published value), check "
    'it and write outputs/verdict.json: {"checks": [{"name": ..., "kind": '
    '"validation"|"informative"|"claim", "expected": ..., "got": ..., "tolerance": ..., '
    '"pass": true|false}], "pass": true|false}.',
    "Support a pilot mode: when LAB_SMOKE=1, run a cut-down version (smallest grid, fewest "
    "cases or replicates, seconds not minutes) that still exercises every step, writes "
    "every expected output file and writes outputs/verdict.json with the same checks. The "
    "pilot's informative checks decide whether the full run is worth it, so compute them "
    "in pilot mode too, with bounds loose enough for the small sample.",
    "When a computation fails (a fit with too few points, NaN, no convergence), record the "
    "failure (NaN or null, pass false) and say so. Never return a reference or expected "
    "value as a fallback: a failed calculation would then look like agreement.",
    "Before relying on an optional feature of an installed program (a LAMMPS package, a "
    "solver option, a compiled-in library), check it exists in the verify step (e.g. "
    "`lmp -h | grep -q GRANULAR`) so a missing feature fails in seconds.",
]

_LOCK = threading.Lock()
_WORD = re.compile(r"[a-z0-9][a-z0-9_.+-]*")


def _words(text: str) -> set[str]:
    low = (text or "").lower()
    return set(_WORD.findall(low))


def learned_path(state_dir: Path) -> Path:
    return Path(state_dir) / "lab_pitfalls.json"


def load_learned(state_dir: Path) -> list[dict]:
    p = learned_path(state_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except ValueError:
        return []
    return (
        [d for d in data if isinstance(d, dict) and d.get("text")]
        if isinstance(data, list)
        else []
    )


def all_pitfalls(state_dir: Path | None) -> list[dict]:
    learned = load_learned(state_dir) if state_dir else []
    return [{**p, "kind": "curated"} for p in CURATED] + [
        {**p, "kind": "learned"} for p in learned
    ]


def matching(text: str, state_dir: Path | None, limit: int = 8) -> list[dict]:
    """Pitfalls whose keywords appear in `text` (plan JSON, request, report excerpt)."""
    words = _words(text)
    low = (text or "").lower()
    out = []
    for p in all_pitfalls(state_dir):
        keys = [str(k).lower() for k in p.get("match") or []]
        if not keys:
            continue
        # multi-word keys ("finite volume") match as substrings, single words as words
        if any((k in low) if " " in k else (k in words) for k in keys):
            out.append(p)
    return out[:limit]


def prompt_block(text: str, state_dir: Path | None) -> str:
    """The lessons section for a planning or fixing prompt."""
    hits = matching(text, state_dir)
    from deepresearch.dashboard.labverdict import PLANNER_RULES

    lines = ["Rules learned from earlier runs on this cluster (follow them):"]
    lines += [f"- {r}" for r in GENERAL_RULES]
    lines += [f"- {r}" for r in PLANNER_RULES]
    if hits:
        lines.append("Software-specific lessons:")
        lines += [f"- {p['text']}" for p in hits]
    return "\n".join(lines)


def add_learned(
    state_dir: Path,
    text: str,
    match: list[str],
    source: str = "",
) -> dict:
    """Append a learned pitfall (deduplicated by text). Returns the stored entry."""
    text = " ".join(str(text).split())[:600]
    keys = sorted({str(m).lower().strip() for m in match if str(m).strip()})[:12]
    if not text or not keys:
        raise ValueError("a pitfall needs text and at least one match keyword")
    with _LOCK:
        cur = load_learned(state_dir)
        for d in cur:
            if d.get("text") == text:
                return d
        entry = {
            "id": f"learned-{len(cur) + 1}-{datetime.now():%Y%m%d%H%M%S}",
            "match": keys,
            "text": text,
            "source": source[:200],
            "added": datetime.now().isoformat(timespec="seconds"),
        }
        cur.append(entry)
        p = learned_path(state_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, indent=2))
        tmp.replace(p)
    return entry


def remove_learned(state_dir: Path, pid: str) -> bool:
    with _LOCK:
        cur = load_learned(state_dir)
        keep = [d for d in cur if d.get("id") != pid]
        if len(keep) == len(cur):
            return False
        learned_path(state_dir).write_text(json.dumps(keep, indent=2))
    return True


def software_keys(plan: dict) -> list[str]:
    """Words naming the software of a plan: software[].name, packages and modules."""
    keys: set[str] = set()
    for s in plan.get("software") or []:
        if isinstance(s, dict) and s.get("name"):
            keys.add(str(s["name"]).lower())
    inst = plan.get("install") or {}
    for k in ("conda", "pip", "modules"):
        for x in inst.get(k) or []:
            x = str(x)
            if x.startswith(("--", "https:")):
                continue
            keys.add(re.split(r"[=<>!\[/ ]", x.lower(), maxsplit=1)[0])
    keys.discard("")
    keys -= {
        "python",
        "pip",
        "numpy",
        "pandas",
        "matplotlib",
        "scipy",
        "python-sci",
        "python-ml",
    }
    return sorted(keys)


def lesson_from_fix(failed: dict, fixed_plan: dict) -> dict | None:
    """A learned pitfall from a failed run whose AI-fixed rerun completed, or None."""
    changes = [str(c) for c in fixed_plan.get("fix_changes") or [] if str(c).strip()]
    keys = software_keys(fixed_plan)
    if not changes or not keys:
        return None
    err = str(failed.get("error") or failed.get("stage") or "").strip()
    text = "; ".join(changes)[:450]
    if err and not err.startswith("Ended:"):
        text = f"{err[:120]} -> {text}"
    return {"text": text, "match": keys[:6], "source": f"run #{failed.get('id')}"}


# --------------------------------------------------------------------------- science guards

_HEREDOC = re.compile(
    r"(?P<cmd>[^\n]*?)<<-?\s*(?P<q>['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=q)[^\n]*\n"
    r"(?P<body>.*?)\n[ \t]*(?P=tag)[ \t]*(?:\n|$)",
    re.S,
)
# a measured-looking number: 3.68, 12%, 0.45 m/s, 38%
_NUMBER = re.compile(
    r"(?<![\w.])[-+]?\d+(?:\.\d+)?\s*(?:%|°|C\b|K\b|m/s|us\b|µs|ms\b|x\b)|\d+\.\d+"
)
_REF_NAME = re.compile(
    r"ref|ghia|bench|exp(?:t|erim)|lit|publish|measur|observ|paper|table|truth|valid",
    re.I,
)
# words that turn a sentence into a claim about results
_CLAIM = re.compile(
    r"\b(reduc|increas|decreas|improv|achiev|yield|show|demonstrat|cut|reach|provid|"
    r"exceed|outperform|confirm|result|find|found|conclu|significant|remain)\w*",
    re.I,
)
# phrasing that states a result, not a method ("WCRT scales linearly", "reduces ...")
_CLAIM_STRONG = re.compile(
    r"\b(scales? (linearly|quadratically)|as shown|(?<!model )reduc(es|ed) (the |by |it)|"
    r"increas(es|ed) (the |by )|"
    r"introduc(es|ed) |outperform|is (ideal|optimal|superior)|demonstrat(es|ed)|"
    r"produces? the most|prevent(s|ed) )",
    re.I,
)


def _python_bodies(script: str) -> list[tuple[int, str]]:
    """(first line number, source) of each Python heredoc in the script.

    Also covers `cat << 'EOF' > x.py` (the redirect after the tag) and heredocs that a
    later line runs with python, which the harness's own pre-flight regex misses.
    """
    out = []
    for m in _HEREDOC.finditer(script or ""):
        cmd = m.group("cmd")
        rest = m.group(0).split("\n", 1)[0]  # the whole opening line
        if m.group("q") == "" and "$" in m.group("body"):
            continue
        if (
            re.search(r"\bpython[0-9.]*\b", cmd)
            or re.search(r">\s*\S+\.py\b", rest)
            or re.search(r"^\s*(import|from)\s+\w", m.group("body"), re.M)
        ):
            out.append((script[: m.start("body")].count("\n") + 1, m.group("body")))
    return out


_BUILTINS = set(dir(__import__("builtins"))) | {"__file__", "__name__", "__doc__"}


def mathtext_escapes(script: str) -> list[str]:
    """`'$\tau$'` in a normal (not raw) Python string: \t becomes a tab, \a a bell,
    and matplotlib's mathtext parser fails (run #88: '$\tau \approx 1.34$'). f-strings
    are checked on their literal parts too."""
    hits = []
    for line0, body in _python_bodies(script):
        try:
            tree = ast.parse(body)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                v = "".join(
                    x.value for x in node.values
                    if isinstance(x, ast.Constant) and isinstance(x.value, str)
                )  # fmt: skip
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                v = node.value
            else:
                continue
            if "$" in v and re.search(
                r"\$[^$]*[\t\a\b\f\v\r][^$]*\$|\$[^$]*[\t\a\b\f\v\r]", v
            ):
                hits.append(f"line {line0 + node.lineno - 1}: {v.strip()[:40]!r}")
    return list(dict.fromkeys(hits))[:4]


def undefined_names(script: str) -> list[str]:
    """Names a Python heredoc reads but never defines (NameError at run time).

    Conservative: a name assigned, imported, or bound anywhere in the body counts as
    defined. Caught run #82, whose report f-string used d3_fp32_mlups after an hour
    of GPU work had finished.
    """
    hits = []
    for line0, body in _python_bodies(script):
        try:
            tree = ast.parse(body)
        except SyntaxError:
            continue
        defined: set[str] = set()
        star = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                defined.add(node.id)
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                defined.add(node.name)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    if a.name == "*":
                        star = True
                    defined.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                defined.update(node.names)
            elif isinstance(node, ast.MatchAs) and node.name:
                defined.add(node.name)
        if star:
            continue
        seen = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id not in defined
                and node.id not in _BUILTINS
                and node.id not in seen
            ):
                seen.add(node.id)
                hits.append(f"{node.id} (line {line0 + node.lineno - 1})")
    return hits[:5]


# $NAME inside an unquoted heredoc is expanded by bash; under `set -u` an unset one kills
# the job (run #83: a matplotlib label '$C_D(t)$' in `python3 - << EOF`).
_UNQ_HEREDOC = re.compile(r"<<-?\s*([A-Za-z_]\w*)\s*[^\n]*\n(.*?)\n\1\s*$", re.S | re.M)
_SH_ASSIGN = re.compile(
    r"(?:^|[\s;(])(?:export\s+|local\s+|readonly\s+|declare\s+(?:-\w+\s+)*)?([A-Za-z_]\w*)=",
    re.M,
)
_SH_LOOPVAR = re.compile(r"\b(?:for|read(?:\s+-\w+)*)\s+([A-Za-z_]\w*)")
_SH_ENV = {
    "HOME",
    "USER",
    "PATH",
    "PWD",
    "TMPDIR",
    "HOSTNAME",
    "SHELL",
    "LANG",
    "RANDOM",
    "SECONDS",
    "LINENO",
    "BASH_SOURCE",
    "OLDPWD",
    "UID",
    "EUID",
    "PPID",
    "IFS",
    "LD_LIBRARY_PATH",
    "PYTHONPATH",
    "CONDA_PREFIX",
    "VIRTUAL_ENV",
    "MODULEPATH",
    "OMP_NUM_THREADS",
    "CUDA_VISIBLE_DEVICES",
    "LAB_SMOKE",
    "PARAM_SMOKE",
    "LAB_RUN_ID",
}


def heredoc_unset_vars(script: str) -> list[str]:
    """$VARs bash would expand inside unquoted heredocs that the script never sets."""
    s = script or ""
    known = set(_SH_ASSIGN.findall(s)) | set(_SH_LOOPVAR.findall(s)) | _SH_ENV
    hits = []
    for m in _UNQ_HEREDOC.finditer(s):
        body = m.group(2)
        ln = s[: m.start(2)].count("\n") + 1
        for v in re.finditer(
            r"(?<!\\)\$(?:\{([A-Za-z_]\w*)([^}]*)\}|([A-Za-z_]\w*))", body
        ):
            name = v.group(1) or v.group(3)
            if v.group(1) and v.group(2)[:1] in (":", "-", "=", "?", "+"):
                continue  # ${X:-default} and friends are safe under set -u
            if (
                name.startswith(("SLURM_", "LADDER_", "PIXI_", "CONDA_", "PARAM_"))
                or name in known
            ):
                continue
            item = f"${name} (line {ln + body[: v.start()].count(chr(10))})"
            if item not in hits:
                hits.append(item)
    return hits[:5]


def escape_heredoc_unset_vars(script: str) -> tuple[str, list[str]]:
    """Escape $VARs in unquoted heredocs that the script never sets (a `$C_D` in a
    matplotlib label). Under `set -u` they would stop the job; escaped, Python sees the
    literal text the author meant. Returns (new script, names escaped)."""
    s = script or ""
    known = set(_SH_ASSIGN.findall(s)) | set(_SH_LOOPVAR.findall(s)) | _SH_ENV
    fixed: list[str] = []

    def fix_body(body: str) -> str:
        def sub(v: re.Match) -> str:
            name = v.group(1) or v.group(3)
            if v.group(1) and v.group(2)[:1] in (":", "-", "=", "?", "+"):
                return v.group(0)
            if (
                name.startswith(("SLURM_", "LADDER_", "PIXI_", "CONDA_", "PARAM_"))
                or name in known
            ):
                return v.group(0)
            if name not in fixed:
                fixed.append(name)
            return "\\" + v.group(0)

        return re.sub(
            r"(?<!\\)\$(?:\{([A-Za-z_]\w*)([^}]*)\}|([A-Za-z_]\w*))", sub, body
        )

    out, pos = [], 0
    for m in _UNQ_HEREDOC.finditer(s):
        out.append(s[pos : m.start(2)])
        out.append(fix_body(m.group(2)))
        pos = m.end(2)
    out.append(s[pos:])
    return "".join(out), fixed


def _literal_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def hardcoded_findings(script: str) -> list[str]:
    """Findings written as fixed text into outputs or the console.

    Flags write()/print() calls whose argument is a plain string literal (not an
    f-string or format call) of at least six words that either contains a
    measured-looking number or, when written to a file, states a finding (a
    "FINDINGS"/"CONCLUSION" section, or claim verbs like "reduces", "scales linearly").
    Returns up to five examples ("line N: text").
    """
    hits: list[str] = []
    for start, body in _python_bodies(script):
        try:
            tree = ast.parse(body)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name not in ("write", "writelines", "print"):
                continue
            for a in node.args:
                s = _literal_str(a)
                if not s or len(s.split()) < 6:
                    continue
                has_num = bool(_NUMBER.search(s))
                claim = bool(_CLAIM.search(s))
                # console progress lines ("Running scenario A (tau = 0.0)...") are fine;
                # a print counts only when it states a finding with a number
                if name == "print" and not (has_num and claim):
                    continue
                # written text: a number, or a claim sentence (qualitative conclusions
                # written before the job ran, run draft #49)
                if name != "print" and not (
                    has_num or (claim and _CLAIM_STRONG.search(s))
                ):
                    continue
                hits.append(f"line {start + a.lineno - 1}: {s.strip()[:90]}")
    # claims first: they are the reason for the warning
    hits.sort(key=lambda h: not (_CLAIM.search(h) and _NUMBER.search(h)))
    # shell: echo "... 3.2% ..." > outputs/...
    for i, ln in enumerate((script or "").splitlines(), 1):
        m = re.match(r"\s*echo\s+(['\"])(.+?)\1\s*>>?\s*\S*outputs/", ln)
        if (
            m
            and "$" not in m.group(2)
            and len(m.group(2).split()) >= 6
            and _NUMBER.search(m.group(2))
        ):
            hits.append(f"line {i}: {m.group(2)[:90]}")
    return hits[:5]


def _floats(node: ast.AST) -> list[float] | None:
    if isinstance(node, (ast.List, ast.Tuple)):
        vals = []
        for e in node.elts:
            if isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.USub):
                e = e.operand
            if (
                isinstance(e, ast.Constant)
                and isinstance(e.value, (int, float))
                and not isinstance(e.value, bool)
            ):
                vals.append(float(e.value))
            else:
                return None
        return vals
    if isinstance(node, ast.Call) and node.args:  # np.array([...])
        return _floats(node.args[0])
    return None


def typed_table_findings(script: str) -> list[str]:
    """Numeric tables typed into the script that look like reference data.

    A literal list of at least 8 numbers, 5 or more of them with three or more decimals,
    assigned to a name that suggests reference data (ghia, ref, benchmark, experimental,
    measured...) or at least 12 such numbers under any name.
    """
    hits: list[str] = []
    for start, body in _python_bodies(script):
        try:
            tree = ast.parse(body)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            targets: list[str] = []
            value = None
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            elif isinstance(node, ast.keyword) and node.arg:
                targets, value = [node.arg], node.value
            elif isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    kn = _literal_str(k) if k is not None else None
                    vals = _floats(v)
                    if kn and vals and _looks_ref(kn, vals):
                        hits.append(
                            f"line {start + v.lineno - 1}: '{kn}' ({len(vals)} values)"
                        )
                continue
            if value is None:
                continue
            vals = _floats(value)
            if not vals:
                continue
            name = targets[0] if targets else "?"
            if _looks_ref(name, vals):
                hits.append(
                    f"line {start + value.lineno - 1}: {name} ({len(vals)} values)"
                )
    return hits[:5]


def _looks_ref(name: str, vals: list[float]) -> bool:
    precise = sum(
        1 for v in vals if len(repr(abs(v)).split(".")[-1]) >= 3 and v != int(v)
    )
    if len(vals) < 8 or precise < 5:
        return False
    return bool(_REF_NAME.search(name)) or len(vals) >= 12


# A reference "check" that only proves some page was downloaded, not the numbers used:
# `'3.2' in content`, `grep -q "benchmark"`. Runs #55-#63 all did this; one fetched an
# unrelated README (#77) and would have "verified" the Schafer-Turek values against it.
_WEAK_REF = re.compile(
    r"""(?:['"]\d(?:\.\d{1,2})?['"]\s+in\s+\w+)|"""  # '3.2' in content
    r"""(?:grep\s+-q\w*(?:\s+-\w+)*\s+['"]?(?:benchmark|reference|[A-Za-z][a-z]+|\d+\.\d+)['"]?\s+\S*(?:inputs/|\.html|\.xml|\.md|REF))""",
)


# `if x <= 0.1: x = 3.5`: a computed value that looks wrong is replaced by a plausible
# constant, so a failed calculation reports a believable number (run #80, SAMSE limit).
_SUBSTITUTE = re.compile(
    r"if\s+(\w+)\s*(?:<=?|==|>=?)\s*[-\d.eE]+\s*:\s*\n?\s*\1\s*=\s*(-?\d+(?:\.\d+)?)\b"
)


def value_substitutions(script: str) -> list[str]:
    hits = []
    for _, body in _python_bodies(script):
        for m in _SUBSTITUTE.finditer(body):
            if float(m.group(2)) == 0.0:
                continue  # clamping to zero (rates, probabilities) is normal numerics
            ln = body[: m.start()].count("\n") + 1
            hits.append(
                f"{m.group(1)} is replaced by {m.group(2)} when out of range (line ~{ln})"
            )
    return hits[:3]


def weak_reference_checks(script: str) -> list[str]:
    hits = []
    for n, ln in enumerate(script.splitlines(), 1):
        if _WEAK_REF.search(ln) and re.search(
            r"curl|wget|content|\.html|\.md|inputs/", script
        ):
            hits.append(f"line {n}: {ln.strip()[:80]}")
    return hits[:3]


def science_warnings(plan: dict) -> list[str]:
    script = str((plan or {}).get("script") or "")
    warns = []
    if re.search(r"np\.diff\(\s*np\.sign\(", script) and not re.search(
        r"np\.sign\([^)]*\)\s*\[[^\]]*!=\s*0|nonzero|diff\s*!=\s*0|abs\([^)]*\)\s*>",
        script,
    ):
        warns.append(
            "Crossing detection with np.diff(np.sign(...)) counts a step from exactly 0 "
            "(e.g. both curves zero below threshold) as a crossing (run #71 reported "
            "T*=0.045 instead of ~0.25). Drop zero entries before looking for sign changes."
        )
    mt = mathtext_escapes(script)
    if mt:
        warns.append(
            "Matplotlib math text in a normal string has backslashes Python turns into "
            "control characters (\\t, \\a, \\b): "
            + "; ".join(mt)
            + ". Use a raw string (r'$\\tau$')."
        )
    und = undefined_names(script)
    if und:
        warns.append(
            "The script's Python uses names it never defines: "
            + ", ".join(und)
            + ". It would stop with a NameError, possibly after the whole run."
        )
    unset = heredoc_unset_vars(script)
    if unset:
        warns.append(
            "Bash expands these inside an unquoted heredoc but the script never sets "
            "them: " + ", ".join(unset) + ". Under set -u the job stops there; quote "
            "the tag (<< 'EOF') or escape the $ (e.g. in matplotlib labels)."
        )
    subs = value_substitutions(script)
    if subs:
        warns.append(
            "A computed value is replaced by a fixed number when it comes out wrong ("
            + "; ".join(subs)
            + "); a failed calculation would then report a plausible result. Record "
            "the failure (NaN, pass false) instead."
        )
    weak = weak_reference_checks(script)
    if weak:
        warns.append(
            "The reference check only tests that a downloaded page contains a word or a short "
            "number ("
            + "; ".join(weak)
            + "); it would pass on an unrelated page. Either "
            "parse the exact values from a data file, or type them with a citation in a "
            "comment and drop the download."
        )
    hard = hardcoded_findings(script)
    if hard:
        warns.append(
            "The script writes results as fixed text instead of computing them ("
            + "; ".join(hard[:3])
            + "). Build summaries from the computed values."
        )
    tables = typed_table_findings(script)
    if tables:
        warns.append(
            "Reference data is typed into the script ("
            + "; ".join(tables[:3])
            + "). Download it from a citable source in the job (list the URL in inputs) and "
            "spot-check a few values, so a mistyped number can't skew the comparison."
        )
    return warns


def read_verdict(run_dir: Path) -> dict | None:
    """outputs/verdict.json written by the job (known-answer checks), if valid."""
    p = Path(run_dir) / "outputs" / "verdict.json"
    if not p.exists():
        return None
    try:
        v = json.loads(p.read_text("utf-8", "replace"))
    except ValueError:
        return {"pass": None, "error": "verdict.json is not valid JSON"}
    if not isinstance(v, dict):
        return {"pass": None, "error": "verdict.json is not an object"}
    checks = [c for c in v.get("checks") or [] if isinstance(c, dict)][:50]
    ok = v.get("pass")
    if not isinstance(ok, bool):
        ok = all(c.get("pass") is True for c in checks) if checks else None
    out: dict = {"pass": ok, "checks": checks}
    audit = audit_verdict(checks, Path(run_dir) / "outputs")
    if audit:
        out["audit"] = audit
        if ok is True and any(a.startswith(("MISMATCH", "IDENTICAL")) for a in audit):
            out["pass"] = False  # the script's own pass flag contradicts its numbers
    return out


def _num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(str(x).strip())
    except ValueError:
        return None


def audit_verdict(checks: list[dict], outputs: Path | None = None) -> list[str]:
    """Re-check a job's own verdict instead of trusting its pass flags.

    - MISMATCH: numeric expected/got/tolerance say fail but the script said pass.
    - LOOSE: tolerance is over 20% of the expected value (run #89: tau 1.78 passed
      against 1.53 with tolerance 0.45).
    - IDENTICAL: summary.json has two sections (arms of a comparison) with byte-identical
      results (run #89: 'frictional' and 'inertial' regimes were the same run).
    """
    notes = []
    for c in checks:
        exp, got, tol = (
            _num(c.get("expected")),
            _num(c.get("got")),
            _num(c.get("tolerance")),
        )
        name = str(c.get("name") or "?")[:60]
        if exp is None or got is None or tol is None:
            continue
        within = abs(got - exp) <= tol * (1 + 1e-9)
        # a threshold check ("amplitude above 0.05") legitimately passes far from
        # `expected`; only call it a contradiction when the name gives no direction
        onesided = re.search(
            r"(above|below|exceed|greater|less|more than|at least|at most|min|max|>|<|"
            r"formation|onset|present|detected)",
            name, re.I,
        )  # fmt: skip
        if c.get("pass") is True and not within and not onesided:
            notes.append(
                f"MISMATCH {name}: |{got:g} - {exp:g}| > {tol:g} but marked pass"
            )
        elif exp != 0 and tol > 0.2 * abs(exp) and c.get("pass") is True:
            notes.append(
                f"LOOSE {name}: tolerance {tol:g} is {100 * tol / abs(exp):.0f}% of "
                f"{exp:g}; the pass says little"
            )
    if outputs is not None:
        sp = outputs / "summary.json"
        try:
            summ = json.loads(sp.read_text("utf-8", "replace")) if sp.exists() else None
        except ValueError:
            summ = None
        if isinstance(summ, dict):
            secs = {
                k: json.dumps(v, sort_keys=True)
                for k, v in summ.items()
                if isinstance(v, dict) and len(v) >= 3
            }
            seen: dict[str, str] = {}
            for k, dump in secs.items():
                if dump in seen:
                    notes.append(
                        f"IDENTICAL summary sections '{seen[dump]}' and '{k}': the two "
                        "cases produced the same numbers, so the comparison did not run"
                    )
                else:
                    seen[dump] = k
    return notes[:8]

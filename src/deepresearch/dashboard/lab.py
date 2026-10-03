"""Lab runs: turn a question in a report into a real computational job on an HPC cluster.

Flow: suggestions -> plan (AI, reviewed and editable) -> submit -> watch -> fetch -> write-up.

Nothing here fakes science. If software will not install or a job fails, the run is marked
failed with the log that says why.

Cluster access goes through a Target (Slurm over SSH today). Job state lives on the
cluster (the job folder and Slurm accounting); the local database is a mirror that the
watcher keeps in step, so a closed browser, a restarted dashboard or a sleeping laptop
never loses a job.
"""

from __future__ import annotations

import datetime as dt
import difflib
import ast
import hashlib
import json
import os
import re
import shlex
import sys
import sqlite3
import subprocess
import threading
import time

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from deepresearch.dashboard import labcores, labguard, labverdict
from deepresearch.dashboard.labloop import LabVerdictMixin
from deepresearch.dashboard.cluster import (  # noqa: F401  (re-exported)
    GCLOUD_LOGIN_HINT,
    MAX_FETCH_BYTES,
    MAX_FILE_BYTES,
    NotSubmitted,
    ScopedTarget,
    SlurmSSHTarget,
    TargetError,
    _gcloud_error,
    _tar_b64,
    _worker_script,
    load_targets,
    task_prefix,
)

PLAN_MODEL = "gemini-3.8-flash"  # default; GEMINI_FOLLOWUP_MODEL overrides it
# gemini-3.8-flash list prices, USD per 1M tokens (thinking billed as output).
FLASH_IN_1M = 0.75
FLASH_CACHED_1M = 0.075
FLASH_OUT_1M = 3.75
# Google Search grounding per 1,000 queries. The 5,000 free queries a month are shared
# with every other Gemini use and cannot be seen from here, so this is the worst case.
SEARCH_PER_1K = 14.00
MAX_FETCH_TRIES = 5  # fetch attempts before a finished job is marked failed

STATES = (
    "planning",  # AI is writing the plan
    "plan_failed",
    "draft",  # plan ready for review
    "submitting",
    "smoke",  # cut-down run on the warm node before the real one
    "queued",  # accepted by Slurm, waiting for a node
    "running",  # the batch script is running (installing or computing)
    "fetching",  # copying results back
    "analyzing",  # AI is writing the results note
    "completed",
    "failed",
    "cancelled",
)
ACTIVE = ("submitting", "smoke", "queued", "running", "fetching", "analyzing")
FINAL = ("completed", "failed", "cancelled")
SLURM_DONE = {
    "COMPLETED": "completed",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
    "TIMEOUT": "failed",
    "NODE_FAIL": "failed",
    "OUT_OF_MEMORY": "failed",
    "PREEMPTED": "failed",
    "BOOT_FAIL": "failed",
    "DEADLINE": "failed",
}
# Lab runs whose submit is running in this process right now (see poll()).
_IN_FLIGHT: set[tuple[str, int]] = set()
# always-on warm node: one keeper thread per process; targets paused by a manual Stop
_KEEPER: threading.Thread | None = None
_KEEPER_LOCK = threading.Lock()
_WARM_PAUSED: set[str] = set()
_SMOKE_FIXING: set[tuple[str, int]] = (
    set()
)  # runs whose AI smoke fix runs in this process
GONE_POLLS = 8  # empty squeue+sacct polls (~2 min) before a running job is fetched


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cost(usage, searches: int = 0) -> float | None:
    if not usage and not searches:
        return None
    inp = getattr(usage, "prompt_token_count", 0) or 0
    cached = min(getattr(usage, "cached_content_token_count", 0) or 0, inp)
    out = (getattr(usage, "candidates_token_count", 0) or 0) + (
        getattr(usage, "thoughts_token_count", 0) or 0
    )
    tool = getattr(usage, "tool_use_prompt_token_count", 0) or 0
    return round(
        (inp - cached + tool) / 1e6 * FLASH_IN_1M
        + cached / 1e6 * FLASH_CACHED_1M
        + out / 1e6 * FLASH_OUT_1M
        + searches / 1000 * SEARCH_PER_1K,
        4,
    )


def _search_count(resp) -> int:
    """Google Search queries a generate_content reply ran (each one is billed)."""
    n = 0
    for cand in getattr(resp, "candidates", None) or []:
        gm = getattr(cand, "grounding_metadata", None)
        n += len(getattr(gm, "web_search_queries", None) or [])
    return n


# A backslash sequence inside a JSON string: group 1 = valid escape (kept as is),
# otherwise an invalid one (its backslash gets doubled). Matching valid pairs first
# keeps an already-correct `\\\\d` from being split into `\\` + `\\d`.
_ESCAPE = re.compile(r'\\(\\|["/bfnrt]|u[0-9a-fA-F]{4})|\\')


def _loads_lenient(body: str) -> Any:
    """json.loads that survives what models put inside long string values.

    Plans embed whole bash/Python scripts in a JSON string. Models often leave regex or
    LaTeX backslashes unescaped (`\\d`, `\\alpha`, `\\(`) and sometimes raw newlines or
    tabs; strict JSON rejects both ("Invalid \\escape", "Invalid control character").
    Try strict first, then allow control characters, then double every backslash that
    does not start a valid JSON escape, which is what the model meant.
    """
    last: ValueError | None = None
    for text, strict in (
        (body, True),
        (body, False),
        (_ESCAPE.sub(lambda m: m.group(0) if m.group(1) else "\\\\", body), False),
    ):
        try:
            return json.loads(text, strict=strict)
        except json.JSONDecodeError as e:
            last = e
            if e.msg == "Extra data":
                # a complete object followed by more text (a second object, notes):
                # keep the first complete value
                try:
                    return json.JSONDecoder(strict=strict).raw_decode(text.lstrip())[0]
                except ValueError:
                    pass
    raise last or ValueError("no JSON")


def extract_json(text: str) -> Any:
    """First JSON object or array in a model reply (fenced or bare), parsed leniently."""
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text or "", re.S)
    body = m.group(1) if m else (text or "")
    try:
        return _loads_lenient(body)
    except ValueError:
        pass
    start = min([i for i in (body.find("{"), body.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("no JSON in model reply")
    opener = body[start]
    closer = "}" if opener == "{" else "]"
    end = body.rfind(closer)
    return _loads_lenient(body[start : end + 1])


PLAN_KEYS = ("script", "resources", "install")


def _usable_plan(out: Any) -> dict | None:
    new = out.get("plan") if isinstance(out, dict) else None
    return new if isinstance(new, dict) and all(k in new for k in PLAN_KEYS) else None


def restore_control_chars(script: str) -> tuple[str, int]:
    """Undo JSON escapes that turned LaTeX or regex backslashes into control characters.

    A model writing `\\rangle` or `\\frac` as `\\r...`/`\\f...` in a JSON string yields a
    carriage return or form feed in the script (run #35: a matplotlib label split across
    lines, a Python syntax error). A bare CR, FF or backspace inside a line never belongs in
    a job script, so put the backslash back. CRLF line ends are left alone.
    """
    n = 0
    out = []
    for ch, nxt in zip(script, script[1:] + "\n"):
        if ch == "\r" and nxt != "\n":
            out.append("\\r")
            n += 1
        elif ch in ("\f", "\b"):
            out.append("\\f" if ch == "\f" else "\\b")
            n += 1
        else:
            out.append(ch)
    return "".join(out), n


# What the site Python environment modules provide (import-tested on the cluster; see
# ucr-slurm-production build/python-sci.sbatch). Used to flag redundant installs.
PYTHON_SCI_PACKAGES = set(
    """numpy scipy pandas matplotlib seaborn scikit-learn sklearn statsmodels
sympy numba xarray netcdf4 h5py tables pytables zarr dask polars pyarrow astropy astroquery
skyfield sunpy cartopy shapely geopandas pyproj rasterio networkx biopython pysam
scikit-image opencv pillow jupyterlab ipykernel ipywidgets tqdm requests certifi pyyaml rich
cutadapt multiqc snakemake uv""".split()
)
PYTHON_ML_PACKAGES = set(
    """torch numpy scipy pandas scikit-learn sklearn transformers
jupyterlab""".split()
)
# Site modules that are a whole Python stack. pip packages go into a venv on top of the
# module (build_sbatch), so the module's packages stay importable.
PYTHON_ENV_MODULES = ("python-sci", "python-ml")
# pip packages whose compiled core crashes when layered on a Python environment module:
# OR-Tools bundles its own abseil/protobuf, and python-sci/2026.09 puts conda's
# libabsl_*.so (2605) on the same process; CP-SAT's Solve() segfaults (exit 139) on even a
# one-variable model once the module's site-packages are visible. Verified on Ursa Major
# 2026-09-29 (Lab run #38): ortools 9.12, 9.14 and 9.15 all crash on top of python-sci
# and all solve in a plain venv. These get an isolated venv (no --system-site-packages),
# with the module's Python and the plan's other pip packages installed alongside.
ISOLATE_FROM_MODULE_PIP = ("ortools",)
ISOLATED_BASE_PIP = ["numpy", "pandas", "matplotlib", "scipy"]
# A script that builds its own venv puts another Python first on PATH and hides the
# packages the harness installed (issue #113).
_SCRIPT_VENV = re.compile(
    r"^\s*(?:uv\s+venv|python[0-9.]*\s+-m\s+venv|virtualenv)\b", re.M
)


def validate_plan(target: SlurmSSHTarget | None, plan: dict) -> list[str]:
    """Check a plan against the cluster catalog before anything is submitted.

    Returns human-readable warnings (empty when the plan fits). Never raises: a plan
    with warnings can still be edited or submitted by the reviewer.
    """
    if not target or not isinstance(plan, dict):
        return []
    warns: list[str] = []
    r = plan.get("resources") or {}
    part_name = r.get("partition") or target.default_partition
    part = target.partitions.get(part_name)
    if target.partitions and part is None:
        warns.append(
            f"Partition '{part_name}' does not exist; available: "
            + ", ".join(target.partitions)
        )
    gpus = int(r.get("gpus") or 0) if str(r.get("gpus") or 0).isdigit() else 0
    if part is not None and isinstance(part, dict):
        have_gpus = int(part.get("gpus") or 0)
        if gpus and not have_gpus:
            gp = [k for k, v in target.partitions.items() if v.get("gpus")]
            warns.append(
                f"{gpus} GPU(s) requested on '{part_name}', which has none"
                + (f"; use {', '.join(gp)}" if gp else "")
            )
        elif have_gpus and gpus > have_gpus:
            warns.append(f"{gpus} GPUs requested; '{part_name}' nodes have {have_gpus}")
        mx = part.get("max_nodes")
        nodes = r.get("nodes") or 1
        if isinstance(mx, int) and str(nodes).isdigit() and int(nodes) > mx:
            warns.append(f"{nodes} nodes requested; '{part_name}' has at most {mx}")
    if getattr(target, "catalog", None):
        have, needs = target.known_modules()
        mods = [str(m) for m in (plan.get("install") or {}).get("modules") or []]
        loaded_mpi = {m.split("/")[0] for m in mods} & {
            "openmpi",
            "mpich",
            "intel-oneapi-mpi",
        }
        for m in mods:
            if m not in have:
                base = m.split("/")[0]
                alts = sorted(x for x in have if x.split("/")[0] == base and "/" in x)
                warns.append(
                    f"Module '{m}' is not installed"
                    + (f"; available: {', '.join(alts[:6])}" if alts else "")
                )
            elif needs.get(m) and needs[m] not in loaded_mpi:
                warns.append(
                    f"Module '{m}' needs `module load {needs[m]}` before it "
                    f"(add '{needs[m]}' earlier in install.modules)"
                )
        # modules the cluster's smoke test found broken (load but cannot run)
        broken = ((target.catalog or {}).get("module_health") or {}).get("broken") or {}
        for m in mods:
            hit = broken.get(m) or next(
                (v for k, v in broken.items() if k.split("/")[0] == m), None
            )
            if hit:
                warns.append(
                    f"Module '{m}' is broken on the cluster ({hit[:120]}); use a "
                    "Pixi/conda environment instead"
                )
        # Python stacks do not mix: one environment module, never py-* or bare python
        pyenv = [m for m in mods if m.split("/")[0] in ("python-sci", "python-ml")]
        stray = [m for m in mods if m.split("/")[0] == "python" or m.startswith("py-")]
        if len(pyenv) > 1:
            warns.append(
                "Load only one Python environment module (" + ", ".join(pyenv) + ")"
            )
        if stray:
            warns.append(
                "Python modules " + ", ".join(stray) + " are not a working stack; load "
                "python-sci (CPU science) or python-ml (PyTorch/GPU), or use conda"
            )
        if pyenv and (plan.get("install") or {}).get("conda"):
            warns.append(
                f"{pyenv[0]} and a conda environment both provide Python; pick one "
                "(extra packages on top of the module: pip, which builds a venv)"
            )
        # installing something that is already a module wastes minutes
        pkgs = [
            re.split(r"[=<>!\[]", str(x))[0].lower()
            for x in ((plan.get("install") or {}).get("conda") or [])
            + ((plan.get("install") or {}).get("pip") or [])
            if not str(x).startswith(("--", "https:"))
        ]
        # a pinned install of something the module has is deliberate when the module's
        # build lacks what the plan checks for (LAMMPS without GRANULAR, run #57)
        verify_txt = " ".join(
            str(v) for v in (plan.get("install") or {}).get("verify") or []
        )
        pinned = {
            re.split(r"[=<>!\[]", str(x))[0].lower()
            for x in (plan.get("install") or {}).get("conda") or []
            if re.search(r"[=<>]", str(x))
        }
        dup = sorted(
            {
                p
                for p in pkgs
                if p in have
                and p not in ("python", "pip", "numpy")
                and not (
                    p in pinned
                    and re.search(rf"\b{re.escape(p)}\b|lmp|grep", verify_txt)
                )
                and not (
                    p == "gmsh" and "import gmsh" in verify_txt
                )  # module has no Python API
            }
        )
        if dup:
            warns.append(
                "Already installed as modules (faster than installing): "
                + ", ".join(dup)
            )
        # packages the loaded Python environment module already provides
        pyenv_mods = [m for m in mods if m.split("/")[0] in ("python-sci", "python-ml")]
        provided = {"python-sci": PYTHON_SCI_PACKAGES, "python-ml": PYTHON_ML_PACKAGES}
        if pyenv_mods:
            have_py = provided.get(pyenv_mods[0].split("/")[0], set())
            redundant = sorted({p for p in pkgs if p in have_py})
            if redundant:
                warns.append(
                    f"{pyenv_mods[0].split('/')[0]} already provides "
                    + ", ".join(redundant)
                    + "; drop these installs (and any venv built only for them)"
                )
        # plan made against an older cluster catalog: its software choices may be stale
        made = plan.get("catalog_generated")
        now = (target.catalog or {}).get("generated")
        if made and now and made != now:
            warns.append(
                f"Planned against the cluster catalog of {str(made)[:16]}; the cluster "
                f"has changed since ({str(now)[:16]}). Re-check the software choices."
            )
        imgs = (plan.get("install") or {}).get("apptainer") or []
        local = {
            c.get("path", "") for c in (target.catalog or {}).get("containers") or []
        }
        for i in imgs:
            if str(i).startswith("/") and str(i) not in local:
                warns.append(f"Container image '{i}' is not in /apps/containers")
    warns += check_script(str(plan.get("script") or ""))
    warns += labguard.science_warnings(plan)
    warns += missing_import_warnings(plan)
    script = str(plan.get("script") or "")
    if (
        "SU2_CFD" in script
        and re.search(r"^TIME_DOMAIN\s*=\s*YES", script, re.M)
        and not re.search(r"^MAX_TIME\s*=", script, re.M)
    ):
        warns.append(
            "SU2 unsteady run without MAX_TIME: SU2 stops at 1 s of physical time by "
            "default, whatever TIME_ITER says (run #92 ran 201 of 2000 steps). Add "
            "MAX_TIME= <total time> to the config."
        )
    inst = plan.get("install") or {}
    builds_python = bool(inst.get("pip") or inst.get("conda")) or any(
        str(m).split("/")[0] in PYTHON_ENV_MODULES for m in inst.get("modules") or []
    )
    if builds_python and _SCRIPT_VENV.search(str(plan.get("script") or "")):
        warns.append(
            "The script builds its own Python venv; that hides packages the harness "
            "installs (and the Python module's). List extra packages under install.pip "
            "and drop the venv lines from the script"
        )
    return warns


# Tokens a model emits when it fails to produce a character; they never belong in code.
_MODEL_ARTIFACTS = ("<unk>", "<pad>", "<|endoftext|>", "<eos>", "\ufffd")
_HEREDOC = re.compile(
    r"(?P<cmd>[^\n]*?)<<-?\s*(?P<q>['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=q)[^\n]*\n"
    r"(?P<body>.*?)\n[ \t]*(?P=tag)[ \t]*(?:\n|$)",
    re.S,
)


# What each site Python module provides (from the catalog usage cards; used to catch a
# script importing a package nobody installs: run #76 imported pandas on python-ml).
# What each Python environment module provides, as IMPORT names. One source of truth
# for both pre-flight checks ("already provides X, drop the install" and "imports X
# but nothing installs it"): two lists that disagreed told the planner to drop and add
# `requests` in turn (run #42, 2026-09-30).
_DIST_TO_IMPORT = {"scikit-learn": "sklearn", "biopython": "Bio", "scikit-image": "skimage",
                   "opencv": "cv2", "pillow": "PIL", "pyyaml": "yaml", "netcdf4": "netCDF4",
                   "pytables": "tables", "jupyterlab": "jupyterlab"}  # fmt: skip
MODULE_PYTHON_PACKAGES = {
    "python-sci": {_DIST_TO_IMPORT.get(p, p) for p in PYTHON_SCI_PACKAGES}
    | {
        "certifi",
        "urllib3",
        "idna",
        "charset_normalizer",
    },  # requests' own dependencies
    "python-ml": {_DIST_TO_IMPORT.get(p, p) for p in PYTHON_ML_PACKAGES}
    | {"matplotlib", "networkx"},  # checked on a compute node 2026-09-29
}
_STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


def script_imports(script: str) -> set[str]:
    """Top-level modules the script's Python imports (heredoc bodies and python -c)."""
    out: set[str] = set()
    for _, body in labguard._python_bodies(script):
        try:
            tree = ast.parse(body)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                out.add(node.module.split(".")[0])
    return {m for m in out if m not in _STDLIB}


def missing_import_warnings(plan: dict) -> list[str]:
    """Imports in the script that no module, pip or conda entry provides."""
    inst = plan.get("install") or {}
    mods = [str(m).split("/")[0] for m in inst.get("modules") or []]
    if inst.get("apptainer") and not any(m in MODULE_PYTHON_PACKAGES for m in mods):
        return []  # Python from a container may carry anything
    pyenv = [m for m in mods if m in MODULE_PYTHON_PACKAGES]
    if not pyenv and not (inst.get("pip") or inst.get("conda")):
        return []
    have: set[str] = set()
    for m in pyenv:
        have |= MODULE_PYTHON_PACKAGES[m]
    listed = [str(x) for x in (inst.get("pip") or []) + (inst.get("conda") or [])]
    have |= set(import_names(listed)) | {_pkg_name(x) for x in listed}
    have |= {_pkg_name(x).replace("-", "_") for x in listed}
    have |= {IMPORT_NAMES.get(_pkg_name(x), _pkg_name(x)) for x in listed}
    if inst.get("conda"):
        have |= {"numpy"}  # every conda Python science package pulls numpy
    if inst.get("conda") or any(
        not m.startswith(("python-sci", "python-ml")) for m in mods
    ):
        # conda envs and other modules bring packages we can't enumerate; only flag
        # imports that are clearly the common scientific stack
        common = {
            "numpy",
            "scipy",
            "pandas",
            "matplotlib",
            "numba",
            "networkx",
            "sklearn",
        }
        need = script_imports(str(plan.get("script") or "")) & common
    else:
        need = script_imports(str(plan.get("script") or ""))
    missing = sorted(m for m in need - have if not m.startswith("_"))
    if not missing:
        return []
    where = f" ({', '.join(pyenv)} doesn't have them)" if pyenv else ""
    return [
        f"The script imports {', '.join(missing)} but nothing installs them{where}; "
        "add them to install.pip (or install.conda)."
    ]


def check_script(script: str) -> list[str]:
    """Static checks on the run script: model artifacts, bash syntax, embedded Python.

    Catches what would otherwise fail seconds into the job (run #16: a `<unk>` token in
    place of `{` inside an f-string). Python is compiled, never executed.
    """
    if not script.strip():
        return []
    warns: list[str] = []
    for tok in _MODEL_ARTIFACTS:
        n = script.count(tok)
        if n:
            line = next(i for i, ln in enumerate(script.splitlines(), 1) if tok in ln)
            shown = "U+FFFD" if tok == "\ufffd" else tok
            warns.append(
                f"Script contains {n} garbled model token(s) '{shown}' (first on line "
                f"{line}); the code there is incomplete"
            )
    try:
        r = subprocess.run(
            ["bash", "-n"], input=script, text=True, capture_output=True, timeout=10
        )
        if r.returncode != 0:
            msg = (r.stderr or "").strip().splitlines()
            warns.append(
                "Shell syntax error in the script: "
                + (msg[0].replace("bash: ", "", 1) if msg else "bash -n failed")[:200]
            )
    except (OSError, subprocess.TimeoutExpired):
        pass  # no bash here: skip, the cluster will still report it
    for m in _HEREDOC.finditer(script):
        cmd, body = m.group("cmd"), m.group("body")
        if m.group("q") == "" and "$" in body:
            continue  # unquoted heredoc: the shell expands it first, can't compile as-is
        is_py = re.search(r"\bpython[0-9.]*\b", cmd) or re.search(
            r"cat\s*>\s*\S+\.py\b", cmd
        )
        if not is_py:
            continue
        try:
            compile(body, m.group("tag"), "exec")
        except SyntaxError as e:
            start = script[: m.start("body")].count("\n") + 1
            ln = start + (e.lineno or 1) - 1
            warns.append(
                f"Python syntax error in the script (line {ln}): {e.msg}"
                + (f": {e.text.strip()[:80]}" if e.text else "")
            )
    return warns


def _dropped_options(old: dict, new: dict) -> list[str]:
    """Command-line options (--name) present in the old script but gone from the new one,
    unless the new plan replaces the software they belonged to (install lists changed)."""
    opt = re.compile(r"(?<![\w-])--[a-zA-Z][\w-]+")
    before = set(opt.findall(str(old.get("script") or "")))
    after = set(opt.findall(str(new.get("script") or "")))
    return sorted(before - after)


# ---------------------------------------------------------------- failure classes
# What kind of failure a log shows decides who can fix it. The AI can fix the plan's
# script; it can't fix a cluster, a registry or its own guess about a tool's internals.
FAILURE_CLASSES = [
    # (class, regex on the log, message shown to the person / given to the AI)
    (
        "install",
        r"\[ERROR\] no install method produced a working environment",
        "No install method produced a working environment (see the [LADDER] lines). "
        "This is the software setup, not the script: change install (another package "
        "source, a module, a container), not the analysis code.",
    ),
    (
        "container",
        r"FATAL:\s+While (?:making image|pulling)|unable to parse image name",
        "A container image could not be pulled or found.",
    ),
    (
        "tool-crash",
        r"Assertion '.*' failed|Segmentation fault|core dumped|signal 11|"
        r"\*\*\* Process received signal",
        "An external program crashed inside itself (assertion or segfault). The usual "
        "cause is an input file it did not expect (name or format): list the files it "
        "wrote (templates, example_* files) and match them exactly.",
    ),
    (
        "glibc",
        r"version `GLIBC_2\.\d+' not found",
        "A binary needs a newer glibc than the compute nodes have (Rocky 8, glibc 2.28). "
        "Pin an older build of that package (conda-forge keeps old versions), or use the "
        "module or a container.",
    ),
    (
        "missing-feature",
        r"lacks? (?:the )?\w+ (?:support|package)|Unrecognized (?:pair|fix|atom) style|"
        r"Package \w+ is not installed|not compiled with",
        "The installed build lacks a feature the plan needs; use a build that has it "
        "(another module variant, conda-forge, or a container).",
    ),
    (
        "numerical",
        r"ZeroDivisionError|FloatingPointError|nan detected|diverg|"
        r"RuntimeWarning: (?:overflow|invalid value)",
        "The computation blew up numerically (division by zero, NaN, divergence). Check "
        "stability limits (time step, relaxation time, CFL) and guard divisions; do not "
        "hide it with try/except.",
    ),
    (
        "tls",
        r"CERTIFICATE_VERIFY_FAILED|unable to get local issuer certificate|"
        r"SSL certificate problem",
        "HTTPS failed certificate checks: the Python from the module has no CA bundle for "
        "urllib. Before any download, export SSL_CERT_FILE (and REQUESTS_CA_BUNDLE) to "
        "certifi's bundle: `export SSL_CERT_FILE=$(python -c 'import certifi; "
        "print(certifi.where())')`. Never disable certificate checks.",
    ),
    (
        "network",
        r"HTTP Error 403|HTTP Error 429|urlopen error|Connection (?:refused|reset|timed out)|"
        r"Read timed out|Temporary failure in name resolution",
        "A download failed (refused, rate-limited or slow). Send a browser-like User-Agent, "
        "use timeouts of 60-120 s, retry with backoff on 429/5xx, and cache responses so a "
        "rerun does not fetch everything again. Do not drop the data it needs.",
    ),
    (
        "api-change",
        # a KeyError alone is usually the script's own dict; only a library's table or
        # frame lookup (astropy Row, pandas) points at a changed API (run #39)
        r"(?:astropy|pandas|table)[^\n]*\n(?:[^\n]*\n){0,12}KeyError: '|"
        r"AttributeError: '\w+' object has no attribute|unexpected keyword argument",
        "A library's data or API differs from what the script assumed (a renamed column, "
        "attribute or argument in the installed version). Print or inspect what the object "
        "actually has (e.g. table.colnames, dir(obj)) and use that, with a fallback for the "
        "old name; do not guess another name blindly.",
    ),
    (
        "syntax",
        r"SyntaxError: |unterminated string literal|IndentationError: ",
        "The script has a syntax error. Check for backslashes lost in JSON escaping (LaTeX "
        "such as \\rangle, \\frac, regex escapes): inside JSON every backslash must be "
        "written as \\\\.",
    ),
    (
        "timeout",
        r"DUE TO TIME LIMIT|CANCELLED AT .* DUE to TIME|exit 124\b",
        "The job ran out of time.",
    ),
    (
        "oom",
        r"oom-kill|Out Of Memory|MemoryError|Killed\s*$",
        "The job ran out of memory.",
    ),
]


def classify_failure(log: str) -> tuple[str, str]:
    """(class, advice) for a failed run's log; ('script', '') when nothing specific."""
    tail = log[-40000:]
    for name, rx, msg in FAILURE_CLASSES:
        if re.search(rx, tail, re.M):
            return name, msg
    return "script", ""


# ------------------------------------------------------------- AI-fix review gate
_REF_FALLBACK = re.compile(
    r"return\s+\(?\s*(-?\d+\.\d+)\s*,\s*0(?:\.0)?\s*\)?|"  # return 1.27, 0.0 on failure
    r"(?:except[^\n]*:\s*\n\s*)\w+\s*=\s*(-?\d+\.\d+)\s*$",
    re.M,
)


def _ref_numbers(script: str) -> set[str]:
    """Numbers the script treats as reference values (expected/REF names, verdict)."""
    out = set()
    for m in re.finditer(
        r"(?:REF|ref|expected|EXPECTED|reference|benchmark)\w*\s*[=:]\s*(-?\d+\.\d+)",
        script,
    ):
        out.add(m.group(1))
    for m in re.finditer(r"['\"]expected['\"]\s*:\s*(-?\d+\.\d+)", script):
        out.add(m.group(1))
    return out


def _imports(script: str) -> set[str]:
    return set(re.findall(r"^\s*(?:import|from)\s+([A-Za-z_]\w*)", script, re.M))


def fix_concerns(old: dict, new: dict) -> list[str]:
    """Things an AI fix did that a person should look at before it runs.

    Deterministic checks on the diff (runs #56, #59, #64): a fallback that returns a
    reference value (a failed fit would then look like agreement), a verdict tolerance or
    expected value that changed, a library removed, a large rewrite, new fixed-text
    findings.
    """
    a = str((old or {}).get("script") or "")
    b = str((new or {}).get("script") or "")
    out: list[str] = []
    refs = _ref_numbers(a) | _ref_numbers(b)
    added = [ln for ln in b.splitlines() if ln not in set(a.splitlines())]
    add_txt = "\n".join(added)
    for m in _REF_FALLBACK.finditer(add_txt):
        val = m.group(1) or m.group(2)
        if val in refs:
            out.append(
                f"a new fallback returns the reference value {val}, so a failed "
                "computation would look like agreement"
            )
    for key in ("tolerance", "expected"):
        rx = rf"['\"]{key}['\"]\s*:\s*([^,}}\n]+)"
        ra = {x.strip() for x in re.findall(rx, a)}
        rb = {x.strip() for x in re.findall(rx, b)}
        if ra and rb and ra != rb:
            out.append(
                f"the verdict's {key} values changed "
                f"(removed {', '.join(sorted(ra - rb)[:3]) or '-'}; "
                f"added {', '.join(sorted(rb - ra)[:3]) or '-'})"
            )
    gone = _imports(a) - _imports(b)
    gone -= {"os", "sys", "re", "json", "time", "math"}
    if gone:
        out.append(f"removes library import(s): {', '.join(sorted(gone))}")
    la, lb = a.splitlines(), b.splitlines()
    if len(la) >= 10:
        import difflib

        ratio = difflib.SequenceMatcher(None, la, lb, autojunk=False).ratio()
        if ratio < 0.7:
            out.append(
                f"rewrites much of the script ({int((1 - ratio) * 100)}% changed)"
            )
    # a changed line that only swaps identifiers inside a formula (k3_v -> k3_u in an
    # RK4 update, run #59): usually a typo the AI introduced, and it changes the maths
    import difflib as _dl

    for op, i1, i2, j1, j2 in _dl.SequenceMatcher(
        None, la, lb, autojunk=False
    ).get_opcodes():
        if op != "replace" or (i2 - i1) != (j2 - j1):
            continue
        for x, y in zip(la[i1:i2], lb[j1:j2]):
            # arithmetic assignment lines only (not strings, titles, shell)
            if not re.match(
                r"\s*[A-Za-z_][\w.\[\]]*\s*[-+*/]?=\s*[^=]", x
            ) or re.search(r"['\"]", x):
                continue
            tx = re.findall(r"[A-Za-z_]\w*|\S", x)
            ty = re.findall(r"[A-Za-z_]\w*|\S", y)
            if len(tx) != len(ty) or not re.search(r"[-+*/]", x.split("=", 1)[-1]):
                continue
            diffs = [(p, q) for p, q in zip(tx, ty) if p != q]
            if 0 < len(diffs) <= 2 and all(
                re.fullmatch(r"[A-Za-z_]\w*", p) and re.fullmatch(r"[A-Za-z_]\w*", q)
                for p, q in diffs
            ):
                sw = ", ".join(f"{p} -> {q}" for p, q in diffs)
                out.append(
                    f"changes a variable inside a formula ({sw}): {y.strip()[:90]}"
                )
    new_hard = set(labguard.hardcoded_findings(b)) - set(labguard.hardcoded_findings(a))
    if new_hard:
        out.append("adds fixed-text results: " + "; ".join(sorted(new_hard)[:2]))
    return out[:6]


def _log_for_fix(log: str, limit: int = 9000) -> str:
    """The part of a job log that explains a failure: its end, plus the first traceback
    or ERROR block when that sits earlier (tools often print pages after the real error)."""
    log = log or ""
    if len(log) <= limit:
        return log or "(no log)"
    tail = log[-(limit * 2 // 3) :]
    m = re.search(
        r"(Traceback \(most recent call last\)|^ERROR|^Error|error:)", log, re.M
    )
    if m and m.start() < len(log) - len(tail):
        head = log[max(0, m.start() - 500) : m.start() + limit // 3]
        return head + "\n[...]\n" + tail
    return log[-limit:]


def _describe(tgt, full: bool) -> str:
    if not tgt:
        return "No cluster configured."
    if getattr(tgt, "catalog", None):
        return tgt.describe_full() if full else tgt.describe_brief()
    return tgt.describe()


def _int(v: Any, default: int) -> int:
    """A resource count from a plan ('2', 2, 2.0) or the default ('auto', '1.5x')."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return int(f) if f == int(f) else default


TIME_RE = re.compile(r"(\d+-)?\d{1,3}(:\d{2}){0,2}")


def estimate_cost(target: SlurmSSHTarget | None, plan: dict) -> float | None:
    if not target:
        return None
    r = plan.get("resources") or {}
    part = target.partitions.get(r.get("partition") or target.default_partition, {})
    rate = part.get("usd_per_hour")
    if rate is None:
        return None
    nodes = max(_int(r.get("nodes"), 1), 1)
    tl = str(r.get("time_limit") or "01:00:00")
    if not TIME_RE.fullmatch(tl):
        tl = "01:00:00"  # build_sbatch falls back the same way
    # shared partitions bill the share of the node the job holds (labcores)
    name = r.get("partition") or target.default_partition
    if name not in target.partitions:
        name = target.default_partition
    share = labcores.node_share(target, name, r)
    return round(nodes * share * _hours(tl) * float(rate), 2)


def _hours(limit: str) -> float:
    """Slurm time limit -> hours, read the way sbatch --time reads it.

    Without a day part: 'MM', 'MM:SS', 'HH:MM:SS'. With one: 'D-HH', 'D-HH:MM',
    'D-HH:MM:SS'.
    """
    days = 0
    s = str(limit).strip()
    parts: list[int]
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d or 0)
        parts = [int(p or 0) for p in s.split(":")] + [0, 0]
        h, m, sec = parts[0], parts[1], parts[2]
    else:
        parts = [int(p or 0) for p in s.split(":")]
        if len(parts) == 1:
            h, m, sec = 0, parts[0], 0
        elif len(parts) == 2:
            h, m, sec = 0, parts[0], parts[1]
        else:
            h, m, sec = parts[-3], parts[-2], parts[-1]
    return days * 24 + h + m / 60 + sec / 3600


# --------------------------------------------------------------------------- prompts

SUGGEST_PROMPT = """You are a computational scientist reviewing a research report. Propose up to 3
computations that could be run on the HPC cluster below to test, quantify or extend claims in
the report with real software and real data or models.

Rules:
- Only propose what is genuinely computable with open-source software (already installed on
  the cluster, or installable from conda-forge, bioconda, PyPI, Spack or a public container)
  and with inputs that are public (datasets, structures, sequences) or can be generated
  (simulations).
- Choose the best science for the question, not only what is preinstalled; installed
  applications start fastest, so prefer them when they fit equally well.
- Use the cluster's real capabilities when they help: multi-node MPI, GPUs, large-memory
  nodes, spot nodes for sweeps over many parameters.
- Prefer decisive computations that finish in minutes to a few hours.
- If nothing in the report is meaningfully computable, return an empty list. Do not invent
  busywork; news summaries, policy and how-to documents usually have nothing to compute.

{target}

REPORT TITLE: {title}

REPORT:
{text}

Return JSON only, in a ```json block:
{{"suggestions": [{{"title": "short name", "question": "the exact scientific question",
"approach": "method and software in one or two sentences", "software": ["package"],
"est_runtime": "e.g. 20 min", "why": "what it would tell the reader"}}],
"note": "one sentence on how computable this report is"}}"""

FIX_PROMPT = """You are fixing a job plan for the HPC cluster below. A pre-flight check found
the problems listed under PROBLEMS. Change ONLY what is needed to fix them: modules, install
lists, resources, the few script lines that set up software, and lines with reported
syntax errors. Keep the science, the
method, the parameters, the inputs and the outputs exactly as they are.

{target}

{lessons}

PROBLEMS:
{problems}

CURRENT PLAN (JSON):
{plan}

Rules:
- Use only module names from the cluster description above, loaded the way it says.
- Python work: load exactly one of python-sci (CPU science: numpy scipy pandas matplotlib
  skyfield astropy xarray ...) or python-ml (PyTorch/GPU). Drop pip/conda installs of packages
  that module already provides, and drop script lines that build a separate venv for them.
  Extra packages the module lacks go in install.pip; the harness installs them into a venv
  on top of the module, so the module's packages stay importable. Never build a venv in the
  script (uv venv, python -m venv): it hides those packages.
- Syntax errors and garbled model tokens (<unk> and similar) in the script: repair only
  those lines so the code is what was evidently intended; change nothing else in the script.
- If a problem cannot be fixed (the science needs something the cluster lacks), leave the
  plan unchanged and say why in "notes".
- Inside JSON strings write every backslash as \\ and line breaks as \n.

Return JSON only, in a ```json block:
{{"plan": <the complete corrected plan, same keys as the current plan>,
"changes": ["one short line per change, e.g. 'python/3.12.14 -> python-sci'"],
"notes": "anything the reviewer should know, or empty"}}"""


RUNFIX_PROMPT = """A job you planned ran on the HPC cluster below and FAILED. Fix the plan so a
rerun gets past this failure. Change ONLY what the error shows is wrong: the lines that
failed, a wrong flag, a missing package or module, a file-format assumption, or resources
(time, memory, partition) if the job was killed for them. Make the SMALLEST change that
fixes it: prefer removing or replacing an unsupported flag, or fixing the lines that
failed, over switching software versions or container images. Change a version only
when the method truly needs it, and then say in "notes" what that risks (e.g. whether
the cluster's GPU driver supports it). Keep the science, method,
parameters and outputs as they are. Do not add retries or try/except that would hide the
error; fix its cause.

{target}

{lessons}

SLURM STATE: {state} (exit {exit_code}, elapsed {elapsed})

END OF THE JOB LOG:
{log}

PLAN THAT FAILED (JSON):
{plan}

Rules:
- Only modules from the cluster description above; one Python environment module at most.
- Inside JSON strings write every backslash as \\\\ and line breaks as \\n.
- An error message can be a symptom. Trace it back through the log to the first thing
  that went wrong (e.g. a model that "cannot estimate a rate" because too few inputs were
  kept: fix why they were dropped, do not remove the analysis option that complained).
  Never remove an analysis step, option or output to make an error go away.
- Every change must appear in "changes". A change you cannot name an error for is not
  allowed.
- If the log does not show why it failed, change nothing and say so in "notes".

Return JSON only, in a ```json block:
{{"plan": <the complete corrected plan, same keys>,
"changes": ["one short line per change, naming the error it fixes"],
"notes": "anything the reviewer should know, or empty"}}"""


PLAN_PROMPT = """You are a computational scientist. Turn the selected material into ONE runnable job
on the HPC cluster below. Use a few web searches (at most 5) to choose the best-established
open-source software for the task and to check exact package names and command-line usage.

{target}

{lessons}
{probes}
SOURCE REPORT: {title}
SELECTED MATERIAL ({scope}):
{text}
{question_line}
Requirements for the job:
- Real software, real data. Never simulate results with sleep, random numbers presented as
  findings, or hard-coded answers. Tiny demonstration inputs are fine only when labelled as
  such in the plan.
- Software: FIRST check the cluster's installed modules and tested recipes below and use
  them when they fit (exact module names; load the compiler/MPI module before modules that
  need it, e.g. install.modules = ["openmpi", "gromacs/2026.1"]). Installing something
  that is already a module wastes 5-10 minutes. Use a prebuilt image from
  /apps/containers by its path inside the script instead of pulling it.
- Otherwise install software inside the job. Preference after modules: a Pixi environment
  from conda-forge/bioconda (list the packages, the harness installs them with
  `pixi add`); else `pip` packages inside that environment; else an Apptainer image
  (list it under install.apptainer; the harness pulls it and exports IMG_<NAME>, NAME being the
  image name without registry or tag, upper-cased, dashes as underscores, e.g.
  docker://vllm/vllm-openai:v0.6.4 -> $IMG_VLLM_OPENAI; use `apptainer exec --nv` for GPUs);
  Spack only for compiled HPC codes (install.spack; the harness builds them in a user
  Spack chained to the site's). The harness verifies each install (imports of listed Python
  packages plus install.verify) and falls back automatically: layered venv -> isolated venv ->
  conda-forge, or Pixi -> relaxed Pixi -> pip; a module that does not load is replaced by its
  conda package. So list what the job needs plainly; do not write install code in the script.
- Download public inputs inside the job (curl/wget work; the cluster has outbound internet).
- The run script runs with the working directory set to the job folder; write every result
  file (CSV, JSON, PNG plots, text summaries) into ./outputs/. Keep outputs under 100 MB.
- Print progress lines. Mark phases with `stage "Running"`, `stage "Post-processing"`
  (a shell function the harness provides).
- Check inputs before the heavy step: right after inputs are downloaded or generated, add a
  few lines under `stage "Checking inputs"` that stop the job (exit 3) with a one-line
  reason if the inputs cannot give a meaningful answer. Fit the check to the job: for
  downloaded data, the record count, coverage of the requested range (dates, regions,
  energies...) and unique identifiers; for generated inputs, that each input file exists
  and is non-empty; for benchmarks, that the server or model answers before timing starts.
  Print what was checked, e.g. "inputs OK: 100 sequences, years 2015-2024, all names
  unique". Check the files the job actually got (after the download), not whether a
  website is reachable. Keep it to seconds and to things that make the result
  meaningless if wrong; do not check the science. Set thresholds loosely (for example at
  least half the requested records) so a check does not stop a run that would have worked.
- Cores: on shared partitions the job gets only the cores it asks for, so set
  resources.cores to what the work can really use (serial code: 1-2; threaded or
  multiprocessing code: its worker count), or resources.whole_node = true when it needs
  every core. Use $SLURM_CPUS_ON_NODE for thread counts, never os.cpu_count(). For MPI
  codes launch with `srun` (Slurm starts one rank per task across all nodes; set
  resources.nodes and resources.ntasks_per_node, one core per rank). Temporary files go
  to $TMPDIR (private, node-local).
  Exit non-zero on failure (the harness uses `set -euo pipefail`).
- Parameter sweeps or many independent cases: the `spot` partition costs about half;
  write the script so a rerun skips cases whose output already exists (spot nodes can be
  reclaimed).
- Pick a partition and node count that fit the problem. The time limit covers software
  installation too (a first pip install of PyTorch-based packages can take 5-10 minutes;
  environments are cached for later runs), so leave headroom.

Return JSON only, in a ```json block, with exactly these keys. It must be valid JSON: inside
strings write every backslash as \\\\ (regexes, LaTeX, Windows paths) and line breaks as \\n.
{{"computable": true or false,
"title": "short name",
"question": "the precise question this job answers",
"why_not": "only when computable is false: why, and what nearby question could be computed",
"approach": "2-4 sentences: method, model or dataset, what is measured",
"software": [{{"name": "...", "source": "module|conda-forge|bioconda|pip|apptainer|spack", "version": "optional", "why": "..."}}],
"inputs": ["data or structures used, with URLs where downloaded"],
"parameters": {{"name": value}},
"parameter_sources": {{"name": "citation (author year, or URL), or 'assumed: why'"}},
"resources": {{"partition": "...", "nodes": 1, "cores": 2, "mem_gb": null, "whole_node": false, "ntasks_per_node": null, "time_limit": "HH:MM:SS", "gpus": 0}},
"install": {{"modules": [], "conda": ["package", ...], "channels": ["conda-forge"], "pip": ["package", "--extra-index-url https://...", ...], "apptainer": ["docker://image:tag"], "spack": ["only for compiled codes that are neither modules nor on conda-forge"], "verify": ["one-line shell checks that prove the software works, e.g. \"SU2_CFD --help | head -1\" or \"python -c 'import rdkit'\""]}},
"script": "bash commands to run after install (no #SBATCH lines, no install commands). Parameters from 'parameters' are exported as env vars named PARAM_<NAME> in upper case; use them.",
"expected_outputs": ["outputs/..."],
"success_criteria": "how to tell the run worked, and what result would count against the claim",
"caveats": "limits of what this computation can show"}}"""

ANALYZE_PROMPT = """You ran a computational job to answer a question raised by a research report.
Write a short results note in Markdown for the report's reader.

QUESTION: {question}
APPROACH: {approach}
PARAMETERS: {params}
SUCCESS CRITERIA: {criteria}
JOB STATE: {state} (Slurm exit {exit_code}, elapsed {elapsed})
CHECKS (outputs/verdict.json): {verdict}
OUTCOME (from the checks): {outcome}

OUTPUT FILES:
{files}

TEXT OUTPUTS (truncated):
{texts}

LOG TAIL:
{log}

Rules: report only what the outputs and log show. Quote the key numbers exactly. Start
**Result** with the OUTCOME word (CONFIRMED, REFUTED, INCONCLUSIVE or BROKEN) and what it
means: REFUTED is a real negative result about the report's claim; INCONCLUSIVE means the
test could not tell (say why, e.g. both arms saturated) and is not evidence either way;
BROKEN means a validation check or the job failed, so do not present the other numbers as
findings. Take
counts of inputs (samples, sequences, structures, cases) from the input or log lines that
state them, not from derived structures (a tree also has internal nodes, a mesh has cells). No LaTeX
(the reader does not render it): write symbols in plain Unicode, e.g. θ, ≤, √2, ×10⁻³. If the job
failed or the outputs do not answer the question, say so plainly and say what to change.
Sections: **Result** (2-4 sentences), **Key numbers** (bullets), **What it means for the
report**, **Limits**, **Next run** (one or two parameter changes worth trying)."""


# --------------------------------------------------------------------------- harness


def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "run").lower()).strip("-")[:n] or "run"


def build_sbatch(
    run_id: int, plan: dict, target: SlurmSSHTarget, sources: list | None = None
) -> str:
    r = plan.get("resources") or {}
    part = str(r.get("partition") or target.default_partition)
    if part not in target.partitions and target.partitions:
        part = target.default_partition
    if not re.fullmatch(r"[\w.-]+", part or ""):
        # never let a plan value put extra lines into run.sbatch
        part = target.default_partition
    nodes = max(_int(r.get("nodes"), 1), 1)
    gpus = max(_int(r.get("gpus"), 0), 0)
    tl = str(r.get("time_limit") or "01:00:00")
    if not TIME_RE.fullmatch(tl):
        tl = "01:00:00"
    inst = plan.get("install") or {}
    mods = [m for m in inst.get("modules") or [] if re.fullmatch(r"[\w./+-]+", str(m))]
    conda = [
        c for c in inst.get("conda") or [] if re.fullmatch(r"[\w.=<>!*,+-]+", str(c))
    ]
    chans = [
        c
        for c in inst.get("channels") or ["conda-forge"]
        if re.fullmatch(r"[\w./:-]+", str(c))
    ]
    pips: list[str] = []
    for p in inst.get("pip") or []:
        p = str(p).strip()
        m = re.fullmatch(r"(--(?:extra-)?index-url)[= ](https://[\w./:-]+)", p)
        if m:  # e.g. the CUDA 12.4 PyTorch wheel index
            pips += [m.group(1), m.group(2)]
        elif re.fullmatch(r"[\w.=<>!\[\],+-]+", p):
            pips.append(p)
    imgs = [
        i for i in inst.get("apptainer") or [] if re.fullmatch(r"[\w./:@-]+", str(i))
    ]
    params = plan.get("parameters") or {}
    exports = "\n".join(
        f"export PARAM_{re.sub(r'[^A-Za-z0-9]', '_', str(k)).upper()}={shlex.quote(str(v))}"
        for k, v in params.items()
    )
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=lab-{task_prefix(target)}{run_id}-{_slug(plan.get('title', ''), 24)}",
        f"#SBATCH --partition={part}",
        f"#SBATCH --nodes={nodes}",
        f"#SBATCH --time={tl}",
        "#SBATCH --output=job.log",
        "#SBATCH --open-mode=append",
    ]
    if gpus:
        lines.append(f"#SBATCH --gres=gpu:{gpus}")
    # cores (and memory) per node: explicit on shared partitions, where a job that
    # asks for nothing gets one core; --exclusive on whole-node partitions (labcores)
    lines += labcores.sbatch_lines(target, part, r)
    if (getattr(target, "partitions", {}) or {}).get(part, {}).get("spot"):
        lines.append("#SBATCH --requeue")  # spot nodes can be reclaimed
    hdr = ((getattr(target, "catalog", None) or {}).get("job_header") or {}).get("path")
    site_header = (
        f"# cluster's site job header (TMPDIR, SCRATCH, caches), published in its catalog\n"
        f"[ -r {shlex.quote(hdr)} ] && . {shlex.quote(hdr)}\n"
        if hdr and re.fullmatch(r"/[\w./-]+", str(hdr))
        else ""
    )
    body = f"""
# Generated by deep-research Lab runs (run #{run_id}). Edit the plan, not this file.
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
mkdir -p outputs
stage() {{ echo "$1" >> "$SLURM_SUBMIT_DIR/stage.txt"; echo "[STAGE] $1"; }}
export -f stage
trap 'rc=$?; if [ $rc -ne 0 ]; then stage "Failed (exit $rc)"; fi' EXIT
echo "[INFO] job $SLURM_JOB_ID on $(hostname), $SLURM_CPUS_ON_NODE CPUs, $(date -u +%FT%TZ)"
export LAB_SMOKE="${{LAB_SMOKE:-0}}"  # 1 = cut-down pilot run (see plan)
[ "$LAB_SMOKE" = 1 ] && echo "[INFO] PILOT: cut-down run"
{site_header}{exports}

stage "Installing software"
"""
    if mods:
        body += "module purge >/dev/null 2>&1 || true\n"
        body += 'LADDER_MOD_FALLBACK=""\n'
        body += "".join(
            f'module load {m} || {{ echo "[LADDER] module {m} did not load; will try '
            f'{m.split("/")[0]} from conda-forge/bioconda"; '
            f'LADDER_MOD_FALLBACK="$LADDER_MOD_FALLBACK {m.split("/")[0]}"; }}\n'
            for m in mods
        )
    body += install_ladder(mods, conda, chans, pips, inst)
    if imgs:
        body += """command -v apptainer >/dev/null 2>&1 || module load apptainer 2>/dev/null || true
if ! command -v apptainer >/dev/null 2>&1; then
  echo "[ERROR] apptainer is not installed on $(hostname); this node cannot run container images."
  exit 3
fi
"""
    for img in imgs:
        var = "IMG_" + _slug(
            img.rsplit("/", 1)[-1].split(":")[0].removesuffix(".sif"), 30
        ).upper().replace("-", "_")
        if img.startswith("/") or img.endswith(".sif"):
            # an image already on the cluster (e.g. /apps/containers/*.sif): use it in
            # place; `apptainer pull` would treat the path as a registry name (run #61)
            body += f"""if [ ! -r {shlex.quote(img)} ]; then
  echo "[ERROR] container image {img} is not on this node"; exit 3
fi
export {var}={shlex.quote(img)}
echo "[INFO] container {img} (on the cluster, used in place)"
"""
            continue
        name = _slug(img, 60)
        body += f"""mkdir -p $HOME/deep-research-lab/images
[ -f $HOME/deep-research-lab/images/{name}.sif ] || apptainer pull $HOME/deep-research-lab/images/{name}.sif {shlex.quote(img)}
export {var}=$HOME/deep-research-lab/images/{name}.sif
echo "[INFO] container {img} -> $HOME/deep-research-lab/images/{name}.sif"
"""
    if sources:
        from deepresearch.sources.staging import staging_block

        body += staging_block(
            sources, getattr(target, "remote_root", "~/deep-research-lab")
        )
    body += f"""
stage "Running"
cat > user_script.sh <<'DR_LAB_EOF'
{plan.get("script", 'echo "no script"; exit 1')}
DR_LAB_EOF
bash -euo pipefail user_script.sh

stage "Done"
echo "[INFO] finished $(date -u +%FT%TZ)"
ls -la outputs
"""
    return "\n".join(lines) + "\n" + body


# ----------------------------------------------------------------------- cluster matching
def workload_shape(plan: dict) -> str:
    """gpu | mpi | sweep | bigmem | cpu, from what the plan says it needs."""
    r = plan.get("resources") or {}
    text = (
        " ".join(
            str(plan.get(k) or "") for k in ("approach", "title", "question")
        ).lower()
        + " "
        + str(plan.get("script") or "")[:20000].lower()
    )
    if _int(r.get("gpus"), 0) > 0 or re.search(
        r"\bcuda\b|--nv\b|\.to\(['\"]cuda|torch\.cuda", text
    ):
        return "gpu"
    if _int(r.get("nodes"), 1) > 1 or re.search(r"\b(srun|mpirun|mpiexec)\b", text):
        return "mpi"
    mem = re.search(r"(\d{3,4})\s*gb\b", text)
    if mem and int(mem.group(1)) > 100:
        return "bigmem"
    if re.search(
        r"parameter sweep|monte carlo|replicates|xargs -p|job array|sbatch --array",
        text,
    ):
        return "sweep"
    return "cpu"


def suggest_partition(
    target, plan: dict, stocked_out: set[str] | None = None
) -> tuple[str, str]:
    """(partition, reason) that fits the plan's shape and is not stocked out."""
    parts = getattr(target, "partitions", {}) or {}
    want = (plan.get("resources") or {}).get("partition") or target.default_partition
    out = set(stocked_out or ())
    shape = workload_shape(plan)
    by_shape = {
        "gpu": ["gpul4"],
        "mpi": ["computehigh", "standard"],
        "bigmem": ["highmem"],
        "sweep": ["computehigh", "spot", "standard"],
        "cpu": ["computehigh", "standard"],
    }[shape]
    ok = [p for p in by_shape if p in parts and p not in out]
    warm = getattr(target, "warm", None) or {}
    if (
        shape == "cpu"
        and want != target.default_partition
        and target.default_partition in ok
        and warm.get("always_on")
        and warm.get("partition") == target.default_partition
        and max(_int((plan.get("resources") or {}).get("nodes"), 1), 1) == 1
    ):
        # a single-node CPU job planned elsewhere would boot a node; the default
        # partition has an always-on warm node that starts it in seconds (v0.48.1)
        return (
            target.default_partition,
            f"single-node CPU work starts in seconds on the always-on warm node "
            f"('{target.default_partition}') instead of booting a '{want}' node",
        )
    if want in parts and want not in out:
        if (shape == "gpu") == bool(parts[want].get("gpus")):
            return want, ""
    if ok:
        why = (
            f"'{want}' is stocked out in the zone"
            if want in out
            else f"a {shape} job fits '{ok[0]}' better than '{want}'"
        )
        return ok[0], why
    return want, ""


def time_from_history(db_path: str, plan: dict, floor_min: int = 10) -> str | None:
    """A time limit from similar completed runs (same software), or None.

    3x the longest similar run plus 5 minutes for installs, rounded up to 5 minutes,
    never below `floor_min`. Similar = shares a software/package name with the plan.
    """
    keys = set(labguard.software_keys(plan))
    if not keys:
        return None
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            rows = conn.execute(
                "SELECT plan, elapsed FROM lab_runs WHERE status='completed' AND elapsed IS NOT NULL "
                "ORDER BY id DESC LIMIT 300"
            ).fetchall()
    except sqlite3.Error:
        return None
    worst = 0
    n = 0
    for pj, el in rows:
        try:
            other = json.loads(pj or "{}")
        except ValueError:
            continue
        if not keys & set(labguard.software_keys(other)):
            continue
        el = str(el)
        sec = int(_hours(el) * 3600) if TIME_RE.fullmatch(el) else 0
        if sec:
            worst, n = max(worst, sec), n + 1
    if not n:
        return None
    mins = max(floor_min, -(-(worst * 3 + 300) // 300) * 5)
    return f"{mins // 60:02d}:{mins % 60:02d}:00"


def _uses_bifrost(target) -> bool:
    """A target opts in with a `bifrost` block in lab_targets.json ({} or {url})."""
    cfg = getattr(target, "cfg", None) or {}
    return "bifrost" in cfg and cfg["bifrost"] not in (False, None)


def _bifrost_for(targets: dict, state_dir: Path):
    """The bifrost client for the first target with a `bifrost` block, if signed in."""
    from deepresearch.dashboard import bifrost as bf

    for t in targets.values():
        if not _uses_bifrost(t):
            continue
        cfg = (getattr(t, "cfg", None) or {}).get("bifrost")
        cfg = cfg if isinstance(cfg, dict) else {}
        c = bf.BifrostClient(state_dir, url=str(cfg.get("url") or bf.DEFAULT_URL))
        return c if c.signed_in() else None
    return None


def stocked_out_partitions(target, client=None) -> set[str]:
    """Partitions whose nodes failed to start for lack of GCP capacity.

    From bifrost's cluster_status when the Lab is signed in to it (R1), else sinfo -R
    over SSH."""
    if client is not None:
        from deepresearch.dashboard import bifrost as bf

        try:
            return bf.stockouts(client)
        except bf.BifrostError:
            pass  # fall back to the SSH scrape
    try:
        out = target.sh(
            "sinfo -h -R -o '%E|%N' 2>/dev/null | grep -iE 'RESOURCE_POOL_EXHAUSTED|stockout|"
            "ZONE_RESOURCE|insufficient capacity' | cut -d'|' -f2; "
            "echo '::P::'; sinfo -h -o '%R|%N' 2>/dev/null",
            timeout=40,
        )
    except Exception:
        return set()
    bad_nodes, _, parts = out.partition("::P::")
    prefixes = {
        re.sub(r"[\[\d].*$", "", n.strip()) for n in bad_nodes.split() if n.strip()
    }
    res = set()
    for ln in parts.splitlines():
        p, _, nodes = ln.partition("|")
        if any(nodes.strip().startswith(px) for px in prefixes if px):
            res.add(p.strip())
    return res


# ----------------------------------------------------------------------- planner probes
PROBE_PROMPT = """You are about to plan a computational job on the HPC cluster below. Before
writing the plan, you may check facts on a compute node: installed module details, program
help text and versions, Python package versions and function signatures, and whether
download URLs work. List the checks that would most reduce the risk of the job failing
(wrong flags, removed APIs, wrong output file names, dead links). At most {max_checks}.

{target}

SOURCE REPORT: {title}
MATERIAL (excerpt):
{text}
{question_line}
Check kinds (use exactly these):
- {{"kind": "module", "name": "su2/8.2.0", "load": ["openmpi"]}}  -> `module show` and the programs it adds
- {{"kind": "help", "cmd": "SU2_CFD --help", "load": ["openmpi", "su2/8.2.0"]}}  -> first 150 lines of output
- {{"kind": "pyversion", "package": "ortools"}}  -> versions available on PyPI and the one in python-sci
- {{"kind": "pyhelp", "target": "ortools.sat.python.cp_model.CpModel.NewFixedSizeIntervalVar", "pip": ["ortools"]}}  -> signature and docstring
- {{"kind": "url", "url": "https://..."}}  -> HTTP status and size
- {{"kind": "features", "cmd": "lmp", "load": ["openmpi", "lammps"]}}  -> the program's full `-h` output
  (compiled-in packages, styles, solvers): use it when the plan relies on an optional package
  (LAMMPS GRANULAR, a GROMACS GPU build, an SU2 option)
- {{"kind": "conda", "package": "lammps"}}  -> versions on conda-forge/bioconda

Return JSON only, in a ```json block: {{"checks": [ ... ]}}"""

PROBE_KINDS = ("module", "help", "pyversion", "pyhelp", "url", "features", "conda")
_SAFE_TOKEN = re.compile(r"[\w.+/:=@-]+")


def _probe_cmd(chk: dict) -> str | None:
    """Shell for one probe, or None when it is unsafe or malformed.

    Probes are read-only: module show/avail, `<program> --help|-h|--version|-version`,
    pip index/download metadata into a throwaway venv, Python `help()` on a dotted name,
    and `curl -sI` on http(s) URLs. Anything else is refused.
    """
    kind = chk.get("kind")
    loads = [str(m) for m in chk.get("load") or [] if _SAFE_TOKEN.fullmatch(str(m))][:4]
    pre = "module purge >/dev/null 2>&1; " + "".join(
        f"module load {shlex.quote(m)} >/dev/null 2>&1; " for m in loads
    )
    if kind == "module":
        name = str(chk.get("name") or "")
        if not _SAFE_TOKEN.fullmatch(name):
            return None
        q = shlex.quote(name)
        return (
            pre + f"module show {q} 2>&1 | head -60; "
            f'for d in $(module show {q} 2>&1 | sed -n \'s/.*prepend_path("PATH","\\([^"]*\\)").*/\\1/p\'); '
            'do echo "programs in $d:"; ls "$d" 2>/dev/null | head -40; done'
        )
    if kind == "help":
        parts = str(chk.get("cmd") or "").split()
        if (
            not parts
            or len(parts) > 3
            or not all(_SAFE_TOKEN.fullmatch(p) for p in parts)
        ):
            return None
        flags = parts[1:]
        if any(
            f not in ("--help", "-h", "-help", "--version", "-version", "-V", "help")
            for f in flags
        ):
            return None
        return (
            pre
            + "timeout 30 "
            + " ".join(shlex.quote(p) for p in parts)
            + " 2>&1 < /dev/null | head -150"
        )
    if kind == "features":
        prog = str(chk.get("cmd") or "").split()
        if len(prog) != 1 or not re.fullmatch(r"[\w.+-]+", prog[0]):
            return None
        q = shlex.quote(prog[0])
        # the whole help text, with package/feature sections first
        return (
            pre
            + f"H=$(timeout 30 {q} -h 2>&1 < /dev/null || timeout 30 {q} --help 2>&1 < /dev/null); "
            + 'echo "$H" | grep -i -A12 -E "installed packages|compiled|features|build" | head -60; '
            + 'echo "--- full help (head)"; echo "$H" | head -120'
        )
    if kind == "conda":
        pkg = str(chk.get("package") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", pkg):
            return None
        return (
            "export PATH=/apps/pixi/bin:$PATH; "
            f"timeout 90 pixi search -c conda-forge -c bioconda {shlex.quote(pkg)} 2>&1 | head -25"
        )
    if kind == "pyversion":
        pkg = str(chk.get("package") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", pkg):
            return None
        return (
            "module load python-sci >/dev/null 2>&1; "
            f'echo "python-sci: $(python3 -c \'import importlib.metadata as m; print(m.version(\\"{pkg}\\"))\' 2>/dev/null || echo not installed)"; '
            f"timeout 60 python3 -m pip index versions {shlex.quote(pkg)} 2>/dev/null | head -3"
        )
    if kind == "pyhelp":
        target = str(chk.get("target") or "")
        pips = [
            str(p)
            for p in chk.get("pip") or []
            if re.fullmatch(r"[A-Za-z0-9_.=<>!-]+", str(p))
        ][:3]
        if not re.fullmatch(r"[A-Za-z_][\w.]*", target):
            return None
        mod = target.split(".")[0]
        inst = (
            'V="$TMPDIR/probe-venv"; python3 -m venv --system-site-packages "$V" >/dev/null && '
            f'"$V/bin/python" -m pip install -q {" ".join(shlex.quote(p) for p in pips)} >/dev/null 2>&1; PY="$V/bin/python"; '
            if pips
            else "PY=python3; "
        )
        code = (
            "import importlib, inspect, pydoc, sys\n"
            f"t = {target!r}\n"
            "parts = t.split('.')\n"
            "obj = None\n"
            "for i in range(len(parts), 0, -1):\n"
            "    try:\n"
            "        obj = importlib.import_module('.'.join(parts[:i]))\n"
            "        for a in parts[i:]:\n"
            "            obj = getattr(obj, a)\n"
            "        break\n"
            "    except (ImportError, AttributeError) as e:\n"
            "        err = e\n"
            "if obj is None:\n"
            "    print('NOT FOUND:', err); sys.exit(0)\n"
            "try:\n"
            "    print('signature:', inspect.signature(obj))\n"
            "except (TypeError, ValueError):\n"
            "    pass\n"
            "print(pydoc.render_doc(obj, renderer=pydoc.plaintext)[:4000])\n"
            f"m = importlib.import_module({mod!r}); print('version:', getattr(m, '__version__', '?'))\n"
        )
        return (
            "module load python-sci >/dev/null 2>&1; "
            + inst
            + f"timeout 60 $PY - <<'PYEOF'\n{code}PYEOF"
        )
    if kind == "url":
        url = str(chk.get("url") or "")
        if not re.fullmatch(r"https?://[\w.:/%?=&~+@,-]+", url) or len(url) > 400:
            return None
        return (
            f"curl -sSIL -m 20 -o /dev/null -w 'HTTP %{{http_code}}, %{{size_download}} bytes\\n' {shlex.quote(url)} 2>&1 | head -c 300; "
            f"curl -sSL -m 20 -r 0-2047 {shlex.quote(url)} 2>/dev/null | head -c 400 | tr -c '[:print:]\\n' '.'"
        )
    return None


def probe_script(checks: list[dict]) -> tuple[str, list[dict]]:
    """A bash script running each accepted probe with a header line; (script, accepted)."""
    ok = []
    lines = [
        "#!/bin/bash",
        "set +e",
        "[ -r /apps/docs/templates/job-header.sh ] && . /apps/docs/templates/job-header.sh >/dev/null 2>&1",
    ]
    for i, c in enumerate(checks):
        if not isinstance(c, dict) or c.get("kind") not in PROBE_KINDS:
            continue
        cmd = _probe_cmd(c)
        if not cmd:
            continue
        ok.append(c)
        lines.append(
            f"echo '=== CHECK {len(ok)}: {json.dumps(c)[:200].replace(chr(39), '')}'"
        )
        lines.append(f"( {cmd} ) 2>&1 | head -c 6000")
    return "\n".join(lines) + "\n", ok


_URL_IN_SCRIPT = re.compile(r"https?://[^\s'\"<>)\\]+")


def script_urls(plan: dict) -> list[str]:
    """Download URLs in a plan's script and inputs (not docs links), deduplicated."""
    text = (
        str(plan.get("script") or "")
        + "\n"
        + "\n".join(str(x) for x in plan.get("inputs") or [])
    )
    urls = []
    for u in _URL_IN_SCRIPT.findall(text):
        u = u.rstrip(".,;:")
        if "$" in u or "{" in u or u in urls:
            continue
        urls.append(u)
    return urls[:15]


# ----------------------------------------------------------------------- install ladder
# Import names that differ from the package name (for the automatic import check).
# conda-forge package names that differ from the pip name. On conda-forge `gmsh` is the
# C++ library and program only; the Python module is `python-gmsh` (run #86: the pixi
# rung installed gmsh, then `import gmsh` failed). Pip gmsh wheels need libGLU, which
# Rocky 8 compute nodes lack, so the conda route is the one that works.
CONDA_NAMES = {
    "gmsh": "python-gmsh",
    "pytorch": "pytorch",
    "torch": "pytorch",
    "opencv-python": "opencv",
    "opencv-python-headless": "opencv",
    "tables": "pytables",
    "rdkit-pypi": "rdkit",
    "scikit-rf": "scikit-rf",
}


IMPORT_NAMES = {
    "scikit-learn": "sklearn",
    "scikit-image": "skimage",
    "opencv-python": "cv2",
    "opencv-python-headless": "cv2",
    "pillow": "PIL",
    "pyyaml": "yaml",
    "biopython": "Bio",
    "beautifulsoup4": "bs4",
    "python-dateutil": "dateutil",
    "batman-package": "batman",
    "pytorch": "torch",
    "tensorflow-cpu": "tensorflow",
    "ortools": "ortools",
    "netcdf4": "netCDF4",
    "pytables": "tables",
    "tables": "tables",
    "py3dmol": "py3Dmol",
    "rdkit-pypi": "rdkit",
    "mdanalysis": "MDAnalysis",
    "pymatgen": "pymatgen",
    "ase": "ase",
    "openmm": "openmm",
    "pyscf": "pyscf",
    "networkx": "networkx",
    "simpy": "simpy",
    "pythermalcomfort": "pythermalcomfort",
    "astropy": "astropy",
    "skyfield": "skyfield",
    "treetime": "treetime",
    "phylo-treetime": "treetime",
    "nextstrain-augur": "augur",
    "augur": "augur",
    "jax": "jax",
    "jaxlib": "jaxlib",
    "numba": "numba",
    "sympy": "sympy",
    "scikit-rf": "skrf",
    "python-gmsh": "gmsh",
}
# conda-only tools with no Python import: never import-checked
NO_IMPORT = {
    "python",
    "pip",
    "gcc",
    "gxx",
    "gfortran",
    "make",
    "cmake",
    "openmpi",
    "mpich",
    "iqtree",
    "mafft",
    "raxml-ng",
    "blast",
    "samtools",
    "bwa",
    "gromacs",
    "lammps",
    "cp2k",
    "quantum-espresso",
    "nodejs",
    "r-base",
    "julia",
    "fftw",
    "hdf5",
    "compilers",
    "cxx-compiler",
    "c-compiler",
    "fortran-compiler",
    "ffmpeg",
}


def _pkg_name(spec: str) -> str:
    return re.split(r"[<>=!~\[ ;]", spec.strip(), maxsplit=1)[0].lower()


# import name -> PyPI/conda package, for modules found in verify commands
IMPORT_TO_PKG = {v: k for k, v in IMPORT_NAMES.items()}
_VERIFY_IMPORT = re.compile(r"\bimport\s+([\w.]+(?:\s*,\s*[\w.]+)*)")
_VERIFY_FROM = re.compile(r"\bfrom\s+([\w.]+)\s+import\b")


def verify_imports(verify: list[str]) -> list[str]:
    """Top-level third-party modules imported by the plan's verify commands."""
    std = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    out: list[str] = []
    for v in verify:
        if "python" not in v:
            continue
        froms = _VERIFY_FROM.findall(v)
        rest = re.sub(r"\bfrom\s+[\w.]+\s+import\s+[\w.]+(?:\s*,\s*[\w.]+)*", " ", v)
        mods = [m for grp in _VERIFY_IMPORT.findall(rest) for m in grp.split(",")]
        mods += froms
        for m in mods:
            top = m.strip().split(".")[0]
            if (
                top
                and top not in std
                and top not in out
                and re.fullmatch(r"[A-Za-z_]\w*", top)
            ):
                out.append(top)
    return out


def import_names(pkgs: list[str]) -> list[str]:
    """Python import names to verify an environment with (best effort)."""
    out = []
    for p in pkgs:
        if p.startswith(("--", "https:")):
            continue
        n = _pkg_name(p)
        if not n or n in NO_IMPORT or n.startswith(("r-", "lib")):
            continue
        mod = IMPORT_NAMES.get(n, n.replace("-", "_"))
        if re.fullmatch(r"[A-Za-z_][\w.]*", mod):
            out.append(mod)
    return sorted(set(out))


_LADDER_FUNCS = r"""
# ---- install ladder: try each way to get the software, keep the first that verifies
LADDER_LOG="$SLURM_SUBMIT_DIR/outputs/environment.json"
ladder_verify() {  # python-bin: import checks and the plan's verify commands
  local py=$1 rc=0
  if [ -n "$LADDER_IMPORTS" ]; then
    "$py" - "$LADDER_IMPORTS" <<'PYEOF' || rc=1
import importlib, sys
bad = []
for m in sys.argv[1].split():
    try:
        importlib.import_module(m)
    except Exception as e:  # noqa: BLE001
        bad.append(f"{m}: {type(e).__name__}: {e}"[:300])
if bad:
    print("[LADDER] import check failed: " + "; ".join(bad))
    sys.exit(1)
print("[LADDER] imports OK: " + sys.argv[1])
PYEOF
  fi
  if [ $rc = 0 ] && [ -n "${LADDER_VERIFY:-}" ]; then
    # every line must succeed (eval of a multi-line string only reports the last one:
    # `lmp -h | grep -q GRANULAR` failed and was ignored, run #75)
    local line
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      # no pipefail: `cmd | grep -q X` exits early and cmd dies of SIGPIPE (141), a
      # false failure that marked a working LAMMPS env bad (run #81)
      ( set +e +o pipefail; eval "$line" ) >> "$TMPDIR/ladder_verify.log" 2>&1 || {
        echo "[LADDER] verify failed: $line"; tail -20 "$TMPDIR/ladder_verify.log"; rc=1; break; }
    done <<< "$LADDER_VERIFY"
  fi
  return $rc
}
ladder_record() {  # rung envdir
  local py; py=$(command -v python3 || command -v python || true)
  local lock=""
  if [ -n "$py" ]; then lock=$("$py" -m pip freeze 2>/dev/null | head -400 | tr '\n' ';' | sed 's/"/\\"/g'); fi
  printf '{"rung": "%s", "env": "%s", "tried": "%s", "python": "%s", "packages": "%s"}\n' \
    "$1" "$2" "${LADDER_TRIED# }" "$($py -V 2>&1 | tr -d '\n')" "$lock" > "$LADDER_LOG"
  mkdir -p "$HOME/deep-research-lab/envs"
  printf '{"key": "%s", "rung": "%s", "run": "%s", "when": "%s"}\n' "$LADDER_KEY" "$1" \
    "$(basename "$SLURM_SUBMIT_DIR")" "$(date -u +%FT%TZ)" >> "$HOME/deep-research-lab/envs/ladder.jsonl"
  echo "[LADDER] using rung $1 ($2)"
}
ladder_try() {  # name envdir build-commands...: build once (cached), activate, verify
  local name=$1 dir=$2; shift 2
  LADDER_TRIED="$LADDER_TRIED $name"
  echo "[LADDER] trying $name"
  mkdir -p "$(dirname "$dir")"
  exec 9>"$dir.lock"; flock 9
  # .bad records which ladder version failed: a failure caused by a ladder bug (not the
  # packages) must not poison the cache for later versions (runs #81, #83)
  if [ -f "$dir/.bad" ] && [ "$(cat "$dir/.bad" 2>/dev/null)" = "$LADDER_VERSION" ]; then
    echo "[LADDER] $name failed before for this package list; skipping"; flock -u 9; return 1
  fi
  [ -f "$dir/.bad" ] && echo "[LADDER] $name failed under an older ladder; retrying"
  rm -f "$dir/.bad"
  if [ ! -f "$dir/.ready" ]; then
    rm -rf "$dir"
    if ! ( set -e; "$@" ) ; then
      echo "[LADDER] $name: install failed"; mkdir -p "$dir"; echo "$LADDER_VERSION" > "$dir/.bad"; flock -u 9; return 1
    fi
    touch "$dir/.ready"
  else
    echo "[INFO] reusing cached environment $dir"
  fi
  flock -u 9
  return 0
}
"""


# Changes whenever the ladder's shell code changes, so a failure recorded by an older
# (possibly buggy) ladder is retried instead of skipped forever.
LADDER_VERSION = hashlib.sha256(_LADDER_FUNCS.encode()).hexdigest()[:10]


def install_ladder(
    mods: list[str], conda: list[str], chans: list[str], pips: list[str], inst: dict
) -> str:
    """Shell that installs the plan's software, falling back rung by rung.

    Python work: (1) venv layered on the Python module; (2) isolated venv on the
    module's Python (native wheels that clash with the module, e.g. OR-Tools); (3) a
    Pixi environment with everything from conda-forge (pip for what conda lacks).
    Conda/Pixi plans: (1) Pixi as listed; (2) Pixi with conda-forge + bioconda and the
    Python pins dropped; (3) pip into a plain venv. Every rung is verified (imports of
    the listed packages plus the plan's install.verify commands) before it is used; a
    rung that fails is cached as bad for that package list. A module that does not load
    is replaced by its conda package. The rung used goes to outputs/environment.json and
    ~/deep-research-lab/envs/ladder.jsonl (read by later plans).
    """
    spack = [
        str(x)
        for x in inst.get("spack") or []
        if re.fullmatch(r"[\w.@%+~=^-]+", str(x))
    ][:4]
    spack_sh = _spack_block(spack) if spack else ""
    pymods = [m for m in mods if m.split("/")[0] in PYTHON_ENV_MODULES]
    names = [x for x in pips if not x.startswith(("--", "https:"))]
    verify = [
        str(v)
        for v in (inst.get("verify") or [])
        if isinstance(v, str) and "\n" not in v
    ][:10]
    imports = import_names(names + conda)
    # packages the verify commands import (python -c 'import numba, scipy.sparse'): the
    # isolated/conda rungs must install them too, since they don't have the module's set
    vimports = verify_imports(verify)
    vpk = [IMPORT_TO_PKG.get(m, m) for m in vimports]
    # the MPI/module fallbacks can add conda packages at run time
    if not (conda or pips or verify):
        return spack_sh + (
            'if [ -n "${LADDER_MOD_FALLBACK:-}" ]; then\n'
            + _pixi_fallback_only(chans)
            + "fi\n"
        )
    # verify is part of the key: a rung marked bad for one plan's checks must not be
    # skipped for a plan with different checks
    key = hashlib.sha256(
        json.dumps([mods, sorted(conda), sorted(chans), pips, verify]).encode()
    ).hexdigest()[:12]
    base = f"$HOME/deep-research-lab/envs/{_slug('-'.join(sorted(conda + names)) or 'env', 40)}-{key}"
    out = _LADDER_FUNCS
    out += f"LADDER_VERSION={LADDER_VERSION}\n"
    out += f'LADDER_KEY={shlex.quote(key)}\nLADDER_TRIED=""\n'
    out += f"LADDER_IMPORTS={shlex.quote(' '.join(imports))}\n"
    out += "LADDER_VERIFY=" + shlex.quote("\n".join(verify)) + "\n"
    out += "export PATH=/apps/pixi/bin:$HOME/.pixi/bin:$PATH\n"
    out += "export PIXI_CACHE_DIR=${PIXI_CACHE_DIR:-$HOME/.cache/rattler}\n"
    q = " ".join(shlex.quote(p) for p in pips)
    isolate_first = any(_pkg_name(n) in ISOLATE_FROM_MODULE_PIP for n in names)
    have = [_pkg_name(n) for n in names]
    extra = " ".join(
        shlex.quote(p) for p in dict.fromkeys(ISOLATED_BASE_PIP + vpk) if p not in have
    )
    rungs: list[tuple[str, str, str]] = []  # name, envdir, build commands (bash)
    out += 'LADDER_OK=""\n'
    if pymods and not conda and not pips:
        # nothing to add: the module itself is rung 1 (no venv, nothing to build)
        out += (
            '( ladder_verify python3 ) && { LADDER_OK=module; LADDER_TRIED=" module"; '
            f"ladder_record module {shlex.quote(pymods[0])}; }} "
            '|| { LADDER_TRIED=" module"; echo "[LADDER] module did not verify"; }\n'
        )
    if pymods and not conda and pips:
        # --system-site-packages is not enough when the module is itself a venv
        # (python-ml: its packages live in its own site-packages, not the base Python's,
        # so a layered venv saw none of them, run #76). A .pth file pointing at the
        # module's site-packages layers it in both cases.
        layered = (
            "layered-venv",
            f"{base}-layered",
            f'python3 -m venv --system-site-packages "{base}-layered" && '
            f'SP=$(python3 -c "import site; print(chr(10).join(site.getsitepackages()))") && '
            f'for d in "{base}-layered"/lib*/python3*/site-packages; do '
            f'echo "$SP" > "$d/_site_module.pth"; done && '
            f'"{base}-layered/bin/python" -m pip install --progress-bar off {q}',
        )
        isolated = (
            "isolated-venv",
            f"{base}-isolated",
            f'python3 -m venv "{base}-isolated" && '
            f'"{base}-isolated/bin/python" -m pip install --progress-bar off {extra} {q}',
        )
        rungs += [isolated, layered] if isolate_first else [layered, isolated]
    if pymods and not conda:
        if not pips:
            rungs.append(
                (
                    "isolated-venv",
                    f"{base}-isolated",
                    f'python3 -m venv "{base}-isolated" && '
                    f'"{base}-isolated/bin/python" -m pip install --progress-bar off {extra}',
                )
            )
        conda_all = list(
            dict.fromkeys(
                ["python=3.12", "pip", "numpy", "scipy", "pandas", "matplotlib"] + vpk
            )
        )
        add_names = " ".join(
            shlex.quote(CONDA_NAMES.get(_pkg_name(n).lower(), _pkg_name(n)))
            for n in names
        )
        rungs.append(
            (
                "pixi-conda-forge",
                f"{base}-pixi",
                f'mkdir -p "{base}-pixi" && cd "{base}-pixi" && pixi init -c conda-forge . >/dev/null && '
                f"pixi add {' '.join(shlex.quote(c) for c in conda_all)}"
                + (
                    f" && (pixi add {add_names} || "
                    f"pixi run python -m pip install --progress-bar off {q})"
                    if names
                    else ""
                ),
            )
        )
    elif not (pymods and not conda):
        # a script that imports gmsh needs python-gmsh next to a bare conda `gmsh`
        pk = list(conda)
        for c in conda:
            alt = CONDA_NAMES.get(_pkg_name(c).lower())
            if (
                alt
                and alt != _pkg_name(c)
                and alt not in pk
                and alt.startswith("python-")
            ):
                pk.append(alt)
        # a pinned old build (e.g. lammps=2023.08.02 for glibc 2.28) may not exist for a
        # new Python: don't force python=3.12 next to a pin, let the solver choose
        pinned_pkg = any(
            re.search(r"[=<>]", c) and not re.match(r"python\b", c) for c in conda
        )
        if pips or not any(re.match(r"python\b", c) for c in pk):
            if not any(re.match(r"python\b", c) for c in pk) and (pips or imports):
                pk.append("python" if pinned_pkg else "python=3.12")
        if pips and not any(re.match(r"pip\b", c) for c in pk):
            pk.append("pip")
        chan_args = " ".join("-c " + shlex.quote(c) for c in chans)
        pip_step = (
            f" && pixi run python -m pip install --progress-bar off {q}" if pips else ""
        )
        rungs.append(
            (
                "pixi",
                f"{base}-pixi",
                f'mkdir -p "{base}-pixi" && cd "{base}-pixi" && pixi init {chan_args} . >/dev/null && '
                f"pixi add {' '.join(shlex.quote(c) for c in pk)}{pip_step}",
            )
        )
        loose = [c for c in pk if not re.match(r"python\b", c)] + ["python", "pip"]
        # the relaxed rung drops version pins, but keeps pins on compiled programs
        # (NO_IMPORT: lammps, gromacs...): those pins are usually there for a reason the
        # solver can't see (glibc 2.28 on the nodes, run #75)
        vtxt = " ".join(verify).lower()
        loose = [
            c
            if c.startswith("python")
            or (re.search(r"[=<>]", c) and _pkg_name(c) in NO_IMPORT)
            else re.split(r"[=<>]", c)[0]
            for c in loose
        ]
        rungs.append(
            (
                "pixi-loose",
                f"{base}-pixi2",
                f'mkdir -p "{base}-pixi2" && cd "{base}-pixi2" && '
                f"pixi init -c conda-forge -c bioconda . >/dev/null && "
                f"pixi add {' '.join(shlex.quote(c) for c in sorted(set(loose)))}{pip_step}",
            )
        )
        pyish = [
            _pkg_name(c)
            for c in conda
            if _pkg_name(c) not in NO_IMPORT and not c.startswith("python")
        ]
        # pip can't provide a compiled program the checks call (lmp, gmx...): a pip rung
        # would "verify" Python imports and then fail at run time (run #75: lmp: command
        # not found after the pip-venv rung)
        needs_binary = any(
            _pkg_name(c).lower() in vtxt and _pkg_name(c) in NO_IMPORT for c in conda
        ) or bool(re.search(r"\blmp\b|\bgmx\b|SU2_CFD|\bnvcc\b", " ".join(verify)))
        if (pyish or pips) and not needs_binary:
            rungs.append(
                (
                    "pip-venv",
                    f"{base}-pip",
                    f'python3 -m venv "{base}-pip" && "{base}-pip/bin/python" -m pip install '
                    f"--progress-bar off {' '.join(shlex.quote(x) for x in pyish)} {q}",
                )
            )
    for name, envdir, build in rungs:
        act = (
            f'set +u; eval "$(cd "{envdir}" && pixi shell-hook)"; set -u; '
            "if declare -F ursa_conda_libs >/dev/null; then ursa_conda_libs; else "
            'export LD_LIBRARY_PATH="$CONDA_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"; fi'
            if name.startswith("pixi")
            else f'. "{envdir}/bin/activate"'
        )
        py = "python3"
        out += (
            f'if [ -z "$LADDER_OK" ] && ladder_try {name} "{envdir}" bash -c {shlex.quote(build)}; then\n'
            f'  ( {act}; ladder_verify {py} ) && {{ {act}; LADDER_OK={name}; ladder_record {name} "{envdir}"; }} '
            f'|| {{ echo "[LADDER] {name} did not verify"; echo "$LADDER_VERSION" > "{envdir}/.bad"; }}\n'
            "fi\n"
        )
    if inst.get("apptainer"):
        # the plan also lists container images; they may carry what the rungs lacked
        out += (
            'if [ -z "$LADDER_OK" ]; then\n'
            '  echo "[LADDER] no Python/conda rung worked (tried:$LADDER_TRIED); '
            'continuing with the container image(s)"\n'
            "fi\n"
        )
    else:
        out += (
            'if [ -z "$LADDER_OK" ]; then\n'
            '  echo "[ERROR] no install method produced a working environment (tried:$LADDER_TRIED)"\n'
            "  exit 4\nfi\n"
        )
    out += (
        'if [ -n "${LADDER_MOD_FALLBACK:-}" ]; then\n'
        + _pixi_fallback_only(chans)
        + "fi\n"
    )
    return spack_sh + out


def _spack_block(specs: list[str]) -> str:
    """User-level Spack builds chained to the site's /apps/spack (reuses its packages).

    For compiled codes that are neither a module nor on conda-forge. The user instance
    lives in ~/deep-research-lab/spack and is shared by later jobs.
    """
    q = " ".join(shlex.quote(s) for s in specs)
    names = " ".join(specs)
    return (
        f"# ---- user-level Spack (chained to /apps/spack), for: {names}\n"
        "SP=$HOME/deep-research-lab/spack\n"
        'exec 8>"$SP.lock"; flock 8\n'
        'if [ ! -x "$SP/bin/spack" ]; then\n'
        '  git clone --depth 1 -q https://github.com/spack/spack.git "$SP"\n'
        '  mkdir -p "$SP/etc/spack"\n'
        "  printf 'upstreams:\\n  site:\\n    install_tree: /apps/spack/opt/spack\\n' "
        '> "$SP/etc/spack/upstreams.yaml"\n'
        "fi\n"
        '. "$SP/share/spack/setup-env.sh"\n'
        f'echo "[LADDER] spack: installing {names} (reuses /apps/spack builds)"\n'
        f'spack install -j "${{SLURM_CPUS_ON_NODE:-8}}" --reuse {q}\n'
        "flock -u 8\n"
        f"spack load {q}\n"
        f'echo "[LADDER] spack loaded: {names}"\n'
    )


def _pixi_fallback_only(chans: list[str]) -> str:
    """Modules that failed to load: install them from conda-forge/bioconda instead."""
    return (
        "  export PATH=/apps/pixi/bin:$HOME/.pixi/bin:$PATH\n"
        '  FB="$HOME/deep-research-lab/envs/modfallback-$(echo $LADDER_MOD_FALLBACK | tr " " "-")"\n'
        '  mkdir -p "$FB"; ( cd "$FB" && [ -f .ready ] || { pixi init -c conda-forge -c bioconda . >/dev/null '
        "&& pixi add $LADDER_MOD_FALLBACK && touch .ready; } )\n"
        '  set +u; eval "$(cd "$FB" && pixi shell-hook)"; set -u\n'
        '  echo "[LADDER] replaced modules with conda packages:$LADDER_MOD_FALLBACK"\n'
    )


class EmptyReply(ValueError):
    """The model returned no text (for example it stopped on a tool-call limit)."""

    def __init__(self, finish: str, cost: float | None):
        super().__init__(f"model returned no text (finish reason: {finish})")
        self.finish = finish
        self.cost = cost


def labfetch_problem(info: dict) -> str:
    from deepresearch.dashboard import labfetch

    return labfetch.runtime_problem(info)


# --------------------------------------------------------------------------- store + service


PLAN_DIFF_SKIP = {
    "warnings", "plan_before_fix", "fix_changes", "fix_notes", "fix_diff",
    "catalog_generated", "caveats", "fix_concerns", "url_checks",
    "plan_before_refine", "refine", "review", "review_error", "laptop_fetch",
    "refine_kept", "plan_refine_discarded", "runtime_blocked",
}  # fmt: skip


def plan_diff(before: dict, after: dict, max_lines: int = 400) -> dict:
    """What an AI fix changed: a unified diff of the script and the other plan fields.

    Returns {"script": "<unified diff>", "fields": [{"key", "before", "after"}],
    "truncated": bool}. Shown in plan review so the reviewer sees the edit, not only
    the model's own summary of it.
    """
    a = str((before or {}).get("script") or "").splitlines()
    b = str((after or {}).get("script") or "").splitlines()
    lines = list(difflib.unified_diff(a, b, "before", "after", n=2, lineterm=""))
    fields = []
    for k in sorted(set(before or {}) | set(after or {})):
        if k == "script" or k in PLAN_DIFF_SKIP:
            continue
        x, y = (before or {}).get(k), (after or {}).get(k)
        if x != y:
            fields.append(
                {
                    "key": k,
                    "before": json.dumps(x, sort_keys=True)[:600],
                    "after": json.dumps(y, sort_keys=True)[:600],
                }
            )
    return {
        "script": "\n".join(lines[:max_lines]),
        "fields": fields,
        "truncated": len(lines) > max_lines,
    }


NODE_FAIL_WARN = 3  # node failures before the queue label suggests another partition


class Lab(LabVerdictMixin):
    def __init__(
        self,
        db_path: str,
        config_factory: Callable,
        state_dir: Path,
        targets: dict[str, SlurmSSHTarget] | None = None,
        results_dir: Path | None = None,
        workspace: str = "main",
        bifrost: Any = None,
    ):
        self.db_path = db_path
        self._config = config_factory
        # state_dir: shared settings (targets, catalogs, lessons); results_dir: this
        # workspace's fetched outputs (Main: state_dir/lab, as before workspaces)
        self.state_dir = state_dir
        self.results_dir = results_dir or state_dir / "lab"
        self.workspace = workspace
        self.targets = targets if targets is not None else load_targets(state_dir)
        # The hosted bifrost MCP server for reads (R1): None when no target asks for
        # it or deep-research is not signed in. Submit/watch/fetch stay on SSH.
        self.bifrost = (
            bifrost if bifrost is not None else _bifrost_for(self.targets, state_dir)
        )
        if self.bifrost is not None:
            from deepresearch.dashboard import bifrost as bf

            client = self.bifrost
            for t in self.targets.values():
                if _uses_bifrost(t) and hasattr(t, "catalog_source"):
                    t.catalog_source = lambda c=client: bf.catalog(c)
        for t in self.targets.values():
            if getattr(t, "catalog_path", ""):
                try:
                    t.load_catalog(self.state_dir, refresh=False)  # cache only
                except Exception:
                    pass  # no cache yet; fetched on first use
        self._genai = None
        self._client_lock = threading.Lock()
        # the referee reads every new draft (one Flash call, ~$0.01); tests switch it off
        self.auto_review = os.environ.get("DR_LAB_REVIEW", "1") != "0"
        # referee -> fixer rounds on new drafts (v0.49.0); off with DR_LAB_REFINE=0
        self.auto_refine = os.environ.get("DR_LAB_REFINE", "1") != "0"
        self._fetch_tries: dict[int, int] = {}
        self._warm_checked: dict[str, float] = {}  # target -> last ensure_warm()
        self._stocked_out: dict[
            str, set[str]
        ] = {}  # target -> partitions GCP can't fill
        self._gone_polls: dict[int, int] = {}
        self._watch_lock = threading.Lock()
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()
        from deepresearch.sources import SourceRegistry

        self.sources = SourceRegistry(db_path)
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS lab_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    scope TEXT NOT NULL,            -- 'selection' or 'document'
                    selection TEXT,
                    request TEXT,                   -- optional user instruction
                    target TEXT,
                    status TEXT NOT NULL,
                    stage TEXT,
                    plan TEXT,                      -- JSON (editable while draft)
                    script TEXT,                    -- generated sbatch
                    job_id TEXT,
                    slurm_state TEXT,
                    node TEXT,
                    elapsed TEXT,
                    exit_code TEXT,
                    error TEXT,
                    result_md TEXT,
                    files TEXT,                     -- JSON list
                    estimate_usd REAL,
                    ai_cost_usd REAL DEFAULT 0,
                    rerun_of INTEGER,
                    created_at TEXT,
                    updated_at TEXT,
                    submitted_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_lab_runs_session ON lab_runs(session_id);
                CREATE TABLE IF NOT EXISTS lab_suggestions (
                    session_id INTEGER PRIMARY KEY,
                    data TEXT,
                    cost_usd REAL,
                    created_at TEXT
                );
                """
            )
            cols = {r[1] for r in conn.execute("PRAGMA table_info(lab_runs)")}
            if "data_sources" not in cols:  # names picked at launch, for the planner
                conn.execute("ALTER TABLE lab_runs ADD COLUMN data_sources TEXT")
            if "verdict" not in cols:  # outputs/verdict.json: known-answer checks
                conn.execute("ALTER TABLE lab_runs ADD COLUMN verdict TEXT")
            if "smoke" not in cols:  # smoke-test rounds and the plan as submitted
                conn.execute("ALTER TABLE lab_runs ADD COLUMN smoke TEXT")
            if "cluster" not in cols:  # bifrost: efficiency and diagnosis (v0.53.0)
                conn.execute("ALTER TABLE lab_runs ADD COLUMN cluster TEXT")
            conn.commit()

    # ---- plumbing --------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _client(self):
        # One shared client, created under a lock: two planning threads racing here
        # used to build two clients, and the loser was garbage-collected (closing its
        # HTTP session) mid-request: "Cannot send a request, as the client has been
        # closed."
        with self._client_lock:
            if self._genai is None:
                from google import genai

                self._genai = genai.Client(api_key=self._config().api_key)
            return self._genai

    def _model(self) -> str:
        cfg = self._config()
        return getattr(cfg, "followup_model", None) or PLAN_MODEL

    BUSY_BACKOFF_S = (5.0, 20.0, 45.0)  # Gemini 429/5xx retries (v0.51.0)

    def _ask(self, prompt: str, search: bool) -> tuple[str, float | None]:
        from google.genai import types

        cfg = (
            types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
            if search
            else None
        )
        client = self._client()  # hold a reference for the whole call
        resp = None
        for attempt in range(len(self.BUSY_BACKOFF_S) + 1):
            try:
                resp = client.models.generate_content(
                    model=self._model(), contents=prompt, config=cfg
                )
                break
            except Exception as e:
                # "503 UNAVAILABLE: high demand" and 429s pass in seconds to minutes
                # (2026-10-01, a fix-blocked step failed on one); retry those, raise
                # everything else at once
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                if code not in (429, 500, 502, 503, 504) or attempt >= len(
                    self.BUSY_BACKOFF_S
                ):
                    raise
                time.sleep(self.BUSY_BACKOFF_S[attempt])
        assert resp is not None
        cost = _cost(getattr(resp, "usage_metadata", None), _search_count(resp))
        if not (resp.text or "").strip():
            # Flash with Google Search can stop on TOO_MANY_TOOL_CALLS with only
            # thoughts and no answer; surface the reason instead of "no JSON".
            cand = (getattr(resp, "candidates", None) or [None])[0]
            reason = getattr(cand, "finish_reason", None)
            raise EmptyReply(getattr(reason, "name", None) or str(reason), cost)
        return resp.text, cost

    def _ask_plan(
        self, run_id: int, prompt: str, search: bool = False
    ) -> tuple[dict | None, dict, str]:
        """Ask for a plan JSON ({"plan", "changes", "notes"}). One retry, telling the model
        what was wrong, when the reply has no JSON or no complete plan: a single bad reply
        (runs #36, #38: cut-off JSON, "no usable plan") must not end the repair.
        Returns (plan or None, the parsed reply, why the last reply was unusable)."""
        why = ""
        out: Any = None
        for attempt in range(2):
            ask = prompt
            if attempt:
                ask = (
                    prompt
                    + "\n\nYour previous reply could not be used: "
                    + why
                    + ". Reply again with ONLY the JSON block, complete, with every key "
                    "of the plan (script, resources, install and the rest). Inside JSON "
                    "strings write every backslash as \\\\ and line breaks as \\n."
                )
            try:
                reply, cost = self._ask(ask, search=search and attempt == 0)
            except EmptyReply as e:
                self._add_cost(run_id, e.cost)
                why = f"the reply was empty ({e.finish})"
                continue
            self._add_cost(run_id, cost)
            try:
                out = extract_json(reply)
            except ValueError as e:
                why = f"its JSON did not parse ({str(e)[:120]})"
                continue
            new = _usable_plan(out)
            if new is not None:
                new["script"], _ = restore_control_chars(str(new["script"]))
                return new, out, ""
            why = "it had no complete plan (script, resources and install are required)"
        return None, out if isinstance(out, dict) else {}, why

    def target(self, name: str | None = None) -> SlurmSSHTarget | None:
        t: SlurmSSHTarget | None
        if name and name in self.targets:
            t = self.targets[name]
        else:
            t = next(iter(self.targets.values()), None)
        if t is None or self.workspace in ("", "main"):
            return t
        # other workspaces: same cluster, own run folders and task names
        return ScopedTarget(t, f"ws-{self.workspace}")  # type: ignore[return-value]

    def _key(self, run_id: int) -> tuple[str, int]:
        """Key for the process-wide in-flight sets (run ids repeat across workspaces)."""
        return (self.workspace, int(run_id))

    CATALOG_MAX_AGE_H = 24.0

    def catalog(self, name: str | None = None, refresh: bool = False) -> dict:
        """Load (or refresh) the target's cluster catalog; returns a status summary."""
        tgt = self.target(name)
        if not tgt or not getattr(tgt, "catalog_path", ""):
            return {"available": False, "reason": "no catalog_path configured"}
        err = ""
        try:
            tgt.load_catalog(self.state_dir, refresh=refresh)
        except Exception as e:  # keep any cached copy
            err = str(e)[:300]
        cat = tgt.catalog or {}
        s = cat.get("summary") or {}
        return {
            "available": bool(tgt.catalog),
            "target": tgt.name,
            "fetched": tgt.catalog_fetched,
            "generated": cat.get("generated"),
            "modules": s.get("modules"),
            "spack_packages": s.get("spack_packages"),
            "recipes": len(cat.get("recipes") or []),
            "gpu": s.get("gpu"),
            "error": err,
        }

    def _fresh_catalog(self, tgt: SlurmSSHTarget | None) -> None:
        """Before prompting: refresh a catalog older than a day (errors ignored)."""
        if not tgt or not getattr(tgt, "catalog_path", ""):
            return
        age_h = 1e9
        if tgt.catalog_fetched:
            try:
                then = dt.datetime.fromisoformat(tgt.catalog_fetched)
                if then.tzinfo is not None:
                    then = then.astimezone().replace(tzinfo=None)
                age_h = (dt.datetime.now() - then).total_seconds() / 3600
            except ValueError:
                pass
        try:
            tgt.load_catalog(self.state_dir, refresh=age_h > self.CATALOG_MAX_AGE_H)
        except Exception:
            pass

    def get(self, run_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM lab_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        for k in ("plan", "files", "data_sources", "verdict", "smoke", "cluster"):
            try:
                d[k] = json.loads(d[k]) if d.get(k) else None
            except ValueError:
                pass
        # the outcome (confirmed/refuted/inconclusive/broken) is derived, never stored,
        # so runs from before v0.39.0 get one too
        d["assessment"] = labverdict.assess(
            d.get("verdict") if isinstance(d.get("verdict"), dict) else None,
            str(d.get("status") or ""),
        )
        return d

    def pulse(self) -> dict:
        """Cheap state for the client's notifier: runs in flight and recently finished
        ones (v0.51.0, K21). One small query; no cluster call, no model call."""
        with self._conn() as conn:
            marks = ",".join("?" * len(ACTIVE))
            active = [
                r[0]
                for r in conn.execute(
                    f"SELECT id FROM lab_runs WHERE status IN ({marks})", ACTIVE
                )
            ]
            done = conn.execute(
                "SELECT id, session_id, status, stage, plan, verdict, finished_at "
                "FROM lab_runs WHERE status IN ('completed','failed','cancelled') "
                "AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 20"
            ).fetchall()
        out = []
        for r in done:
            d = self._row(r)
            out.append(
                {
                    "id": d["id"],
                    "session_id": d["session_id"],
                    "status": d["status"],
                    "stage": d.get("stage"),
                    "title": (
                        (d.get("plan") or {}) if isinstance(d.get("plan"), dict) else {}
                    ).get("title")
                    or "",
                    "outcome": (d.get("assessment") or {}).get("outcome"),
                    "finished_at": d.get("finished_at"),
                }
            )
        return {"active": active, "finished": out}

    def runs_for(self, session_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM lab_runs WHERE session_id = ? ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def all_runs(self, limit: int = 500) -> list[dict]:
        """Every run, newest first, with the report title (for the All Lab runs page)."""
        with self._conn() as conn:
            has_sessions = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
            q = (
                "SELECT r.*, s.prompt AS session_prompt FROM lab_runs r "
                "LEFT JOIN sessions s ON s.id = r.session_id"
                if has_sessions
                else "SELECT r.*, NULL AS session_prompt FROM lab_runs r"
            )
            rows = conn.execute(q + " ORDER BY r.id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def active(self) -> list[dict]:
        marks = ",".join("?" * len(ACTIVE))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM lab_runs WHERE status IN ({marks}) ORDER BY id", ACTIVE
            ).fetchall()
        return [self._row(r) for r in rows]

    def _update(
        self, run_id: int, only_if: tuple[str, ...] | None = None, **fields
    ) -> bool:
        """Write fields; with `only_if`, only while the run is in one of those states.

        Background threads (planning, the watcher) pass `only_if` so a cancel that
        lands while they work is not overwritten. Returns whether the row changed.
        """
        if not fields:
            return False
        for k in ("plan", "files", "verdict", "smoke", "cluster"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k])
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        sql = f"UPDATE lab_runs SET {cols} WHERE id = ?"
        args: list[Any] = [*fields.values(), run_id]
        if only_if:
            sql += f" AND status IN ({','.join('?' * len(only_if))})"
            args += only_if
        with self._conn() as conn:
            changed = conn.execute(sql, args).rowcount > 0
            conn.commit()
        return changed

    def _add_cost(self, run_id: int, usd: float | None) -> None:
        if usd:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE lab_runs SET ai_cost_usd = COALESCE(ai_cost_usd, 0) + ? WHERE id = ?",
                    (usd, run_id),
                )
                conn.commit()

    def active_for(self, session_ids: list[int]) -> list[dict]:
        """Runs of these sessions that still hold (or are about to hold) a Slurm job."""
        if not session_ids:
            return []
        marks = ",".join("?" * len(session_ids))
        states = ",".join("?" * len(ACTIVE))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM lab_runs WHERE session_id IN ({marks}) "
                f"AND status IN ({states}) ORDER BY id",
                (*session_ids, *ACTIVE),
            ).fetchall()
        return [self._row(r) for r in rows]

    def purge_session(self, session_ids: list[int]) -> None:
        import shutil

        marks = ",".join("?" * len(session_ids))
        with self._conn() as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    f"SELECT id FROM lab_runs WHERE session_id IN ({marks})",
                    session_ids,
                )
            ]
            conn.execute(
                f"DELETE FROM lab_runs WHERE session_id IN ({marks})", session_ids
            )
            conn.execute(
                f"DELETE FROM lab_suggestions WHERE session_id IN ({marks})",
                session_ids,
            )
            conn.commit()
        for i in ids:
            shutil.rmtree(self.results_dir / f"run_{i}", ignore_errors=True)

    # ---- suggestions -----------------------------------------------------
    def suggestions(
        self, session_id: int, title: str, text: str, refresh=False
    ) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM lab_suggestions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row and not refresh:
            return {
                **json.loads(row["data"]),
                "cost_usd": row["cost_usd"],
                "cached": True,
            }
        tgt = self.target()
        self._fresh_catalog(tgt)
        prompt = SUGGEST_PROMPT.format(
            target=_describe(tgt, full=False),
            title=title,
            text=_strip_sources(text)[:60000],
        )
        try:
            reply, cost = self._ask(prompt, search=False)
        except EmptyReply as e:
            raise ValueError(f"{e}{_spent(e.cost)}") from e
        try:
            data = extract_json(reply)
        except ValueError as e:
            raise ValueError(f"{e}{_spent(cost)}") from e
        if isinstance(data, list):
            data = {"suggestions": data}
        if not isinstance(data, dict):
            raise ValueError(f"suggestions are not a JSON object{_spent(cost)}")
        data["suggestions"] = [
            s for s in data.get("suggestions") or [] if isinstance(s, dict)
        ][:3]
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO lab_suggestions VALUES (?, ?, ?, ?)",
                (session_id, json.dumps(data), cost, _now()),
            )
            conn.commit()
        return {**data, "cost_usd": cost, "cached": False}

    # ---- plan ------------------------------------------------------------
    def create(
        self,
        session_id: int,
        scope: str,
        selection: str,
        request: str = "",
        target: str | None = None,
        rerun_of: int | None = None,
        plan: dict | None = None,
        data_sources: list[str] | None = None,
    ) -> dict:
        tgt = self.target(target)
        for n in data_sources or []:
            if self.sources.get(n) is None:
                raise ValueError(f"data source '{n}' does not exist")
        now = _now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO lab_runs (session_id, scope, selection, request, target, status, "
                "stage, plan, rerun_of, created_at, updated_at, data_sources) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    scope,
                    selection,
                    request,
                    tgt.name if tgt else None,
                    "draft" if plan else "planning",
                    "Plan copied for re-run" if plan else "Reading the selection",
                    json.dumps(plan) if plan else None,
                    rerun_of,
                    now,
                    now,
                    json.dumps(data_sources) if data_sources else None,
                ),
            )
            conn.commit()
            run_id = cur.lastrowid or 0
        if plan and tgt:
            plan = {**plan, "warnings": self._check(tgt, plan)}
            self._update(
                run_id,
                plan=plan,
                script=build_sbatch(run_id, plan, tgt, self._safe_sources(plan)),
                estimate_usd=estimate_cost(tgt, plan),
            )
        return self.get(run_id) or {}

    # ---- data sources ---------------------------------------------------
    def _plan_sources(self, plan: dict) -> list:
        """The registry records for plan["data_sources"]; unknown names are errors."""
        names = [str(n) for n in (plan or {}).get("data_sources") or []]
        out = []
        for n in names:
            s = self.sources.get(n)
            if s is None:
                raise ValueError(f"data source '{n}' does not exist")
            out.append(s)
        return out

    def _check(self, tgt, plan: dict) -> list[str]:
        return (
            validate_plan(tgt, plan)
            + labcores.warnings(tgt, plan)
            + self.source_warnings(plan)
            + self.url_warnings(plan)
            + self.env_warnings(plan)
            + self.match_warnings(tgt, plan)
            + self.cluster_check_warnings(tgt, plan)
        )

    def cluster_check_warnings(self, tgt, plan: dict) -> list[str]:
        """bifrost's script_check on the batch file this plan builds (R1).

        Only its errors become warnings: they are what bifrost would refuse at submit
        time (no core request on a shared partition, unknown module, a GPU request on a
        CPU partition, over a cap). Its warnings overlap the Lab's own checks. A bifrost
        outage never blocks planning: the check is skipped."""
        if self.bifrost is None or not tgt or not plan.get("script"):
            return []
        from deepresearch.dashboard import bifrost as bf

        try:
            script = build_sbatch(0, plan, tgt, self._safe_sources(plan))
            issues = bf.script_issues(self.bifrost, script)
        except Exception:
            return []
        return [
            "Cluster check (bifrost): " + i["message"]
            for i in issues
            if i["severity"] == "error"
        ]

    def match_warnings(self, tgt, plan: dict) -> list[str]:
        """Partition and time-limit advice from the workload shape and past runs."""
        if not tgt or not getattr(tgt, "partitions", None):
            return []
        warns = []
        part, why = suggest_partition(tgt, plan, self._stocked_out.get(tgt.name))
        if why:
            warns.append(f"Partition: {why}; consider resources.partition = '{part}'")
        r = plan.get("resources") or {}
        part_now = str(r.get("partition") or tgt.default_partition)
        if part_now in tgt.partitions:
            q = labcores.request(tgt, part_now, r)
            shape = workload_shape(plan)
            if q["defaulted"] and shape in ("mpi", "sweep"):
                warns.append(
                    f"Cores: this looks like {shape} work but gets the default "
                    f"{labcores.DEFAULT_CORES} cores on shared '{part_now}'; set "
                    "resources.cores (or resources.ntasks_per_node for MPI ranks)"
                )
        hist = time_from_history(self.db_path, plan)
        tl = str((plan.get("resources") or {}).get("time_limit") or "01:00:00")
        if hist and TIME_RE.fullmatch(tl) and _hours(tl) > 2.5 * _hours(hist):
            warns.append(
                f"Time limit {tl} is far above similar past runs (suggested {hist}); "
                "a tighter limit lowers the worst-case cost"
            )
        return warns

    @staticmethod
    def url_warnings(plan: dict) -> list[str]:
        """Download URLs that failed their check on a compute node (while planning)."""
        bad = [
            u for u in (plan.get("url_checks") or [])
            if isinstance(u, dict) and u.get("ok") is False
            and u.get("url") in script_urls(plan)
        ]  # fmt: skip
        return [
            f"URL did not download on the cluster ({u.get('status') or 'no answer'}): "
            f"{str(u.get('url'))[:160]}"
            for u in bad[:5]
        ]

    def env_warnings(self, plan: dict) -> list[str]:
        """Nothing blocks on ladder memory; this only notes a known-good environment."""
        return []

    def _safe_sources(self, plan: dict) -> list:
        """Like _plan_sources for previews: unknown names are skipped (they show up as
        a pre-flight warning instead of breaking the plan view)."""
        return [
            s
            for n in (plan or {}).get("data_sources") or []
            if (s := self.sources.get(str(n))) is not None
        ]

    def source_warnings(self, plan: dict) -> list[str]:
        """Pre-flight for data: unknown or unreachable sources, relay size, and a script
        that ignores the staged copy."""
        from deepresearch.sources.staging import RELAY_MAX_BYTES

        warns: list[str] = []
        script = str((plan or {}).get("script") or "")
        for n in (plan or {}).get("data_sources") or []:
            s = self.sources.get(str(n))
            if s is None:
                warns.append(f"Data source '{n}' does not exist")
                continue
            if s.status == "unreachable":
                warns.append(
                    f"Data source '{s.name}' failed its last test: {s.last_error}"
                )
            size = s.manifest.total_bytes if s.manifest else 0
            cap = int(s.options.get("max_relay_bytes") or RELAY_MAX_BYTES)
            if s.effective_staging == "relay" and size > cap:
                warns.append(
                    f"Data source '{s.name}' is {size / 1024**3:.1f} GB, over the relay "
                    f"limit ({cap / 1024**3:.1f} GB); use direct staging or raise the limit"
                )
            if script and s.env_var not in script:
                warns.append(
                    f"The script never reads ${s.env_var}; data source '{s.name}' would "
                    "be staged but unused"
                )
        return warns

    def _data_note(self, run: dict, tgt) -> str:
        from deepresearch.sources.staging import plan_data_note

        try:
            srcs = self._plan_sources({"data_sources": run.get("data_sources") or []})
        except ValueError:
            return ""
        return "\n" + plan_data_note(
            srcs, getattr(tgt, "remote_root", "~/deep-research-lab")
        )

    def make_plan(self, run_id: int, title: str) -> None:
        """Runs in a background thread: AI decomposes the selection into a job plan."""
        run = self.get(run_id)
        if not run:
            return
        tgt = self.target(run["target"])
        try:
            if not self._update(
                run_id,
                only_if=("planning",),
                stage="Researching software and methods (web search)",
            ):
                return  # cancelled before it started
            self._fresh_catalog(tgt)
            probes = self._run_probes(run_id, run, tgt, title)
            notes = self.env_notes(tgt)
            if notes:
                probes += "\n" + notes + "\n"
            prompt = PLAN_PROMPT.format(
                target=_describe(tgt, full=True),
                probes=probes,
                lessons=labguard.prompt_block(
                    " ".join(
                        [
                            title,
                            run.get("request") or "",
                            (run["selection"] or "")[:20000],
                        ]
                    ),
                    self.state_dir,
                ),
                title=title,
                scope="a highlighted passage"
                if run["scope"] == "selection"
                else "the whole report",
                text=_strip_sources(run["selection"] or "")[:60000],
                question_line=(
                    f"\nTHE USER'S INSTRUCTION: {run['request']}\n"
                    if run.get("request")
                    else ""
                )
                + self._data_note(run, tgt),
            )
            no_search = ""
            try:
                reply, cost = self._ask(prompt, search=True)
            except EmptyReply as e:
                self._add_cost(run_id, e.cost)
                self._update(
                    run_id,
                    only_if=("planning",),
                    stage="Web search stopped early; planning without it",
                )
                no_search = (
                    f"Planned without web search (search stopped: {e.finish}); "
                    "check package names and flags."
                )
                reply, cost = self._ask(prompt, search=False)
            self._add_cost(run_id, cost)
            plan = extract_json(reply)
            if not isinstance(plan, dict):
                raise ValueError("plan is not a JSON object")
            if no_search:
                plan["caveats"] = " ".join(
                    x for x in (no_search, str(plan.get("caveats") or "")) if x
                )
            if not plan.get("computable", True):
                self._update(
                    run_id,
                    only_if=("planning",),
                    status="plan_failed",
                    stage="Not computable",
                    plan=plan,
                    error=plan.get("why_not")
                    or "The model judged this not computable.",
                )
                return
            for key in ("script", "resources", "install"):
                if key not in plan:
                    raise ValueError(f"plan is missing '{key}'")
            if run.get("data_sources"):
                plan["data_sources"] = list(run["data_sources"])
            plan["url_checks"] = self._check_urls(run_id, tgt, plan)
            plan["warnings"] = self._check(tgt, plan)
            if tgt and getattr(tgt, "catalog", None):
                plan["catalog_generated"] = tgt.catalog.get("generated")  # type: ignore[union-attr]
            self._update(
                run_id,
                only_if=("planning",),
                status="draft",
                stage="Plan ready for review"
                + (f" ({len(plan['warnings'])} warnings)" if plan["warnings"] else ""),
                plan=plan,
                script=build_sbatch(run_id, plan, tgt, self._safe_sources(plan))
                if tgt
                else None,
                estimate_usd=estimate_cost(tgt, plan),
            )
        except Exception as e:
            self._update(
                run_id,
                only_if=("planning",),
                status="plan_failed",
                stage="Planning failed",
                error=str(e)[:500],
            )
            return
        if self.auto_review:
            self._auto_review(run_id)
            if self.auto_refine:
                self._auto_refine(run_id)

    # ---- referee -> fixer loop before the draft is shown (v0.49.0) -------------
    REFINE_MAX_ROUNDS = 2

    @staticmethod
    def _needs_refine(review: dict | None) -> bool:
        """'flawed', or any high-severity finding: worth one fixer round before a person
        reads the draft. Medium/low concerns stay advice."""
        if not review:
            return False
        return review.get("verdict") == "flawed" or any(
            f.get("severity") == "high" for f in review.get("findings") or []
        )

    @staticmethod
    def _review_rank(review: dict | None) -> tuple[int, int, int]:
        """Lower is better: verdict (sound < concerns < flawed), then high findings,
        then all findings. Used to keep the best refine round, not the last (L7)."""
        rv = review or {}
        order = {"sound": 0, "concerns": 1, "flawed": 2}
        fs = rv.get("findings") or []
        return (
            order.get(str(rv.get("verdict")), 3),
            sum(1 for f in fs if f.get("severity") == "high"),
            len(fs),
        )

    def _auto_refine(self, run_id: int) -> None:
        """Referee findings -> fixer -> referee again, up to REFINE_MAX_ROUNDS, on a new
        draft only. What I did by hand all day on 2026-09-30, every time with success.
        The referee stays advice: this only ever produces another DRAFT (never submits),
        keeps the first plan in `plan_before_refine` for Undo, and stops when the referee
        is satisfied, a round changes nothing, or a step fails. History in `refine`."""
        history: list[dict] = []
        first: dict | None = None
        # every reviewed version (round 0 = as first written), to keep the best one
        snaps: list[tuple[int, dict]] = []
        skip = ("plan_before_refine", "refine")
        for rnd in range(1, self.REFINE_MAX_ROUNDS + 1):
            cur = self.get(run_id)
            if not cur or cur["status"] != "draft":
                return
            plan = cur.get("plan") or {}
            rv = plan.get("review")
            if not self._needs_refine(rv) or self.review_stale(plan):
                break
            if first is None:
                first = {
                    k: v
                    for k, v in plan.items()
                    if k not in ("warnings", "plan_before_fix", "plan_before_refine")
                }
                snaps.append((0, {k: v for k, v in plan.items() if k not in skip}))
            entry: dict = {
                "round": rnd,
                "before": (rv or {}).get("verdict"),
                "findings": len((rv or {}).get("findings") or []),
            }
            self._update(
                run_id,
                only_if=("draft",),
                stage=f"Referee found problems; AI is revising the plan (round {rnd})",
            )
            h0 = self._plan_hash(plan)
            try:
                out = self.fix_plan(run_id)
            except Exception as e:  # a failed fix leaves the reviewed draft as it was
                entry["error"] = f"fix: {str(e)[:200]}"
                history.append(entry)
                break
            fx = out.get("fix") or {}
            entry["changes"] = list(fx.get("changes") or [])[:12]
            # judge by the plan, not the fixer's change list: run #46 (2026-09-30)
            # rewrote the script and returned an empty list
            if self._plan_hash((self.get(run_id) or {}).get("plan") or {}) == h0:
                entry["error"] = "the fixer changed nothing"
                history.append(entry)
                break
            if not entry["changes"]:
                entry["changes"] = [
                    "(the fixer edited the plan without listing changes)"
                ]
            try:
                self.review(run_id)
            except Exception as e:
                entry["error"] = f"referee: {str(e)[:200]}"
                history.append(entry)
                break
            now_plan = (self.get(run_id) or {}).get("plan") or {}
            after = now_plan.get("review") or {}
            entry["after"] = after.get("verdict")
            history.append(entry)
            if after and not self.review_stale(now_plan):
                snaps.append(
                    (rnd, {k: v for k, v in now_plan.items() if k not in skip})
                )
        if not history:
            return
        cur = self.get(run_id)
        if not cur or cur["status"] != "draft":
            return
        kept = None
        if len(snaps) > 1:
            # the referee's judgments vary and a revision can make a plan worse
            # (2026-09-30: flawed -> concerns -> flawed); keep the best version,
            # the newest among equals, never silently the last one
            best_rank = min(self._review_rank(sp.get("review")) for _, sp in snaps)
            best_rnd, best = [
                (r, sp)
                for r, sp in snaps
                if self._review_rank(sp.get("review")) == best_rank
            ][-1]
            if best_rnd != snaps[-1][0] or self.review_stale(cur.get("plan") or {}):
                review_kept = best.get("review")
                discarded = {
                    k: v for k, v in (cur.get("plan") or {}).items() if k not in skip
                }
                try:
                    self.edit_plan(run_id, best)
                except ValueError:
                    return  # left draft meanwhile
                kept = {
                    "round": best_rnd,
                    "verdict": (review_kept or {}).get("verdict"),
                    "instead_of": snaps[-1][0],
                    "instead_of_verdict": (snaps[-1][1].get("review") or {}).get(
                        "verdict"
                    ),
                }
                cur = self.get(run_id) or cur
                plan_now = dict(cur.get("plan") or {})
                plan_now["review"] = review_kept
                plan_now["plan_refine_discarded"] = discarded
                self._update(run_id, only_if=("draft",), plan=plan_now)
                cur = self.get(run_id) or cur
        plan = dict(cur.get("plan") or {})
        plan["refine"] = history
        if first is not None:
            plan["plan_before_refine"] = first
        if kept:
            plan["refine_kept"] = kept
        else:
            plan.pop("refine_kept", None)
            plan.pop("plan_refine_discarded", None)
        last = history[-1]
        verdict = (
            (plan.get("review") or {}).get("verdict")
            or last.get("after")
            or last.get("before")
        )
        if kept and kept["round"] == 0:
            how = "the AI revisions were not better, so the first plan is kept"
        elif kept:
            how = f"kept round {kept['round']}, the best the referee saw"
        else:
            how = f"{len(history)} round{'s' if len(history) > 1 else ''}"
        stage = (
            f"Plan ready for review (revised by AI after the referee, "
            f"{how}; referee now: {verdict})"
        )
        if plan.get("warnings"):
            stage += f" ({len(plan['warnings'])} warnings)"
        self._update(run_id, only_if=("draft",), plan=plan, stage=stage)

    def undo_refine(self, run_id: int) -> dict:
        """Back to the plan exactly as first written, before the referee/fixer rounds."""
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "draft":
            raise ValueError(f"run is {run['status']}; only drafts can be changed")
        first = (run.get("plan") or {}).get("plan_before_refine")
        if not isinstance(first, dict):
            raise ValueError("no AI revision to undo")
        return self.edit_plan(run_id, first)

    # ---- fetch on this laptop when a site blocks the cluster (v0.50.0) -----------
    def laptop_fetch(
        self, run_id: int, urls: list[str] | None = None,
        extra_problems: list[str] | None = None,
    ) -> dict:  # fmt: skip
        """Fetch the plan's cluster-blocked URLs here, stage them as a local data source,
        attach it, and have the fixer read `$DS_<NAME>` instead of downloading. Only ever
        a draft; returns the run plus `laptop_fetch` = {source, files, failed}."""
        from deepresearch.dashboard import labfetch
        from deepresearch.sources import DataSource
        from deepresearch.sources.service import check

        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "draft":
            raise ValueError(f"run is {run['status']}; only drafts can be changed")
        plan = dict(run.get("plan") or {})
        offer = [u["url"] for u in labfetch.blocked_urls(plan)]
        want = [u for u in (urls or offer) if u in offer]
        if not want:
            raise ValueError("no blocked download URLs to fetch for this plan")
        name = labfetch.source_name(self.workspace, run_id)
        dest = labfetch.fetch_root() / name
        self._update(
            run_id,
            only_if=("draft",),
            stage=f"Fetching {len(want)} blocked URL(s) on this laptop",
        )
        got = labfetch.fetch(want, dest)
        if not got["files"]:
            self._update(run_id, only_if=("draft",), stage="Laptop fetch failed")
            why = "; ".join(f"{f['url'][:80]}: {f['error']}" for f in got["failed"][:3])
            raise ValueError(f"nothing could be fetched on this laptop either ({why})")
        src = self.sources.get(name)
        if src is None:
            src = self.sources.add(
                DataSource(
                    name=name,
                    title=f"Laptop fetch for Lab run #{run_id}",
                    description="Fetched on the laptop because the site blocks the "
                    "cluster. urls.json lists each file's URL, status, size and sha256.",
                    kind="local_folder",
                    uri=str(dest),
                    protection_level="P1",
                    tags=["lab-fetch"],
                )
            )
        check(self.sources, src)
        names = [str(n) for n in plan.get("data_sources") or []]
        if name not in names:
            names.append(name)
        prior = plan.get("laptop_fetch") or {}
        plan["data_sources"] = names
        plan["laptop_fetch"] = {
            "source": name,
            "folder": str(dest),
            "urls": sorted(
                set(prior.get("urls") or []) | {f["url"] for f in got["files"]}
            ),
            "files": got["files"],
            "failed": got["failed"],
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        self._update(run_id, only_if=("draft",), plan=plan)
        problems = labfetch.problems_for_fixer(src.env_var, got["files"])
        out = self.fix_plan(
            run_id, extra_problems=problems + list(extra_problems or [])
        )
        return {**out, "laptop_fetch": plan["laptop_fetch"]}

    def _note_runtime_blocked(self, run_id: int, log: str, plan: dict) -> dict | None:
        """Record on the plan when a job's log shows a site refusing the cluster (L8)."""
        from deepresearch.dashboard import labfetch

        info = labfetch.runtime_blocked(log, plan)
        if not info:
            return None
        cur = self.get(run_id)
        if cur:
            p = dict(cur.get("plan") or {})
            p["runtime_blocked"] = {
                **info,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            self._update(run_id, plan=p)
        return info

    def fix_blocked(self, run_id: int) -> dict:
        """A finished run whose log shows a site refusing the cluster (v0.51.0, L8):
        a new draft (`rerun_of` = the run) with the blocked fixed URLs fetched on this
        laptop and staged, or, when the job builds its URLs, a fixer pass told to fetch
        fewer, larger responses and cache them. Never submits."""
        from deepresearch.dashboard import labfetch

        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        rb = (run.get("plan") or {}).get("runtime_blocked")
        if run["status"] not in ("completed", "failed") or not rb:
            raise ValueError("this run's log shows no site refusing the cluster")
        plan = {
            k: v
            for k, v in (run.get("plan") or {}).items()
            if k not in PLAN_DIFF_SKIP - {"catalog_generated", "caveats"}
        }
        new = self.create(
            run["session_id"],
            run["scope"],
            run.get("selection") or "",
            run.get("request") or "",
            run.get("target"),
            rerun_of=run_id,
            plan=plan,
        )
        nid = new["id"]
        urls = [u for u in rb.get("urls") or [] if u in script_urls(plan)]
        if urls:
            # the same path as pre-flight's "Fetch on this laptop", for these URLs
            p = dict((self.get(nid) or {}).get("plan") or {})
            p["url_checks"] = [
                {
                    "url": u,
                    "ok": False,
                    "status": f"HTTP {(rb.get('statuses') or ['429'])[0]}",
                }
                for u in urls
            ]
            self._update(nid, only_if=("draft",), plan=p)
        try:
            if urls:
                # the refusals came from the job's own requests too: the fetch is staged,
                # and the fixer is also told to stop the job hammering the site (run
                # #42's query loop kept calling loc.gov after its fixed URL was staged)
                out = self.laptop_fetch(
                    nid, extra_problems=[labfetch.runtime_problem(rb)]
                )
            else:
                out = self.fix_plan(nid, extra_problems=[labfetch.runtime_problem(rb)])
        except Exception as e:
            # the new draft stays (a copy of the plan); say what did not happen
            self._update(
                nid,
                only_if=("draft",),
                stage="Copied for a fix after site refusals; the automatic fix did not "
                f"finish ({str(e)[:160]}). Use Fix with AI or Fetch on this laptop.",
            )
            return {**(self.get(nid) or new), "blocked": rb, "fix_error": str(e)[:300]}
        # Say plainly when the new draft still talks to a refusing host (run #42's
        # 600+ queries cannot be pre-fetched; the fixer said so in its notes): that
        # needs a redesign (as run #48 did with decade facets), not another submit.
        cur = self.get(nid) or out
        script = str((cur.get("plan") or {}).get("script") or "")
        still = [h for h in rb.get("hosts") or [] if h in script]
        if still and cur.get("status") == "draft":
            stage = (
                f"New draft after site refusals: it still calls {', '.join(still)}, "
                "which refused the cluster; read the AI's notes. A redesign (fewer, "
                "larger requests, or data staged in advance) is likely needed."
            )
            self._update(nid, only_if=("draft",), stage=stage)
            out = {**out, "stage": stage, "still_calls": still}
        return {**out, "blocked": rb}

    def _auto_review(self, run_id: int) -> None:
        """The referee after planning: two tries (a busy model or a bad reply is common),
        then a note on the plan saying it did not run, so the dialog offers "Run referee"
        with the reason instead of silently showing nothing (run #43). Advice only: a
        failed referee never changes the plan's substance or its status."""
        last = ""
        for attempt in range(2):
            try:
                self.review(run_id)
                return
            except ValueError as e:  # not a draft any more, or no usable review
                last = str(e)
                if "only drafts" in last:
                    return
            except Exception as e:  # network, quota, model error
                last = f"{type(e).__name__}: {e}"
            if attempt == 0:
                self._stop.wait(5)
        cur = self.get(run_id)
        if cur and cur["status"] == "draft":
            plan = dict(cur.get("plan") or {})
            if not plan.get("review"):
                plan["review_error"] = last[:300]
                self._update(run_id, only_if=("draft",), plan=plan)

    def edit_plan(self, run_id: int, plan: dict) -> dict:
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] not in ("draft", "plan_failed"):
            raise ValueError(f"run is {run['status']}; only drafts can be edited")
        tgt = self.target(run["target"])
        plan = {**plan, "warnings": self._check(tgt, plan)}
        if not self._update(
            run_id,
            only_if=("draft", "plan_failed"),
            plan=plan,
            status="draft",
            error=None,
            script=build_sbatch(run_id, plan, tgt, self._safe_sources(plan))
            if tgt
            else None,
            estimate_usd=estimate_cost(tgt, plan),
        ):
            now = self.get(run_id) or run
            raise ValueError(f"run is {now['status']}; only drafts can be edited")
        return self.get(run_id) or {}

    FIX_MAX_ROUNDS = 2

    def review(self, run_id: int) -> dict:
        """Adversarial referee pass on a draft (v0.46.0): could the test ever fail, could
        it ever pass? Advice only: stored on the plan as `review`, never edits the plan's
        substance and never blocks submit. One model call (one retry on a bad reply)."""
        from deepresearch.dashboard import labreview

        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "draft":
            raise ValueError(f"run is {run['status']}; only drafts can be reviewed")
        plan = dict(run.get("plan") or {})
        prompt = labreview.build_prompt(plan, str(plan.get("question") or ""))
        review: dict | None = None
        why = ""
        for attempt in range(2):
            ask = prompt if not attempt else prompt + (
                "\n\nYour previous reply could not be used: " + why
                + ". Reply again with ONLY the JSON block."
            )  # fmt: skip
            try:
                reply, cost = self._ask(ask, search=False)
            except EmptyReply as e:
                self._add_cost(run_id, e.cost)
                why = f"the reply was empty ({e.finish})"
                continue
            self._add_cost(run_id, cost)
            try:
                review = labreview.normalize(extract_json(reply))
                break
            except ValueError as e:
                why = str(e)[:160]
        if review is None:
            raise ValueError(f"the referee returned no usable review twice ({why})")
        review["plan_hash"] = self._plan_hash(plan)
        cur = self.get(run_id) or run
        new = {**(cur.get("plan") or {}), "review": review}
        new.pop("review_error", None)
        if not self._update(run_id, only_if=("draft",), plan=new):
            now = self.get(run_id) or cur
            raise ValueError(f"run is {now['status']}; the review was not saved")
        return self.get(run_id) or {}

    @staticmethod
    def _plan_hash(plan: dict) -> str:
        """Hash of what the referee judged (script, resources, install, parameters)."""
        import hashlib

        core = {
            k: plan.get(k) for k in ("script", "resources", "install", "parameters")
        }
        return hashlib.sha256(
            json.dumps(core, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]

    def review_stale(self, plan: dict) -> bool:
        r = plan.get("review") or {}
        return bool(r) and r.get("plan_hash") != self._plan_hash(plan)

    def fix_plan(self, run_id: int, extra_problems: list[str] | None = None) -> dict:
        """Ask the planner to fix only what pre-flight flagged, then check again.

        Never submits. Stores the corrected plan as the draft and keeps the previous
        plan in `plan_before_fix` so the reviewer can undo. Up to FIX_MAX_ROUNDS model
        calls: a second round only if the first left warnings. Returns the run plus
        `fix` = {changes, notes, rounds, remaining}.
        """
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "draft":
            raise ValueError(f"run is {run['status']}; only drafts can be fixed")
        tgt = self.target(run["target"])
        if not tgt:
            raise ValueError("no cluster target configured")
        self._fresh_catalog(tgt)
        original = dict(run.get("plan") or {})
        plan = dict(original)
        plan.pop("plan_before_fix", None)
        warns = self._check(tgt, plan)  # includes "planned against an older catalog"
        from deepresearch.dashboard import labreview

        referee = (
            labreview.as_problems(plan.get("review"))
            if not self.review_stale(plan)
            else []
        )
        warns = list(extra_problems or []) + warns + referee
        if not warns:
            return {
                **(self.get(run_id) or {}),
                "fix": {
                    "changes": [],
                    "notes": "No problems found against the current cluster catalog.",
                    "rounds": 0,
                    "remaining": [],
                },
            }
        changes: list[str] = []
        notes: list[str] = []
        rounds = 0
        while warns and rounds < self.FIX_MAX_ROUNDS:
            rounds += 1
            body = {
                k: v
                for k, v in plan.items()
                if k not in ("warnings", "plan_before_fix", "review", "refine",
                             "plan_before_refine", "laptop_fetch", "refine_kept",
                             "plan_refine_discarded", "runtime_blocked")
            }  # fmt: skip
            prompt = FIX_PROMPT.format(
                target=_describe(tgt, full=True),
                lessons=labguard.prompt_block(json.dumps(body), self.state_dir),
                problems="\n".join(f"- {w}" for w in warns),
                plan=json.dumps(body, indent=1)[:60000],
            )
            new, out, why = self._ask_plan(run_id, prompt)
            if new is None:
                notes.append(
                    f"The model returned no usable plan twice ({why}); nothing changed."
                )
                break
            changes += [str(c) for c in out.get("changes") or []][:20]
            if out.get("notes"):
                notes.append(str(out["notes"]))
            plan = new
            if getattr(tgt, "catalog", None):
                plan["catalog_generated"] = tgt.catalog.get("generated")  # type: ignore[union-attr]
            warns = self._check(tgt, plan)
        before = {
            k: v
            for k, v in original.items()
            if k not in ("warnings", "plan_before_fix", "plan_before_refine", "refine")
        }
        if getattr(tgt, "catalog", None):
            plan["catalog_generated"] = tgt.catalog.get("generated")  # type: ignore[union-attr]
        warns = self._check(tgt, plan)
        concerns = fix_concerns(before, plan)
        if concerns:
            notes.insert(0, "".join(f"REVIEW: {c}. " for c in concerns).strip())
        if original.get("review"):
            plan["review"] = original["review"]  # judged the old plan: shown as stale
        for k in (
            "plan_before_refine",
            "refine",
            "laptop_fetch",
            "refine_kept",
            "plan_refine_discarded",
        ):  # records survive
            if k in original:
                plan[k] = original[k]
        if original.get("data_sources") and not plan.get("data_sources"):
            plan["data_sources"] = list(original["data_sources"])
        plan = {
            **plan,
            "warnings": warns,
            "plan_before_fix": before,
            "fix_changes": changes,
            "fix_concerns": concerns,
            "fix_notes": " ".join(notes),
            "fix_diff": plan_diff(before, plan),
        }
        if not self._update(
            run_id,
            only_if=("draft",),
            plan=plan,
            stage="Plan fixed by AI, re-checked"
            + (f" ({len(warns)} warnings left)" if warns else " (no warnings)"),
            script=build_sbatch(run_id, plan, tgt, self._safe_sources(plan)),
            estimate_usd=estimate_cost(tgt, plan),
        ):
            now = self.get(run_id) or run
            raise ValueError(
                f"run is {now['status']} now; the AI fix was not applied to it"
            )
        return {
            **(self.get(run_id) or {}),
            "fix": {
                "changes": changes,
                "notes": " ".join(notes),
                "rounds": rounds,
                "remaining": warns,
            },
        }

    def undo_fix(self, run_id: int) -> dict:
        """Restore the plan as it was before the last AI fix."""
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "draft":
            raise ValueError(f"run is {run['status']}; only drafts can be changed")
        before = (run.get("plan") or {}).get("plan_before_fix")
        if not isinstance(before, dict):
            raise ValueError("no AI fix to undo")
        return self.edit_plan(run_id, before)

    def fix_failed(self, run_id: int) -> dict:
        """A failed run: ask the planner to fix what the job log shows, as a new draft.

        The failed run is left untouched. The new run is a draft (`rerun_of` = the failed
        run) that goes through pre-flight like any plan; nothing is submitted. Returns the
        new run plus `fix` = {changes, notes, remaining}.
        """
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "failed":
            raise ValueError(
                f"run is {run['status']}; only failed runs can be fixed this way"
            )
        tgt = self.target(run["target"])
        if not tgt:
            raise ValueError("no cluster target configured")
        plan = {
            k: v
            for k, v in (run.get("plan") or {}).items()
            if k not in PLAN_DIFF_SKIP - {"catalog_generated", "caveats"}
        }
        if not plan.get("script"):
            raise ValueError("the failed run has no plan to fix")
        log_path = self.results_dir / f"run_{run_id}" / "job.log"
        log = log_path.read_text("utf-8", "replace") if log_path.exists() else ""
        if not log.strip() and run.get("job_id"):
            try:
                log, _ = tgt.log(run_id, 0)
            except Exception:  # cluster unreachable: fix from state alone
                log = ""
        self._fresh_catalog(tgt)
        fclass, fadvice = classify_failure(log)
        prompt = RUNFIX_PROMPT.format(
            target=_describe(tgt, full=True),
            lessons=labguard.prompt_block(
                json.dumps(plan) + " " + _log_for_fix(log, 3000), self.state_dir
            ),
            state=(run.get("slurm_state") or run.get("stage") or "FAILED")
            + (f". DIAGNOSIS ({fclass}): {fadvice}" if fadvice else ""),
            exit_code=run.get("exit_code"),
            elapsed=run.get("elapsed"),
            log=_log_for_fix(log),
            plan=json.dumps(plan, indent=1)[:60000],
        )
        new: dict | None = None
        changes: list[str] = []
        notes = ""
        why = ""
        for attempt in range(2):
            cand, out, why = self._ask_plan(
                run_id,
                prompt
                if attempt == 0
                else prompt
                + '\n\nYour previous answer changed the plan but its "changes" list was '
                'empty. Return the same fix again with one line per change in "changes".',
            )
            if cand is None:
                break  # already retried inside _ask_plan
            new = cand
            changes = [str(c) for c in (out.get("changes") or [])][:20]
            notes = str(out.get("notes") or "")
            if changes or new == plan:
                break  # a described fix, or an honest "nothing to fix"
        if new is None:
            raise ValueError(
                f"the model returned no usable plan twice ({why}); nothing changed"
            )
        dropped = _dropped_options(plan, new)
        if dropped:
            # Removing an analysis option is how a fix hides an error (run #25: the model
            # dropped --covariation instead of fixing why inputs were thrown away). Keep
            # the fix, but say so loudly so the reviewer decides.
            notes = (
                f"REVIEW: this fix removes option(s) {', '.join(dropped)} from the "
                "script. Removing an option can hide an error instead of fixing it; "
                "check the log for an earlier cause before accepting. " + notes
            ).strip()
        if not changes and new != plan:
            raise ValueError(
                "the AI changed the plan without saying what or why; nothing was "
                "created. Edit the plan by hand, or try again."
            )
        if not changes:
            raise ValueError(
                "the AI found nothing to fix from the log"
                + (f": {notes[:300]}" if notes else "")
            )
        concerns = fix_concerns(plan, new)
        if concerns:
            notes = (notes + " " + "".join(f"REVIEW: {c}. " for c in concerns)).strip()
        new["fix_changes"] = changes
        new["fix_concerns"] = concerns
        new["fix_notes"] = notes
        new["fix_diff"] = plan_diff(
            {k: v for k, v in plan.items() if k not in PLAN_DIFF_SKIP}, new
        )
        new["caveats"] = (
            str(new.get("caveats") or "")
            + f" AI fix of failed run #{run_id}: "
            + "; ".join(changes)
        ).strip()
        created = self.create(
            run["session_id"],
            run["scope"],
            run.get("selection") or "",
            run.get("request") or "",
            run.get("target"),
            rerun_of=run_id,
            plan=new,
        )
        return {
            **created,
            "fix": {
                "changes": changes,
                "notes": notes,
                "remaining": (created.get("plan") or {}).get("warnings") or [],
            },
        }

    # ---- submit / cancel -------------------------------------------------
    def submit(self, run_id: int) -> dict:
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "draft":
            raise ValueError(
                f"run is {run['status']}; only a reviewed draft can be submitted"
            )
        tgt = self.target(run["target"])
        if not tgt:
            raise ValueError("no compute target configured (lab_targets.json)")
        # Claim the draft atomically: a double-click or a second tab must not send
        # a second Slurm job (the first would be orphaned and keep billing).
        if not self._update(
            run_id,
            only_if=("draft",),
            status="submitting",
            stage="Submitting to " + tgt.label,
            submitted_at=_now(),
        ):
            now = self.get(run_id) or run
            raise ValueError(f"run is {now['status']}; it was already submitted")
        _IN_FLIGHT.add(self._key(run_id))
        try:
            plan = run["plan"] or {}
            sources = self._plan_sources(plan)
            from deepresearch.sources.staging import relay_upload, sources_json

            for src in sources:
                if src.effective_staging == "relay":
                    self._update(
                        run_id, stage=f"Uploading data source {src.name} to the cluster"
                    )
                    # Re-lists the source first: the cluster copy, its folder name and
                    # the provenance all follow today's contents.
                    relay_upload(tgt, src, log=lambda m: None)
                    self.sources.save_check(src)
            # Built after staging so $DS_<NAME> points at the folder just uploaded.
            script = build_sbatch(run_id, plan, tgt, sources)
            self._update(run_id, script=script)
            files = {
                "run.sbatch": script,
                "plan.json": json.dumps(plan, indent=2),
                "README.txt": f"deep-research Lab run #{run_id} for session "
                f"#{run['session_id']}\n{plan.get('question', '')}\n",
            }
            if sources:
                files["sources.json"] = sources_json(
                    sources, getattr(tgt, "remote_root", "~/deep-research-lab")
                )
            if self._smoke_applies(tgt, plan):
                # Smoke test first, on the warm node: the real run only starts if the
                # cut-down run of this exact plan passes (decisions, Lab plan v3).
                tgt.upload(run_id, files, fresh=True)
                for src in sources:
                    self.sources.record_use(src, "lab_run", run_id)
                # Stay in flight until the pilot is queued and the run says "smoke":
                # queueing it is slow (ssh), and meanwhile the watcher must not take
                # the still-"submitting" run for one a stopped dashboard left behind.
                try:
                    self._start_smoke(run_id, tgt, plan, round_no=1, original=plan)
                finally:
                    _IN_FLIGHT.discard(self._key(run_id))
                self.ensure_watcher()
                return self.get(run_id) or {}
            job = self._dispatch(run_id, tgt, plan, files)
            for src in sources:
                self.sources.record_use(src, "lab_run", run_id)
        except NotSubmitted as e:
            # Nothing reached the cluster (sign-in expired, VPN or tunnel down): keep
            # the reviewed plan as a draft so Submit works again once it's fixed,
            # instead of a failed run whose only way back is a new draft.
            _IN_FLIGHT.discard(self._key(run_id))
            self._update(
                run_id,
                only_if=("submitting",),
                status="draft",
                stage="Not submitted",
                error=str(e)[:500],
                submitted_at=None,
            )
            raise
        except Exception as e:
            _IN_FLIGHT.discard(self._key(run_id))
            self._update(
                run_id,
                only_if=("submitting",),
                status="failed",
                stage="Submit failed",
                error=str(e)[:500],
                finished_at=_now(),
            )
            raise
        _IN_FLIGHT.discard(self._key(run_id))
        if not self._update(
            run_id,
            only_if=("submitting",),
            status="queued",
            stage="Queued on the warm Lab node"
            if job.startswith("warm:")
            else "Queued, waiting for a node",
            job_id=job,
            submitted_at=_now(),
        ):
            # Cancelled while the upload/sbatch was in flight: the job exists now,
            # so stop it rather than leave it billing untracked.
            self._update(run_id, job_id=job)
            try:
                tgt.cancel(job)
            except Exception:
                pass
        self.ensure_watcher()
        return self.get(run_id) or {}

    def cancel(self, run_id: int) -> dict:
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] in ("planning", "draft", "plan_failed"):
            self._update(run_id, status="cancelled", stage="Cancelled before submit")
        elif run["status"] in ("fetching", "analyzing"):
            # The Slurm job already ended; there is nothing to scancel. Cancelling now
            # skips the rest of the fetch and the AI write-up.
            self._update(
                run_id,
                status="cancelled",
                stage="Cancelled after the job ended",
                finished_at=_now(),
            )
        elif run["status"] == "smoke":
            tgt = self.target(run["target"])
            task = (run.get("smoke") or {}).get("task")
            if tgt and task:
                try:
                    tgt.warm_cancel(task)
                except Exception:
                    pass  # the task will still end with the worker
            self._update(
                run_id,
                only_if=("smoke",),
                status="cancelled",
                stage="Cancelled during the pilot",
                finished_at=_now(),
            )
        elif run["status"] == "submitting" and not run.get("job_id"):
            # No job id yet: either the upload/sbatch is still in flight (submit sees
            # the cancel and scancels the job it gets back) or the dashboard died
            # mid-submit and nothing will ever finish it.
            self._update(
                run_id,
                only_if=("submitting",),
                status="cancelled",
                stage="Cancelled during submit",
                error="If a job id appears on the cluster later, cancel it there "
                "(squeue --me).",
                finished_at=_now(),
            )
        elif run["status"] in ACTIVE and run.get("job_id"):
            tgt = self.target(run["target"])
            if tgt:
                try:
                    tgt.cancel(run["job_id"])
                except Exception:
                    # scancel fails when the job ended between the page's last poll
                    # and this click; the watcher has then moved the run on.
                    now = self.get(run_id) or run
                    if now["status"] not in ("fetching", "analyzing", *FINAL):
                        raise
                    if now["status"] in FINAL:
                        return now
            self._update(
                run_id,
                only_if=ACTIVE,
                status="cancelled",
                stage="Cancelled",
                finished_at=_now(),
            )
        else:
            raise ValueError(f"run is {run['status']}")
        return self.get(run_id) or {}

    def replan(self, run_id: int) -> dict:
        """Put a run whose planning failed back to `planning` (caller starts make_plan)."""
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "plan_failed":
            raise ValueError(f"run is {run['status']}")
        self._update(
            run_id, status="planning", stage="Retrying the plan", error=None, plan=None
        )
        return self.get(run_id) or {}

    def _switch_partition(self, run: dict, tgt) -> bool:
        """A queued job whose nodes keep failing on a stocked-out partition moves to one
        that has capacity (same plan, new partition), once. True when it moved."""
        plan = dict(run.get("plan") or {})
        if plan.get("partition_switched"):
            return False
        out = (
            stocked_out_partitions(tgt, self.bifrost)
            if self.bifrost is not None
            else stocked_out_partitions(tgt)
        )
        self._stocked_out[tgt.name] = out
        cur = (plan.get("resources") or {}).get("partition") or tgt.default_partition
        if cur not in out:
            return False
        new, why = suggest_partition(tgt, plan, out)
        if new == cur:
            return False
        try:
            tgt.cancel(run["job_id"])
        except Exception:
            return False
        plan["resources"] = {**(plan.get("resources") or {}), "partition": new}
        plan["partition_switched"] = f"{cur} -> {new} (GCP had no capacity for {cur})"
        script = build_sbatch(run["id"], plan, tgt, self._safe_sources(plan))
        try:
            tgt.upload(
                run["id"],
                {"run.sbatch": script, "plan.json": json.dumps(plan, indent=2)},
                fresh=False,
            )
            job = tgt.sbatch_uploaded(run["id"])
        except Exception as e:
            self._update(
                run["id"],
                only_if=ACTIVE,
                error=f"partition switch failed: {str(e)[:200]}",
            )
            return False
        self._update(
            run["id"],
            only_if=ACTIVE,
            plan=plan,
            script=script,
            job_id=job,
            estimate_usd=estimate_cost(tgt, plan),
            stage=f"Moved from {cur} to {new}: GCP had no capacity for {cur}; queued again",
        )
        return True

    # ---- planner probes (warm node) ---------------------------------------
    PROBE_MAX = 6
    PROBE_WAIT_S = 420

    def _warm_exec(
        self, tgt, task: str, script: str, wait_s: int
    ) -> tuple[int | None, str]:
        """Run a short read-only script on the warm node; (rc, output). Never raises."""
        try:
            tgt.warm_enqueue(task, {"run.sh": script}, need_sec=min(wait_s, 600))
            self._keep_warm(tgt, force=True)
        except Exception as e:
            return None, f"(could not reach the warm node: {str(e)[:200]})"
        t0 = time.time()
        while time.time() - t0 < wait_s:
            try:
                t = tgt.warm_task(task)
            except Exception:
                t = {"where": "?"}
            if t.get("where") == "done":
                return t.get("rc"), tgt.warm_task_log(task, 40000)
            time.sleep(5)
        try:
            tgt.warm_cancel(task)
        except Exception:
            pass
        return None, "(timed out waiting for the warm node)"

    def _run_probes(self, run_id: int, run: dict, tgt, title: str) -> str:
        """Ask the model which facts to check, check them on the warm node, return text."""
        if not tgt or not getattr(tgt, "warm", None):
            return ""
        try:
            self._update(
                run_id,
                only_if=("planning",),
                stage="Choosing what to check on the cluster",
            )
            reply, cost = self._ask(
                PROBE_PROMPT.format(
                    max_checks=self.PROBE_MAX,
                    target=_describe(tgt, full=True)[:30000],
                    title=title,
                    text=_strip_sources(run["selection"] or "")[:15000],
                    question_line=f"\nTHE USER'S INSTRUCTION: {run['request']}\n"
                    if run.get("request")
                    else "",
                ),
                search=False,
            )
            self._add_cost(run_id, cost)
            data = extract_json(reply)
            checks = (data.get("checks") if isinstance(data, dict) else data) or []
            script, ok = probe_script(
                [c for c in checks if isinstance(c, dict)][: self.PROBE_MAX]
            )
            if not ok:
                return ""
            self._update(
                run_id,
                only_if=("planning",),
                stage=f"Checking {len(ok)} facts on the warm Lab node (software help, versions, URLs)",
            )
            rc, out = self._warm_exec(
                tgt, f"probe-{run_id}-{int(time.time())}", script, self.PROBE_WAIT_S
            )
        except Exception as e:  # probes help; they never block planning
            return f"\n(Cluster checks were skipped: {str(e)[:160]})\n"
        return (
            "\nFACTS CHECKED ON THE CLUSTER JUST NOW (trust these over memory; use the exact "
            "flags, versions, signatures and file names shown):\n" + out[:24000] + "\n"
        )

    def _check_urls(self, run_id: int, tgt, plan: dict) -> list[dict]:
        """curl every download URL of a new plan from the warm node."""
        urls = script_urls(plan)
        if not urls or not tgt or not getattr(tgt, "warm", None):
            return []
        self._update(
            run_id,
            only_if=("planning",),
            stage=f"Checking {len(urls)} download URL(s) on the cluster",
        )
        lines = ["#!/bin/bash"]
        for u in urls:
            q = shlex.quote(u)
            lines.append(
                f"echo \"URL {u} $(curl -sSL -m 25 -o /dev/null -w '%{{http_code}} %{{size_download}}' -r 0-65535 {q} 2>&1 | tail -c 120)\""
            )
        rc, out = self._warm_exec(
            tgt, f"urls-{run_id}-{int(time.time())}", "\n".join(lines) + "\n", 240
        )
        res: list[dict[str, Any]] = []
        for u in urls:
            m = re.search(r"^URL " + re.escape(u) + r" (\S+)(?: (\S+))?", out, re.M)
            if not m:
                res.append({"url": u, "ok": None, "status": "not checked"})
                continue
            code = m.group(1)
            # 206 = the partial range we asked for
            ok = code.isdigit() and 200 <= int(code) < 400
            res.append(
                {
                    "url": u,
                    "ok": ok,
                    "status": f"HTTP {code}" if code.isdigit() else code[:60],
                }
            )
        return res

    def _ladder_tail(self, tgt) -> str:
        """The end of envs/ladder.jsonl: through bifrost files_read (R1) or SSH tail."""
        if self.bifrost is not None:
            from deepresearch.dashboard import bifrost as bf

            try:
                d = (
                    self.bifrost.call(
                        "files_read",
                        {"path": "~/deep-research-lab/envs/ladder.jsonl", "bytes": 1},
                    )
                    or {}
                )
                size = int((d.get("chunk") or {}).get("file_bytes") or 0)
                start = max(0, size - 60000)
                d = (
                    self.bifrost.call(
                        "files_read",
                        {
                            "path": "~/deep-research-lab/envs/ladder.jsonl",
                            "offset": start,
                            "bytes": 65536,
                        },
                    )
                    or {}
                )
                text = str((d.get("untrusted") or {}).get("text") or "")
                return text.split("\n", 1)[1] if start and "\n" in text else text
            except (bf.BifrostError, ValueError, TypeError):
                pass
        try:
            return tgt.run(
                "tail -n 400 ~/deep-research-lab/envs/ladder.jsonl 2>/dev/null",
                timeout=30,
            ).stdout.decode("utf-8", "replace")
        except Exception:
            return ""

    def env_notes(self, tgt) -> str:
        """Which install rung worked for recent package lists (ladder.jsonl), for prompts."""
        if not tgt or not getattr(tgt, "warm", None):
            return ""
        raw = self._ladder_tail(tgt)
        if not raw:
            return ""
        runs = {}
        for ln in raw.splitlines():
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if d.get("rung") and d.get("rung") not in ("layered-venv", "pixi"):
                runs[d.get("key")] = (
                    d  # only the interesting ones: a fallback was needed
                )
        if not runs:
            return ""
        return (
            "Recent jobs needed a fallback install method (the harness does this "
            "automatically; list packages normally): "
            + "; ".join(
                f"{d.get('run')}: {d.get('rung')}" for d in list(runs.values())[-8:]
            )
        )

    # ---- smoke test and warm node ------------------------------------------
    SMOKE_MAX_ROUNDS = 3  # AI fixes of a failing smoke test before giving up
    WARM_RECHECK_S = 120.0

    @staticmethod
    def _plan_core(plan: dict) -> dict:
        """The parts of a plan that decide what runs (for 'is this still the same plan')."""
        return {
            k: v
            for k, v in (plan or {}).items()
            if k not in PLAN_DIFF_SKIP
            and k not in ("fix_changes", "fix_notes", "smoke_fix")
        }

    @staticmethod
    def _smoke_applies(tgt, plan: dict) -> bool:
        if not getattr(tgt, "warm", None) or plan.get("smoke") is False:
            return False
        r = plan.get("resources") or {}
        # CPU warm node: a GPU plan's smoke test would fail for want of a GPU
        return _int(r.get("gpus"), 0) == 0

    @staticmethod
    def _warm_full_ok(tgt, plan: dict) -> bool:
        """Short single-node CPU runs on the warm node's partition skip the node boot."""
        cfg = getattr(tgt, "warm", None)
        if not cfg:
            return False
        r = plan.get("resources") or {}
        part = r.get("partition") or tgt.default_partition
        tl = str(r.get("time_limit") or "01:00:00")
        tl = tl if TIME_RE.fullmatch(tl) else "01:00:00"
        return (
            part == cfg["partition"]
            and max(_int(r.get("nodes"), 1), 1) == 1
            and _int(r.get("gpus"), 0) == 0
            and _hours(tl) * 60 <= cfg["max_full_min"]
        )

    def _keep_warm(self, tgt, force: bool = False) -> str | None:
        """Make sure a warm worker exists (at most every WARM_RECHECK_S seconds)."""
        now = time.time()
        if (
            not force
            and now - self._warm_checked.get(tgt.name, 0) < self.WARM_RECHECK_S
        ):
            return None
        self._warm_checked[tgt.name] = now
        return tgt.ensure_warm()

    @staticmethod
    def _home(path: str) -> str:
        return "$HOME" + path[1:] if path.startswith("~") else path

    def _dispatch(self, run_id: int, tgt, plan: dict, files: dict | None) -> str:
        """Start the real run: on the warm node when it fits, else as its own Slurm job.

        `files` None means they are already in the run folder (after a smoke test).
        Returns the job id ('warm:<task>' for the warm node).
        """
        if self._warm_full_ok(tgt, plan):
            if files:
                tgt.upload(run_id, files, fresh=True)
            r = plan.get("resources") or {}
            tl = str(r.get("time_limit") or "01:00:00")
            sec = int(_hours(tl if TIME_RE.fullmatch(tl) else "01:00:00") * 3600)
            d = self._home(tgt.job_dir(run_id))
            task = f"{task_prefix(tgt)}full-{run_id}"
            run_sh = (
                "#!/bin/bash\n"
                f'RUN="{d}"\nexport SLURM_SUBMIT_DIR="$RUN"\ncd "$RUN"\n'
                'echo "[INFO] running on the warm Lab node $(hostname -s)" >> job.log\n'
                f"exec timeout --kill-after=30 {sec} bash run.sbatch >> job.log 2>&1\n"
            )
            tgt.warm_enqueue(
                task, {"run.sh": run_sh}, need_sec=sec + 120, exclusive=True
            )
            self._keep_warm(tgt, force=True)
            return "warm:" + task
        if files:
            return tgt.submit(run_id, files)
        return tgt.sbatch_uploaded(run_id)

    def _start_smoke(
        self, run_id: int, tgt, plan: dict, round_no: int, original: dict,
        rounds: list | None = None,
    ) -> None:  # fmt: skip
        cfg = tgt.warm or {}
        d = self._home(tgt.job_dir(run_id))
        task = f"{task_prefix(tgt)}smoke-{run_id}-{round_no}"
        sec = int(cfg.get("smoke_min", 15)) * 60
        run_sh = (
            "#!/bin/bash\n"
            f'RUN="{d}"\nS="$RUN/smoke"\nrm -rf "$S"; mkdir -p "$S"\n'
            'cp "$RUN/run.sbatch" "$S/"; for f in plan.json sources.json; do '
            '[ -f "$RUN/$f" ] && cp "$RUN/$f" "$S/"; done\n'
            'cd "$S"\nexport SLURM_SUBMIT_DIR="$S" LAB_SMOKE=1\n'
            f"exec timeout --kill-after=20 {sec} bash run.sbatch > job.log 2>&1\n"
        )
        tgt.warm_enqueue(task, {"run.sh": run_sh}, need_sec=sec + 60)
        state = self._keep_warm(tgt, force=True) or ""
        self._update(
            run_id,
            only_if=("submitting", "smoke"),
            status="smoke",
            stage=f"Pilot (round {round_no}): "
            + (
                "warm Lab node booting"
                if state.startswith(("started", "pending"))
                else "queued on the warm Lab node"
            ),
            error=None,
            smoke={
                "task": task,
                "round": round_no,
                "rounds": rounds or [],
                "original": self._plan_core(original),
                "fixing": False,
            },
        )

    _SMOKE_ERR = re.compile(
        r"Traceback|Error:|error:|No such file|not found|Segmentation fault|Killed|"
        r"command not found|FAILED|Failed \(exit",
    )

    def _poll_smoke(self, run: dict, tgt) -> None:
        sm = dict(run.get("smoke") or {})
        if sm.get("fixing") and self._key(run["id"]) not in _SMOKE_FIXING:
            # the dashboard restarted while the AI was fixing it (run #88): resume the fix
            # from the saved log instead of failing a run nobody did anything wrong with
            last = (sm.get("rounds") or [{}])[-1]
            _SMOKE_FIXING.add(self._key(run["id"]))
            threading.Thread(
                target=self._smoke_fix,
                args=(
                    run["id"], str(last.get("log_tail") or ""), last.get("rc"),
                    list(last.get("missing") or []), "",
                ),
                daemon=True,
            ).start()  # fmt: skip
            return
        if sm.get("fixing") or not sm.get("task"):
            return
        run_id = run["id"]
        t = tgt.warm_task(sm["task"])
        n = sm.get("round", 1)
        if t["where"] in ("queue", "missing"):
            if t["where"] == "missing":
                sm["missing"] = int(sm.get("missing") or 0) + 1
                if sm["missing"] < 6:  # renames between polls; look again
                    self._update(run_id, only_if=("smoke",), smoke=sm)
                    return
                self._update(
                    run_id,
                    only_if=("smoke",),
                    status="failed",
                    stage="Pilot lost",
                    error="The smoke-test task disappeared from the warm node's queue "
                    "(the node may have been reclaimed). Submit again to retry.",
                    finished_at=_now(),
                )
                return
            state = self._keep_warm(tgt) or ""
            if state:
                self._update(
                    run_id,
                    only_if=("smoke",),
                    stage=f"Pilot (round {n}): "
                    + (
                        "warm Lab node booting"
                        if state.startswith(("started", "pending"))
                        else "queued on the warm Lab node"
                    ),
                )
            return
        if t["where"] == "running":
            st = tgt.read_file(run_id, "smoke/stage.txt", 2000).strip().splitlines()
            self._update(
                run_id,
                only_if=("smoke",),
                node=t["node"],
                stage=f"Pilot (round {n}) on {t['node']}"
                + (f": {st[-1]}" if st else ""),
            )
            return
        # done
        rc = t["rc"]
        log = tgt.read_file(run_id, "smoke/job.log", 60000)
        plan = run.get("plan") or {}
        missing = tgt.missing_outputs(
            run_id, "smoke", [str(x) for x in plan.get("expected_outputs") or []]
        )
        clean_timeout = rc == 124 and not self._SMOKE_ERR.search(log[-20000:])
        passed = (rc == 0 and not missing) or clean_timeout
        fclass, fadvice = ("", "") if passed else classify_failure(log)
        blocked = self._note_runtime_blocked(run_id, log, plan)
        if blocked and not passed:
            fadvice = (fadvice + " " + labfetch_problem(blocked)).strip()
        sm["rounds"] = list(sm.get("rounds") or []) + [
            {
                "round": n,
                "rc": rc,
                "missing": missing,
                "passed": passed,
                "class": fclass,
                "note": "timed out without errors (the script may ignore LAB_SMOKE)"
                if clean_timeout
                else "",
                "seconds": (t["finished"] - t["started"])
                if t["finished"] and t["started"]
                else None,
                "log_tail": log[-4000:],
            }
        ]
        if passed:
            # the pilot's own checks: a design that cannot discriminate (both arms
            # saturated) stops here instead of spending the full run to learn nothing
            gate = self._pilot_gate(run, tgt)
            if gate:
                sm["pilot"] = gate
                self._update(
                    run_id,
                    only_if=("smoke",),
                    status="draft",
                    stage="Pilot: the test cannot discriminate; "
                    + (
                        "AI is re-planning the design"
                        if self._auto_replan_allowed(run)
                        else "change the design, then submit"
                    ),
                    smoke=sm,
                    submitted_at=None,
                )
                self._maybe_auto_replan(self.get(run_id) or run, pilot=True)
                return
            same = self._plan_core(plan) == sm.get("original")
            if same:
                try:
                    job = self._dispatch(run_id, tgt, plan, None)
                except Exception as e:
                    self._update(
                        run_id,
                        only_if=("smoke",),
                        status="failed",
                        stage="Submit failed after the pilot",
                        error=str(e)[:500],
                        smoke=sm,
                        finished_at=_now(),
                    )
                    return
                self._update(
                    run_id,
                    only_if=("smoke",),
                    status="queued",
                    stage=f"Pilot passed (round {n}); "
                    + (
                        "queued on the warm Lab node"
                        if job.startswith("warm:")
                        else "queued, waiting for a node"
                    ),
                    job_id=job,
                    smoke=sm,
                )
            else:
                # the AI changed the plan to get here: a person approves it first
                self._update(
                    run_id,
                    only_if=("smoke",),
                    status="draft",
                    stage=f"Pilot passed after {n - 1} AI fix(es): review the changes, then submit",
                    smoke=sm,
                    submitted_at=None,
                )
            return
        if n >= self.SMOKE_MAX_ROUNDS:
            self._fail_smoke(run, sm, log, f"Pilot failed {n} times", fadvice)
            return
        # the same failure class twice in a row means the AI is not getting anywhere
        prev = [r.get("class") for r in sm["rounds"][:-1]]
        if fclass in ("container", "timeout", "oom") or (
            prev and prev[-1] == fclass and fclass in ("install", "missing-feature")
        ):
            self._fail_smoke(
                run, sm, log,
                f"Pilot failed ({fclass}); not handed to the AI again", fadvice,
            )  # fmt: skip
            return
        sm["fixing"] = True
        if not self._update(
            run_id,
            only_if=("smoke",),
            stage=f"Pilot failed (round {n}, exit {rc}"
            + (f", missing {', '.join(missing[:3])}" if missing else "")
            + "); AI is fixing it",
            smoke=sm,
        ):
            return
        _SMOKE_FIXING.add(self._key(run_id))
        threading.Thread(
            target=self._smoke_fix,
            args=(run_id, log, rc, missing, fadvice),
            daemon=True,
        ).start()

    def _fail_smoke(
        self, run: dict, sm: dict, log: str, why: str, advice: str = ""
    ) -> None:
        dest = self.results_dir / f"run_{run['id']}"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "job.log").write_text(log)  # so Fix with AI and the Log view have it
        last = (sm.get("rounds") or [{}])[-1]
        self._update(
            run["id"],
            only_if=("smoke",),
            status="failed",
            stage=why,
            error=f"{why}; last exit {last.get('rc')}"
            + (
                f", missing outputs: {', '.join(last.get('missing') or [])}"
                if last.get("missing")
                else ""
            )
            + ". The full run was not started."
            + (f" {advice}" if advice else ""),
            smoke={**sm, "fixing": False},
            finished_at=_now(),
        )

    def _smoke_fix(
        self, run_id: int, log: str, rc: int | None, missing: list[str],
        advice: str = "",
    ) -> None:  # fmt: skip
        """Background: ask the AI to fix a failed smoke test, then run the next round."""
        try:
            self._smoke_fix_inner(run_id, log, rc, missing, advice)
        finally:
            _SMOKE_FIXING.discard(self._key(run_id))

    def _smoke_fix_inner(
        self, run_id: int, log: str, rc: int | None, missing: list[str],
        advice: str = "",
    ) -> None:  # fmt: skip
        run = self.get(run_id)
        if not run or run["status"] != "smoke":
            return
        sm = dict(run.get("smoke") or {})
        tgt = self.target(run["target"])
        plan = {
            k: v for k, v in (run.get("plan") or {}).items() if k not in PLAN_DIFF_SKIP
        }
        try:
            if not tgt:
                raise ValueError("no target")
            self._fresh_catalog(tgt)
            if not advice:
                fclass, fadvice = classify_failure(log)
                if fadvice:
                    advice = f"({fclass}) {fadvice}"
            extra = (
                f"Expected output files that were missing or empty: {', '.join(missing)}. "
                if missing
                else ""
            )
            prompt = RUNFIX_PROMPT.format(
                target=_describe(tgt, full=True),
                lessons=labguard.prompt_block(
                    json.dumps(plan) + " " + log[-3000:], self.state_dir
                ),
                state="SMOKE TEST FAILED: the plan ran with LAB_SMOKE=1 (a cut-down run) on the "
                "warm Lab node. "
                + extra
                + (f"DIAGNOSIS: {advice} " if advice else "")
                + "Fix the cause. If the script ignores LAB_SMOKE, "
                "also make it honour it (smallest sizes, every step, every output)",
                exit_code=rc,
                elapsed="-",
                log=_log_for_fix(log),
                plan=json.dumps(plan, indent=1)[:60000],
            )
            new, out, why = self._ask_plan(run_id, prompt)
            changes = [str(c) for c in (out.get("changes") or [])][:20]
            if new is None:
                raise ValueError(f"the AI returned no usable plan twice ({why})")
            if not changes and self._plan_core(new) != self._plan_core(plan):
                # changed the plan but did not say what: ask once for the list
                new2, out2, _ = self._ask_plan(
                    run_id,
                    prompt
                    + '\n\nYour previous answer changed the plan but its "changes" list '
                    'was empty. Return the same fix with one line per change in "changes".',
                )
                if new2 is not None and out2.get("changes"):
                    new, out = new2, out2
                    changes = [str(c) for c in (out2.get("changes") or [])][:20]
            if not changes or self._plan_core(new) == self._plan_core(plan):
                raise ValueError(
                    "the AI found nothing to fix"
                    + (
                        f": {str(out.get('notes'))[:200]}"
                        if isinstance(out, dict) and out.get("notes")
                        else ""
                    )
                )
            # mechanical problems pre-flight can see are fixed before spending a cluster
            # round on them (run #86: the AI's fix kept a `$C_D` that killed the job again)
            new["script"], esc = labguard.escape_heredoc_unset_vars(str(new["script"]))
            if esc:
                changes.append(
                    "escaped unset shell variable(s) in an unquoted heredoc: "
                    + ", ".join("$" + e for e in esc)
                )
            blocking = [
                w
                for w in check_script(str(new["script"]))
                + [x for x in labguard.science_warnings(new) if "never defines" in x]
            ]
            if blocking:
                prompt2 = (
                    prompt
                    + "\n\nYour previous answer still has these problems (found by a static "
                    "check, before running). Return the corrected plan, same JSON format:\n- "
                    + "\n- ".join(blocking)
                    + "\n\nYour previous plan:\n"
                    + json.dumps(new, indent=1)[:60000]
                )
                new2, out2, _ = self._ask_plan(run_id, prompt2)
                if new2 is not None:
                    new2["script"], _ = labguard.escape_heredoc_unset_vars(
                        str(new2["script"])
                    )
                    new = new2
                    changes += [str(c) for c in (out2.get("changes") or [])][:10]
                still = check_script(str(new["script"])) + [
                    x for x in labguard.science_warnings(new) if "never defines" in x
                ]
                if still:
                    raise ValueError(
                        "the AI's fix still fails static checks: " + still[0][:160]
                    )
            dropped = _dropped_options(plan, new)
            concerns = fix_concerns(plan, new)
            original = sm.get("original") or {}
            new["fix_changes"] = list(
                (run.get("plan") or {}).get("fix_changes") or []
            ) + [f"smoke round {sm.get('round', 1)}: {c}" for c in changes]
            new["fix_notes"] = (
                (f"REVIEW: removes option(s) {', '.join(dropped)}. " if dropped else "")
                + "".join(f"REVIEW: {c} " for c in concerns)
                + str(out.get("notes") or "")
            ).strip()
            new["fix_concerns"] = (
                list((run.get("plan") or {}).get("fix_concerns") or []) + concerns
            )
            new["fix_diff"] = plan_diff(original, new)
            if run.get("data_sources"):
                new["data_sources"] = list(run["data_sources"])
            new["warnings"] = self._check(tgt, new)
            if tgt.catalog:
                new["catalog_generated"] = tgt.catalog.get("generated")
            sources = self._plan_sources(new)
            script = build_sbatch(run_id, new, tgt, sources)
            files = {"run.sbatch": script, "plan.json": json.dumps(new, indent=2)}
            tgt.upload(run_id, files, fresh=False)
            if not self._update(
                run_id, only_if=("smoke",), plan=new, script=script,
                estimate_usd=estimate_cost(tgt, new),
            ):  # fmt: skip
                return  # cancelled meanwhile
            self._start_smoke(
                run_id, tgt, new, int(sm.get("round", 1)) + 1,
                original={}, rounds=sm.get("rounds"),
            )  # fmt: skip
            # keep the plan Chuck submitted as the reference, not the fixed one
            cur = self.get(run_id) or {}
            sm2 = dict(cur.get("smoke") or {})
            sm2["original"] = original
            self._update(run_id, only_if=("smoke",), smoke=sm2)
        except Exception as e:
            run = self.get(run_id) or run
            self._fail_smoke(
                run, sm, log, f"Pilot failed; AI fix failed ({str(e)[:160]})"
            )

    def warm_status(self, name: str | None = None) -> dict:
        tgt = self.target(name)
        if not tgt or not getattr(tgt, "warm", None):
            return {"enabled": False}
        cfg = tgt.warm or {}
        return {
            **tgt.warm_status(),
            "always_on": bool(cfg.get("always_on")),
            "keeper_paused": tgt.name in _WARM_PAUSED,
        }

    def warm_start(self, name: str | None = None) -> dict:
        tgt = self.target(name)
        if not tgt or not getattr(tgt, "warm", None):
            raise ValueError("no warm worker configured for this target")
        _WARM_PAUSED.discard(tgt.name)
        state = self._keep_warm(tgt, force=True)
        return {"state": state, **tgt.warm_status()}

    def warm_stop(self, name: str | None = None) -> dict:
        tgt = self.target(name)
        if not tgt or not getattr(tgt, "warm", None):
            raise ValueError("no warm worker configured for this target")
        tgt.warm_stop()
        # a manual stop pauses the always-on keeper until the next manual start
        _WARM_PAUSED.add(tgt.name)
        return {
            "stopping": True,
            "keeper_paused": bool((tgt.warm or {}).get("always_on")),
        }

    # ---- always-on warm node (v0.48.0) -------------------------------------------
    KEEPER_INTERVAL_S = 300.0

    def start_warm_keeper(self) -> None:
        """For targets with `"warm": {"always_on": true}`: keep one warm worker running
        at all times, even with no Lab runs (jobs then start in seconds). One keeper per
        process (Main's Lab starts it; workspaces share the node). A manual Stop pauses
        it until Start. The worker itself never exits for idleness (idle_min 0) and is
        replaced near its time limit."""
        if not any(
            (getattr(t, "warm", None) or {}).get("always_on")
            for t in self.targets.values()
        ):
            return
        with _KEEPER_LOCK:
            global _KEEPER
            if _KEEPER and _KEEPER.is_alive():
                return
            _KEEPER = threading.Thread(
                target=self._keeper_loop, daemon=True, name="lab-warm-keeper"
            )
            _KEEPER.start()

    def _keeper_loop(self) -> None:
        while not self._stop.is_set():
            self.keep_warm_once()
            self._stop.wait(self.KEEPER_INTERVAL_S)

    def keep_warm_once(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, t in self.targets.items():
            cfg = getattr(t, "warm", None) or {}
            if not cfg.get("always_on") or name in _WARM_PAUSED:
                continue
            try:
                out[name] = self._keep_warm(t, force=True) or ""
            except Exception as e:  # cluster unreachable, sign-in expired: try later
                out[name] = f"error: {str(e)[:200]}"
        return out

    def log(self, run_id: int, offset: int = 0) -> dict:
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        local = self.results_dir / f"run_{run_id}" / "job.log"
        if run["status"] in ("completed", "failed", "cancelled") and local.exists():
            data = local.read_bytes()
            off = offset if 0 <= offset <= len(data) else 0
            return {
                "text": data[off:].decode("utf-8", "replace"),
                "size": len(data),
                "source": "local",
            }
        tgt = self.target(run["target"])
        if run["status"] == "smoke" and tgt:
            text = tgt.read_file(run_id, "smoke/job.log", 200000)
            data = text.encode()
            off = offset if 0 <= offset <= len(data) else 0
            return {
                "text": data[off:].decode("utf-8", "replace"),
                "size": len(data),
                "source": "smoke",
            }
        if not run.get("job_id"):
            return {"text": "", "size": 0, "source": "none"}
        if not tgt:
            return {"text": "", "size": 0, "source": "none"}
        text, size = tgt.log(run_id, offset)
        return {"text": text, "size": size, "source": "cluster"}

    # ---- watcher ---------------------------------------------------------
    def ensure_watcher(self) -> None:
        with self._watch_lock:
            if self._watcher and self._watcher.is_alive():
                return
            if not self.active():
                return
            self._stop.clear()
            self._watcher = threading.Thread(
                target=self._watch_loop, daemon=True, name="lab-watcher"
            )
            self._watcher.start()

    def _watch_loop(self, interval: float = 15.0) -> None:
        idle_rounds = 0
        while not self._stop.is_set():
            runs = self.active()
            if not runs:
                idle_rounds += 1
                if idle_rounds > 2:
                    return
            else:
                idle_rounds = 0
            for run in runs:
                try:
                    self.poll(run)
                except Exception as e:  # network blip: keep the run, try again later
                    self._update(
                        run["id"], only_if=ACTIVE, error=f"watcher: {str(e)[:300]}"
                    )
            self._stop.wait(interval)

    def poll(self, run: dict) -> None:
        """Advance one active run by one step. Safe to call repeatedly."""
        if (
            run["status"] == "submitting"
            and not run.get("job_id")
            and self._key(run["id"]) not in _IN_FLIGHT
        ):
            # Left over from a dashboard that stopped mid-submit: nothing will ever
            # finish it, and it would block cancel/delete and keep the watcher busy.
            self._update(
                run["id"],
                only_if=("submitting",),
                status="failed",
                stage="Submit interrupted",
                error="The dashboard stopped while submitting this run. Check the "
                "cluster (squeue --me) for a stray job, then Re-run.",
                finished_at=_now(),
            )
            return
        tgt = self.target(run["target"])
        if tgt and run["status"] == "smoke":
            return self._poll_smoke(run, tgt)
        if tgt and str(run.get("job_id") or "").startswith("warm:"):
            self._keep_warm(tgt)
        if not tgt or not run.get("job_id"):
            return
        if run["status"] in ("fetching", "analyzing"):
            return self._finish(run, tgt)
        st = tgt.status(run["id"], run["job_id"])
        state = st["slurm_state"]
        stage = st["stage"] or run.get("stage")
        upd: dict[str, Any] = {
            "slurm_state": state or run.get("slurm_state"),
            "node": st["node"] or run.get("node"),
            "elapsed": st["elapsed"] or run.get("elapsed"),
            "error": None,
        }
        if state in ("PENDING", "CONFIGURING") or (
            not state and run["status"] == "queued"
        ):
            reason = st.get("reason") or ""
            label = {
                "PENDING": "Queued, waiting for a node",
                "CONFIGURING": "Node booting",
            }.get(state, "Queued")
            if reason and reason not in ("None", "Priority"):
                label += f" ({reason})"
            fails = int(st.get("node_fails") or 0)
            if fails >= NODE_FAIL_WARN and self._switch_partition(run, tgt):
                return
            if fails:
                part = ((run.get("plan") or {}).get("resources") or {}).get("partition")
                label = (
                    f"Requeued after {fails} node failure{'s' if fails != 1 else ''}"
                    + (f" on {part}" if part else "")
                    + ": the cluster could not start a node"
                    + (
                        ". Consider cancelling and resubmitting on another partition"
                        if fails >= NODE_FAIL_WARN
                        else ""
                    )
                )
            upd.update(status="queued", stage=label)
        elif state in ("RUNNING", "COMPLETING"):
            upd.update(status="running", stage=stage or "Running")
        elif not state and run["status"] == "running":
            # squeue and sacct both know nothing (accounting off, or the record aged
            # out while the laptop slept). The job's own stage markers decide; after
            # a few empty polls with no marker, fetch whatever is there.
            last = (st.get("stage") or "").strip()
            gone = self._gone_polls.get(run["id"], 0) + 1
            self._gone_polls[run["id"]] = gone
            if last == "Done" or last.startswith("Failed") or gone >= GONE_POLLS:
                self._gone_polls.pop(run["id"], None)
                inferred = "COMPLETED" if last == "Done" else "FAILED"
                upd.update(
                    status="fetching",
                    stage="Fetching results (Slurm no longer lists the job)",
                    slurm_state=inferred,
                )
                if not self._update(run["id"], only_if=ACTIVE, **upd):
                    return
                return self._finish(self.get(run["id"]) or run, tgt)
        elif state in SLURM_DONE or state.startswith("CANCELLED"):
            upd.update(
                status="fetching",
                stage="Fetching results",
                exit_code=st["exit_code"],
                slurm_state=state,
            )
            # `run` was read at the start of the watcher pass; a cancel since then
            # must win, so every write here is conditional on the run still being live.
            if not self._update(run["id"], only_if=ACTIVE, **upd):
                return
            return self._finish(self.get(run["id"]) or run, tgt)
        self._update(run["id"], only_if=ACTIVE, **upd)

    def _finish(self, run: dict, tgt: SlurmSSHTarget) -> None:
        run = self.get(run["id"]) or run  # the watcher's copy may predate a cancel
        dest = self.results_dir / f"run_{run['id']}"
        state = run.get("slurm_state") or ""
        final = SLURM_DONE.get(state.split()[0] if state else "", "failed")
        if run["status"] == "fetching":
            try:
                files = tgt.fetch(run["id"], dest)
            except Exception as e:
                # A fetch can block the watcher for up to 10 minutes; stop retrying
                # after a few attempts instead of stalling every other run forever.
                tries = self._fetch_tries.get(run["id"], 0) + 1
                self._fetch_tries[run["id"]] = tries
                if tries < MAX_FETCH_TRIES:
                    raise
                self._fetch_tries.pop(run["id"], None)
                self._update(
                    run["id"],
                    only_if=("fetching",),
                    status="failed",
                    stage="Could not fetch results",
                    error=f"fetch failed {tries} times: {str(e)[:300]} "
                    "(the outputs are still on the cluster)",
                    finished_at=_now(),
                )
                return
            self._fetch_tries.pop(run["id"], None)
            if not self._update(
                run["id"],
                only_if=("fetching",),
                files=files,
                status="analyzing",
                stage="Writing up results",
            ):
                return  # cancelled while fetching
            run = self.get(run["id"]) or run
        if run.get("status") != "analyzing":
            return
        log_file = dest / "job.log"
        if log_file.exists():
            self._note_runtime_blocked(
                run["id"], log_file.read_text("utf-8", "replace"), run.get("plan") or {}
            )
            run = self.get(run["id"]) or run
        verdict = labguard.read_verdict(dest)
        if verdict is not None:
            self._update(run["id"], verdict=verdict)
            run = self.get(run["id"]) or run
        note, cost = self._analyze(run, dest, final)
        self._add_cost(run["id"], cost)
        stage = "Completed" if final == "completed" else f"Ended: {state or 'unknown'}"
        outcome = labverdict.assess(verdict, final) if final == "completed" else None
        if outcome:
            stage = f"Completed: {outcome['outcome'].upper()}"
            if verdict and verdict.get("audit") and outcome["outcome"] == "confirmed":
                stage += (
                    "; review the verdict ("
                    + verdict["audit"][0].split(" ")[0].lower()
                    + " check)"
                )
        changed = self._update(
            run["id"],
            only_if=("analyzing",),
            status=final,
            stage=stage,
            result_md=note,
            finished_at=_now(),
        )
        if changed:
            self._record_cluster_facts(run, final)
        if changed and final == "completed":
            self._learn_from_fix(run)
            done = self.get(run["id"]) or run
            self._attach_note(done)
            if outcome and outcome["outcome"] == "inconclusive":
                self._maybe_auto_replan(done)

    def _record_cluster_facts(self, run: dict, final: str) -> None:
        """bifrost's view of a finished Slurm job, kept on the run (R1).

        `cluster.efficiency` (requested vs used cores and memory) for every finished
        job; `cluster.diagnosis` (job_explain rule, class and findings) for failed ones,
        next to the Lab's own classifier so the two can be compared before the old one
        is retired. Warm-worker tasks are not Slurm jobs and are skipped."""
        job = str(run.get("job_id") or "")
        if self.bifrost is None or not job.isdigit():
            return
        from deepresearch.dashboard import bifrost as bf

        facts: dict = {}
        try:
            eff = bf.efficiency(self.bifrost, job)
            if eff:
                facts["efficiency"] = eff
            if final != "completed":
                facts["diagnosis"] = bf.explain(self.bifrost, job)
        except bf.BifrostError as e:
            facts["error"] = str(e)[:300]
        if facts:
            facts["job_id"] = job
            facts["as_of"] = _now()
            self._update(run["id"], cluster=facts)

    def _learn_from_fix(self, run: dict) -> None:
        """A completed AI fix of a failed run becomes a lesson for future plans."""
        plan = run.get("plan") or {}
        if not run.get("rerun_of") or not plan.get("fix_changes"):
            return
        failed = self.get(int(run["rerun_of"]))
        if not failed or failed.get("status") != "failed":
            return
        lesson = labguard.lesson_from_fix(failed, plan)
        if lesson:
            try:
                labguard.add_learned(
                    self.state_dir,
                    lesson["text"],
                    lesson["match"],
                    f"{lesson['source']} fixed by run #{run['id']}",
                )
            except (OSError, ValueError):
                pass  # a lesson is a bonus; never fail a finished run over it

    def _analyze(self, run: dict, dest: Path, final: str) -> tuple[str, float | None]:
        plan = run.get("plan") or {}
        files = [f for f in run.get("files") or [] if not f.get("skipped")]
        texts = []
        budget = 40000
        for f in files:
            p = dest / f["path"]
            if (
                f["path"].startswith("outputs/")
                and p.suffix.lower()
                in (
                    ".txt",
                    ".csv",
                    ".json",
                    ".md",
                    ".tsv",
                    ".dat",
                    ".log",
                    ".out",
                    ".xvg",
                )
                and p.exists()
                and budget > 0
            ):
                chunk = p.read_text("utf-8", "replace")[: min(8000, budget)]
                texts.append(f"--- {f['path']} ---\n{chunk}")
                budget -= len(chunk)
        log_path = dest / "job.log"
        log_tail = (
            log_path.read_text("utf-8", "replace")[-6000:] if log_path.exists() else ""
        )
        prompt = ANALYZE_PROMPT.format(
            question=plan.get("question", ""),
            approach=plan.get("approach", ""),
            params=json.dumps(plan.get("parameters") or {}),
            criteria=plan.get("success_criteria", ""),
            state=final,
            exit_code=run.get("exit_code"),
            elapsed=run.get("elapsed"),
            files="\n".join(f"- {f['path']} ({f['size']} bytes)" for f in files)
            or "(none)",
            texts="\n\n".join(texts) or "(none)",
            log=log_tail,
            verdict=json.dumps(run.get("verdict"))[:3000]
            if run.get("verdict")
            else "none written",
            outcome=labverdict.outcome_line(
                {"verdict": run.get("verdict"), "status": final}
            ),
        )
        try:
            return self._ask(prompt, search=False)
        except Exception as e:
            return (
                f"**Result**\n\nThe job ended as `{final}`. The AI write-up failed "
                f"({str(e)[:200]}); see the files and log.",
                getattr(e, "cost", None),  # an empty reply is still billed
            )

    def file_path(self, run_id: int, rel: str) -> Path:
        base = (self.results_dir / f"run_{run_id}").resolve()
        p = (base / rel).resolve()
        if not p.is_relative_to(base) or not p.is_file():
            raise FileNotFoundError(rel)
        return p


def _spent(cost: float | None) -> str:
    return f" (${cost:.4f} spent on the attempt)" if cost else ""


def _strip_sources(md: str) -> str:
    m = re.search(r"\n\*\*Sources:?\*\*\s*\n", md or "")
    if m:
        return md[: m.start()]
    m = re.search(r"\n#+\s*Sources\s*\n", md or "")
    return md[: m.start()] if m else (md or "")

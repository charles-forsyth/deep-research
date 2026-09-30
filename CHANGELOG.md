# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Versions before 1.0 may change behaviour between minor
releases.

## [Unreleased]

## [0.43.0] - 2026-09-30

### Added

- **Export and import a workspace as a zip**, to share or keep a snapshot. Export .zip /
  Import .zip in the Workspaces dialog, or `deep-research workspace export|import`.
  Export leaves the workspace untouched and removes local sign-in references and saved
  search-index ids from the copy; no API key or cluster settings are ever included.
  Import always creates a new workspace and checks the zip first (file paths, links,
  sizes, checksums against its manifest, database integrity); a bad zip adds nothing.

## [0.42.0] - 2026-09-30

### Added

- **Copy into another workspace.** "Copy to..." on a report and "Copy to workspace..." on
  a project copy it with every sub-report and follow-up, highlights and notes, Lab runs
  and their outputs, the data sources they used and the project's notebooks. Numbers are
  renumbered in the target and "Session #N" / "Lab run #N" mentions follow; the source
  is never changed and a failed copy changes nothing. Also
  `deep-research workspace copy --to demo --project 3 --report 290`.

## [0.41.0] - 2026-09-30

### Added

- **Workspace switcher** in the dashboard's top bar: shows the current workspace; the
  Workspaces dialog lists them with counts and switches, creates, duplicates, renames,
  archives and deletes (to the trash, typed confirmation, never Main). Switching reloads
  the page so nothing from the previous workspace stays on screen. Outside Main the only
  cue is a coloured dot and a faint tinted line under the top bar.
- A restart resumes Lab watching in every workspace with runs in flight.

## [0.40.0] - 2026-09-30

### Added

- **Workspaces (foundation).** Separate libraries of reports, projects, notes, notebooks,
  Lab runs, data sources and audio. Main is the existing library and stays exactly where
  it is (`~/.config/deepresearch/`); others live in `~/.config/deepresearch/workspaces/<id>/`.
  The Gemini key, cluster settings and the Lab's lessons are shared.
- `deep-research --workspace ID ...` (or `DR_WORKSPACE`) for any command, and
  `deep-research workspace list|create|duplicate|rename|archive|unarchive|delete`.
  Delete moves the folder to `workspaces/.trash/` and never applies to Main.
- Dashboard API: `/api/workspaces` (list, create, update, duplicate, delete); every
  request is served from the workspace named in `X-DR-Workspace` (default Main). The
  switcher in the top bar comes in 0.41.

### Changed

- Lab runs of a workspace other than Main use their own cluster folders
  (`<remote_root>/ws-<id>/run_<n>`), warm-node task names and Slurm job names, so run
  numbers that repeat across workspaces never share a folder. Main's are unchanged.
- Research started from the dashboard in a workspace always writes to that workspace.

## [0.39.0] - 2026-09-30

### Added

- **Lab outcomes instead of pass/fail.** A finished Lab run is CONFIRMED, REFUTED,
  INCONCLUSIVE or BROKEN, with a one-line reason. Checks are grouped as validation (the
  model is sane), informative (the test can tell) and claim (the report's claim). Runs #79
  and #78 used to say FAILED; they now say INCONCLUSIVE (both arms at 0%, or identical
  numbers), and #98 says REFUTED (a sound test, the claim did not hold). Old runs get an
  outcome from inferred check kinds. New module `dashboard/labverdict.py`.
- **Notes on the report.** A finished run attaches one note to its report (green, magenta
  or amber by outcome) on the passage it tested. The report itself is never edited. The
  outcome and write-up also feed the report brief and summary audio, the project summary,
  dossier and voice overview.
- **The smoke test is now a pilot.** The pilot computes the same checks on a small
  sample; if they show the test cannot discriminate, the full run is not started.
- **One automatic re-plan.** An inconclusive run (or pilot) is re-planned once by the AI,
  which must change the design (regime, literature-calibrated parameters, matched arms,
  informative checks) without changing the claim. The result is a draft for review;
  nothing is submitted automatically. New module `dashboard/labloop.py`.
- **Parameter sources.** Plans carry `parameter_sources` (a citation or "assumed: why" per
  parameter), shown in plan review.

### Changed

- Planner rules: label every check's kind, test claims two-sided with sign and size, match
  the arms, calibrate from the literature and print the regime.
- Stage after completion is "Completed: OUTCOME"; the write-up leads with the outcome.

## [0.38.2] - 2026-09-30

### Fixed

- **Long reports were saved with only their last part.** Deep Research returns long reports as several `model_output` steps; the SDK's `output_text` holds only the last one, and that is what was stored. Every multi-part report since about 2026-09-26 lost its first half or more (one kept 12%). Reports, the reader, search, exports, summaries and audio all read the stored text, so the audio "full read" and summary only covered the end of the report. The whole report is now the joined text of every part.
- New `deep-research repair [IDS] [--apply] [--resynthesize] [--json]` restores damaged reports while Google still keeps the interaction (appended follow-ups are kept; embeddings are cleared so search re-indexes). `--resynthesize` rebuilds recursive reports whose synthesis started from a cut main report (children first). Dry run by default. On this machine: 29 reports restored, 10 recursive reports re-synthesized.
- Audio is re-made when the report text changes (the cache now keys on a hash of the text), so a repaired report does not keep playing its old, short clip.

### Changed

- The audio summary scales with the report: 2-3 minutes for short reports, up to about 5 for long ones, and is told to cover every section.
- Lab results are part of the story: a report's summary audio and briefs, the project AI summary, and the project voice overview now include each Lab run's write-up (what it computed and showed), not just its pass/fail line. Lab runs attach to reports; they never edit them.

## [0.38.1] - 2026-09-30

### Fixed

- **Phones: every page was cut off on the right.** The app shell's one grid column grew to its widest child (the top bar, 430 px on a 390 px phone) and `overflow: hidden` then clipped the right edge of every page: the top bar's + button, stats, cards, the status bar, and the start of report lines. The column is now capped at the screen width (`minmax(0, 1fr)`). A headless-browser audit of 10 views at 320, 360, 390, 430 and 768 px found clipping in 30 views before and none after.
- Phones: Lab run titles wrap instead of being cut mid-word; the Data sources table keeps Name, Kind, Status and Files with sensible widths; project stats show "data sources" in full; Depth and Breadth stack in the launch dialog; larger tap targets in the file browser; room to scroll the last row clear of the status bar.

## [0.38.0] - 2026-09-30

### Added

- **File browser for adding data sources** (docs/SPEC.md 21.9): "+ Add source" and the project page's "Browse files" open a browser over everything already signed in on this machine: this computer, each Google Drive (My Drive, Shared with me, shared drives, full-text search), Google Cloud Storage by project and bucket, and each S3 remote (CephRDS). Click to preview (Docs render as Markdown, Sheets as a table); tick files or open a folder and add it as one source. Typing a location is still one click away.
- **Google Drive sources** (kind `gdrive`): a Drive folder or hand-picked Docs, Sheets and files. Docs arrive as Markdown, Sheets as CSV, Slides as PDF, so research runs, Ask and Lab runs can read them; Lab gets them by relay. Picked files are found again after a rename or move, and a missing one is named.
- Expired rclone sign-ins are reported with the command to fix them (`rclone config reconnect <remote>:`).

### Fixed

- Error messages from rclone and gcloud keep the cause when it comes after a long URL.

## [0.37.0] - 2026-09-29

### Added

- **Projects** (docs/SPEC.md section 22): a container above reports, data sources, notebooks and Lab runs, one per grant, paper, proposal or thesis. The left pane opens with a Projects strip (All, Inbox, each project) that filters the archive; a Projects overview; a project page with stats, reports, data sources, Lab runs and verdicts, notebooks, highlights and most-cited sources.
- A report can be in several projects; one is its **home** and supplies defaults: the launcher files new research into a chosen project and pre-picks its data sources, Ask and the Lab dialog pre-pick them too, and Lab runs use the project's cluster target and prefer its partition.
- Project **AI summary** (bottom line, findings, agreements and conflicts, computational evidence from Lab verdicts, gaps, next steps; flagged when out of date), **Ask this project** (semantic search limited to the project's reports plus its data sources, with citations), project **briefs**, and an **AI voice overview**.
- Three new brief styles everywhere: grant background and significance section, lay summary, literature review.
- **Exports** per project: dossier (Markdown, standalone HTML, Print/PDF), BibTeX and CSV citations, JSON, and a research package (.zip) with an Obsidian-ready `reports/` folder, notebooks, Lab write-ups and small outputs, audio, and RO-Crate 1.1 metadata.
- **Sort inbox**: suggested groups from your tags and from report embeddings (average-linkage clustering, distinctive-word names, optional one-call AI naming); nothing is filed until you accept a group. Each project also lists unfiled reports that look like it.
- Protection level is a project setting and label (never enforced); a project shows the strictest of its own and its data sources' levels. A Nexus grant or lab id can be stored as text.

### Fixed

- Opening something in the dashboard while it was still loading could be replaced by the saved tabs when loading finished.

## [0.36.0] - 2026-09-29

### Added

- `--json` on every command (`research`, `start`, `followup`, `list`, `show`, `search`, `tree`, `delete`, `cleanup`, `auth`, `estimate`, `dashboard`; `sources` already had it). stdout is exactly one JSON document and all log output moves to stderr, so other tools can call the CLI and parse the result. Failures print `{"error": ...}` and exit non-zero. `start --json` prints the new session id; `show --json --recursive` nests child reports; `cleanup --json` is a dry run unless `--force`. See docs/SPEC.md 9.6.

### Changed

- `DeepResearchAgent.follow_up()` returns the answer text (empty string on failure) instead of `None`. Existing callers ignore the return value.
- `sources ... --json` shares the same writer, so its JSON also never mixes with log lines; `sources discover --json` now exits 2 when every catalog failed, like the plain output.

## [0.35.9] - 2026-09-29

### Added

- Pre-flight warns about unsteady SU2 runs without MAX_TIME: SU2 stops at 1 s of physical time by default whatever TIME_ITER says (run #92 ran 201 of 2000 steps and missed the vortex shedding).
- Known problems: LAMMPS runs must check the final atom count (run #89's pile fell apart to 6 atoms and `lost ignore` hid it); SU2 MAX_TIME.

## [0.35.8] - 2026-09-29

### Added

- The dashboard re-checks each job's own verdict instead of trusting its pass flags: numbers that contradict a "pass", tolerances over 20% of the expected value, and comparison arms whose summaries are identical (run #89: both regimes were the same simulation, yet "passed"). Contradictions and identical arms turn the verdict into a fail; loose tolerances are shown as "passed, but see the re-check".
- Known problems: BGK lattice Boltzmann needs tau >= 0.55 (blew up at 0.535, run #70, reproduced on a compute node), pressure probes on the first fluid node; SU2 unsteady force coefficients need AERO_COEFF history output and the start-up transient dropped (C_D near 30 in the first steps, run #87).

## [0.35.7] - 2026-09-29

### Added

- Pre-flight warns about crossing detection with `np.diff(np.sign(...))`: a step from exactly zero (both curves zero below a threshold) counts as a crossing (run #71 reported T*=0.045 instead of about 0.25).

## [0.35.6] - 2026-09-29

### Fixed

- A smoke fix interrupted by a dashboard restart resumes from the saved log instead of failing the run (run #88).
- The install import check knows `python-gmsh` imports as `gmsh` (run #87).

### Added

- Pre-flight catches matplotlib math text in normal (not raw) strings, where `\\tau` and `\\approx` turn into control characters (run #88), including in f-strings.

## [0.35.5] - 2026-09-29

### Fixed

- An AI smoke fix is checked statically before it goes back to the cluster: unset `$VARS` in unquoted heredocs are escaped automatically, and syntax errors or undefined names go back to the AI once with the exact problem; a fix that still fails is stopped instead of burning a smoke round (run #86 lost two rounds to a `$C_D` in a plot label).

## [0.35.4] - 2026-09-29

### Fixed

- Python gmsh installs: pip gmsh wheels need libGLU (missing on compute nodes) and conda-forge `gmsh` has no Python module, so the conda rung now installs `python-gmsh` (run #86; checked on a compute node).

### Added

- Known problems: SU2 8.2 has no SPATIAL_ORDER_FLOW (use MUSCL_FLOW/SLOPE_LIMITER_FLOW); LAMMPS 2D fix pour needs gravity along -y and `lattice` takes no `units box`; Python gmsh needs python-gmsh.

## [0.35.3] - 2026-09-29

### Added

- Pre-flight catches names a script's Python uses but never defines (run #82 finished an hour of GPU benchmarks, then died on a NameError in its report), and `$VARS` bash would expand inside an unquoted heredoc that the script never sets (run #83: a matplotlib label `$C_D` killed the job under `set -u`).
- Pre-flight warns when a computed value is replaced by a fixed number when it comes out of range, so a failed calculation can't report a plausible result.

### Fixed

- Install check commands run without pipefail: `lmp -h | grep -q GRANULAR` died of SIGPIPE and marked a working LAMMPS environment broken (run #81).
- A failed install rung is recorded with the ladder version; environments marked broken by an older (buggy) ladder are retried instead of skipped forever.

## [0.35.2] - 2026-09-29

### Added

- Pre-flight warns when the script imports a Python package nothing installs (no module that has it, not in pip or conda), before the job runs.

### Fixed

- Layering pip packages on python-ml hid all of its packages (pandas: run #76 computed every benchmark, then failed in post-processing): python-ml is itself a venv, so `--system-site-packages` pointed at the bare base Python. The layered venv now gets a .pth file pointing at the module's site-packages (checked on a compute node).
- conda activation scripts that read unset variables (hwloc, run #77) no longer abort the job under `set -u`.
- A smoke test whose AI fix was interrupted by a dashboard restart is failed with a clear message instead of staying "fixing" forever; a task caught mid-rename between warm-node folders is no longer reported lost.

## [0.35.1] - 2026-09-29

### Fixed

- Install verification ran all `install.verify` lines as one `eval`, so only the last line counted: `lmp -h | grep -q GRANULAR` failed and was ignored (run #75). Each line now runs on its own with pipefail, and the first failure fails the rung.
- A conda plan with a pinned compiled program (conda-forge `lammps=2023.08.02`, the build that runs on glibc 2.28) no longer gets `python=3.12` forced next to it (no solution existed; checked on a compute node), the relaxed rung keeps pins on compiled programs, and there is no pip rung when the checks call a binary pip can't provide (it "verified" Python imports, then `lmp: command not found`).

## [0.35.0] - 2026-09-29

Found by the first plans made with 0.34 (#75-#77): the planner used the new lessons (pinned conda-forge LAMMPS with a GRANULAR check, `inlet_00000.dat`, the local CUDA container), and the checks around it needed to catch up.

### Added

- **Weak reference checks** are a pre-flight warning: downloading a paper, abstract page or README and testing that it contains a word or short number (`grep -q "1.53" paper.html`, `'3.2' in content`). Every plan in the batch did this; it passes on unrelated pages (#77 fetched an LBM README to "verify" Schafer-Turek values) and fails when a page moves. The planning rule now says: parse tables from a data file; type a few scalar references with a citation.

### Fixed

- "Already installed as modules" no longer fires for a pinned conda package the plan checks a feature of (conda-forge `lammps=2023.08.02` for GRANULAR, which the module lacks), or for the gmsh Python API (the module has only the binary).

## [0.34.1] - 2026-09-29

### Fixed

- A research task Google stops advancing is cancelled and marked failed ("Stalled") after 4 reconnects in a row bring no new event (about 40 minutes), instead of reconnecting and replaying the same thoughts until the 3-hour limit (#287: 13 identical replays).
- Stuck detection in the dashboard ignores replayed lines: progress is a log line not seen before, not a growing file.

## [0.34.0] - 2026-09-29

Resilience release: every failure of the 15-run batch (runs #55-#74) traced to a cause and fixed at the source, so the same failure can't recur, instead of patching plans by hand.

### Added

- **Failure classes.** A failed smoke test or run is classified from its log: software setup, container image, program crash (assertion/segfault), binary too new for the nodes (glibc), missing feature in the installed build, numerical blow-up, time limit, out of memory, or script error. The class and a concrete diagnosis go into the AI fix prompt and the error shown to you. Setup, container, time and memory failures are not handed to the AI again after the same class repeats (the AI "found nothing to fix" 3 times in the batch because the cause was outside the script). The Lab view names the class for each smoke round.
- **Review gate on AI fixes.** Every AI fix (smoke loop, Fix with AI, fix of a failed run) is checked for: a fallback that returns the reference value (a failed fit would look like agreement, run #56), changed verdict tolerances or expected values, a variable swapped inside a formula (k3_v -> k3_u in an RK4 step, run #59), a removed library (run #64), a large rewrite, and new fixed-text results. Findings show as "Check before running" on the draft.
- **Stuck research.** A running session whose log hasn't grown for 45 minutes, or whose process is gone, shows "This research looks stuck" with Stop and re-run / Just stop, and a stuck marker in the session list (#287 sat "in progress" at Google for hours with no output).
- **Planner checks:** `features` (a program's full `-h` output: compiled-in packages and styles) and `conda` (versions on conda-forge/bioconda).
- **Lessons** (known problems + usage cards): SU2 unsteady inlet profiles read `<stem>_00000.dat`; site LAMMPS has no GRANULAR (use conda-forge lammps=2023.08.02); newer conda-forge binaries need glibc 2.29+ (nodes run 2.28); local container images and where nvcc lives; lattice-Boltzmann stability. Two new planning rules: never return a reference value as a fallback, and verify optional features of a program in the verify step.

### Fixed

- Container images already on the cluster (`/apps/containers/*.sif`) are used in place; they were passed to `apptainer pull`, which read the path as a registry name (run #61).

## [0.33.3] - 2026-09-29

### Fixed

- Install ladder: a plan that only loads a Python module (no pip) now uses the module as is first, instead of failing into venvs that lack its packages. Packages imported by `install.verify` commands (numba, networkx, ...) are installed in the isolated-venv and Pixi fallbacks. The verify list is part of the environment key, so a rung marked bad for one plan's checks is not skipped for another plan with different checks. Five of the first 12 batch runs failed on this (exit 4).

## [0.33.2] - 2026-09-29

### Changed

- The warm Lab node scales out with the backlog: one worker plus one per two queued tasks, up to `warm.max_workers` (default 3). A batch of 15 runs no longer queues behind a single node.

## [0.33.1] - 2026-09-29

### Added

- The planner's cluster description includes the catalog's new usage cards: verified facts per installed package that AI plans got wrong before (SU2 incompressible solver keys and output files, OpenFOAM environment, GROMACS rank counts, OR-Tools isolation, pseudopotentials for Quantum ESPRESSO). Needs ursa-catalog with `usage_cards` (ucr-slurm-production).

## [0.33.0] - 2026-09-29

Lab plan v3 (nexus `2026-09-29_Deep_Research_Lab_Plan_v3.md`), releases 2-5 in one version: warm node, smoke test, install ladder, planner probes, cluster matching.

### Added

- **Warm Lab node.** One long-lived Slurm job (`lab-warm`, default `computehigh`, 4 h limit, stops after 20 idle minutes) runs Lab work from a spool folder on the cluster, so a burst of runs shares one booted node instead of booting one per job. Short single-node CPU runs on its partition (time limit up to 2 h) run there directly as `warm:<task>` jobs; everything else still gets its own Slurm job. Status, start and stop: `GET /api/lab/warm`, `POST /api/lab/warm/start|stop`, and a line on the Lab runs page. Settings under `"warm"` in `lab_targets.json` (`false` disables it).
- **Smoke test before every CPU run.** Submit first runs the plan on the warm node with `LAB_SMOKE=1` (a cut-down run the planner is now asked to support) in `run_N/smoke/`, and checks the exit code and that every expected output exists. Pass: the real run starts. Fail: the AI fixes the plan from the smoke log and runs it again, up to 3 rounds. If the AI had to change the plan, the run comes back as a draft with the diff for review instead of starting by itself.
- **Install ladder.** Each install is verified (imports of the listed Python packages plus the plan's new `install.verify` commands) and falls back on its own: layered venv, then isolated venv, then conda-forge for Python on a module; Pixi, then relaxed Pixi with bioconda, then pip for conda plans; a module that won't load is replaced by its conda package; `install.spack` builds in a user Spack chained to `/apps/spack`; plans with a container carry on to the image. A failed rung is remembered for that package list; the rung used goes to `outputs/environment.json` and `envs/ladder.jsonl`, and recent fallbacks are shown to the planner.
- **Planner probes.** Before writing a plan, the AI picks up to 6 read-only checks (module details, `--help`/`--version` output, PyPI versions, Python signatures and docstrings, URL status), which run on the warm node; the answers go into the planning prompt. Every download URL of a new plan is also fetched from the cluster, and one that fails is a pre-flight warning.
- **Cluster matching.** Pre-flight suggests a partition that fits the job's shape (GPU, MPI, sweep, big memory) and flags time limits far above what similar past runs needed. A queued job whose nodes keep failing on a partition GCP can't fill moves once, automatically, to another machine family (same plan, noted on the run).

## [0.29.0] - 2026-09-29

### Added

- Lab plans now learn from earlier runs. Every plan and AI-fix prompt carries rules drawn from real failures (compute every reported number, download reference data, read tool output folders, write known-answer checks, support a smoke mode) plus software-specific lessons that match the plan: OR-Tools, TreeTime, vLLM, SU2, FreeRTOS on a host, SimPy scheduling models, hand-written CFD, pythermalcomfort, MLPerf Tiny URLs.
- When an AI fix of a failed run completes, the fix is saved as a lesson for future plans that use the same software (`<state_dir>/lab_pitfalls.json`; list, add and remove at `/api/lab/pitfalls`).
- Pre-flight flags results written into the script as fixed text and reference tables typed from memory. Both came up in the AI drafts of runs #49-#51, which would otherwise have completed with confident wrong answers.
- Known-answer checks: a job can write `outputs/verdict.json`. A failed check marks the run "Completed, known-answer check FAILED", the write-up leads with it, and the run view lists every check.

## [0.28.2] - 2026-09-29

### Fixed

- Lab runs that pip-install OR-Tools on top of the `python-sci` module crashed at the first CP-SAT solve (segmentation fault, exit 139; run #38). OR-Tools bundles its own abseil/protobuf, which clash with the module's libraries. Such plans now get an isolated venv on the module's Python, with numpy, pandas, matplotlib and scipy installed alongside. Verified on Ursa Major: OR-Tools 9.12, 9.14 and 9.15 all crash layered on the module and all solve isolated.

## [0.28.1] - 2026-09-29

### Fixed

- Lab submit with an expired Google sign-in said only "gcloud could not build the SSH command: to select an already authenticated account to use." (the last line of gcloud's advice). It now says the sign-in expired and to run `gcloud auth login`.
- A submit that never reached the cluster (expired sign-in, VPN or IAP tunnel down, ssh unable to connect) no longer marks the run failed. It stays a draft with a "Not submitted" note, so Submit works again once the connection is fixed; errors after the cluster was reached still fail the run.

## [0.28.0] - 2026-09-28

A full code and usability review (backend bug hunt with reproducing tests, frontend code
review, hands-on browser testing on a copy of real data). Every backend fix has a test
that failed before it.

### Security
- **The dashboard listens on 127.0.0.1 only by default.** A non-loopback `--host` needs
  `--allow-remote`; while loopback-only, requests from other machines get 403. A dashboard
  started on `0.0.0.0` by an older version restarts on `127.0.0.1`.
- Source previews never show hidden files (`.env`, `.ssh`) or follow links.
- Public bucket keys like `data//etc/x` or `../x` can no longer write outside the download
  folder.
- SVG and other active files from Lab jobs download (sandboxed) instead of rendering in
  the dashboard; a plan's partition value can no longer add lines to `run.sbatch`.

### Fixed
- Lab: a double-click or second tab no longer submits two Slurm jobs; a run stuck in
  "submitting" (dashboard stopped mid-submit) can be cancelled and is failed by the
  watcher; editing or AI-fixing a plan no longer overwrites a run submitted meanwhile; a
  running job that Slurm stops reporting is finished from its stage markers; cancelling
  during submit cancels the job it gets back; non-numeric resources no longer crash.
- Sources: S3/GCS include and exclude filters apply when fetching; relay staging re-lists
  the source and checks the real size before uploading, so a changed folder is uploaded
  again under its new content hash; folder fetches never stop silently at 5,000 files;
  report and notebook sources keep the same content hash across processes (their index is
  no longer rebuilt every run); `sources rm 12` finds the source named "12"; deleting a
  source keeps the provenance of reports that used it; a test running next to an index
  build no longer drops the index; binary previews and non-listing bucket replies no
  longer cause errors; the Add source form refuses `ftp:`, `mailto:` and folders outside
  the allowed roots; `cleanup` keeps temporary stores a running research may still use;
  catalog caches are written atomically; temp copies for research uploads are removed.
- Dashboard: Enter in a confirm dialog only confirms when the confirm button has focus
  (destructive dialogs start on Cancel), and a replaced dialog can never fire later;
  notebook saves in flight no longer drop newer edits; archive search no longer hides live
  runs from the counters; a report refreshes itself when its run finishes; highlights that
  span a citation marker survive a reload; many silent failures now show an error.

### Changed
- All dialogs close on Escape, keep keyboard focus inside and return it; closing the Lab
  plan review with unsaved edits asks first; Submit to the cluster takes a second click
  that shows the worst-case cost.
- Switching tabs keeps the New research form (files and data sources too), search answers,
  Ask drafts and the reading position.
- Failed and crashed reports show the error with Re-run instead of reading tools.
- Find in report: next/previous, "3 of 471", Escape closes. LaTeX in reports reads as plain
  Unicode (`X_r/h ≈ 6.26`, `y⁺`). A failed Lab run marks the step it failed at.
- New **Notes** page (every highlight and note across reports); sources can be edited
  (title, tags, filters, credentials, staging, level); File Search Store picker in the
  launcher; discovery shows plain format names and filters by catalog.
- Keyboard and screen reader support (focusable rows and tabs, labels, focus ring),
  higher contrast for small text, and phone layout fixes. "Intel" is now "Details";
  report links are "Citations"; the Failed filter and counter agree.

### Documentation

- `docs/SPEC.md` brought fully up to date (module map, tables, CLI, client, settings, controls, errors, test suite, operations, Lab and data sources); README features, settings, data locations and screenshots refreshed; `ARCHITECTURE.md` and `docs/DASHBOARD_DESIGN.md` updated for Lab runs, data sources and localhost.

## [0.27.0] - 2026-09-28

### Added
- **Free public cloud data.** A new "public bucket" source reads AWS Open Data (`s3://`) and
  Google Cloud public datasets (`gs://`) anonymously: no account, no credentials, nothing
  billed. Requester-pays and private buckets are refused with a clear message. Lab jobs
  download them on the node with plain `curl`.
- **More catalogs in Find open datasets:** AWS Open Data (free buckets only), Google Cloud
  public datasets, and Google Earth Engine (links to the catalog page; Earth Engine data is
  used inside Earth Engine). Kaggle is not included.

### Changed
- An `s3://` or `gs://` source without credentials is now a public bucket. Add
  `--auth rclone:<remote>` (S3/Ceph) or `--auth gcloud` (GCS) for buckets that need a login.
- Discovery ranks title and tag matches above matches deep in a description, and shows
  licence text without Markdown link syntax.

## [0.26.0] - 2026-09-28

### Added
- **Find open datasets.** `deep-research sources discover QUERY` and a "Find open
  datasets" panel on the Sources page search Data.gov, Zenodo and Hugging Face at once and
  show license, publisher and direct file links; "add as source" turns a file into a data
  source. Nothing is downloaded while searching.
- **Saved search indexes per source.** `deep-research sources index NAME` (or "build" on the
  source page) uploads a source once into a named File Search Store; research runs reuse it
  instead of uploading the files every time, and rebuild it when the source changes.
- **Provenance fingerprint.** Every report and Lab run shows an "inputs" fingerprint of what
  it was built from (prompt, uploads, script, data sources with their content hash); `show`
  prints it and exports include it.
- **Plan review in sections, with the AI fix diff.** Review reads as 1 What and why, 2
  Software and data, 3 Settings, 4 Result check, 5 Script, with a jump bar. After Fix with
  AI, "What changed" shows the actual edit (script diff and changed settings such as
  "partition: spot -> standard"), not only the AI's summary.

### Fixed
- Foreground `research --source` runs now record which sources the report used.

## [0.25.1] - 2026-09-28

### Fixed
- **Lab runs page loads a summary, not every run in full.** `GET /api/lab/runs` returned
  each run's selection, write-up and plan (1.4 MB for 26 runs); it now returns only what
  the list shows.

## [0.25.0] - 2026-09-28

### Added
- **Lab runs page.** Every Lab run in one list, newest first, with status, the report it
  belongs to, partition and job id, worst-case cost and last update, and a status filter.
  Click a row to open its report at that run. `GET /api/lab/runs`. On phones, Lab runs and
  Data sources move from the top bar to the bottom of the archive panel and the command
  palette.

### Fixed
- **Queued runs no longer hide node failures.** A spot job that Slurm keeps requeueing after
  node failures (run #29: 15 NODE_FAIL attempts, shown as "Queued, waiting for a node")
  now reads "Requeued after N node failures on spot: the cluster could not start a node",
  and after 3 failures suggests cancelling and resubmitting on another partition.

### Changed
- **Smaller selection toolbar.** One Highlight button (amber) with the other colours behind
  a small arrow, and plainer labels.

## [0.24.0] - 2026-09-28

### Added
- **Data sources in research runs.** `research`/`start --source NAME` (repeatable) and a
  Data sources picker in the launcher. Readable files are fetched on this machine and
  searched like `--upload` (up to 200 files and 200 MB per source).
- **Data sources in follow-up questions.** `followup --source NAME` and an "Include data"
  picker above the Ask box. The source text goes to the model with the question; the report
  records only your question and which sources were used.

### Changed
- **`cleanup` keeps named stores.** It now deletes only temporary upload stores (named
  `deep-research-temp-*`) and unnamed leftovers from older versions; data source indexes and
  stores you named are kept and listed. `cleanup --all` deletes every store, as before.
  Upload stores are now created with a `deep-research-temp-<time>` display name.

## [0.23.0] - 2026-09-28

### Added
- **Data sources.** A shared registry of places data lives: web/open datasets, GCS buckets,
  S3 buckets including CephRDS (through rclone remotes), folders and files in your home
  directory, and your own reports and notebooks. The registry stores references and
  credential references, never data or secrets. `deep-research sources
  add|list|show|test|browse|preview|rm`; `/api/sources` endpoints; a Sources page in the
  dashboard with a folder browser and file preview.
- **Data sources in Lab runs.** Pick sources in the New lab run dialog or in plan review.
  Sources the cluster cannot reach (CephRDS behind the campus VPN, local files) are relayed:
  fetched on this machine and uploaded to a cached, read-only copy on the cluster. Web and
  GCS sources are downloaded on the node in a "Staging data" stage. The job reads
  `$DS_<NAME>`; `sources.json` records exactly which data (manifest hash) a run used. The
  planner is told about the chosen sources, and pre-flight warns about unknown or failing
  sources, oversized relays, and scripts that never read their data.

### Fixed
- **Lab submit never overwrites an existing run folder.** A reused run id (fresh database,
  restore) used to write into the old `run_N` on the cluster and replace its logs; the old
  folder is now renamed `run_N.prev-<timestamp>` first.

## [0.22.1] - 2026-09-28

### Fixed
- **pip packages on top of python-sci / python-ml no longer hide the module's packages**
  (#113). The harness used to build a separate Pixi environment with its own Python for
  any pip packages, so a plan that loaded `python-sci` and pip-installed one extra package
  could not import the module's numpy/pandas/matplotlib (Lab run #30:
  `ModuleNotFoundError: No module named 'matplotlib'`, after the AI fix correctly dropped
  matplotlib as "provided by python-sci"). With exactly one Python environment module and
  pip packages (no conda), the harness now builds a cached venv on the module's own Python
  with `--system-site-packages` and pip-installs only the extras. Conda plans still use
  Pixi.
- Pre-flight warns when the run script builds its own venv (`uv venv`, `python -m venv`,
  `virtualenv`), which hides packages the harness installed the same way. The fix prompt
  says to list extras under `install.pip` instead.

## [0.22.0] - 2026-09-27

### Added
- **Fix with AI on failed runs.** A failed run gets a Fix with AI button. The planner reads
  the job log (its end, plus the first traceback or ERROR block when that sits earlier)
  and the plan, and changes only what the error shows is wrong. The result is a NEW draft
  (`rerun_of` = the failed run) that goes through pre-flight; nothing is submitted and the
  failed run is left as it was. `POST /api/lab/<id>/fix-failed`. Guards: every change must
  be listed (one retry if the model changes the plan silently, then refuse); a fix that
  removes command-line options is marked "REVIEW" because that is how an error gets
  hidden instead of fixed. Replayed on the real failed runs #6, #23, #25 and #26 (about
  $0.05 each): correct targeted fixes for #26 (dedupe strain names) and #23 (skyline file
  format); #6 upgrades vLLM to a version that has the flag, with a note on the GPU driver;
  #25 treats the symptom (drops --covariation), which the REVIEW note now flags.
- **Inputs are checked before the heavy step.** The planner adds a few lines under
  `stage "Checking inputs"` right after inputs are fetched or generated: record count,
  range coverage and unique IDs for downloaded data, non-empty input files for generated
  inputs, a responding server for benchmarks. A bad input stops the job in seconds with a
  one-line reason (exit 3) instead of crashing minutes later in another tool. No extra job
  or node: it runs inside the real job.

### Fixed
- Write-ups take input counts from the input or log lines that state them, not from derived
  structures (run #27 reported 183 "sequences": tips plus internal nodes of a 100-tip tree).
- Model replies with a second JSON object or trailing notes after the plan parse (the
  first complete object is used) instead of failing with "Extra data".

## [0.21.1] - 2026-09-27

### Fixed
- Pre-flight now checks the run script itself, not only modules and resources. Run #16
  failed 35 s into the job: the planner had emitted a garbled `<unk>` token in place of `{`
  inside a Python f-string. `check_script` flags model artifact tokens (`<unk>`, `<pad>`,
  U+FFFD, ...), runs `bash -n` on the script, and compiles (never runs) every Python
  heredoc (`python3 - << 'EOF'`, `cat > x.py << 'EOF'`), reporting the script line number.
  Checked against all 19 stored plans: flags only run #16. Fix with AI may now repair the
  reported lines, and nothing else in the script.

## [0.21.0] - 2026-09-27

### Added
- **Fix with AI** button on the Lab review dialog, next to the cluster-check warnings. It
  sends the plan and the exact warnings back to the planner with the current cluster
  catalog and asks it to fix only what is flagged (modules, installs, resources and the
  software-setup lines of the script), keeping the science, parameters, inputs and outputs.
  The server re-runs the check; if warnings remain it tries once more (2 model calls at
  most), then shows what changed and what is left. It never submits. The previous plan is
  kept, and **Undo fix** restores it. API: `POST /api/lab/<id>/fix`, `POST /api/lab/<id>/undo-fix`.
- Pre-flight warns when a plan installs packages the loaded Python environment module
  already provides (for example pip `skyfield numpy` with `python-sci`).
- Plans record the catalog they were made against (`catalog_generated`); a plan made before
  the cluster catalog changed gets a warning to re-check its software choices.

## [0.20.2] - 2026-09-27

### Fixed
- Lab pre-flight now catches plans that name real modules which cannot work together or
  cannot run. Run #21 loaded `python/3.12` + `py-numpy`/`py-scipy`/`py-pandas`: every name
  existed, but the py-* modules were built for a different Python and would have died with
  "No module named numpy". New warnings: a module the cluster's smoke test marked broken
  (catalog `module_health.broken`), a bare `python` or `py-*` module, two Python
  environment modules at once, and a Python environment module mixed with a conda env.
- The planner is told which modules are broken (describe_full) and, through the catalog
  rules, to use exactly one Python environment (python-sci for CPU science, python-ml for
  PyTorch/GPU) or conda.

## [0.20.1] - 2026-09-27

### Fixed
- A lab plan could fail with "Invalid \escape" and be lost (run #21): the model left a
  regex or LaTeX backslash unescaped inside the job script it returns as a JSON string.
  Plans are now parsed leniently: raw newlines and tabs are accepted, and a backslash
  that does not start a valid JSON escape is kept as a literal backslash, while correct
  escapes are left alone. The plan prompt also asks for valid escaping.

## [0.20.0] - 2026-09-27

Lab runs now know what the cluster really has.

### Added
- Cluster catalog. A target with `catalog_path` reads the cluster's published catalog
  (Ursa Major: `/apps/docs/catalog.json`, generated by `ursa-catalog`): partitions, all
  installed modules, tested recipes, install tools, prebuilt containers and site rules.
  Cached locally, refreshed when older than a day or from the Lab panel's
  "cluster info ... refresh" link.
- Suggestions and plans are written from the catalog: suggestions see every installed
  package and what the cluster can do (multi-node MPI, GPUs, large memory, spot); plans
  see exact module names and versions, tested load lines and container paths, and are
  told to use installed software first.
- Pre-flight check. Every plan is checked against the catalog when it is made, edited or
  rerun: unknown partitions or modules (with the versions that exist), missing MPI loads,
  GPU or node counts the partition cannot give, packages that are already installed, and
  unknown container paths. The review dialog lists the problems before you submit.
- The job script sources the cluster's site job header when the catalog publishes one
  (TMPDIR, SCRATCH, caches, the pip-in-conda libstdc++ fix), so the cluster owns those
  settings.
- `resources.ntasks_per_node` for MPI jobs; spot partitions add `--requeue`.
- `GET /api/lab/catalog`, `POST /api/lab/catalog/refresh`.

### Changed
- Partition prices and sizes come from the catalog when it is loaded (the hand-written
  table is the fallback), so the cost estimate matches the cluster's `ursa-cost`.

### Verified live
- Catalog fetched from Ursa Major: 197 modules, 1,694 Spack packages, 17 recipes, GPU
  driver 580.178.04 / CUDA 13.0, 6 partitions including spot.
- A 2-node, 8-rank LAMMPS plan (`openmpi`, `lammps/20250722.4`, computehigh) passed the
  pre-flight check and ran as Slurm job 166 across `c3nodeset-[0-1]`, with the private
  per-job TMPDIR.
- A plan loading `gromacs/2026.1-cuda` without `openmpi` and conda-installing `lammps` got
  both warnings.

## [0.19.7] - 2026-09-26

Fixes from a bug-finding review of the Lab and the research agent.

### Fixed
- Cancelling a lab run could be undone. A cancel while the plan was being written came
  back as a draft; a cancel while the job was running could be overwritten by the
  watcher, which then paid for a write-up and marked the run completed. Background
  writes now apply only while the run is still in the state they expect.
- Cancelling a run after its Slurm job had ended (fetching or writing up) returned an
  error or was overwritten. It now cancels cleanly and skips the write-up.
- Deleting a report deleted its lab runs even while their Slurm jobs were queued or
  running, leaving jobs on the cluster with nothing tracking them. It now refuses until
  they are cancelled, and the dashboard shows why.
- Cancelling a multi-level (recursive) session cancelled only the root task at Google;
  every running child task kept running and billing. All running children are now
  cancelled too.
- Two different package lists could share one cached software environment (the folder
  name was cut at 60 characters), so a job could run without packages it asked for.
  The name now ends in a hash of the packages, channels and pip flags, and the build
  runs under a lock so two jobs never build the same environment at once.
- The compute estimate misread Slurm time limits: `30:00` (30 minutes) was costed as
  30 hours and `1-12` (36 hours) as about 24.
- A run card could freeze (and never send its completion notice) when a slow request
  finished after the Lab panel had been redrawn. Stale replies are now ignored.
- A running card redrew every few seconds as the elapsed time changed, closing an open
  "Selected passage" and reloading an open log from the start. Now only the clock
  line updates.
- A results fetch that kept failing was retried forever and blocked every other run.
  After 5 failed attempts the run is marked failed (the outputs stay on the cluster).
- A streamed research task that ended failed, incomplete or over budget with partial
  text was saved as completed; one that completed with no text stayed "running" and
  later showed as crashed. Both are now recorded as failed, with any partial text kept.
- `DR_TASK_TIMEOUT_MIN` was only checked when a stream reconnected, never while one
  stayed connected.

### Changed
- Lab AI cost now includes Google Search queries ($14 per 1,000, a worst case since the
  free monthly quota is shared), bills cached input at the cached rate, and is reported
  even when a suggestions reply cannot be parsed or a write-up comes back empty. Briefs
  now count thinking tokens. The planning label reads ~$0.10-0.30.
- `GEMINI_FOLLOWUP_MODEL` now really overrides the model for Lab suggestions, plans and
  write-ups and for dashboard briefs, comparisons and audio scripts, as 0.19.1 said.

## [0.19.6] - 2026-09-26

### Fixed
- The CLI failed with "API key not valid" when run from a folder whose own `.env` had
  an old `GEMINI_API_KEY` (common in other projects). It now reads the saved settings
  (`~/.config/deepresearch/.env`, written by `auth login`) before `./.env`, the same
  order the dashboard uses. A variable exported in the shell still wins, and `./.env`
  still fills anything the saved settings do not set.

## [0.19.5] - 2026-09-26

### Fixed
- Two lab plans started at the same moment could fail with "Cannot send a request, as
  the client has been closed." Each thread built its own Gemini client and the losing
  one was garbage-collected mid-request. The Lab and the dashboard features now share
  one client created under a lock.
- Cancelling a running session from the dashboard never cancelled it at Google: the
  one-line temporary client was closed before the request went out, and the note said
  "cloud cancel failed". It now uses a named client.

## [0.19.4] - 2026-09-26

### Added
- "Retry plan" on a lab run whose planning failed. It plans the same input again under
  the same run number (`POST /api/lab/{id}/replan`), so a failed card no longer has to
  be deleted and started over. Not shown when the model judged the request not
  computable.

## [0.19.3] - 2026-09-26

### Fixed
- Lab planning could fail with "no JSON in model reply". With Google Search on,
  `gemini-3.8-flash` sometimes stops with `TOO_MANY_TOOL_CALLS` after thinking and
  returns no text (reproduced 3 of 3 times on run #14's input). The plan prompt now caps
  searches at 5 (3 of 3 plans then succeeded). If a search reply is still empty, the
  plan is written once more without search and its caveats say so. An empty model
  reply now shows its finish reason instead of "no JSON in model reply".

## [0.19.2] - 2026-09-26

### Fixed
- The Lab "New lab run" dialog still said "Gemini 3.1 Pro" and "~$0.10-0.20"; it now
  says Gemini 3.8 Flash and ~$0.10. The suggestions hint now says about a cent (was
  about 5 cents).

### Docs
- `docs/SPEC.md` brought up to v0.19.2: header version, Lab model calls listed in 13.1,
  section 17 intro, and Flash cost figures for Lab runs (20.5).

## [0.19.1] - 2026-09-26

### Changed
- Every general model call now uses `gemini-3.8-flash` by default: follow-ups, gap
  analysis, synthesis, search answers, and Lab suggestions, plans and write-ups.
  (Before: `gemini-3.1-pro-preview`.) The Deep Research agent itself, embeddings and
  text-to-speech are unchanged. `GEMINI_FOLLOWUP_MODEL` still overrides it.
- Lab AI cost is now computed at Flash rates ($0.75 in / $3.75 out per 1M tokens).

## [0.19.0] - 2026-09-26

### Added
- Lab runs: run a real computation on an HPC cluster to test something a report says,
  and keep the results with the report.
  - Every report has a Lab runs section. "Suggest computations" proposes up to three
    computations the report makes possible (Gemini with Google Search, a few cents).
  - Start a run from a suggestion, from highlighted text ("Lab run" on the selection
    bar) or from the whole report. Gemini researches the method and software and writes
    a job plan and Slurm script.
  - Nothing runs until you review it: the dialog shows the plan, the full script, the
    resources and the estimated cluster cost; parameters and resources are editable.
  - The job installs its own software (module, cached Pixi environment, pip, or an
    Apptainer image), runs, and is watched in the background with live stage and log.
  - Outputs are fetched to the laptop and shown on the report (images inline, files as
    links) with an AI note: result, key numbers, what it means, limits, next run.
  - Rerun copies a plan into a new draft so you can change parameters.
  - Targets live in `lab_targets.json` in the config folder (not in the repository).
    The first target type is Slurm over SSH through `gcloud` IAP with a persistent
    connection.
- `docs/SPEC.md` section 20 describes the design.

### Tested
- Real runs on Ursa Major: a Qiskit CHSH Bell test (S = 2.84 against the 2.83 bound,
  CPU node, 1 min 48 s) and vLLM on an L4 GPU. Failures found on the way are fixed in
  the harness: pip without pip in the environment, libstdc++ too old for pip wheels,
  and missing Apptainer on compute nodes.

## [0.18.0] - 2026-09-26

### Fixed
- Security: any web page open in the same browser could cancel a running research run
  by sending an empty form POST to the dashboard. Every write request must now be JSON
  (with or without a body), writes from another origin are refused, and requests whose
  Host is a public domain name are refused (DNS rebinding). IP addresses, machine names,
  `.local`/`.lan`/`.home.arpa` and tailnet `.ts.net` names keep working; add other
  names with `DR_ALLOWED_HOSTS`.
- Recursive runs dropped every child report that took longer than 10 minutes from the
  final synthesis, although the run still waited for (and paid for) those children.
  Nearly all real child runs take longer than that. Every child report is now used.
- A research task that Google ended as `cancelled`, `incomplete` or `budget_exceeded`
  made the worker poll forever; a lost stream reconnected forever. Both now stop.
- One failed status check no longer fails a long polled run (30 in a row do).
- Gap analysis could spawn more child tasks than `--breadth`.

### Added
- `DR_TASK_TIMEOUT_MIN` (default 180, 0 = no limit): safety limit per research task.
  A task still running after that is cancelled at Google, so it stops billing, and is
  marked failed with a clear message. Runs are never cut short before the limit.
- `docs/SPEC.md`: complete system specification (requirements with test mapping, architecture,
  data model, CLI, HTTP API, cost model, security model, known gaps).

### Changed
- `DeepResearchConfig.recursion_timeout` is replaced by `task_timeout_min`.
- Dependencies: pydantic 2.13.5, python-dotenv 1.2.3, tenacity 9.1.4, pytest 9.1.1, pytest-cov 7.1.0,
  plus security patches for urllib3, idna, pyasn1 and anyio. GitHub Actions: checkout v7,
  upload-artifact v7. Dependabot groups minor/patch updates; ruff and mypy upgrades are taken
  deliberately because they change lint and type rules.

## [0.17.5] - 2026-09-26

### Fixed
- Recursive runs (`--depth 2+`) started from the dashboard or `deep-research start`
  wrote the report into a new row and left the pre-created row empty; after the
  run the empty row showed as "crashed" and the real report looked like a second
  run. The root node now adopts the pre-created row.

## [0.17.4] - 2026-09-26

### Fixed
- Follow-up questions failed with "API key not valid" when the dashboard had
  been started from a folder containing an old `.env`: the CLI had already
  loaded that key before the child process started, so changing the child's
  folder was not enough. Background dashboard processes now build their
  environment with the user settings file taking precedence over a folder
  `.env` (keys exported in the real shell still win).

## [0.17.3] - 2026-09-26

### Fixed
- The dashboard and the research runs it starts no longer run in the folder
  they were launched from. A stale `.env` in that folder (loaded before the
  user settings file) silently replaced the saved API key, so runs failed with
  "API key not valid".
- A run that fails before Google creates an interaction (bad key, quota,
  network) is now marked failed instead of sitting at "running".

### Added
- `/api/health?check=1` verifies the key with Google (cached 10 minutes). The
  header chip shows INVALID and launching is blocked when Google rejects it.

## [0.17.2] - 2026-09-26

### Changed
- Professional repository setup: rewritten README with screenshots, SECURITY.md, SUPPORT.md,
  CHANGELOG.md, form-based issue templates, pull request template, CODEOWNERS, pre-commit config,
  EditorConfig and release workflow.
- CI now also checks dashboard JavaScript syntax, runs tests on Python 3.13, reports coverage and
  verifies that the built wheel ships the dashboard assets. Actions updated to checkout v6 and
  setup-uv v7; Dependabot now tracks `uv.lock`.
- Package metadata: license, authors, keywords, classifiers and project URLs; `pytest` moved from
  runtime to development dependencies.
- The dashboard's cost field shows a short message instead of a raw API error when usage cannot be
  fetched.

### Removed
- Obsolete planning documents (`design_doc.md`, `requirements.md`, `todo_v1.0.0.md`,
  `release_notes.md`); their content is covered by the changelog, roadmap and architecture docs.

## [0.17.1] - 2026-09-26

### Fixed
- Notebook Read and Split views drew the editor text one letter per line over the preview.
- Notebook preview shows citation chips and folds long source lists; Split stacks vertically on
  phones.

## [0.17.0] - 2026-09-26

### Added
- Citation cards: `[cite: n]` markers become chips; hover or tap shows the claim and its numbered
  source. Paragraphs with figures but no citation are flagged.
- Read aloud: word-for-word browser speech with paragraph highlighting, speed, voice, skip and pause.
- Audio export: MP3 in one of eight Gemini voices, either the full report or an AI-written 2-3 minute
  spoken summary, with the cost shown before creation. Audio is cached and listed per report.
- Brief builder: report or notebook to executive brief, slide outline or email, saved as a notebook.
- Research map of all indexed reports by embedding similarity.
- Re-run and compare: linked re-runs, sources added and dropped, new paragraphs highlighted, optional
  AI summary of what changed.
- Live timeline replacing the raw log view: agent lanes, timestamped thoughts, errors, elapsed time,
  actual versus estimated cost, and browser notifications when runs finish.
- Actual run cost from Google's interaction usage record.

### Changed
- Cost estimates recalibrated to Google's published per-run figures; the previous model was about
  10x too low. The CLI `estimate` command uses the same model.

### Fixed
- Building the search index no longer changes a session's `updated_at` time.

## [0.16.3] - 2026-09-26

### Fixed
- Dashboard usable on phones and tablets: archive and inspector are slide-out drawers, and
  horizontal overflow at tablet widths is gone.

## [0.16.2] - 2026-09-26

### Fixed
- Sessions left "running" by dead processes are now marked crashed: top-level runs record their
  PID, and rows with no PID are judged by activity.

## [0.16.1] - 2026-09-26

### Fixed
- Dashboard child processes run with `python -I` so a stray local module cannot shadow the package.

## [0.16.0] - 2026-09-26

### Added
- Web dashboard (`deep-research dashboard --start/--stop/--restart/--status`): launch research with a
  cost estimate, live logs, typeset reader, annotations, Markdown notebooks, semantic search, export,
  stars, tags and a command palette. Standard-library server, no new Python dependencies.

## [0.15.1] - 2026-09-26

### Fixed
- Expanded and corrected `--help` text; `start` now forwards `--stores`.

## [0.15.0] - 2026-09-25

### Changed
- Updated for google-genai 2.x (Interactions API steps schema) and the
  `deep-research-preview-04-2026` agent.

## [0.14.1] - 2026-02-19

### Added
- Exponential backoff for API calls and retrying SQLite operations; optional debug tracebacks.

## [0.14.0] - 2026-02-19

### Changed
- `search` is now semantic vector search over the local history, with a synthesized, cited answer.

## [0.13.5] - 2026-02-19

### Added
- `search` alias for `research`.

## [0.13.4] - 2026-02-19

### Changed
- The single-file tool was split into a `deepresearch` package with a matching test suite.

## [0.13.0] - 2025-12-13

### Added
- A bare prompt runs `research`; quiet mode (`-q`) for piping.

## [0.1.0] - 2025-12-12

### Added
- First release: CLI for the Gemini Deep Research agent with streaming, file upload, session history,
  follow-ups, headless runs and export. Versions 0.1.0 to 0.12.0 were published the same week while
  the CLI took shape; see the git tags for details.

[0.17.2]: https://github.com/charles-forsyth/deep-research/compare/v0.14.1...v0.17.2
[0.17.1]: https://github.com/charles-forsyth/deep-research/pull/75
[0.17.0]: https://github.com/charles-forsyth/deep-research/pull/74
[0.16.3]: https://github.com/charles-forsyth/deep-research/pull/72
[0.16.2]: https://github.com/charles-forsyth/deep-research/pull/71
[0.16.1]: https://github.com/charles-forsyth/deep-research/pull/70
[0.16.0]: https://github.com/charles-forsyth/deep-research/pull/69
[0.15.1]: https://github.com/charles-forsyth/deep-research/pull/68
[0.15.0]: https://github.com/charles-forsyth/deep-research/pull/67
[0.14.1]: https://github.com/charles-forsyth/deep-research/releases/tag/v0.14.1
[0.14.0]: https://github.com/charles-forsyth/deep-research/releases/tag/v0.14.0
[0.13.5]: https://github.com/charles-forsyth/deep-research/releases/tag/v0.13.5
[0.13.4]: https://github.com/charles-forsyth/deep-research/releases/tag/v0.13.4
[0.13.0]: https://github.com/charles-forsyth/deep-research/releases/tag/v0.13.0
[0.1.0]: https://github.com/charles-forsyth/deep-research/releases/tag/v0.1.0

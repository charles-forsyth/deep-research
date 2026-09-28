# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Versions before 1.0 may change behaviour between minor
releases.

## [Unreleased]

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

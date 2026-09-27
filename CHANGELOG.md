# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Versions before 1.0 may change behaviour between minor
releases.

## [Unreleased]

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

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Versions before 1.0 may change behaviour between minor
releases.

## [Unreleased]

### Added
- `docs/SPEC.md`: complete system specification (requirements with test mapping, architecture,
  data model, CLI, HTTP API, cost model, security model, 19 known gaps).

### Changed
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

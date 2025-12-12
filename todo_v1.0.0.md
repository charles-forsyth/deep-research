# Roadmap to v1.0.0 (Audit Response)

Based on the automated code audit, the following steps are required to reach a stable v1.0.0 release.

## 🛡️ Phase 1: Stability & Modernization (v0.8.0)
- [ ] **SQLite WAL Mode:** Enable `PRAGMA journal_mode=WAL;` to allow simultaneous read/write (e.g., viewing list while research runs).
- [ ] **Modern Python Syntax:** Replace `List[str]`, `Optional[str]` with `list[str]`, `str | None` (Python 3.12+).
- [ ] **Pydantic Best Practices:** Refactor `DeepResearchConfig` to use `@field_validator` instead of custom `__init__` logic.
- [ ] **Process Robustness:** Store PIDs of detached processes in the database. Implement `status` check to verify if PID is actually alive (handle zombies/crashes).
- [ ] **Garbage Collection:** Implement `deep-research gc` to scan for and delete orphaned Cloud File Stores.

## 🏗️ Phase 2: Architecture Refactor (v0.9.0)
- [ ] **Src Layout:** Move code to `src/deep_research/` structure to fix import issues and improve packaging.
- [ ] **Modularization:** Split the monolithic `deep_research.py` into:
    - `src/deep_research/config.py`
    - `src/deep_research/db.py` (SessionManager)
    - `src/deep_research/agent.py` (DeepResearchAgent)
    - `src/deep_research/cli.py` (Main entry point)
    - `src/deep_research/utils.py` (FileManager, DataExporter)

## 🚀 Phase 3: Product Polish (v1.0.0)
- [ ] **Interactive Auth:** Implement `deep-research auth login` to prompt for API key and save it securely to `~/.config/...`.
- [ ] **Docker Support:** Add a `Dockerfile` for containerized execution (server/cloud deployment).
- [ ] **Interactive TUI:** (Revisit) Re-attempt the Textual interface once the modular architecture is stable.

## 📦 Deployment
- [ ] **PyPI Publishing:** Prepare to publish to PyPI for `pipx install deep-research` support.

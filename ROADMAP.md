# Roadmap

Where the project is heading. Items are ideas, not commitments; open an issue to discuss or pick one up.

## Next up
- [ ] **Optional dashboard login** (shared token) for use beyond a trusted network.
- [ ] **Deep Research Max** toggle in the dashboard launcher.
- [ ] **Scheduled re-runs** of saved questions, with the compare view as a change digest.
- [ ] **Collaborative planning** (review the agent's research plan before it runs).

## Longer term: academic research edition

## 🏆 Phase 1: Foundation & Integrity (The Trust Architecture)
*   [ ] **Citation Integrity Engine (CIE):** Real-time validation of every generated citation against the Crossref API.
    *   *Traffic Light System:* Green (Verified DOI), Yellow (Semantic Mismatch), Red (Retracted/Unknown).
*   [ ] **BibTeX Linter:** Automated sanitation of `.bib` output to ensure compatibility with LaTeX/Overleaf.
*   [ ] **Zotero Integration:** Use `pyzotero` to index the user's library. Ground all answers in the user's existing PDF collection (Local RAG).

## 🔗 Phase 2: Workflow Symbiosis (Integration Layer)
*   [ ] **Overleaf Git Bridge:** Treat the agent as a collaborator. Push generated LaTeX sections directly to an Overleaf project via Git.
*   [ ] **Jupyter Co-Scientist:** Implement `%%deep_research` magic commands for IPython. Generate reproducible plotting code that is aware of the current dataframe schema.
*   [ ] **Asset Injection:** Programmatically upload generated figures/tables to manuscript repositories.

## 🧠 Phase 3: Knowledge Synthesis (Second Brain)
*   [ ] **Obsidian Export:** Generate "Atomic Notes" with `[[WikiLinks]]` based on semantic similarity to the user's existing vault.
*   [ ] **RO-Crate Packaging:** Standardize exports using Research Object Crate (JSON-LD) for FAIR data compliance.
*   [ ] **Benchling Adapter:** Push experimental protocols directly to Electronic Lab Notebooks (ELN).

## 🤖 Phase 4: Collaborative Intelligence (Multi-Agent)
*   [ ] **The Swarm:** 
    *   **The Librarian:** Finds sources.
    *   **The Reviewer:** Critiques logic/citations.
    *   **The Writer:** Drafts text.
*   [ ] **Collaborative Spaces:** Shared state for human teams to fork and branch research paths.

---

## ✅ Completed (Core Engine)
- [x] **Recursive Deep Research:** Autonomous gap analysis and parallel child task execution (`--depth`, `--breadth`).
- [x] **Smart Context Ingestion:** Auto-upload local files/folders (`--upload`).
- [x] **Headless Mode:** Fire-and-forget background execution with robust PID tracking.
- [x] **Session Management:** SQLite history with WAL mode concurrency and `tree` visualization.
- [x] **Garbage Collection:** Auto-cleanup of cloud resources (`cleanup`).
- [x] **Web Dashboard (v0.16.0):** `deep-research dashboard --start/--stop/--restart`, a dark browser workstation on port 7420 with launch, live logs, annotation, notebooks, semantic search and export.
- [x] **Dashboard v0.17:** citation cards, research map, re-run and compare, live timeline with actual cost and notifications, brief builder, read-aloud and AI-voice audio export (full text or summary).

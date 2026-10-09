# Roadmap

Where the project is heading. Items are ideas, not commitments; open an issue to discuss or pick one up.

## Next up
The full open list (decisions, demo items, gaps, prospects) is section 24 of docs/SPEC.md.

- [x] Workspace switcher in the dashboard top bar (0.41).
- [x] Copy projects and reports into a workspace (0.42).
- [x] Export and import a workspace as a zip (0.43).
- [x] Lab self-repair hardening (0.44).
- [x] Demo workspace: seven disciplines, each with a report, data sources and finished Lab results (2026-09-30).
- [x] Always-on warm node and computehigh default (0.48).
- [x] Referee -> fixer rounds before a draft is shown; planning rules on checks (0.50).
- [x] Fetch on this laptop when a site blocks the cluster (0.50).
- [x] Docs in sync with the code, with a spec guard test (0.50.1, 0.50.2).
- [x] Honest cancel, CLI depth limits, full delete, safe `auth login` (0.50.3).
- [x] Best refine round kept; refusals at run time offered as a fix; Lab finish notifications (0.51).
- [x] Cluster layer out of `lab.py` into `dashboard/cluster.py`, with its own tests; step 1 of the hpc-agent plan (0.52).
- [x] Agent-friendly CLI: `status`, `list --status`, `search --no-answer` (0.62) and `deep-research lab` (0.63).
- [ ] **Google Doc export** (`export ID --gdoc`, Share menu) through the Google Docs API; builder proven outside the repo (SPEC F14).
- [ ] **Follow-up chaining**: say that follow-ups see only the original report, or chain them (SPEC K24, G10).
- [x] ~~Read-only hpc-agent MCP server~~: became ursa-bifrost.
- [ ] **Live collaboration** on a shared workspace (sync), building on the zip export.
- [ ] **Browse from the CLI**: `deep-research sources browse-place drive:gd` to pick Drive files without the dashboard.
- [ ] **AWS S3 with credentials** in the file browser (needs an AWS profile or rclone S3 remote on this machine).
- [x] Projects claims board (0.45).
- [x] Lab adversarial review (referee) before submit (0.46).
- [x] `deep-research projects` CLI (0.47).
- [ ] **Optional dashboard login** (shared token) for use beyond a trusted network.
- [x] Deep Research Max toggle in the dashboard launcher (0.61).
- [ ] **Scheduled re-runs** of saved questions, with the compare view as a change digest.
- [x] Collaborative planning, "Plan first" (0.61).

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
- [x] **Projects (v0.37.0):** projects as the home screen with home-project defaults, project AI summary, Ask this project, briefs, voice overview, Inbox sorting, and dossier / citations / research package (Obsidian + RO-Crate) exports.
- [x] **Dashboard v0.17:** citation cards, research map, re-run and compare, live timeline with actual cost and notifications, brief builder, read-aloud and AI-voice audio export (full text or summary).

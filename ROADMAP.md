# 🗺️ Product Roadmap

Future plans for the Gemini Deep Research CLI.

## ✅ Completed
- [x] **Smart Context Ingestion:** Auto-upload local files/folders to temporary Cloud Stores for analysis (`--upload`).
- [x] **Structured Data Export:** Save reports as JSON or CSV with schema enforcement (`--output`).
- [x] **Session Management:** Local SQLite database to save research history, list past tasks, and resume sessions days later.
- [x] **Headless Mode:** Run as a detached background service (`start` command).
- [x] **Mission Control TUI:** Rich terminal dashboard with split panes and live streaming (`tui` command).

## 🚧 Planned Features

### User Experience
- [ ] **Interactive Steerability:** Allow users to pause the agent and provide mid-stream feedback/direction without restarting.

### Data & Output
- [ ] **Citation Validator:** Auto-verify URLs in the final report to flag dead links or hallucinations.

### Advanced Capabilities
- [ ] **Multi-Agent "Swarm":** Parallelize research by spawning sub-agents for different aspects of a broad topic.
- [ ] **Token Budgeting:** Track token usage and set cost limits (e.g., "Stop after $2.00").
- [ ] **MCP Support:** Implement Model Context Protocol to allow the agent to query local databases or tools directly.

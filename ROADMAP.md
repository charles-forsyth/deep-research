# 🗺️ Product Roadmap

Future plans for the Gemini Deep Research CLI.

## ✅ Completed
- [x] **Smart Context Ingestion:** Auto-upload local files/folders to temporary Cloud Stores for analysis (`--upload`).

## 🚧 Planned Features

### User Experience
- [ ] **Mission Control TUI:** Replace streaming text with a rich terminal dashboard (Textual/Rich) showing split panes for "Thinking", "Results", and "Progress".
- [ ] **Interactive Steerability:** Allow users to pause the agent and provide mid-stream feedback/direction without restarting.

### Data & Output
- [ ] **Structured Export:** Enforce JSON/CSV output schemas for machine-readable reports.
- [ ] **Citation Validator:** Auto-verify URLs in the final report to flag dead links or hallucinations.
- [ ] **Session Management:** Local SQLite database to save research history, list past tasks, and resume sessions days later.

### Advanced Capabilities
- [ ] **Multi-Agent "Swarm":** Parallelize research by spawning sub-agents for different aspects of a broad topic.
- [ ] **Token Budgeting:** Track token usage and set cost limits (e.g., "Stop after $2.00").
- [ ] **MCP Support:** Implement Model Context Protocol to allow the agent to query local databases or tools directly.
- [ ] **Headless Daemon:** Run as a background service with webhook notifications upon completion.

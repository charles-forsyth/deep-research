# Architecture: Deep Research CLI

This document outlines the high-level architecture of the `deep-research` tool. It follows a **Controller-Service-Repository** pattern adapted for a Python CLI environment.

## 1. High-Level Diagram

```mermaid
graph TD
    User[User] --> CLI[CLI Parser (argparse)]
    CLI --> Config[Configuration (Pydantic)]
    CLI --> Agent[DeepResearchAgent]
    
    subgraph Core Logic
        Agent -->|Uploads| FM[FileManager]
        Agent -->|Streams| API[Gemini API]
        Agent -->|Persists| DB[SessionManager (SQLite)]
    end
    
    subgraph Recursive Loop
        Agent -->|Analyze Gaps| API
        Agent -->|Spawn Children| ThreadPool[ThreadPoolExecutor]
        ThreadPool -->|New Instance| Agent
        Agent -->|Synthesize| API
    end
    
    subgraph Infrastructure
        FM -->|Files| GCS[Google Cloud File Store]
        DB -->|History| SQLite[~/.config/deepresearch/history.db]
    end
```

## 2. Key Components

### 2.1. The Controller (`main`)
Handles command parsing (`research`, `start`, `tree`, `cleanup`). It instantiates the `DeepResearchConfig` to validate environment variables before execution.

### 2.2. The Orchestrator (`DeepResearchAgent`)
The central brain.
*   **Stream Handling:** Consumes Server-Sent Events (SSE) for real-time feedback.
*   **Recursive Logic:** Implements the `_execute_recursion_level` method. It analyzes reports for information gaps and uses `ThreadPoolExecutor` to spawn parallel child research tasks (Depth > 1).
*   **Resilience:** Contains retry logic for API connection drops.

### 2.3. State Manager (`SessionManager`)
A SQLite wrapper handling persistence.
*   **WAL Mode:** Enabled for high concurrency (background writers + foreground readers).
*   **Schema:** Tracks `interaction_id`, `pid` (for headless), `parent_id` (for recursion), and `depth`.

### 2.4. Infrastructure (`FileManager` & `detach_process`)
*   **RAG:** Uploads local files to Gemini File Search Stores. Implements `cleanup` logic to force-delete documents and stores to prevent cloud clutter.
*   **Headless:** Uses platform-specific subprocess creation (`DETACHED_PROCESS` on Windows, `start_new_session` on POSIX) to allow the CLI to exit while the agent keeps running.

## 3. Data Flow (Recursive Mode)

1.  **Phase 1:** Parent Agent performs initial research on the user prompt.
2.  **Gap Analysis:** The report is fed back to Gemini to identify $N$ missing key details (`breadth`).
3.  **Fan-Out:** The Agent spawns $N$ threads. Each thread creates a Child Session in SQLite (linked via `parent_id`).
4.  **Parallel Execution:** Child Agents execute `start_research_poll` independently.
5.  **Fan-In:** The Parent waits for all threads to complete.
6.  **Synthesis:** All child reports are merged into a final comprehensive answer.

## 4. Design Decisions

*   **Pydantic V2:** Used for strict configuration and input validation.
*   **Rich:** Used for all terminal output to provide a modern, readable DX (Developer Experience).
*   **No AsyncIO:** The project uses `threading` instead of `asyncio` because the `google-genai` synchronous client is robust and easier to debug in a CLI context, and `ThreadPoolExecutor` sufficiently handles I/O-bound API calls.

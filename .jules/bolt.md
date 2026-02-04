# Bolt's Journal - Critical Learnings

## 2025-05-15 - Initial Setup
**Learning:** Starting the mission to optimize the Gemini Deep Research Agent. Identified potential N+1 query issue in `SessionManager.list_sessions`.
**Action:** Measure query counts in `list_sessions` and optimize using JOINs and batched updates.

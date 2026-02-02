## 2025-05-14 - [SessionManager Optimization]
**Learning:** SQLite Write-Ahead Logging (WAL) and recursive process checks in a CLI can lead to significant overhead and binary artifacts in the workspace.
**Action:** Always clean up temporary `.db-shm` and `.db-wal` files before submission. Use `LEFT JOIN` to solve N+1 query problems in recursive database lookups.

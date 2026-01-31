# Bolt's Journal - Critical Learnings

## 2025-05-23 - [N+1 Query & Redundant DB Init]
**Learning:** In a recursive agent architecture, multiple instances of the session manager are created across different threads/tasks. If each instance performs full database initialization (PRAGMAs, migrations, column checks), it adds significant overhead to process startup. Additionally, listing sessions with parent-child relationships often leads to N+1 query patterns if parent data is fetched row-by-row.
**Action:** Implement class-level tracking of initialized database paths to skip redundant setup. Use SQL `LEFT JOIN` to fetch related parent data in a single result set. Always batch `UPDATE` statements using `WHERE id IN (...)` when processing multiple rows to minimize expensive disk commits in SQLite.

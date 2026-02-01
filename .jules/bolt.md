
## 2025-05-15 - [Database Optimization Patterns]
**Learning:** SQLite operations in a loop (N+1 queries and individual commits) are a major bottleneck in CLI tools with history. Batching updates and using JOINs for parent/child checks significantly improves responsiveness. Additionally, redundant migrations in every class instantiation can be avoided using a class-level initialization cache.
**Action:** Always check for (N)$ database patterns in list/search methods and use batching/JOINs. Implement a class-level `_initialized_dbs` set for idempotent setup logic.

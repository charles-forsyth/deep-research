## 2024-05-23 - Keep Commits Atomic
**Learning:** Including unrelated changes in a commit, such as modifying a lockfile (`uv.lock`) when implementing a feature or optimization, pollutes the commit and makes it harder to review. This was blocking feedback during code review.
**Action:** Always revert lockfile changes before committing if they are not directly related to a dependency update. Ensure commits are atomic and only contain relevant changes.

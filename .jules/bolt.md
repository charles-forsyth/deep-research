## 2024-07-25 - Do Not Modify Lockfiles

**Learning:** I discovered that modifying the `uv.lock` file is a significant side effect that is unrelated to the core task of performance optimization. It can introduce unpredictable dependency issues and break the build. Lockfiles must remain untouched unless the change is specifically about managing dependencies.

**Action:** I will always revert any changes to `uv.lock` or other lockfiles to their original state before submitting my work.

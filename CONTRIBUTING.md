# Contributing to Deep Research CLI

First off, thanks for taking the time to contribute! 🎉

## 🛠️ Development Setup

We use **[uv](https://github.com/astral-sh/uv)** for blindingly fast dependency management.

1.  **Install uv:**
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

2.  **Clone & Sync:**
    ```bash
    git clone https://github.com/charles-forsyth/deep-research.git
    cd deep-research
    uv sync
    ```

3.  **Install Pre-commit Hook:**
    We enforce strict linting. Install the hook to catch errors before you commit.
    ```bash
    # The repo comes with a hook in .git/hooks/pre-commit
    # Ensure it is executable
    chmod +x .git/hooks/pre-commit
    ```

## 🧪 Testing

We use `pytest` and `unittest.mock`.

```bash
# Run all tests
uv run pytest

# Run a specific test file
uv run pytest tests/test_logic.py
```

## 🎨 Coding Standards

*   **Python Version:** 3.12+
*   **Linter:** `ruff` (enforced via pre-commit).
*   **Type Hinting:** Use modern syntax (`list[str]`, `str | None`).
*   **Validation:** Use `Pydantic V2`.

## 🔄 Workflow

1.  Create a branch: `git checkout -b feature/my-feature`
2.  Make changes.
3.  **Run Tests:** `uv run pytest`
4.  **Run Lint:** `uv run ruff check .`
5.  Commit (The hook will verify tests/lint).
6.  Push and open a PR.

## 🐛 Reporting Bugs

Please use the **Bug Report** issue template on GitHub and include:
*   Your OS and Python version.
*   The output of `deep-research list` (if relevant).
*   Logs from `~/.config/deepresearch/logs/` (scrubbed of API keys).
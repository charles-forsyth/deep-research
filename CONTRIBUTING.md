# Contributing

Thanks for your interest in improving Deep Research. Bug reports, fixes, docs and ideas are all
welcome.

## Development setup

You need Python 3.12+ and [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/charles-forsyth/deep-research.git
cd deep-research
uv sync                      # creates .venv with runtime and dev dependencies
uv run pre-commit install    # run ruff and hygiene checks on every commit
```

Run the tool from your checkout with `uv run deep-research ...`. The dashboard can be run in the
foreground on a spare port while you work on it:

```bash
uv run deep-research dashboard --foreground --host 127.0.0.1 --port 7421
```

## Checks

CI runs exactly these; please run them before opening a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/
uv run pytest
```

The test suite needs no network access or API key. Gemini calls are faked, and dashboard tests run a
real HTTP server on an ephemeral port against a temporary database. New behaviour should come with
tests; bug fixes should include a test that fails without the fix.

For dashboard changes, also check the page in a browser at desktop and phone widths (about 390 px),
and confirm the browser console shows no errors. `node --check` catches JavaScript syntax errors.

## Coding standards

- Python 3.12+ syntax (`list[str]`, `str | None`), type hints on public functions, Pydantic v2.
- Keep the runtime dependency list small. The dashboard deliberately uses only the standard library
  on the server and vanilla JavaScript in the browser, with no build step.
- Never commit API keys, `.env` files, research databases or generated audio.
- Keep cost-affecting behaviour honest: show estimates before spending, and label estimates as such.

## Pull requests

1. Branch from `main` (`feat/...`, `fix/...`, `docs/...`).
2. Keep each PR focused, and describe what changed and how you tested it.
3. Use [Conventional Commits](https://www.conventionalcommits.org/) style titles, for example
   `feat(dashboard): add research map` or `fix: stop stale runs showing as running`.
4. Bump the version in `pyproject.toml` and add an entry to [CHANGELOG.md](CHANGELOG.md) for
   user-visible changes.
5. PRs are squash-merged once CI passes.

## Reporting bugs and requesting features

Use the issue templates. Include `deep-research --version`, your OS and Python version, and the
relevant lines from `~/.config/deepresearch/logs/` with API keys and private content removed. For
security issues, follow [SECURITY.md](SECURITY.md) instead.

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE) and
that you will follow the [Code of Conduct](CODE_OF_CONDUCT.md).

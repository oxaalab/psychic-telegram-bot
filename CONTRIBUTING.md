# Contributing

Thanks for your interest!

## Ways to help
- Report bugs and propose features via GitHub issues.
- Improve docs and translations under `src/i18n/locales/`.
- Tackle “good first issue” and “help wanted” labels.

## Development setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install
uvicorn src.main:app --host 0.0.0.0 --port 50042 --reload
```

## Style & checks
- **ruff** for linting, **black** for formatting.
- Run all checks:
  ```bash
  make lint && make format && make test
  ```

## Commit messages
Follow conventional commits (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, etc.).

## Pull requests
- Fork, create a feature branch, keep PRs focused.
- Include tests where reasonable.
- Update `CHANGELOG.md` when user‑visible changes land.

By contributing you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

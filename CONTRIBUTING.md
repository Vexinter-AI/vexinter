# Contributing to Vexinter

Thanks for your interest in contributing. Vexinter is source-available
under the [Community License](LICENSE-COMMUNITY.md) (Fair Source); by
contributing you agree that your contributions are licensed under the
same terms.

## Ways to contribute

- **Bug reports** — open an issue with reproduction steps.
- **Feature requests** — describe the use case before proposing code.
- **Code** — bug fixes, performance, docs, tests.
- **Security issues** — **do not** open a public issue; see
  [SECURITY.md](SECURITY.md).

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check vexinter/ services/ tests/
mypy vexinter/ services/
```

PostgreSQL-backed tests use testcontainers and need Docker running.

## Pull requests

1. Fork the repository and create a topic branch.
2. Keep changes focused; one concern per PR.
3. Ensure `pytest`, `ruff check` and `mypy` pass.
4. Add or update tests for behavior changes.
5. Write commit messages describing **why**, not just what.

By submitting a PR you agree that your contribution may be included in
the project under the Community License, and that maintainers may ask
for changes or close the PR at their discretion.

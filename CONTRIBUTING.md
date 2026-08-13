# Contributing to DVLib

Thanks for helping improve DVLib.

## Development setup

DVLib requires Python 3.11 or newer. Create a virtual environment, install the
project with its development dependencies, and run the complete local checks:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
python -m ruff check dvlib tests
python -m bandit -q -r dvlib
python -m pytest -q
python -m build
python -m twine check dist/*
```

## Changes and pull requests

- Keep changes focused and include tests for behavior changes and bug fixes.
- Preserve deterministic package archives, lockfiles, and generated filelists.
- Treat tarballs, manifests, registry responses, and file paths as untrusted.
- Update the README when a command, configuration option, or operational
  assumption changes.
- Use a clear pull-request description that explains the problem, the chosen
  approach, and how the result was verified.

By contributing, you agree that your contributions will be licensed under the
MIT License covering this project. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).

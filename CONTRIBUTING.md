# Contributing to aems-agent

Thanks for your interest in contributing. The agent is a small FastAPI service that runs on a user's machine, and the surface area is intentionally narrow.

## Dev setup

```bash
git clone https://github.com/aems-app/aems-agent
cd aems-agent
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Running tests

```bash
python -m pytest
```

## Lint + type check

```bash
ruff check src tests
black --check src tests
mypy src
```

PRs that break any of the three are blocked by CI.

## SPDX headers

Every file under `src/aems_agent/` must carry the SPDX header:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
```

If you add a new file, run `python scripts/add_spdx.py` — it's idempotent, so re-running it is safe.

## Building binaries

```bash
pip install ".[build]"
pyinstaller packaging/aems-agent.spec
```

Cross-platform binaries (Windows, macOS, Linux) are built by `.github/workflows/build.yml` on every `v*` tag push.

## Filing issues

Open at [https://github.com/aems-app/aems-agent/issues](https://github.com/aems-app/aems-agent/issues). Include:

- The agent version (`aems-agent --version`).
- The OS (Windows / macOS / Linux) and Python version.
- A minimal reproducer.

## Pull requests

- Branch from `main`.
- Keep the PR focused.
- New behaviour needs a test.
- Update `CHANGELOG.md` under an `## Unreleased` heading.

## License

By contributing you agree your contributions are licensed under AGPL-3.0-or-later, matching the project licence.

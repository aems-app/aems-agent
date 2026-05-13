# Changelog

All notable changes to `aems-agent` are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/) and the project uses [SemVer](https://semver.org/).

## 0.3.3 — 2026-05-13

Bundle text-clearing bugfix. Required by the AEMS server-side native-text routing fix landed in `aems` `7de79e77`.

### Fixed

- **`generate_bundle` no longer zeros native text on every page when a typed report has a short cover page.** [`grading_bundle.py:178`](src/aems_agent/grading_bundle.py#L178) previously cleared `effective_text` for every page in `smart` mode whenever `doc_has_handwriting` was true, and `doc_has_handwriting` was set if **any single page** had fewer than `_MIN_TEXT_LENGTH (50)` chars. A typed multi-page report with a 33-char cover page (Phase 9 `salmi_simon.pdf`: `"Homework 1\nSimon Salmi\nApril 2024"`) tripped this and shipped image-only bundles with empty text on every page. The AEMS server's native-text router then had no input and the LLM-vision detector silently dropped typed continuation pages as non-answer.

  The text-clearing is now per-page: only the specific low-text pages have `effective_text=""`. Long typed pages keep their PyMuPDF text. Doc-wide image rendering for `smart` strategy is preserved (handwritten exam pages still render as images for the LLM).

  Regression: [`tests/test_grading_bundle.py::test_smart_strategy_preserves_typed_text_across_short_cover`](tests/test_grading_bundle.py).

## 0.3.2 — 2026-05-11

First public OSS release.

### Changed

- **Relicensed from MIT to AGPL-3.0-or-later.** Reason: the agent links [PyMuPDF](https://github.com/pymupdf/PyMuPDF) in-process via [`aems-pdf-annotator`](https://github.com/aems-app/aems-pdf-annotator). PyMuPDF is AGPL-3.0; under the AGPL combined-work rules the agent must inherit the same licence. The MIT licence that previously applied to v0.3.1 and earlier remains in effect for those versions and any forks of them.
- `pyproject.toml`: `license = {text = "AGPL-3.0-or-later"}` (was `"MIT"`); annotator dep pinned to `aems-pdf-annotator>=0.2.0,<0.3.0` to match the public-OSS release of that package.

### Added

- Full AGPL-3.0 text in [LICENSE](LICENSE) (was a 5-line placeholder).
- `SPDX-License-Identifier: AGPL-3.0-or-later` header on every file under `src/aems_agent/`. Applied with [`scripts/add_spdx.py`](scripts/add_spdx.py), which is idempotent and reusable.
- `--version` Typer flag on the CLI: `aems-agent --version` prints `aems-agent <version>` and exits 0.
- `pyproject.toml` metadata: `[project.urls]` block, `keywords`, `classifiers`, `authors[].email`.
- README relicense banner.
- `CHANGELOG.md` (this file).
- `CONTRIBUTING.md` and `SECURITY.md`.

### Existing functionality (no behavioural change in this release)

- Local REST API on `127.0.0.1:61234` for AEMS hosted app integration.
- Token-authenticated pairing.
- System-tray support (Windows / macOS / Linux).
- PyInstaller-built standalone binaries on Windows, macOS, and Linux.
- Canvas download bridge and offline grading bundle support.

## 0.3.1 (and earlier) — MIT-licensed pre-release

Internal MIT-licensed iterations. Not in the public OSS history. Source PDFs stay local, AEMS account auth + grading orchestration live in the hosted app.

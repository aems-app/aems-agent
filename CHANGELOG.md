# Changelog

All notable changes to `aems-agent` are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/) and the project uses [SemVer](https://semver.org/).

## 0.4.10 — 2026-05-31

Repo hygiene + two real CI regressions surfaced by the v0.4.9 review.

### Fixed

- **`tests/test_packaging.py` import error on Python 3.10.** The new packaging-regression test added in v0.4.9 imported `tomllib` unconditionally; `tomllib` is stdlib only on Python 3.11+, and the project's `requires-python = ">=3.10"` means CI's 3.10 matrix entry failed test collection. The import now falls back to `tomli` on 3.10 and the dev extras pin `tomli>=2.0; python_version < "3.11"`.
- **macOS `--tray` no longer silently fails to start uvicorn.** In v0.4.9, if `_prepare_tray_icon()` raised before the server thread started (missing `pystray`, AppKit init error, …), the Darwin code path exited without ever calling `uvicorn.run()` — diverging from Windows/Linux, where a tray failure is non-fatal. The macOS branch now falls back to running uvicorn directly on the main thread when tray setup fails.

### Internal

- **Black formatting normalised across `src/` and `tests/`.** v0.4.8 + v0.4.9 left two pre-existing `black --check` violations on `src/aems_agent/config.py` (long `allowed_origins` lambda) and `tests/test_release_metadata.py`. Both now pass `black --check`, restoring the `lint` CI job to green on `main`.
- **`.gitattributes` added with `* text=auto eol=lf`.** The v0.4.9 edits mixed LF lines into files that were previously 100% CRLF (cli.py, tray.py, config.py, routes.py), producing noisy diffs. All tracked text files have been renormalised to LF in the index, and the new `.gitattributes` keeps cross-platform contributors aligned. Windows scripts (`.bat`, `.cmd`, `.ps1`) stay CRLF.
- **`.gitignore` now excludes `artifacts/`.** Defensive complement to the sdist allow-list — even if a future Hatch upgrade re-introduces `auto-discovery` for sdists, the CI `download-artifact` output directory will not leak into a tarball. (Post-mortem correction: the v0.4.9 CHANGELOG attributed the 318 MB sdist bloat to `dist/` + `build/`; both were already gitignored. The real culprit was `artifacts/`, which `actions/download-artifact` creates at workflow time and was not gitignored prior to this release.)

## 0.4.9 — 2026-05-31

Follow-up patch release fixing real defects discovered after `0.4.8` shipped: an oversized sdist regression in the published release, a macOS tray-thread crash, fragile per-platform clipboard handling, and a couple of packaging hygiene items.

### Fixed

- **Sdist no longer sweeps PyInstaller/release artifacts.** The `0.4.8` source distribution on PyPI/GitHub was 318 MB because Hatch's default sdist included the CI runner's `dist/` and `build/` directories. `pyproject.toml` now declares an explicit `[tool.hatch.build.targets.sdist] only-include` allow-list (`src`, `tests`, `packaging`, project metadata files). The release workflow also runs the Python `sdist`/wheel step *before* downloading the per-platform PyInstaller artifacts, so the working tree is clean when the sdist is built.
- **`--tray` no longer crashes on macOS.** `pystray.Icon.run()` must own the main thread on Darwin (the AppKit run loop is main-thread-only). On macOS the agent now inverts control: uvicorn runs on a worker thread and the tray icon runs on the main thread; on Windows/Linux the existing daemon-thread layout is preserved.
- **Clipboard handling unified behind a single native helper.** The previous Windows path piped text into `clip` as `utf-16-le` and the Tk fallback in the tray's token-copy action created hidden root windows on a worker thread. `src/aems_agent/clipboard.py` now wraps `clip` / `pbcopy` / `wl-copy` / `xclip` with conservative defaults; pairing-PIN copy and the tray "Copy token" menu item both route through it.
- **Packaging hygiene.** PyInstaller spec now disables UPX (which trips multiple AVs on Windows) and builds macOS as `windowed=True` (no spurious Terminal window when launched from Finder). Stale macOS docstring in `config.py` corrected.

## 0.4.8 — 2026-05-31

Linux-host validation pass driven from the Hetzner prod server: install + run + grading-bundle + annotation write all work on Ubuntu 24.04 / Python 3.12, but uncovered three issues that this release fixes.

### Fixed

- **`aems-pdf-annotator` dependency pin broadened to `>=0.2.0,<0.4.0`.** The previous `<0.3.0` ceiling blocked installation of the latest annotator (0.3.0), which is a JS-bundle-only release with no Python ABI changes (`PDFAnnotator`, `payload_to_annotations`, `SUPPORTED_CONTRACT_VERSIONS`, `ContractValidationError` all unchanged). Re-verified by grep against `aems_pdf_annotator/contract.py` in 0.3.0. Without this fix, `pip install aems-agent` together with the latest annotator wheel failed with `ResolutionImpossible`.

### Added

- **`AEMS_AGENT_PIN_FILE` environment variable for headless PIN reveal.** SSH-only / systemd / docker-installed agents have no TTY, no system-tray, and no clipboard, so the three existing PIN-surfacing channels (`_maybe_echo_pairing_pin`, `_copy_pin_to_clipboard`, `_notify_pairing_pin`) are all no-ops and operators can't discover the pairing PIN. When `AEMS_AGENT_PIN_FILE` is set to a writable path, every successful `/pair/initiate` atomically replaces that file with a one-line JSON object — `{"pin":..., "origin":..., "expires_in":..., "written_at":...}` — at mode 0600. Designed for `systemd` `Environment=AEMS_AGENT_PIN_FILE=/run/aems-agent.pin` style installs.

## 0.4.7 — 2026-05-27

Bundle size + annotation-coordinate fixes driven by Zohar's Day-2 retest report (2026-05-26).

### Fixed

- **Grading bundle is now lossy WebP at quality=85 (was lossless q=95).** Multi-page handwritten exams were producing 50-150 MB JSON bundles, which exceeded the AEMS server's `MAX_CONTENT_LENGTH` and surfaced as `413 Payload Too Large` mid-grading. Vision LLMs do not benefit from lossless input; lossy q=85 matches the rest of the AEMS image pipeline. After this change, 8-student SE1020-style bundles are typically 1-4 MB per submission.
- **`serialize_annotation_entry` no longer flips PyMuPDF → PDF on read.** The browser annotator's overlay/rendering code consumes rects in PyMuPDF top-left space; the read-path flip in v0.4.6 forced the UI to compensate, and the undo path's compensating flip then double-flipped under some PDF.js viewport conventions, producing the "Ctrl-Z restores annotation at wrong position" symptom. Both read and write paths now use the PyMuPDF top-left contract end-to-end; the API helper `_pdf_rect_to_pymupdf` continues to convert incoming browser-supplied PDF bottom-left rects on the add path.

### Internal

- `_pymupdf_rect_to_pdf` helper removed (no remaining callers).

## 0.4.6 — 2026-05-25

Tray + Windows icon now uses the AEMS brand glyph (the "A" mark from the aems.app website / aems-web favicon) tinted by status, instead of the generic green-checkmark badge the agent shipped with from 0.3.x onward.

### Changed

- **Live tray icon shows the AEMS glyph in white on a status-coloured rounded-rectangle badge.** At startup it picks green (running with a storage folder set) or yellow (running without). The shape mapping is preserved; only the foreground glyph changed from a generic checkmark to the brand glyph.
- **Packaged Windows app icon** (the static `.ico` baked at build time, used by Start menu / taskbar / Alt-Tab thumbnails) now also uses the brand glyph at multiple resolutions (16/20/24/32/40/48/64/128/256). It is rendered once in green; it does NOT track live status — only the live tray icon does.

### Known limitations

- The red status colour exists in `icons.py` and is rendered for the multi-resolution `.ico` build path, but the live tray runtime does not currently repaint to red on failure: by the time `tray_status` would become "failed" or "unavailable", the tray thread is already dead and there is no live icon to recolour. The red variant is available for a future "storage folder disappeared" watcher.

### Internal

- New `src/aems_agent/assets/` package shipping `aems-logo-mask.png` (512×512 rasterised AEMS-website favicon). `icons.py` loads only the alpha channel and composites it over the badge, so future re-colourings stay one-line edits.
- PyInstaller spec extended with the asset entry so frozen builds bundle the mask.

## 0.4.5 — 2026-05-25

Follow-up to the 0.4.3/0.4.4 tray folder-picker chain.

### Fixed

- **Folder picker now stays in front of other windows.** The 0.4.3 STA PowerShell helper relied on default window activation, which can place the dialog behind whatever the user was focused on when they clicked "Set Storage Folder" — particularly easy to hit with a maximized browser. Now creates a transient invisible `Form` with `TopMost=$true` and passes it as the `ShowDialog` owner so the dialog inherits z-order and steals focus.

## 0.4.4 — 2026-05-25

Cosmetic follow-up to 0.4.3. The tray "Set Storage Folder" command now runs its PowerShell helper hidden.

### Fixed

- **PowerShell console window no longer flashes next to the folder picker.** The 0.4.3 STA helper was launched without `CREATE_NO_WINDOW`, so every click on "Set Storage Folder" briefly displayed a black shell window beside the dialog. The helper now runs with the no-window creation flag and the folder picker appears alone.

## 0.4.3 — 2026-05-25

Reliability hotfix for the desktop-agent path. A first-time tester (Zohar) hit a chain of issues during his prod run: the tray's "Set Storage Folder" dialog refused to close on Windows, in-place upgrades failed with `Error opening file for writing` because the running `aems-agent.exe` was locked, and once the agent shipped without a storage folder configured every PUT to `/files/*` returned an opaque `503` "Storage path not configured" that the web UI could not preempt.

### Fixed

- **Windows tray folder picker now runs in a dedicated STA PowerShell process.** Tk dialogs launched from the pystray callback thread are unreliable on Windows — users can land in a folder window that refuses to confirm or close. The picker is now `[System.Windows.Forms.FolderBrowserDialog]` invoked through `powershell -STA`, which has its own message pump and process lifetime. The Tk implementation remains as a cross-platform fallback for macOS/Linux.
- **The Windows installer now stops a running agent before copying files.** `aems-agent.exe` is killed with `taskkill /IM aems-agent.exe /T /F` at the start of both install and uninstall sections. Without this, upgrades on top of a tray-resident v0.4.0/v0.4.1/v0.4.2 silently failed at write time and the user had to open Task Manager. Note: this is intentionally a hard-kill — the agent has no critical async state to flush.

### Added

- **`POST /pair/initiate` now returns `storage_path`** alongside the existing `challenge_id` / `agent_name` / `expires_in` / `requires_pin` fields. The AEMS web pairing UI already reads this field; previously it was always `undefined`, which meant the "Pairing cannot continue: no storage folder set" gate could never trigger and users would only discover the misconfiguration much later at upload time. The value is the literal `storage_path` from the agent's config — `null` when unconfigured.

## 0.4.2 — 2026-05-25

Reliability hotfix. A first-time Personal-plan tester (Zohar) hit two blocking issues during his prod run today; both are fixed here.

### Fixed

- **Quit-via-tray now terminates the whole agent process.** Previously the tray menu's "Quit" called `icon.stop()`, which only ended the daemon tray thread. The uvicorn HTTP server kept running headlessly on `127.0.0.1:61234` with no tray icon. The user thought the agent was off, tried to launch the .exe again, and the second instance silently failed to bind the port in `--noconsole` PyInstaller mode — no error message, no tray icon, no recourse short of Task Manager. `on_quit` now follows up `icon.stop()` with `os._exit(0)` so the process exits cleanly.
- **Startup port-conflict is now visible.** Before launching uvicorn the agent probes `127.0.0.1:<port>` and, if the bind would fail, surfaces a tk message-box explaining what's wrong. If the squatter responds to `/health` like an AEMS Agent we treat it as "already running" and exit 0; otherwise we report "port in use by something else" and exit 1. This replaces the previous silent failure of windowed builds.

### Operational note

If you are upgrading from `0.4.1` and the previous agent's tray icon disappeared but the process is still listening (which is the bug this release fixes), open Task Manager, end `aems-agent.exe`, then run the new installer.

## 0.3.7 — 2026-05-21

Security hardening of the local-bridge surface, plus reliability fixes on the file-IO and CRUD paths. All changes are local to the agent and require no AEMS server-side update.

### Security

- **Loopback Host header enforcement for every route.** The agent now rejects requests whose `Host` header is not one of the expected local endpoints for the configured bind target and port. This closes the DNS-rebinding gap where a hostile origin could reach the local service through an attacker-controlled hostname that later resolves to `127.0.0.1`.
- **Pairing PIN no longer prints to non-interactive stdout.** The PIN is still available through the tray notification and clipboard hand-off, but daemonized launches and captured stdout logs no longer receive the secret by default.
- **Pairing flow hardening.** `POST /pair/initiate` now refuses to overwrite an active challenge, `POST /pair/complete` no longer consumes the challenge on pre-PIN validation failures, failure details are collapsed to `Pairing failed`, and repeated bad PIN attempts trigger a temporary lockout.
- **JSON endpoints now enforce a request-body cap.** The agent reads JSON bodies through a bounded stream before parsing, preventing authenticated memory-exhaustion requests against the results, assignment, grading-bundle, and annotation CRUD endpoints.

### Fixed

- **JSON and Canvas downloads now use randomized temp files with cleanup on failure.** This removes predictable `.tmp` filenames and avoids orphaned temp files when `os.replace()` fails.
- **PDF download hashing no longer buffers the whole file in memory.** `/files/{aid}/{sid}` and `/files/{aid}/{sid}/annotated` now compute SHA-256 headers incrementally.
- **Annotated-PDF cache freshness now uses nanosecond mtimes.** This avoids same-second cache reuse on fast file updates.
- **Annotation CRUD responses now round-trip rect coordinates back in PDF space.** The agent converts rects back from PyMuPDF space before returning them to the browser/UI.

### Note

This release tags the work that landed in `master` commit `d943780` (`Harden local agent pairing and request handling`) plus the file-IO follow-ups, all of which were in the `Unreleased` section since the `0.3.6` release on 2026-05-17. The wire-level pair / file / data routes are byte-compatible with 0.3.6; existing paired tokens stay valid across upgrade.

## 0.3.6 — 2026-05-17

Assessment-level cleanup endpoint required by the AEMS server-side delete flow that landed in `aems` `df047252`.

### Added

- **`DELETE /files/{assignment_id}`** — removes all local files associated with an assessment, including the per-submission storage directory (`{storage}/{aid}/`), the assessment data directory (`{storage}/_data/{aid}/`), and the per-assessment grading-bundle cache (`{storage}/_cache/bundles/{aid}/`). Returns per-path booleans so the caller can see which trees actually existed. 404 when nothing matched, so the server can keep its own state consistent. Token-authenticated, rate-limited like every other `/files/*` route. The AEMS web UI now hits this endpoint whenever an offline-mode assessment is deleted, so the local agent storage no longer accumulates orphan directories across the lifecycle of a course.

### Note

- v0.3.5 was a Windows-icon-only release. Users on v0.3.5 (or earlier) who continue to delete assessments through the AEMS UI will see the server-side cleanup silently no-op for their local agent until they install v0.3.6. The hosted server-side fix landed in `aems` `df047252` and shipped to api.aems.app on 2026-05-17.

## 0.3.5 — 2026-05-17

Windows agent icon refresh for both the running tray instance and the packaged installer/executable.

### Fixed

- **The installed Windows app now ships a real embedded `.ico` instead of silently falling back when `packaging/icon.ico` is missing.** The build path generates a multi-resolution icon asset automatically before PyInstaller runs, and the spec does the same when invoked directly. This closes the path where the taskbar/start-menu icon degraded to the default executable look.
- **The live tray icon now renders at a native runtime size instead of drawing at 256x256 and relying on OS downscaling.** Runtime and packaged icons now share the same renderer, so the visible tray glyph and the bundled EXE icon stay aligned.

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

# Changelog

All notable changes to `aems-agent` are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/) and the project uses [SemVer](https://semver.org/).

## 0.4.22 — 2026-06-15

Packaging follow-up. End-user behaviour and the API surface are unchanged.

### Fixed

- **Portable Windows `install.ps1` autostart now actually writes the registry value.** v0.4.21 added the autostart step but used `Set-ItemProperty -Path ... -Name 'AEMS Agent' -Value $value -Type String`; `Set-ItemProperty` does not accept `-Type`, so PowerShell silently routed the call into the `catch` block, surfaced a soft warning, and left `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\AEMS Agent` unset. Portable-bundle users were still missing autostart after sign-out / reboot despite the v0.4.21 release notes claiming the gap was closed. Switched to `New-ItemProperty ... -PropertyType String -Force`, which is the canonical cmdlet for the create-or-overwrite case, and added a regression test that fails if the broken form ever reappears.

## 0.4.21 — 2026-06-15

Packaging follow-up. End-user behaviour and the API surface are unchanged.

### Fixed

- **Portable Windows `install.ps1` now registers HKCU autostart**, matching what `aems-agent-setup.exe` (the NSIS installer) writes to `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. Without this step, users who installed from the raw PyInstaller bundle still had to relaunch the tray manually after every sign-in. The web UI's "Does not auto-start" warning was therefore accurate for them even though it is now hidden on Windows; aligning the two install paths so the warning's absence is truthful for every Windows user, not just those who ran the .exe.

## 0.4.20 — 2026-06-14

Packaging follow-up. End-user behaviour and the API surface are unchanged.

### Fixed

- **Portable Windows bundle now ships a kill-then-replace installer.** Users who download the raw PyInstaller bundle (or developers iterating with `python packaging/build.py`) previously had to hand-kill the tray before copying files; doing the swap with the tray still bound to `127.0.0.1:61234` left the new launch tripping `_preflight_port_or_die`'s "Another AEMS Agent is already running" dialog. `packaging/windows/install.ps1` (also copied into `dist/aems-agent/install.ps1` by the build) stops every `aems-agent` process, waits for the port to actually free via a `TcpListener` bind probe, atomically wipes `_internal/`, swaps `aems-agent.exe`, and starts the tray once. The NSIS installer (`aems-agent-setup.exe`) already had the equivalent flow; this closes the gap for users who never touch the installer.

## 0.4.19 — 2026-06-12

Follow-up to v0.4.18 acting on a code review of the security-hardening slice. Completes the Canvas download size cap and closes two file-descriptor edge cases. No API surface change.

### Fixed

- **Canvas downloads are now capped while streaming, not after buffering.** v0.4.18 enforced `MAX_DOWNLOAD_BYTES` only after `httpx.AsyncClient.get()` had already read the entire response body into memory, so a hostile or buggy allowlisted host could still force the agent to buffer an arbitrarily large response before the check ran. The download now uses `http_client.stream(...)` and caps bytes while iterating `aiter_bytes()`, aborting the connection the moment the limit is crossed.
- **No file-descriptor leak if `os.fdopen()` fails.** The owner-only secure writers in `config.py` and the pairing-PIN writer in `routes.py` opened a raw fd with `os.open()` and handed it to `os.fdopen()`; if `fdopen()` raised (e.g. resource exhaustion) the fd leaked. Both now close the fd on that failure path.

### Internal

- The Canvas download tests mock `client.stream()` (shared `_stream_client` helper) instead of `client.get()`, and cover the streaming size-cap abort.

## 0.4.18 — 2026-06-12

Security-hardening release from a full-codebase review. No API surface changed for well-formed clients; several classes of malformed or hostile input are now rejected earlier and more cheaply, and the CI/release pipeline runs with least-privilege tokens.

### Security

- **Canvas download URLs are pinned to the manifest host.** `download_submissions()` previously joined `download_url` onto `canvas_base_url` by string concatenation, so a manifest carrying `@evil.example.com/x` (or a full URL) would have redirected the request — including the Canvas bearer token — to a host outside the validated allowlist. `_build_download_url()` now requires an absolute path and verifies the joined URL still resolves to the same scheme/host/port as the validated Canvas base; offending submissions are marked `failed` without any request leaving the agent. Protocol-relative (`//host/…`) paths are rejected too, so a future refactor to `urljoin` semantics cannot reopen the gap.
- **PDF uploads are size-capped while streaming.** `PUT /files/...` and `PUT /files/.../annotated` read the whole request body into memory *before* checking the 200 MB cap, so an oversized body was buffered in full before being rejected — a memory-exhaustion vector. Both routes now stream through the same bounded reader the JSON endpoints use and abort with 413 as soon as the cap is crossed (or up front via `Content-Length`). Canvas downloads gained a matching 200 MB cap, and skip-path hashing is chunked instead of `read_bytes()`.
- **`/grading-bundle` render parameters are validated.** `dpi` (int, 30–600), `max_pages` (positive int), and `force_refresh` (bool) are now type- and range-checked before any PDF work. Previously `dpi: 100000` would render an arbitrarily large pixmap (memory exhaustion) and non-int types crashed the worker with a 500.
- **Path segments reject reserved names.** `assignment_id`/`submission_id` now refuse Windows reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9` — the storage folder may live on a Windows volume even when the agent doesn't), enforce a 128-char cap, and refuse a leading underscore: `_data`/`_cache` are agent-internal namespaces, and `DELETE /files/_data` would otherwise have deleted every stored grading result across all assignments.
- **Secrets are created owner-only, atomically.** The auth token, the X25519 private key, `config.json`, and the headless pairing-PIN file were written with default permissions and then `chmod`ed, leaving a window where they were world-readable on multi-user POSIX systems. All are now created `0600` via `os.open(..., O_CREAT, 0o600)`. Legacy token/config files are tightened on next access.
- **Keypair recovery no longer rotates the key.** If only `agent_public.key` went missing, `ensure_keypair()` regenerated a whole new keypair, silently invalidating every payload the server had sealed to the old key. The public key is now rederived from the surviving private key, keeping the advertised `encryption_key_id` stable.
- **Windows system binaries are invoked by absolute path.** `clip` and `powershell` were resolved by bare name; Windows `CreateProcess` searches the current working directory before `PATH`, so a planted `clip.exe`/`powershell.exe` next to the launch directory would have been executed. Both now resolve under `%SystemRoot%\System32`, and `pbcopy` uses `/usr/bin/pbcopy` on macOS.
- **Bearer-token comparison can't 500.** `secrets.compare_digest` in str mode raises `TypeError` on non-ASCII input (ASGI servers decode raw header bytes as latin-1), which surfaced as a 500; the comparison now runs on UTF-8 bytes and hostile tokens get a clean 403.
- **CI/release workflows run least-privilege.** `ci.yml` is pinned to `contents: read`; `build.yml`'s build jobs (which execute third-party tooling) drop from workflow-wide `contents: write` to `read`, with `contents: write` + `id-token: write` granted only to the `release` job that actually publishes.
- **Dependency floors exclude known-vulnerable releases:** `pillow>=10.3.0` (CVE-2023-50447 ImageMath eval, CVE-2024-28219 buffer overflow), `fastapi>=0.115.0` (pulls starlette ≥0.40, multipart DoS CVE-2024-47874), and an explicit `h11>=0.16.0` floor (request smuggling CVE-2025-43859 — h11 is uvicorn-transitive but the agent is an HTTP server, so the floor is pinned directly).

### Fixed

- **"Agent already running" detection actually works now.** The pre-flight port probe queried `/health`, which requires a bearer token — the probe always got 401 and every port conflict was misreported as "another program is using the port". It now probes the unauthenticated `/status` endpoint and matches the `aems-agent` service marker. The probe also resolves the bind address family via `getaddrinfo`, so `--host ::1` no longer fails the IPv4-only pre-flight bind, and wildcard hosts probe via loopback (connecting to `0.0.0.0` fails on Windows).

### Internal

- 40 new regression tests: URL pinning (full-URL / userinfo / protocol-relative / missing `download_url`), download + upload size caps, reserved/overlong path segments, grading-bundle parameter validation, non-ASCII token handling, owner-only file modes (POSIX), public-key rederivation stability, and pre-flight port-check behaviour (free port / foreign squatter / running agent).
- `test_tray_failure_reporting` waits on a deadline poll instead of a fixed 200 ms sleep, removing an order-dependent flake on slow CI runners.

## 0.4.17 — 2026-06-08

Critical macOS-only follow-up to v0.4.16. The double-click launcher fix in v0.4.16 made a Finder double-click run `run --tray`, but the `.app` bundle's `Info.plist` declared **both** `LSBackgroundOnly=true` and `LSUIElement=true`, which are contradictory. `LSBackgroundOnly` marks the app a pure background daemon that the WindowServer forbids from presenting any UI — including a menu-bar `NSStatusBar` item. So even though the agent now started, the pystray tray icon was suppressed and the user still saw nothing in the menu bar: the same "double-click does nothing" symptom v0.4.16 set out to fix (the agent server did start, so the website connected, but the tray menu — Settings / Set Storage Folder / Copy Token / Quit — was unreachable on macOS).

### Fixed

- **macOS menu-bar tray icon now appears.** `packaging/aems-agent.spec` drops `LSBackgroundOnly` from the bundle `info_plist` and keeps `LSUIElement=true` alone. `LSUIElement` is the correct "agent app" contract: no Dock icon, but the app may own a menu-bar status item. Tray rendering and all other launch paths are unchanged.

### Internal

- `test_macos_spec_uses_bundle_directive_with_brand_metadata` now asserts the spec does **not** contain `LSBackgroundOnly`, so the conflict cannot reopen silently.

### Verification note

- This change lives in the `sys.platform == 'darwin'` BUNDLE block, which only executes on a macOS host. It cannot be built or exercised on Linux/Windows CI runners. Final confirmation that the tray icon renders is the macOS release job plus an Apple tester double-clicking the built `.app`.

## 0.4.16 — 2026-06-07

Critical macOS-only fix. The Apple tester on v0.4.13/v0.4.14/v0.4.15 got past Gatekeeper via the documented Sequoia path but then reported that double-clicking `AEMS Agent.app` did nothing visible. Codex traced this to a single launcher gap.

### Fixed

- **Frozen macOS `.app` double-click now actually starts the agent.** The bundle declares `LSUIElement=true` (no Dock entry), and Finder double-clicks invoked `Contents/MacOS/aems-agent` with no subcommand. The Typer CLI requires a subcommand, so the process exited immediately with `Missing command.` — and because `LSUIElement` suppresses Dock + window UI, the user saw absolutely nothing happen. `packaging/launcher.py` now detects "frozen + darwin + no args" and rewrites `sys.argv` to `["run", "--tray"]` before invoking `aems_agent.cli.main()`, mirroring what the `com.aems.agent.plist` LaunchAgent does at login. Terminal invocations with arguments, `--version`, Linux/Windows frozen invocations, and dev-mode (non-frozen) invocations are all left alone.

### Internal

- New regression `test_launcher_defaults_finder_double_click_to_run_tray` covers the rewrite predicate across all six cases (frozen+darwin+0-args, frozen+darwin+subcommand, frozen+darwin+`--version`, non-frozen, frozen+Linux, frozen+Windows). If a future maintainer flattens the launcher again, the suite fires.

### Known follow-ups (NOT in this release — Codex review identified, deferred)

- The web's `_isLoopbackPermissionError` heuristic in `aems-web/src/aems/web/static/js/core/agent-discovery.js` returns `true` for a bare `TypeError: Failed to fetch` from a public-HTTPS origin to loopback. That's also what happens when the agent isn't running at all (connection refused). So Sofia's "agent never started" symptom got surfaced as "Blocked by browser" — wrong root-cause attribution. v0.4.16 fixes the underlying "agent never started" problem, so this mislabel stops firing for her, but the heuristic remains overly permissive for users whose agent legitimately is down.
- The `targetAddressSpace: 'loopback'` fetch option is set across 14 sites in `aems-web` (discovery / badge / launch / settings / global probe) while `aems-web`'s `local-mode/download-orchestrator.js` explicitly omits it with a comment saying Chrome 148+ rejects requests that declare it. This contradiction across the codebase should be unified to the omission strategy in a follow-up.
- No macOS `CFBundleURLTypes` for an `aems-agent://` scheme yet — Windows installer registers it, macOS doesn't, so any future "Launch agent from browser" UX is incomplete on macOS.
- Paid-path `codesign --force --deep` carryover from v0.4.13 untouched (needs real macOS Developer ID host for inside-out rewrite per TN2206).

## 0.4.15 — 2026-06-07

Patch release after Codex caught that the v0.4.14 CI build failed before the release artifacts published: the new "Verify DMG contents" step did its job and red-flagged a missed wiring in the workflow itself.

### Fixed

- **The release workflow's "Build DMG from signed .app" step now reuses `packaging/build.py`'s staging helper** instead of calling `hdiutil create -srcfolder "dist/AEMS Agent.app"` directly. v0.4.14 fixed the DMG staging in `build.py` but the workflow still built the DMG straight from the `.app`, so `com.aems.agent.plist` and the `Applications` symlink never made it into the released DMG — the same gap v0.4.14 was supposed to close. The workflow now invokes `build.build_macos_dmg(Path("dist/aems-agent"))` via inline Python so CI and local macOS builds emit the same DMG root.
- **`src/aems_agent/icons.py` module docstring** corrected — it still described the Windows icon as the green status badge even after `ensure_windows_icon` was switched to `render_app_icon` (brand navy) in v0.4.14.

### Internal

- Regression test `test_workflow_relies_on_pyinstaller_signing_when_developer_id_absent` now asserts the workflow's DMG-build block does **not** contain `-srcfolder "dist/AEMS Agent.app"` and **does** call `build.build_macos_dmg`, so the v0.4.14-style drift can't reopen silently.

### Validation gap (still carried over from v0.4.13)

- Developer ID path's `codesign --force --deep --options runtime --timestamp --sign ...` still uses `--deep` for signing instead of TN2206's inside-out approach. Untouched because validating the rewrite requires a real macOS host with a Developer ID identity available — same reason as v0.4.14.

## 0.4.14 — 2026-06-07

Follow-up to v0.4.13 acting on a Codex review of the macOS packaging slice. The Apple tester on v0.4.13 confirmed the app launches past Gatekeeper via the documented Sequoia path, surfacing three smaller gaps worth closing before declaring the slice done.

### Fixed

- **The DMG now actually contains the LaunchAgent plist + an `/Applications` symlink it has always advertised.** `packaging/build.py` previously built the DMG straight from the `.app` bundle and then wrote `com.aems.agent.plist` next to `AEMS-Agent.dmg` — the plist never made it into the DMG itself, contradicting both the README and `packaging/macos/README.md`. `_prepare_macos_dmg_staging_dir()` now stages the `.app`, the plist, and an `/Applications` symlink into a folder that becomes the DMG root, so the drag-to-Applications convention works without forcing users to find Applications in a separate Finder window. Regression-tested at the unit level (`test_build_macos_dmg_stages_app_launch_agent_and_applications_alias`) and at the artifact level (new CI step mounts the built DMG and asserts all three are present).
- **macOS Finder / Applications / Spotlight icon is now AEMS brand navy, not the menu-bar status green.** v0.4.13 wired the `.icns` to `render_status_icon("green", …)`, which is the menu-bar tray badge whose palette encodes runtime state (green = running, yellow = no storage, red = error). That treatment is wrong for the product-identity icon Finder shows on the bundle. New `render_app_icon()` uses `APP_ICON_BRAND_COLOR = (0, 71, 145, 255)` — the same `--brand-primary: #004791` token the aems-web design system uses — with the AEMS glyph composited in white at a slightly larger Apple-HIG inset (~62% of the icon bounds), and a corner radius tuned to read as a modern macOS app icon. Tray rendering is unchanged.

### Internal

- New CI gate **smoke-tests the frozen `.app`** with `"dist/AEMS Agent.app/Contents/MacOS/aems-agent" --version` immediately after PyInstaller, before any signing step. If the BUNDLE migration silently broke a `hiddenimport`, a `ctypes.dlopen` lookup, or a relocated `Contents/Frameworks/` lib, the build fails before the release tag is published. This is the macOS analogue of the Windows `Verify installer exists` step.
- New CI gate **mounts the built DMG and asserts the three top-level items** (`AEMS Agent.app/`, `com.aems.agent.plist`, `Applications` symlink). Catches DMG-staging regressions like the v0.4.13 missing-plist one before users see them.
- Test coverage now asserts `APP_ICON_BRAND_COLOR == (0, 71, 145, 255)` and that the rendered app icon's brand-fill region matches navy at a sampled pixel, so a future revert to the green status palette fires the regression suite.
- Windows ICO now also uses the new `render_app_icon()` instead of the green status badge, so the Start menu / taskbar icon matches the macOS Finder icon.

### Carryover from Codex review (NOT shipped)

- **Developer ID signing path still uses `codesign --force --deep`** in `.github/workflows/build.yml:158`. Apple's TN2206 recommends `--deep` for verification and inside-out signing for the actual signing pass. Left in place because validating the inside-out rewrite needs a real macOS host with the Developer ID identity available, which we don't have. The unpaid ad-hoc path is unaffected (PyInstaller handles signing).

## 0.4.13 — 2026-06-07

The v0.4.11 + v0.4.12 ad-hoc codesign passes kept failing in CI with variants of `bundle format unrecognized, invalid, or unsuitable` because pre-0.4.13 the macOS bundle was hand-assembled into a flat layout (PyInstaller's onedir tree dropped under `Contents/MacOS/_internal/`). Apple's `codesign` interprets anything under `Contents/MacOS/` as nested-code territory and refuses to walk pip metadata dirs, the Python stdlib tree, or PyInstaller's embedded Python.framework that all sit there. No `codesign` flag combination rescues this layout — the fix is to stop using it.

### Fixed

- **macOS .app is now produced by PyInstaller's `BUNDLE` directive instead of being hand-assembled.** `packaging/aems-agent.spec` declares a `BUNDLE(coll, name="AEMS Agent.app", icon=..., bundle_identifier="com.aems.agent", info_plist={...})` step on darwin. PyInstaller 6.11+ relocates shared libraries to `Contents/Frameworks/` and data to `Contents/Resources/`, leaving only the launcher in `Contents/MacOS/` — the layout `codesign` actually wants. PyInstaller also ad-hoc signs every collected binary AND the .app wrapper by default, so the CI workflow no longer re-signs anything in the no-Developer-ID path; it just runs `codesign --verify --deep --strict` on the bundle.
- **`packaging/build.py` no longer assembles the macOS .app.** `_write_macos_app_bundle()` has been removed; `build_macos_dmg` calls `_locate_pyinstaller_macos_bundle()` which just resolves the BUNDLE-produced `dist/AEMS Agent.app`. Hand-mutating a signed bundle is what Apple TN2206 explicitly warns against.
- **PyInstaller pin tightened to `>=6.20.0,<7.0.0`.** The BUNDLE relocation logic that makes this signing pass cleanly is in 6.11; 6.20 is the conservative floor with the recent framework-handling fixes.

### Internal

- Test coverage now asserts: the spec uses BUNDLE with the brand metadata + icon, the pyinstaller pin floor is ≥6.20, the workflow's ad-hoc path verifies (never re-signs), and `_write_macos_app_bundle` cannot return — a future maintainer restoring the manual assembly path fails the suite before shipping.
- The workflow's ad-hoc step shrank from ~50 lines of find loops, framework-skip filters, and dist-info strip to a single `codesign --verify` call.

## 0.4.12 — 2026-06-07

Follow-up patch: the v0.4.11 release surfaced three CI failures that blocked the macOS .dmg artifact from publishing. All three are now fixed and the macOS, Windows, and Linux jobs publish a full release set.

### Fixed

- **macOS `codesign --deep` rejected PyInstaller's `dist-info` directories.** The v0.4.11 ad-hoc signing step ran `codesign --force --deep --sign - "dist/AEMS Agent.app"` and bailed with `bundle format unrecognized, invalid, or unsuitable. In subcomponent: .../websockets-16.0.dist-info`. `codesign --deep` walks the bundle looking for nested bundles, and pip's `*.dist-info` and `*.egg-info` directories look like malformed bundles to it. The ad-hoc step now enumerates real Mach-O files under `Contents/MacOS/` with `find` + `file -b`, signs each one without `--deep`, then signs the main executable and the bundle wrapper. This produces a valid ad-hoc signature on every loaded binary without tripping over dist-info metadata.
- **`packaging/build.py` crashed on Python 3.10 with `ModuleNotFoundError: tomllib`.** The new `_write_macos_app_bundle` helper added in v0.4.11 imported `tomllib` unconditionally; `tomllib` is stdlib only on Python 3.11+ and the project's `requires-python = ">=3.10"` means the 3.10 matrix entry in the test job failed. Now uses the same `if sys.version_info >= (3, 11): import tomllib else: import tomli as tomllib` shim the rest of the codebase uses.
- **`black --check` failed on three new files.** `icons.py`, `tests/test_icons.py`, and `tests/test_packaging.py` were committed without running the formatter. All three are now reformatted to match the project style; `black --check` is green.

### Internal

- The workflow regression test now asserts the per-Mach-O signing call shape (`codesign --force --sign - --timestamp=none "$APP"` for the wrapper plus the same for the main executable) instead of the previous `--deep` invocation.

## 0.4.11 — 2026-06-07

Unblocks the macOS install path that the v0.4.10 download surfaced as completely broken to an Apple-using tester: the unsigned, icon-less `.app` was rejected by Gatekeeper as "AEMS Agent is damaged" and rendered as a blank document icon in Finder.

### Fixed

- **macOS `.app` now has a real, multi-resolution icon.** `Contents/Resources/aems-agent.icns` is generated from the AEMS brand glyph at every standard Apple IconFamily slot (16, 32, 64, 128, 256, 512, 1024 px — including the retina `@2x` variants `ic11`/`ic12`/`ic13`/`ic14`). `Info.plist` now declares `CFBundleIconFile` + `NSHighResolutionCapable`. Finder, the Dock, and Spotlight now render the badge correctly at every zoom level.
- **macOS `.app` is now ad-hoc code-signed in CI when no Apple Developer ID is configured.** Big Sur+ refuses to launch fully unsigned binaries (`kCSRequireSignature`), which is the exact symptom the tester saw. The CI workflow now runs `codesign --force --deep --sign -` on the `.app` bundle before building the DMG, which flips Gatekeeper from the unrecoverable "damaged" error to the "from an unidentified developer" prompt that users can bypass once via right-click → Open. This is the shipping path most open-source macOS apps without a paid Developer Program enrollment use (Calibre, OBS Studio historically, HandBrake, MacDown, …).
- **CI now signs the `.app` *before* wrapping it in the DMG.** Previously the workflow signed the .app *and* a pre-existing unsigned DMG; the .app *inside* the DMG was therefore unsigned, which would have failed notarization even on the Tier-2 (paid Developer ID) path. `packaging/build.py` now supports `AEMS_AGENT_SKIP_DMG=1` so the workflow can interpose signing between PyInstaller and DMG creation. Developer-ID and ad-hoc paths share the new ordering.

### Internal

- `aems_agent.icons.ensure_macos_icns(path)` — hand-rolled multi-size IconFamily writer (Pillow's ICNS writer is unreliable cross-platform and only emits a single slot). Test coverage asserts magic bytes, total-size header, and every Apple-standard slot from 16x16 to 1024x1024 is present and PNG-encoded.
- `_write_macos_app_bundle()` extracted from `build_macos_dmg()` so CI and local Mac dev builds share one bundle-assembly path.
- New regression tests guard (a) the IconFamily writer producing all 10 slots, (b) the `.app` bundle containing `Resources/aems-agent.icns` + the two `Info.plist` keys, (c) the `AEMS_AGENT_SKIP_DMG` env honored by the build script, and (d) the CI workflow keeping both Developer-ID and ad-hoc signing paths in the correct order around DMG creation.

### Docs

- `README.md` and `packaging/macos/README.md` now spell out the first-launch ritual (drag to Applications → right-click → Open) with the `xattr -dr com.apple.quarantine` fallback, and explain why this is the same flow free open-source macOS apps use.

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

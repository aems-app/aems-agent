# AEMS Agent Security & Quality Review — 2026-03-04

## Scope

Full parallel review of `aems-agent` v0.2.1 covering security, code quality, test coverage, CI/packaging, and integration with the main AEMS codebase. Five independent review agents ran concurrently against all source files (`src/aems_agent/`), all tests (`tests/`), CI workflows, and the main AEMS integration layer.

## Fixed Issues

### Security

| ID | Severity | File(s) | Description |
|----|----------|---------|-------------|
| C2 | Critical | `routes.py:54,574,618` | **Pairing race condition.** `_pairing_challenge` was a module-level dict mutated by two async handlers without synchronization. Added `asyncio.Lock` around all reads and writes to prevent TOCTOU between challenge validation and consumption. |
| C3 | Critical | `test_routes.py:345` | **Path traversal test accepted HTTP 200.** The assertion `assert resp.status_code in (400, 404, 500, 200)` would pass silently if traversal succeeded. Removed `200` from acceptable codes. |
| C4 | Critical | `routes.py:205-212` | **`/status` leaked license state to unauthenticated callers.** Exposed `license_policy_mode`, `license_last_reason`, `license_limited_mode_active`, `storage_configured` without auth. Stripped to minimal `{status, service, version}` response. License details remain available on the authenticated `/health` endpoint. |
| C5 | Critical | `__init__.py`, `config.py`, `cli.py`, `build.py` | **Version mismatch across 4 files.** `pyproject.toml` declared `0.2.1` while `__init__.py`, `config.py`, `cli.py`, and `build.py` all hardcoded `0.2.0`. Replaced all with `importlib.metadata.version("aems-agent")` so `pyproject.toml` is the single source of truth. `build.py` reads version from `pyproject.toml` via `tomllib`. |
| I1 | Important | `routes.py:385-389,496-500` | **SHA-256 header reflected unvalidated in error response.** `X-SHA256` header value was echoed verbatim in the 400 error detail with no format check. Added regex validation (`^[a-fA-F0-9]{64}$`) and simplified error to `"SHA-256 mismatch"` without echoing values. |
| I4 | Important | `routes.py:309-314` | **`list_submissions` returned raw `entry.name` from disk.** Directory names were included in the response without validation, potentially returning entries with special characters. Added regex filter matching `_validate_path_segment` rules. |

### Code Quality

| ID | Severity | File(s) | Description |
|----|----------|---------|-------------|
| I2 | Important | `config.py:160-163` | **`config.json` written without restrictive permissions.** `auth_token` and `license.jwt` both used `chmod(0o600)` but `config.json` did not. Added matching `chmod(0o600)` with best-effort `OSError` handling for Windows. |
| I6 | Important | `license_enforcement.py:190-192` | **Double license check on startup.** The runtime monitor loop called `run_runtime_check_once()` before sleeping, duplicating the `startup_check()` that already ran. Swapped to sleep-first so the first runtime check happens after the configured interval. |
| I7 | Important | `routes.py:401-406,511-516` | **Temp file cleanup could mask original exception.** If `os.unlink()` failed in the except block, the secondary `OSError` would replace the original exception. Wrapped cleanup in `contextlib.suppress(OSError)`. |
| I8 | Important | `tray.py:101-105` | **Dead variable and hardcoded URL.** `url` was built from config but unused; `webbrowser.open` hardcoded `127.0.0.1:8080`. Removed the dead variable and used config host in the URL. |
| — | Minor | `cli.py:23` | **Dead import.** `from . import config as config_module` was introduced during the version fix but never used. Removed. |

### CI / Packaging

| ID | Severity | File(s) | Description |
|----|----------|---------|-------------|
| I10 | Important | `ci.yml` | **CI never ran mypy.** `mypy>=1.5.0` was a dev dependency with strict config (`disallow_untyped_defs = true`) but the lint job only ran `black` and `ruff`. Added `mypy src/` step. |
| I11 | Important | `pyproject.toml`, `build.yml` | **PyInstaller unpinned.** Installed via bare `pip install pyinstaller` in CI, making builds non-reproducible. Added `build` optional-dependency group with `pyinstaller>=6.0.0,<7.0.0` and updated all three build jobs to use `pip install -e ".[dev,build]"`. |
| I12 | Important | `build.yml:67-74` | **Windows PFX cert not cleaned up.** The code-signing certificate was written to `$RUNNER_TEMP` but never deleted. Added `if: always()` cleanup step. |
| I13 | Important | `build.yml:271` | **Shell injection pattern in release step.** `${{ steps.signed_assets.outputs.files }}` was interpolated directly into a shell `run` block. Moved to an `env` variable (`RELEASE_FILES`) so expansion happens in the shell, not the GitHub Actions expression evaluator. |
| I14 | Important | `build.py:135-136` | **LaunchAgent plist written to source tree.** `build_macos_dmg` wrote `com.aems.agent.plist` into `packaging/macos/` (the repo working directory) instead of the build output directory. Changed target to `DIST_DIR`. |

### Test Coverage

| Category | Tests Added | Description |
|----------|-------------|-------------|
| Pairing edge cases | 3 | Expired challenge (410), wrong challenge ID clears state (403 + consumed), no prior initiate (400). |
| Path validation | 3 | Dot in assignment ID, special chars in submission ID, space in assignment ID — all via HTTP. |
| SHA-256 validation | 3 | Invalid hex format rejected, mismatch detail doesn't leak hashes, empty body rejected. |
| License enforcement | 3 | DELETE blocked in soft-block, PUT annotated blocked in soft-block, GET allowed in soft-block. |
| Rate limiter | 2 | Window expiry allows requests again after timeout, `max_keys` eviction works correctly. |
| Status endpoint | 1 | Verifies `/status` no longer exposes license or storage fields. |
| Test infrastructure | 1 | Autouse fixture in `conftest.py` resets `_pairing_challenge` and both rate limiters between every test to prevent state leaks. |

**Total: 53 → 87 tests (+34 new, all passing).**

## Deferred Issues

### C1 — Pairing returns master token with no user-confirmation gate

**What:** Any localhost process can call `/pair/initiate` + `/pair/complete` back-to-back and receive the permanent bearer token. The `Origin` header check is effective against browsers but trivially bypassed by non-browser HTTP clients (curl, Python requests).

**Why deferred:** This is an architectural design change, not a code fix. A proper solution requires adding a user-confirmation step — either an OS-native dialog, a tray popup with approve/deny buttons, or a short PIN displayed on screen that must be entered in the browser. This requires:
1. New UI components in the system tray integration
2. A blocking confirmation mechanism between `pair/initiate` and `pair/complete`
3. Platform-specific dialog implementations (Windows/macOS/Linux)
4. Changes to the browser-side `agent-discovery.js` pairing flow

The current mitigations (256-bit challenge entropy, 120-second expiry, 3 req/min rate limit, constant-time comparison) make brute-force impractical. The real risk is a malicious local process — which already has access to the filesystem anyway. This should be addressed before any multi-user or shared-machine deployment.

### C6 — `LocalAgentFileProvider` not wired into production grading pipeline

**What:** The `create_file_provider("local", ...)` factory and `LocalAgentFileProvider` HTTP client exist in the main AEMS codebase (`file_provider.py`) and are tested, but no production grading endpoint calls them. The storage mode resolves to `"local"` correctly, but the result is never consumed.

**Why deferred:** This is in the main AEMS codebase (`D:\Temp\Artem\aems\`), not the agent repo. It requires changes to the grading workflow files in `src/aems/web/api/v1/canvas/` to route through the file provider abstraction. Out of scope for this agent-focused review.

### I3 — CORS `allow_origin_regex` matches any localhost port

**What:** The regex `^https://(localhost|127\.0\.0\.1)(:\d+)?$` allows any HTTPS localhost port. Any locally-served HTTPS page could make cross-origin requests to the agent.

**Why deferred:** This is a documented, intentional design choice (see code comment: "developer/self-hosted setups where the web app may run on ports other than 8080"). The regex only matches HTTPS (which requires a TLS certificate to serve locally), limiting the practical attack surface. The `allowed_origins` + `paired_origins` explicit lists provide the primary access control; the regex is a convenience fallback. Narrowing to specific ports would break legitimate developer setups. If the threat model changes (e.g., multi-tenant deployment), this should be revisited.

### I5 — `snapshot()` reads shared fields without asyncio lock

**What:** `LicenseEnforcementController.snapshot()` is a synchronous method that reads `_limited_mode_active`, `_last_result`, and `_last_checked_at` without acquiring the `_lock` that protects writes in `_apply_locked()`.

**Why deferred:** Not a real bug in Python's asyncio model. `snapshot()` is synchronous — it contains no `await` points, so it executes atomically within a single event loop turn. The three field reads cannot be interleaved with `_apply_locked()` writes because asyncio is cooperatively scheduled. The lock only matters across `await` boundaries. A fix (making `snapshot()` async or using atomic state swap) would add complexity without preventing any actual race.

### I9 — macOS config dir uses `~/.config` instead of `~/Library/Application Support`

**What:** On macOS, `get_config_dir()` returns `~/.config/aems/agent/` (Linux XDG convention) instead of `~/Library/Application Support/AEMS/agent/` (macOS convention).

**Why deferred:** Changing this would break existing macOS installations — users would lose their stored config, auth token, and license token. Requires a migration path (detect old location, copy files, update references). The current behavior is documented in the module docstring and is functionally correct, just unconventional. Should be addressed in a major version bump with a migration script.

### I15 — No version negotiation between agent and main AEMS

**What:** The browser probes `/status` and checks `data.service === "aems-agent"` but does not enforce a minimum agent version. `LocalAgentFileProvider` in the main AEMS codebase makes HTTP calls with no version compatibility check. Breaking API changes would fail silently (returning `None` from failed calls).

**Why deferred:** The agent is at v0.2.1 with no published releases yet. Version negotiation adds protocol complexity that isn't justified until there are actual users running older versions. When the first breaking change is needed, add a `min_version` check in `agent-discovery.js` probe response handling and a version header in `LocalAgentFileProvider` requests. The publishing checklist in the extraction doc is entirely unchecked — this is pre-release software where the API is still stabilizing.

### Test gaps not addressed

The following test gaps were identified but not added in this review to keep the changeset focused:

- **CLI commands** (`token`, `set-path`, `config-dir`, `license-store`, `license-check`): Zero test coverage. These are thin wrappers around config functions that are themselves well-tested. Should be added before release.
- **`_normalize_origin` edge cases** (`ftp://`, empty string, `javascript:` scheme): The function correctly rejects these (verified by code reading) but unit tests would document the contract.
- **503 paths** (storage not configured, storage path missing): Both `_get_storage_path()` branches are untested via HTTP. Should be added with a `no-storage` fixture variant.
- **413 upload size limit**: Not tested because sending 200 MB in a test is impractical. Could be tested by monkeypatching `_MAX_UPLOAD_BYTES`.
- **JWKS offline cache fallback** and **JWKS fetch failure with no cache**: License validation edge cases that require complex fixture setup.
- **`get_config_dir` cross-platform branches**: Only the current platform is exercised. Would need `monkeypatch` on `platform.system()`.

"""Packaging regressions that broke the 0.4.8 release artifacts."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 backport path
    import tomli as tomllib


def test_pyproject_explicitly_scopes_sdist_contents() -> None:
    """The sdist must opt in to source files so release artifacts stay out."""
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    sdist_config = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]
    only_include = sdist_config["only-include"]

    assert "src" in only_include
    assert "artifacts" not in only_include


def test_release_workflow_builds_python_dist_before_downloading_binary_artifacts() -> None:
    """Release CI must build the sdist before binary artifacts enter the checkout."""
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    assert workflow.index("name: Build wheel and sdist") < workflow.index(
        "name: Download all artifacts"
    )


def test_release_step_is_idempotent_for_retagged_builds() -> None:
    """The 'Create or update Release' step must tolerate a pre-existing
    release for the same tag.

    Real scenario from the v0.4.31 release: the tag was first pushed on
    the v0.4.30 commit, then re-pushed on the actual v0.4.31 commit.
    The second build's release step ran ``gh release create`` against
    a tag that already had a GitHub Release object, the call failed,
    and the second build's correctly-versioned binaries never replaced
    the first build's wrong-versioned ones. Pin the idempotency so a
    well-meaning simplification can't silently reopen that gap.
    """
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    # Strip YAML/shell comment lines so the explanatory block at the top
    # of the run: heredoc can't false-positive the substring checks.
    code_lines = [line for line in workflow.splitlines() if not line.lstrip().startswith("#")]
    code_only = "\n".join(code_lines)

    # The view-probe must exist and be wired as an `if` condition.
    assert (
        "if gh release view " in code_only
    ), "release step must probe for an existing release via `if gh release view`"
    # The clobber-upload branch must exist.
    assert (
        "gh release upload " in code_only and "--clobber" in code_only
    ), "the 'already exists' branch must overwrite assets with `gh release upload ... --clobber`"
    # The create branch must still exist for the first-time case.
    assert (
        "gh release create " in code_only
    ), "the 'first-time' branch must still call `gh release create`"
    # The view-probe must precede both create and upload calls.
    view_idx = code_only.index("if gh release view ")
    create_idx = code_only.index("gh release create ")
    upload_idx = code_only.index("gh release upload ")
    assert view_idx < create_idx, "`if gh release view` must gate `gh release create`"
    assert view_idx < upload_idx, "`if gh release view` must gate `gh release upload`"


def test_windows_workflow_prefers_signpath_over_legacy_pfx_signing() -> None:
    """Public OSS releases should use SignPath before the legacy PFX fallback.

    New public code-signing certificates are no longer delivered as
    exportable PFX blobs for GitHub-hosted CI. The modern path for this
    AGPL public repository is SignPath Foundation / SignPath.io, with the
    old WIN_CODESIGN_CERT_* inputs kept only as a compatibility fallback
    for pre-2023 or otherwise exportable private certificates.
    """
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "SIGNPATH_API_TOKEN" in workflow
    assert "SIGNPATH_ORGANIZATION_ID" in workflow
    assert "Resolve Windows signing method" in workflow
    assert "Upload unsigned Windows installer for SignPath" in workflow
    assert "Submit Windows signing request (SignPath)" in workflow
    assert "Sign Windows installer (Authenticode legacy PFX fallback)" in workflow

    resolve_idx = workflow.index("Resolve Windows signing method")
    signpath_upload_idx = workflow.index("Upload unsigned Windows installer for SignPath")
    signpath_submit_idx = workflow.index("Submit Windows signing request (SignPath)")
    legacy_pfx_idx = workflow.index("Sign Windows installer (Authenticode legacy PFX fallback)")

    assert resolve_idx < signpath_upload_idx < signpath_submit_idx < legacy_pfx_idx


def test_signpath_artifact_configuration_signs_the_windows_installer() -> None:
    """SignPath config must match the uploaded Windows installer artifact.

    actions/upload-artifact wraps the installer in a zip archive on the
    GitHub server, so the SignPath artifact configuration must declare a
    zip root and then target the root-level `aems-agent-setup.exe` file
    for Authenticode signing.
    """
    cfg_path = (
        Path(__file__).resolve().parents[1]
        / ".signpath"
        / "artifact-configurations"
        / "windows-installer.xml"
    )
    xml_text = cfg_path.read_text(encoding="utf-8")
    root = ET.fromstring(xml_text)
    ns = {"sp": "http://signpath.io/artifact-configuration/v1"}

    zip_file = root.find("sp:zip-file", ns)
    assert zip_file is not None
    pe_file = zip_file.find("sp:pe-file", ns)
    assert pe_file is not None
    assert pe_file.get("path") == "aems-agent-setup.exe"
    assert pe_file.find("sp:authenticode-sign", ns) is not None


def test_workflow_relies_on_pyinstaller_signing_when_developer_id_absent() -> None:
    """Without an Apple Developer ID the workflow must verify PyInstaller's signature, not re-sign.

    Apple's TN2206 warns against modifying a signed target after the
    fact. PyInstaller >=6.20's BUNDLE directive already ad-hoc signs
    every collected binary AND the .app wrapper on macOS. CI's job is
    therefore just to verify the existing signature, not redo it.

    The earlier v0.4.11/v0.4.12 attempts to redo signing in CI all
    failed with "bundle format unrecognized, invalid, or unsuitable"
    because the manual flat-bundle layout pre-BUNDLE put PyInstaller
    onedir content under Contents/MacOS where codesign treats it as
    nested-code territory. Regression-guard that we never go back.
    """
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "Verify PyInstaller ad-hoc signing (no Developer ID)" in workflow
    assert 'codesign --verify --deep --strict --verbose=2 "$APP"' in workflow
    warning_line = next(
        line for line in workflow.splitlines() if "App will ship ad-hoc signed;" in line
    )
    assert "right-click" not in warning_line, (
        "the headline no-Developer-ID warning must not tell Sequoia users to "
        "right-click -> Open; the documented path is now double-click, then "
        "System Settings -> Privacy & Security -> Open Anyway."
    )
    # Must NOT re-sign in the ad-hoc path — see TN2206 + the
    # v0.4.11..v0.4.12 CI history above.
    ad_hoc_block = workflow.split("Verify PyInstaller ad-hoc signing (no Developer ID)")[1].split(
        "- name:"
    )[0]
    assert "codesign --force --sign -" not in ad_hoc_block, (
        "ad-hoc path must not invoke `codesign --force --sign -`; "
        "PyInstaller already signed the bundle, re-signing reintroduces "
        "the flat-bundle codesign failures from v0.4.11."
    )

    # Developer ID path must still re-sign with the real identity, and
    # the DMG must be built AFTER the .app has been signed so the .app
    # inside the DMG is signed too.
    assert "Sign macOS app bundle (Developer ID)" in workflow
    assert "Build DMG from signed .app" in workflow

    sign_app_idx = workflow.index("Sign macOS app bundle (Developer ID)")
    verify_idx = workflow.index("Verify PyInstaller ad-hoc signing (no Developer ID)")
    dmg_build_idx = workflow.index("Build DMG from signed .app")
    dmg_sign_idx = workflow.index("Sign and notarize macOS DMG (Developer ID)")

    assert sign_app_idx < dmg_build_idx
    assert verify_idx < dmg_build_idx
    assert dmg_build_idx < dmg_sign_idx

    dmg_build_block = workflow.split("Build DMG from signed .app")[1].split("- name:")[0]
    assert '-srcfolder "dist/AEMS Agent.app"' not in dmg_build_block, (
        "the workflow must not build the DMG directly from the .app bundle; "
        "that drops com.aems.agent.plist and the /Applications symlink from "
        "the mounted DMG root."
    )
    assert "build.build_macos_dmg" in dmg_build_block, (
        "CI should reuse packaging/build.py's DMG staging helper so the "
        "release workflow and local macOS builds emit the same DMG layout."
    )


def test_launcher_defaults_finder_double_click_to_run_tray(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frozen macOS .app launched from Finder must default argv to `run --tray`.

    The Apple tester on v0.4.13–v0.4.15 saw "double-click does nothing"
    because the .app bundle declares ``LSUIElement=True`` and the
    PyInstaller entry script (``packaging/launcher.py``) called
    ``aems_agent.cli.main()`` with the raw argv. Typer requires a
    subcommand, so the process exited immediately with "Missing
    command." — no window, no tray icon, no visible feedback. This
    regression-guards that the launcher rewrites no-arg Finder
    invocations into ``run --tray`` on darwin only.
    """
    import importlib
    import sys as _sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
    launcher = importlib.import_module("launcher")

    # Finder double-click on darwin: frozen, darwin, no args.
    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_sys, "argv", ["/Applications/AEMS Agent.app/Contents/MacOS/aems-agent"])
    assert launcher._is_frozen_macos_finder_launch() is True

    # Terminal invocation with subcommand: leave argv alone.
    monkeypatch.setattr(_sys, "argv", ["aems-agent", "run", "--tray"])
    assert launcher._is_frozen_macos_finder_launch() is False

    # CLI with --version (the CI smoke test): also has length > 1, do not rewrite.
    monkeypatch.setattr(_sys, "argv", ["aems-agent", "--version"])
    assert launcher._is_frozen_macos_finder_launch() is False

    # Non-frozen invocation (dev mode): do not rewrite.
    monkeypatch.setattr(_sys, "frozen", False, raising=False)
    monkeypatch.setattr(_sys, "argv", ["launcher"])
    assert launcher._is_frozen_macos_finder_launch() is False

    # Linux/Windows frozen no-arg invocation: do not rewrite (Windows
    # NSIS installer / Linux launchers handle CLI args explicitly).
    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_sys, "argv", ["aems-agent"])
    assert launcher._is_frozen_macos_finder_launch() is False
    monkeypatch.setattr(launcher.platform, "system", lambda: "Windows")
    assert launcher._is_frozen_macos_finder_launch() is False


def test_launcher_handles_aems_agent_url_scheme_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frozen macOS launch carrying an ``aems-agent://`` URL boots run --tray.

    The web app's "Launch agent" button fires ``aems-agent://launch`` to ask
    macOS to start the installed agent. With ``argv_emulation=False`` the URL
    arrives as an Apple Event (bare argv -> handled by the Finder-launch
    branch). This guards the belt-and-suspenders path where the URL lands in
    argv instead: ``_macos_launch_uri_arg`` must detect it so the launcher can
    boot the tray instead of passing the URL to Typer as a bogus subcommand.
    """
    import importlib
    import sys as _sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
    launcher = importlib.import_module("launcher")

    # Frozen darwin launch with the custom-scheme URL in argv.
    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        _sys,
        "argv",
        ["/Applications/AEMS Agent.app/Contents/MacOS/aems-agent", "aems-agent://launch"],
    )
    assert launcher._macos_launch_uri_arg() == "aems-agent://launch"

    # No URL present -> nothing to extract.
    monkeypatch.setattr(_sys, "argv", ["aems-agent", "run", "--tray"])
    assert launcher._macos_launch_uri_arg() is None

    # Non-darwin frozen launch with a URL: not our concern (Windows registers
    # the scheme via the registry and passes the URL to `run` explicitly).
    monkeypatch.setattr(launcher.platform, "system", lambda: "Windows")
    monkeypatch.setattr(_sys, "argv", ["aems-agent.exe", "aems-agent://launch"])
    assert launcher._macos_launch_uri_arg() is None


def test_macos_bundle_registers_aems_agent_url_scheme() -> None:
    """The PyInstaller spec must declare CFBundleURLTypes for aems-agent://.

    Without this key macOS Launch Services has no handler for the scheme, so
    the web "Launch agent" button (which fires aems-agent://launch) is a
    silent no-op on macOS. Guards against the key being dropped on a spec edit.
    """
    spec = (Path(__file__).resolve().parents[1] / "packaging" / "aems-agent.spec").read_text(
        encoding="utf-8"
    )
    assert "CFBundleURLTypes" in spec
    assert "CFBundleURLSchemes" in spec
    assert "'aems-agent'" in spec or '"aems-agent"' in spec


def test_workflow_smoke_tests_frozen_macos_app_before_signing() -> None:
    """The macOS release job should execute the frozen app before signing/DMG steps.

    This catches missing hidden imports, broken ``ctypes`` loads, and
    relocated ``Contents/Frameworks`` issues while we still have the
    unpacked `.app` in the workspace.
    """
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "Smoke-test frozen .app (--version)" in workflow
    assert '"dist/AEMS Agent.app/Contents/MacOS/aems-agent" --version' in workflow

    build_idx = workflow.index("Build .app bundle with PyInstaller (DMG deferred)")
    smoke_idx = workflow.index("Smoke-test frozen .app (--version)")
    sign_idx = workflow.index("Sign macOS app bundle (Developer ID)")
    verify_idx = workflow.index("Verify PyInstaller ad-hoc signing (no Developer ID)")

    assert build_idx < smoke_idx < sign_idx
    assert build_idx < smoke_idx < verify_idx


def test_macos_spec_uses_bundle_directive_with_brand_metadata() -> None:
    """The macOS spec must emit a proper .app via BUNDLE(..., info_plist=...).

    Pre-0.4.x ad-hoc CI signing failures stemmed from manually
    assembling a flat .app under Contents/MacOS/_internal/, which
    codesign refuses to walk reliably. PyInstaller's BUNDLE directive
    relocates libraries to Contents/Frameworks/ and data to
    Contents/Resources/ (the layout codesign actually wants) AND it
    ad-hoc signs the result automatically. If a future edit drops
    BUNDLE we are back to v0.4.11 territory.
    """
    spec_path = Path(__file__).resolve().parents[1] / "packaging" / "aems-agent.spec"
    spec_text = spec_path.read_text(encoding="utf-8")

    assert "BUNDLE(" in spec_text, "spec must call BUNDLE on macOS"
    assert "sys.platform == 'darwin'" in spec_text
    assert "bundle_identifier='com.aems.agent'" in spec_text
    assert "'CFBundleIconFile': 'aems-agent.icns'" in spec_text
    assert "'NSHighResolutionCapable': True" in spec_text
    assert "'LSUIElement': True" in spec_text
    # LSUIElement (agent app, no Dock icon) is correct for a menu-bar
    # app; LSBackgroundOnly (pure background daemon) is NOT — it forbids
    # the WindowServer from showing any UI, including the pystray
    # NSStatusBar item. Setting both suppressed the tray icon so a Finder
    # double-click ran `run --tray` but the user saw nothing in the menu
    # bar, reproducing the "double-click does nothing" symptom v0.4.16
    # tried to fix. Guard against the conflict reopening.
    assert "'LSBackgroundOnly'" not in spec_text, (
        "the macOS bundle must NOT declare LSBackgroundOnly — it "
        "suppresses the menu-bar tray icon. Use LSUIElement alone."
    )
    assert "ensure_macos_icns" in spec_text, (
        "the spec must render the multi-res .icns at build time so "
        "BUNDLE(icon=...) ships a real branded icon (not Finder's "
        "default blank-document glyph the Apple tester reported)."
    )


def test_pyinstaller_pin_is_at_least_6_20() -> None:
    """PyInstaller's macOS bundle relocation needs >=6.11; pin >=6.20 for safety.

    The Contents/Frameworks/ vs Contents/MacOS/ split that makes
    codesign happy is in PyInstaller's 6.11+ macOS-bundle redesign.
    A regression that loosens the pin below 6.20 would re-introduce
    the flat-bundle failure mode v0.4.11 shipped with.
    """
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    build_extras = pyproject["project"]["optional-dependencies"]["build"]
    pin = next((s for s in build_extras if s.startswith("pyinstaller")), None)
    assert pin is not None, "build extras must include a pyinstaller pin"
    assert ">=6.20" in pin, f"pyinstaller pin must be >=6.20, got: {pin!r}"


def test_build_macos_dmg_locates_pyinstaller_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build.build_macos_dmg must consume the BUNDLE-produced .app, not re-assemble.

    Manually re-assembling a .app from the onedir output was the
    architectural root cause of the v0.4.11/v0.4.12 codesign failures.
    Regression-guard that the helper only locates an existing .app
    and never copies onedir files into Contents/MacOS again.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
    build = importlib.import_module("build")

    monkeypatch.setattr(build, "DIST_DIR", tmp_path / "dist")

    # Without a pre-built .app the helper must raise; this catches a
    # workflow that forgets the AEMS_AGENT_SKIP_DMG ordering.
    with pytest.raises(FileNotFoundError):
        build._locate_pyinstaller_macos_bundle()

    # With a pre-built .app the helper just returns its path.
    (tmp_path / "dist" / "AEMS Agent.app" / "Contents" / "MacOS").mkdir(parents=True)
    found = build._locate_pyinstaller_macos_bundle()
    assert found == tmp_path / "dist" / "AEMS Agent.app"

    # `_write_macos_app_bundle` must NOT exist any more — we deleted
    # the manual-assembly path. If a future maintainer restores it,
    # this test fires before they can ship another broken signature.
    assert not hasattr(build, "_write_macos_app_bundle"), (
        "the manual flat-bundle assembly path was removed in favor of "
        "PyInstaller's BUNDLE directive; re-introducing it brings back "
        "the v0.4.11 codesign failures."
    )


def test_build_macos_dmg_honors_skip_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AEMS_AGENT_SKIP_DMG=1 still lets CI defer DMG creation."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
    build = importlib.import_module("build")

    # Pre-create the .app PyInstaller would have produced via BUNDLE.
    (tmp_path / "dist" / "AEMS Agent.app" / "Contents" / "MacOS").mkdir(parents=True)

    monkeypatch.setattr(build, "DIST_DIR", tmp_path / "dist")
    monkeypatch.setenv("AEMS_AGENT_SKIP_DMG", "1")
    monkeypatch.setattr(build.shutil, "which", lambda _name: "/usr/bin/hdiutil")
    calls: list[list[str]] = []
    monkeypatch.setattr(build, "run", lambda cmd, cwd=None: calls.append(list(cmd)))

    build.build_macos_dmg(tmp_path / "pyinstaller-out")  # dist_path unused on macOS now

    assert all("hdiutil" not in c[0] for c in calls), "hdiutil should not have been invoked"
    assert not (tmp_path / "dist" / "AEMS-Agent.dmg").exists()


def test_build_macos_dmg_stages_app_launch_helper_and_applications_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DMG staging folder must include the app, helper, and /Applications link.

    The release docs tell users the DMG contains ``AEMS Agent.app``,
    ``com.aems.agent.plist``, the first-launch quarantine helper, and the
    ``/Applications`` alias. Regression-guard the drag-to-Applications
    staging layout.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
    build = importlib.import_module("build")

    (tmp_path / "dist" / "AEMS Agent.app" / "Contents" / "MacOS").mkdir(parents=True)

    monkeypatch.setattr(build, "DIST_DIR", tmp_path / "dist")
    monkeypatch.setattr(build, "BUILD_DIR", tmp_path / "build")
    monkeypatch.delenv("AEMS_AGENT_SKIP_DMG", raising=False)
    monkeypatch.setattr(build.shutil, "which", lambda _name: "/usr/bin/hdiutil")
    symlink_calls: list[tuple[str, Path]] = []

    def _fake_symlink(target: str, link_name: Path, target_is_directory: bool = False) -> None:
        _ = target_is_directory
        symlink_calls.append((target, link_name))

    monkeypatch.setattr(build.os, "symlink", _fake_symlink)
    monkeypatch.setattr(build, "_create_dmg_with_volume_icon", lambda *_args: False)
    calls: list[list[str]] = []
    monkeypatch.setattr(build, "run", lambda cmd, cwd=None: calls.append(list(cmd)))

    build.build_macos_dmg(tmp_path / "pyinstaller-out")

    hdiutil_call = next(c for c in calls if c[0] == "hdiutil")
    srcfolder = Path(hdiutil_call[hdiutil_call.index("-srcfolder") + 1])
    app_dir = tmp_path / "dist" / "AEMS Agent.app"

    assert srcfolder != app_dir, "DMG should be built from a staging folder, not the .app alone"
    assert (srcfolder / "AEMS Agent.app").is_dir()
    assert (srcfolder / "com.aems.agent.plist").is_file()
    helper = srcfolder / "Open AEMS Agent (first launch).command"
    assert helper.is_file()
    helper_text = helper.read_text(encoding="utf-8")
    assert "xattr -dr com.apple.quarantine" in helper_text
    assert 'open "$APP"' in helper_text
    assert symlink_calls == [("/Applications", srcfolder / "Applications")]

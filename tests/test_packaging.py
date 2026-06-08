"""Packaging regressions that broke the 0.4.8 release artifacts."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

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


def test_build_macos_dmg_stages_app_launch_agent_and_applications_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DMG staging folder must include the app, LaunchAgent, and /Applications link.

    The release docs tell users the DMG contains both ``AEMS Agent.app`` and
    ``com.aems.agent.plist``. Building the DMG directly from the ``.app``
    bundle drops the plist on the floor and forces users to hunt for
    ``/Applications`` in a second Finder window. Regression-guard the
    drag-to-Applications staging layout.
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
    calls: list[list[str]] = []
    monkeypatch.setattr(build, "run", lambda cmd, cwd=None: calls.append(list(cmd)))

    build.build_macos_dmg(tmp_path / "pyinstaller-out")

    hdiutil_call = next(c for c in calls if c[0] == "hdiutil")
    srcfolder = Path(hdiutil_call[hdiutil_call.index("-srcfolder") + 1])
    app_dir = tmp_path / "dist" / "AEMS Agent.app"

    assert srcfolder != app_dir, "DMG should be built from a staging folder, not the .app alone"
    assert (srcfolder / "AEMS Agent.app").is_dir()
    assert (srcfolder / "com.aems.agent.plist").is_file()
    assert symlink_calls == [("/Applications", srcfolder / "Applications")]

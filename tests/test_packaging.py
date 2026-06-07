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


def test_workflow_ad_hoc_signs_when_developer_id_absent() -> None:
    """The macOS job must produce a code-signed .app even without Apple Developer ID.

    macOS Big Sur+ refuses to launch fully unsigned binaries
    ("damaged"), which is the exact symptom the Apple tester
    reported. The fallback is an ad-hoc ``codesign --sign -`` that
    flips Gatekeeper from "damaged" to the right-click-Open prompt.
    Regression-guard that the workflow keeps both the Developer ID
    path AND the ad-hoc fallback, and that the DMG is built AFTER
    the .app has been signed (otherwise the .app inside the DMG is
    unsigned even when secrets are present).
    """
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "Ad-hoc sign macOS app bundle (no Developer ID)" in workflow
    # Ad-hoc path signs the bundle wrapper without --deep because
    # PyInstaller dist-info dirs in _internal/ break codesign --deep.
    assert 'codesign --force --sign - --timestamp=none "$APP"' in workflow
    assert 'codesign --force --sign - --timestamp=none "$APP/Contents/MacOS/aems-agent"' in workflow
    # The find loop must skip *.framework/, *.dist-info/, *.egg-info/
    # paths — codesign cannot re-sign PyInstaller's pre-signed
    # Python.framework ("bundle format is ambiguous"), and pip metadata
    # dirs have no executables anyway. If a future edit re-introduces
    # framework signing, the macOS job will fail again the same way
    # v0.4.12 did.
    assert "! -path '*/*.framework/*'" in workflow
    assert "! -path '*/*.dist-info/*'" in workflow
    assert "Sign macOS app bundle (Developer ID)" in workflow
    assert "Build DMG from signed .app" in workflow
    assert "AEMS_AGENT_SKIP_DMG" in workflow

    sign_app_idx = workflow.index("Sign macOS app bundle (Developer ID)")
    ad_hoc_idx = workflow.index("Ad-hoc sign macOS app bundle (no Developer ID)")
    dmg_build_idx = workflow.index("Build DMG from signed .app")
    dmg_sign_idx = workflow.index("Sign and notarize macOS DMG (Developer ID)")

    # Both signing paths must run before the DMG is built, and the DMG
    # codesign+notarize must run after the DMG has been built.
    assert sign_app_idx < dmg_build_idx
    assert ad_hoc_idx < dmg_build_idx
    assert dmg_build_idx < dmg_sign_idx


def test_macos_app_bundle_has_icon_and_high_dpi_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS .app must ship a real icon, an Info.plist that references it, and retina opt-in.

    Pre-0.4.x bundles shipped without any Resources/aems-agent.icns or
    CFBundleIconFile, which is why the Apple tester saw a blank
    document icon in Finder. Regression-guard the three keys that
    together produce a branded app icon.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
    build = importlib.import_module("build")

    # Synthesize a fake PyInstaller onedir output: an executable file
    # plus a sibling _internal dir of bundled binaries.
    fake_dist = tmp_path / "pyinstaller-out"
    fake_dist.mkdir()
    (fake_dist / "aems-agent").write_bytes(b"#!/bin/sh\nexit 0\n")
    (fake_dist / "_internal").mkdir()
    (fake_dist / "_internal" / "lib.dylib").write_bytes(b"\x00")

    # Redirect DIST_DIR so we don't touch the real one in the repo.
    monkeypatch.setattr(build, "DIST_DIR", tmp_path / "dist")

    app_dir = build._write_macos_app_bundle(fake_dist)

    assert app_dir.exists() and app_dir.is_dir()
    icns = app_dir / "Contents" / "Resources" / "aems-agent.icns"
    assert icns.exists(), "Resources/aems-agent.icns missing"
    assert icns.read_bytes()[:4] == b"icns", "icns file lacks Apple IconFamily magic"

    plist = (app_dir / "Contents" / "Info.plist").read_text()
    assert "<key>CFBundleIconFile</key><string>aems-agent</string>" in plist
    assert "<key>NSHighResolutionCapable</key><true/>" in plist
    assert "<key>CFBundleExecutable</key><string>aems-agent</string>" in plist


def test_build_macos_dmg_honors_skip_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AEMS_AGENT_SKIP_DMG=1 lets CI interpose a codesign step before DMG creation."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
    build = importlib.import_module("build")

    fake_dist = tmp_path / "pyinstaller-out"
    fake_dist.mkdir()
    (fake_dist / "aems-agent").write_bytes(b"#!/bin/sh\nexit 0\n")

    monkeypatch.setattr(build, "DIST_DIR", tmp_path / "dist")
    monkeypatch.setenv("AEMS_AGENT_SKIP_DMG", "1")
    # Even if hdiutil is somehow present on the runner, the env flag wins.
    monkeypatch.setattr(build.shutil, "which", lambda _name: "/usr/bin/hdiutil")
    calls: list[list[str]] = []
    monkeypatch.setattr(build, "run", lambda cmd, cwd=None: calls.append(list(cmd)))

    build.build_macos_dmg(fake_dist)

    assert all("hdiutil" not in c[0] for c in calls), "hdiutil should not have been invoked"
    assert not (tmp_path / "dist" / "AEMS-Agent.dmg").exists()

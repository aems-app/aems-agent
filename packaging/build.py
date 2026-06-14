#!/usr/bin/env python3
"""
Cross-platform build script for the AEMS Local Bridge Agent.

Usage:
    python packaging/build.py [--platform windows|macos|linux]

Outputs:
    dist/aems-agent/     — PyInstaller output
    dist/aems-agent-*    — Platform-specific installer
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGING_DIR = PROJECT_ROOT / "packaging"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"


def ensure_windows_icon_asset() -> Path:
    """Generate the packaged Windows icon before building."""
    src_dir = PROJECT_ROOT / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    from aems_agent.icons import ensure_windows_icon

    return ensure_windows_icon(PACKAGING_DIR / "icon.ico")


def run(cmd: List[str], cwd: Optional[Path] = None) -> None:
    """Run a command and raise on failure."""
    print(f"  > {' '.join(str(c) for c in cmd)}")
    subprocess.check_call(cmd, cwd=str(cwd or PROJECT_ROOT))


def _read_pyproject_version() -> str:
    """Return the project version from pyproject.toml (best-effort)."""
    import tomllib

    pyproject_path = PROJECT_ROOT / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    return str(data.get("project", {}).get("version", "0.0.0"))


def _write_version_file() -> None:
    """Drop the resolved pyproject version into the package source.

    PyInstaller bundles loose the dist-info metadata, so config.py reads
    this file as a fallback when ``importlib.metadata.version`` fails.
    """
    version = _read_pyproject_version()
    version_file = PROJECT_ROOT / "src" / "aems_agent" / "_version.txt"
    version_file.write_text(version, encoding="utf-8")
    print(f"  Wrote {version_file} -> {version}")


def build_pyinstaller() -> Path:
    """Run PyInstaller to create the agent distribution."""
    spec_file = PACKAGING_DIR / "aems-agent.spec"
    if not spec_file.exists():
        raise FileNotFoundError(f"Spec file not found: {spec_file}")

    _write_version_file()
    ensure_windows_icon_asset()

    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            str(spec_file),
        ]
    )

    output = DIST_DIR / "aems-agent"
    if not output.exists():
        raise RuntimeError(f"PyInstaller output not found at {output}")

    return output


def _ship_windows_install_script(dist_path: Path) -> None:
    """Copy install.ps1 into the dist tree.

    Ships the fallback installer alongside the portable PyInstaller bundle
    so users (and devs running ``packaging/build.py`` locally without NSIS)
    get a kill-then-replace flow that mirrors what
    ``packaging/windows/installer.nsi`` does for ``aems-agent-setup.exe``.
    Without this, hand-extracting the bundle and double-clicking
    aems-agent.exe over a running tray trips
    ``_preflight_port_or_die``'s "already running" dialog.
    """
    src = PACKAGING_DIR / "windows" / "install.ps1"
    if not src.exists():
        return
    dest = dist_path / "install.ps1"
    shutil.copy2(src, dest)
    print(f"  Windows install script: {dest}")


def build_windows_installer(dist_path: Path) -> Path:
    """Build NSIS installer for Windows."""
    _ship_windows_install_script(dist_path)

    nsi_file = PACKAGING_DIR / "windows" / "installer.nsi"
    if not nsi_file.exists():
        print("  [SKIP] NSIS script not found, skipping Windows installer")
        return dist_path

    # Check if NSIS is available
    nsis_path = shutil.which("makensis")
    if not nsis_path:
        print("  [SKIP] makensis not found in PATH")
        return dist_path

    version = _read_pyproject_version()

    run(
        [
            nsis_path,
            f"/DDIST_DIR={dist_path}",
            f"/DOUTPUT_DIR={DIST_DIR}",
            f"/DAGENT_VERSION={version}",
            str(nsi_file),
        ]
    )

    installer = DIST_DIR / "aems-agent-setup.exe"
    if installer.exists():
        print(f"  Windows installer: {installer}")
    return installer


def _locate_pyinstaller_macos_bundle() -> Path:
    """Return the path to the .app PyInstaller produced via BUNDLE.

    The PyInstaller spec's ``BUNDLE(coll, name="AEMS Agent.app", ...)``
    step emits a proper Apple-conformant .app bundle: the launcher in
    ``Contents/MacOS/``, shared libraries and frameworks relocated to
    ``Contents/Frameworks/``, data in ``Contents/Resources/``. This is
    the layout ``codesign`` expects, and PyInstaller already ad-hoc
    signs the collected binaries plus the .app wrapper for us.

    Earlier releases manually assembled a flat .app from the onedir
    output (everything under ``Contents/MacOS/_internal/``); that layout
    refused to codesign cleanly because pip metadata directories
    (``*.dist-info``), the Python stdlib tree, and PyInstaller's
    embedded ``Python.framework`` all sit in a location codesign
    interprets as nested-code territory. We now let PyInstaller own
    the bundle so the codesigning rules Apple documents (TN2206) line
    up with reality.
    """
    app_name = "AEMS Agent"
    app_dir = DIST_DIR / f"{app_name}.app"
    if not app_dir.exists():
        raise FileNotFoundError(
            f"PyInstaller .app bundle not found at {app_dir}. The spec "
            'must include a BUNDLE(coll, name="AEMS Agent.app", ...) '
            "step on darwin — see packaging/aems-agent.spec."
        )
    return app_dir


def _macos_launch_agent_plist() -> str:
    """Return the bundled macOS LaunchAgent plist payload."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.aems.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Applications/AEMS Agent.app/Contents/MacOS/aems-agent</string>
        <string>run</string>
        <string>--tray</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>"""


def _write_macos_launch_agent(path: Path) -> Path:
    """Write the macOS LaunchAgent plist and return the output path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_macos_launch_agent_plist(), encoding="utf-8")
    return path


def _prepare_macos_dmg_staging_dir(app_dir: Path) -> Path:
    """Stage the DMG contents next to the build output.

    The DMG should contain the signed app bundle, the optional LaunchAgent
    plist, and an ``/Applications`` symlink so Finder supports the
    drag-to-Applications install convention without forcing users to open
    a second window.
    """
    stage_dir = BUILD_DIR / "macos-dmg-stage"
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    staged_app_dir = stage_dir / app_dir.name
    shutil.copytree(app_dir, staged_app_dir, symlinks=True)
    _write_macos_launch_agent(stage_dir / "com.aems.agent.plist")

    applications_link = stage_dir / "Applications"
    if applications_link.exists() or applications_link.is_symlink():
        applications_link.unlink()
    os.symlink("/Applications", applications_link, target_is_directory=True)
    return stage_dir


def build_macos_dmg(dist_path: Path) -> Path:
    """Build the macOS DMG around the PyInstaller-produced .app bundle.

    Skips DMG creation when ``AEMS_AGENT_SKIP_DMG=1`` is set — kept for
    backward compatibility with CI knobs, though the workflow no
    longer needs to interpose a codesign step (PyInstaller has already
    done it).

    The ``dist_path`` argument is the onedir output and is unused on
    macOS now; the function reads the BUNDLE-produced .app instead.
    Kept in the signature so the cross-platform dispatcher in
    ``main()`` remains uniform.
    """
    _ = dist_path  # retained for signature compatibility; unused here
    app_name = "AEMS Agent"
    dmg_path = DIST_DIR / "AEMS-Agent.dmg"
    app_dir = _locate_pyinstaller_macos_bundle()
    _write_macos_launch_agent(DIST_DIR / "com.aems.agent.plist")

    if os.environ.get("AEMS_AGENT_SKIP_DMG") == "1":
        print("  AEMS_AGENT_SKIP_DMG=1 — skipping hdiutil DMG creation")
    elif shutil.which("hdiutil"):
        stage_dir = _prepare_macos_dmg_staging_dir(app_dir)
        if dmg_path.exists():
            dmg_path.unlink()
        run(
            [
                "hdiutil",
                "create",
                "-volname",
                app_name,
                "-srcfolder",
                str(stage_dir),
                "-ov",
                str(dmg_path),
            ]
        )
        print(f"  macOS DMG: {dmg_path}")

    return dmg_path


def build_linux_packages(dist_path: Path) -> Path:
    """Build Linux installer artefacts: .desktop, systemd unit, and tar.gz.

    The README advertises ``aems-agent-linux-<arch>.tar.gz`` on the Releases page; this
    function now actually produces it. The tarball contains the PyInstaller
    ``onedir`` output plus the desktop/service unit files and a small
    ``install.sh`` that wires the agent into ``~/.local/share/aems-agent`` and
    the user-systemd path. End users:

        tar xzf aems-agent-linux-<arch>.tar.gz
        cd aems-agent-linux
        ./install.sh        # installs to ~/.local/share/aems-agent
        aems-agent run --tray
                            # or: systemctl --user enable --now aems-agent.service
    """
    linux_pkg_dir = PACKAGING_DIR / "linux"
    linux_pkg_dir.mkdir(parents=True, exist_ok=True)

    # Create .desktop entry
    desktop_entry = linux_pkg_dir / "aems-agent.desktop"
    desktop_entry.write_text("""[Desktop Entry]
Type=Application
Name=AEMS Agent
Comment=AEMS Local Bridge Agent
Exec=aems-agent run --tray
Icon=aems-agent
Categories=Utility;Education;
StartupNotify=false
Terminal=false
X-GNOME-Autostart-enabled=true
""")

    # Create systemd user service
    service_file = linux_pkg_dir / "aems-agent.service"
    service_file.write_text("""[Unit]
Description=AEMS Local Bridge Agent
After=network.target

[Service]
Type=simple
ExecStart=%h/.local/bin/aems-agent run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""")

    install_script = linux_pkg_dir / "install.sh"
    install_script.write_text("""#!/usr/bin/env bash
# AEMS Agent — user-mode Linux installer.
# Idempotent: re-running upgrades the install in place.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
prefix="${AEMS_AGENT_PREFIX:-$HOME/.local/share/aems-agent}"
bin_dir="$HOME/.local/bin"
systemd_user_dir="$HOME/.config/systemd/user"

echo "Installing AEMS Agent -> $prefix"
mkdir -p "$prefix"
cp -a "$here/aems-agent/." "$prefix/"

mkdir -p "$bin_dir"
ln -sf "$prefix/aems-agent" "$bin_dir/aems-agent"
chmod +x "$prefix/aems-agent" || true

mkdir -p "$systemd_user_dir"
cp "$here/aems-agent.service" "$systemd_user_dir/aems-agent.service"

case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "Note: $bin_dir is not on PATH. Add: export PATH=\\"$bin_dir:\\$PATH\\"" ;;
esac

echo "Installed. Try: aems-agent --version"
echo "Optional autostart: systemctl --user enable --now aems-agent.service"
""")
    os.chmod(install_script, 0o755)  # noqa: S103 - executable script

    print(f"  Linux desktop entry:   {desktop_entry}")
    print(f"  Linux systemd service: {service_file}")
    print(f"  Linux install script:  {install_script}")

    # Bundle everything into aems-agent-linux-<arch>.tar.gz so the README claim is true.
    import tarfile

    arch = platform.machine() or "x86_64"
    tarball = DIST_DIR / f"aems-agent-linux-{arch}.tar.gz"
    if tarball.exists():
        tarball.unlink()

    with tarfile.open(tarball, "w:gz") as tar:
        # The PyInstaller onedir output sits at dist_path; ship it as ./aems-agent/
        tar.add(str(dist_path), arcname="aems-agent")
        tar.add(str(install_script), arcname="install.sh")
        tar.add(str(service_file), arcname="aems-agent.service")
        tar.add(str(desktop_entry), arcname="aems-agent.desktop")
        readme_payload = (
            "AEMS Agent — Linux installer bundle\n"
            "===================================\n"
            "\n"
            "Quick start:\n"
            "    tar xzf aems-agent-linux-*.tar.gz\n"
            "    cd aems-agent-linux\n"
            "    ./install.sh\n"
            "    aems-agent run --tray\n"
            "    # or: systemctl --user enable --now aems-agent.service\n"
            "\n"
            "Headless boxes:\n"
            "    Set AEMS_AGENT_PIN_FILE=/run/aems-agent.pin before starting the agent\n"
            "    so the pairing PIN is written there on each /pair/initiate.\n"
        ).encode("utf-8")
        import io as _io

        info = tarfile.TarInfo(name="README.txt")
        info.size = len(readme_payload)
        info.mode = 0o644
        tar.addfile(info, _io.BytesIO(readme_payload))

    print(f"  Linux tarball:         {tarball} ({tarball.stat().st_size} bytes)")
    return tarball


def main() -> None:
    """Main build entry point."""
    parser = argparse.ArgumentParser(description="Build AEMS Agent installer")
    parser.add_argument(
        "--platform",
        choices=["windows", "macos", "linux", "auto"],
        default="auto",
        help="Target platform (default: auto-detect)",
    )
    args = parser.parse_args()

    target = args.platform
    if target == "auto":
        system = platform.system().lower()
        target = {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(system, "linux")

    print(f"Building AEMS Agent for {target}...")
    print(f"  Project root: {PROJECT_ROOT}")

    # Step 1: PyInstaller
    print("\n[1/2] Running PyInstaller...")
    dist_path = build_pyinstaller()

    # Step 2: Platform-specific packaging
    print(f"\n[2/2] Creating {target} installer...")
    if target == "windows":
        build_windows_installer(dist_path)
    elif target == "macos":
        build_macos_dmg(dist_path)
    elif target == "linux":
        build_linux_packages(dist_path)

    print("\nBuild complete!")


if __name__ == "__main__":
    main()

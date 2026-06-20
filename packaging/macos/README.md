# macOS Packaging

The macOS `.app` bundle and `AEMS-Agent.dmg` are produced by PyInstaller's
`BUNDLE` directive in `packaging/aems-agent.spec` (see v0.4.13 changelog for
why we no longer hand-assemble the bundle), wrapped into a DMG by
`packaging/build.py` (locally on a Mac) or by `.github/workflows/build.yml` on
every tagged release.

## What ships in the DMG

- `AEMS Agent.app/`
  - `Contents/MacOS/aems-agent` — the PyInstaller launcher
  - `Contents/Frameworks/` — bundled shared libraries (PyInstaller 6.20+
    relocates them here so `codesign` walks the tree cleanly)
  - `Contents/Resources/aems-agent.icns` — multi-resolution app icon
    (16, 32, 64, 128, 256, 512, 1024 px slots + retina @2x so Finder and
    the Dock look crisp at every zoom)
  - `Contents/Info.plist` — `CFBundleIconFile=aems-agent.icns`,
    `NSHighResolutionCapable=true`, `LSUIElement=true`
    (background-only / no Dock entry)
- `com.aems.agent.plist` — optional LaunchAgent (auto-start at login)
- `Open AEMS Agent (first launch).command` — double-click helper that removes
  the browser quarantine flag from `/Applications/AEMS Agent.app` and opens it
- `Applications` — symlink for the standard drag-to-Applications install flow

## Code signing

The build pipeline supports two signing tiers:

### Tier 1 — Ad-hoc signed (default)

PyInstaller's `BUNDLE` directive already ad-hoc signs every collected binary
and the `.app` wrapper during build, so CI only needs to verify:

```bash
codesign --verify --deep --strict --verbose=2 "dist/AEMS Agent.app"
```

This satisfies Apple Silicon's `kCSRequireSignature` requirement (every Mach-O
in the bundle carries a signature) but Gatekeeper still refuses to open the app
without user intervention on first launch, because the ad-hoc signature has no
anchor in Apple's PKI. macOS surfaces this as one of:

- "AEMS Agent" can't be opened because Apple cannot check it for malicious
  software. (Sequoia and later)
- "AEMS Agent" is from an unidentified developer. (older macOS)

**User-facing first-launch flow (must appear in every download surface):**

Preferred free/ad-hoc path:

1. Drag **AEMS Agent** from the mounted DMG to **Applications**.
2. Double-click **Open AEMS Agent (first launch).command** in the mounted DMG.
3. The helper removes `com.apple.quarantine` from
   `/Applications/AEMS Agent.app` and opens the app.

Fallback paths:

The exact path depends on the macOS version. Both paths must be documented;
right-click -> Open is no longer the documented reliable bypass on macOS 15
Sequoia.

**On macOS 15 Sequoia and later** — preferred path:

1. Double-click **AEMS Agent** in Applications. Dismiss the warning that appears.
2. Open **System Settings -> Privacy & Security**.
4. Scroll to the **Security** section.
5. Click **Open / Open Anyway** next to AEMS Agent. Authenticate if prompted.
6. Launch AEMS Agent again and confirm **Open** in the new dialog.

Apple only keeps the **Open Anyway** button visible for about an hour after
the failed launch attempt; users should do this step right after the warning
appears.

**On macOS 11 Big Sur through macOS 14 Sonoma:**

1. Drag **AEMS Agent** from the mounted DMG to **Applications**.
2. Right-click (or Control-click) AEMS Agent -> **Open**.
3. Confirm the "from an unidentified developer" warning by clicking
   **Open** again.

**Quarantine fallback (advanced, any macOS version):**

If Gatekeeper still blocks the app because the download carries the
`com.apple.quarantine` extended attribute, the fallback is:

```bash
xattr -dr com.apple.quarantine "/Applications/AEMS Agent.app"
open "/Applications/AEMS Agent.app"
```

This is a **quarantine fallback** only. It will not fix a genuinely broken
bundle, an invalid signature, the wrong architecture, or a system policy that
blocks unsigned software.

This is the same shipping model used by Calibre's free build, OBS Studio
before 2020, HandBrake (historic), MacDown, and most open-source macOS apps
that don't pay for the Apple Developer Program.

### Tier 2 — Developer ID + notarized

When the repo has all six secrets — `MACOS_CERT_P12_BASE64`,
`MACOS_CERT_PASSWORD`, `MACOS_DEVELOPER_IDENTITY`, `APPLE_ID`,
`APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID` — the workflow:

1. Imports the Developer ID `.p12` into a temporary keychain.
2. Signs the `.app` with `--options runtime --timestamp`.
3. Builds the DMG, signs it with the same identity.
4. Submits to `xcrun notarytool` and waits for approval.
5. Staples the notarization ticket so it works offline.
6. Verifies with `spctl --assess`.

After Tier 2 the user just double-clicks; no warnings.

To set this up: enroll in the Apple Developer Program ($99/yr), export
your **Developer ID Application** cert as a password-protected `.p12`,
base64 it, generate an **app-specific password** for `notarytool`, and
drop all six secrets in the repo settings.

## Manual installation (post-DMG)

After dragging the `.app` to Applications:

```bash
cp "/Volumes/AEMS Agent/com.aems.agent.plist" ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.aems.agent.plist
```

(LaunchAgent is optional — users who don't want auto-start at login can
skip it and launch the app manually from `/Applications`.)

# macOS Packaging

The macOS `.app` bundle and `AEMS-Agent.dmg` are produced by
`packaging/build.py` (locally on a Mac) or by `.github/workflows/build.yml`
on every tagged release.

## What ships in the DMG

- `AEMS Agent.app/`
  - `Contents/MacOS/aems-agent` — PyInstaller `onedir` binary
  - `Contents/MacOS/_internal/…` — bundled libraries
  - `Contents/Resources/aems-agent.icns` — multi-resolution app icon
    (16, 32, 64, 128, 256, 512, 1024 px slots so Finder and the Dock
    look crisp at every zoom)
  - `Contents/Info.plist` — `CFBundleIconFile=aems-agent`,
    `NSHighResolutionCapable=true`, `LSUIElement=true`
    (background-only / no Dock entry)
- `com.aems.agent.plist` — optional LaunchAgent (auto-start at login)

## Code signing

The build pipeline supports two signing tiers:

### Tier 1 — Ad-hoc signed (default)

`codesign --force --deep --sign - "dist/AEMS Agent.app"`

This is what ships when the repo has no Apple Developer ID secrets
configured. It satisfies Apple Silicon's `kCSRequireSignature`
requirement (the app will at least *launch*) but Gatekeeper still
refuses on first run because the signature has no anchor in Apple's
PKI.

**User-facing first-launch flow (must appear in every download surface):**

1. Open `AEMS-Agent.dmg` and drag **AEMS Agent** to Applications.
2. Right-click (Control-click) AEMS Agent in Applications → **Open**.
3. Confirm the "from an unidentified developer" warning by clicking
   **Open** again.
4. After this one-time approval the app launches normally forever.

If the user's browser flagged the DMG as quarantined and the
right-click path still fails, the fallback is:

```bash
xattr -dr com.apple.quarantine "/Applications/AEMS Agent.app"
```

This is the same shipping model used by Calibre's free build, OBS
Studio before 2020, HandBrake (historic), MacDown, and most open-source
macOS apps that don't pay for the Apple Developer Program.

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
cp com.aems.agent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.aems.agent.plist
```

(LaunchAgent is optional — users who don't want auto-start at login can
skip it and launch the app manually from `/Applications`.)

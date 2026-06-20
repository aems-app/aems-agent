# AEMS Local Bridge Agent

> **As of v0.3.2 (2026-05-11), this package is licensed under AGPL-3.0-or-later (previously MIT).** The agent links [PyMuPDF](https://github.com/pymupdf/PyMuPDF) in-process via [`aems-pdf-annotator`](https://github.com/aems-app/aems-pdf-annotator), and AGPL applies to the combined work. See [LICENSE](LICENSE) for the full text.

A lightweight companion service that runs on `localhost` and provides REST API access to the local filesystem, enabling the [AEMS](https://aems.app) hosted app to read/write exam PDFs to a user-chosen folder without ever uploading source PDFs to the server.

## What it does

- Runs as a local service on `127.0.0.1:61234` (default).
- Exposes an authenticated REST API the AEMS web app uses to read source PDFs and write annotated PDFs from your local storage folder.
- Enforces local `Host` headers and a PIN-based browser pairing flow before granting the web app access.
- Optional system-tray icon so it's visible while running.
- Optional offline grading bundle support for fully-local workflows.
- Never sends PDF content out unless the user explicitly attaches a Canvas / offline workflow in the hosted app.

## Installation

### Binary installers (recommended for end users)

Pre-built installers — no Python needed — are on the [Releases page](https://github.com/aems-app/aems-agent/releases/latest).

| Platform | File | Notes |
|----------|------|-------|
| Windows | `aems-agent-setup.exe` | Installs to `%LOCALAPPDATA%\AEMS Agent` |
| macOS   | `AEMS-Agent.dmg`       | Drag to Applications, then run the first-launch helper in the DMG |
| Linux   | `aems-agent-linux-x86_64.tar.gz` | Extract, run `./install.sh`, then start `aems-agent run --tray` or enable the user service |

#### macOS: first launch

The macOS download is signed with a **free ad-hoc signature**, not an Apple
Developer ID (we are not in the Apple Developer Program). Browser downloads
also carry Apple's quarantine flag. To make that free path less painful, the
DMG includes a double-clickable helper named
`Open AEMS Agent (first launch).command`.

Preferred path:

1. Open the downloaded `AEMS-Agent.dmg`.
2. Drag **AEMS Agent** to **Applications**.
3. Double-click **Open AEMS Agent (first launch).command** in the same DMG.
   It removes the browser quarantine flag from `/Applications/AEMS Agent.app`
   and opens the app.
4. Use **AEMS Agent** normally after that.

##### macOS 15 Sequoia (and later) fallback

Apple changed the Gatekeeper bypass UX in Sequoia, so right-click -> Open is
no longer the documented reliable path on Sequoia. If the helper is not
available or macOS still blocks the app:

1. Double-click **AEMS Agent** in Applications. Dismiss the warning that appears.
2. Open **System Settings -> Privacy & Security**.
3. Scroll to the **Security** section. You should see a notice that AEMS Agent
   was blocked.
4. Click **Open / Open Anyway** next to AEMS Agent. Authenticate with Touch ID
   or your password if prompted.
5. Launch AEMS Agent again and confirm **Open** in the new dialog.

After that the app runs normally on subsequent launches.

> Apple only keeps the **Open Anyway** button available for about an hour after
> the failed launch attempt, so do this step right after you see the warning.

##### macOS 11 Big Sur through macOS 14 Sonoma

1. Open the downloaded `AEMS-Agent.dmg` and drag **AEMS Agent** to Applications.
2. In **Applications**, **right-click** (or Control-click) AEMS Agent -> **Open**.
3. Confirm the warning dialog by clicking **Open** again.

After that the app runs normally; macOS remembers your choice.

##### Quarantine fallback (advanced)

If Gatekeeper still blocks the app because of the download **quarantine**
attribute that browsers attach to files, you can clear it from Terminal:

```bash
xattr -dr com.apple.quarantine "/Applications/AEMS Agent.app"
open "/Applications/AEMS Agent.app"
```

This is a **quarantine fallback**, not a universal fix. It will not help if
the bundle is genuinely broken, the signature is invalid, the binary is for
the wrong architecture, or a system policy blocks unsigned software.

If you've never seen this before: this is the same first-launch flow every
free macOS app without a $99/yr Apple Developer ID uses — Calibre, OBS Studio,
MacDown, HandBrake (historically), etc. It's not a virus warning; it's a
"we couldn't verify the publisher's identity" warning.

### pip (for developers)

```bash
pip install aems-agent
```

Requires Python 3.10+.

## Usage

```bash
# Print version and exit
aems-agent --version

# Start the agent (default: http://127.0.0.1:61234)
aems-agent run

# Start with system tray icon
aems-agent run --tray

# Custom port/host
aems-agent run --port 9000 --host 0.0.0.0

# Show auth token (the hosted app pairs against this token)
aems-agent token

# Set storage path
aems-agent set-path /path/to/exam/folder

# Show config directory
aems-agent config-dir
```

After `aems-agent run`, open AEMS in your browser, go to **Settings → Privacy & Storage**, and pair the agent.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).

**Relicense note (v0.3.2, 2026-05-11):** Versions ≤ 0.3.1 were licensed under MIT. From v0.3.2 the agent is AGPL-3.0-or-later because it links PyMuPDF (AGPL-3.0) in-process via `aems-pdf-annotator`. The MIT licence that previously applied to forks of v0.3.1 and earlier remains in effect for those versions.

## Code signing policy

Windows release installers are signed when the SignPath Foundation setup and
GitHub Actions settings are configured. The intended public signing statement is:
Free code signing provided by SignPath.io, certificate by SignPath Foundation.
Until that setup is approved and enabled, Windows installer artifacts are
published unsigned and may show Windows SmartScreen warnings.

Signing roles:

- Committers and reviewers: repository maintainers with write access in the
  `aems-app` GitHub organization.
- Release signing approvers: `aems-app` organization owners or maintainers
  delegated by the owners.

Privacy statement for signed Windows installers: the agent runs locally on the
user's machine and does not transfer PDF content or local file data to networked
systems unless the user explicitly starts a Canvas, hosted AEMS, or offline
workflow that requests it. See [SECURITY.md](SECURITY.md) for the local threat
model and [docs/windows-code-signing.md](docs/windows-code-signing.md) for the
SignPath setup.

## Links

- Homepage: [https://aems.app](https://aems.app)
- Source: [https://github.com/aems-app/aems-agent](https://github.com/aems-app/aems-agent)
- Issues: [https://github.com/aems-app/aems-agent/issues](https://github.com/aems-app/aems-agent/issues)
- Annotation engine: [aems-pdf-annotator](https://github.com/aems-app/aems-pdf-annotator)

## Maintainers

- Windows release signing setup: [docs/windows-code-signing.md](docs/windows-code-signing.md)

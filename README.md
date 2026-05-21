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
| macOS   | `AEMS-Agent.dmg`       | Drag to Applications |
| Linux   | `aems-agent-linux.tar.gz` | Extract and run `./aems-agent run` |

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

After `aems-agent run`, open AEMS in your browser, go to **Settings → Storage**, and pair the agent.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).

**Relicense note (v0.3.2, 2026-05-11):** Versions ≤ 0.3.1 were licensed under MIT. From v0.3.2 the agent is AGPL-3.0-or-later because it links PyMuPDF (AGPL-3.0) in-process via `aems-pdf-annotator`. The MIT licence that previously applied to forks of v0.3.1 and earlier remains in effect for those versions.

## Links

- Homepage: [https://aems.app](https://aems.app)
- Source: [https://github.com/aems-app/aems-agent](https://github.com/aems-app/aems-agent)
- Issues: [https://github.com/aems-app/aems-agent/issues](https://github.com/aems-app/aems-agent/issues)
- Annotation engine: [aems-pdf-annotator](https://github.com/aems-app/aems-pdf-annotator)

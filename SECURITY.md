# Security policy

## Reporting a vulnerability

Please email **security@aems.app** with the details. Do not file a public GitHub issue for security-sensitive reports.

When reporting, include:

- A description of the issue.
- Steps to reproduce, or a proof-of-concept.
- The version (`aems-agent --version`).
- Your platform (Windows / macOS / Linux).
- Your assessment of impact.

We will acknowledge within a reasonable time. There is currently no bug-bounty programme.

## Supported versions

This is a fast-moving service. Only the current `main` branch and the latest released version on PyPI / GitHub Releases are supported.

## Threat model

The agent runs as an unprivileged service on the user's machine and binds to `127.0.0.1` by default. It uses local Host-header enforcement plus a paired auth token to gate requests from the AEMS hosted app (which runs in the user's browser). Browser pairing is PIN-based and rate-limited, with temporary lockout after repeated failed PIN attempts. Issues that broaden the agent's authority — token-bypass, path-traversal in the storage folder, request smuggling — are in scope here.

Issues that depend on the user running the agent on `0.0.0.0` and exposing it to the LAN are not in scope unless they would also affect a `127.0.0.1` deployment.

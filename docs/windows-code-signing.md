# Windows code signing

This repository's preferred Windows signing path is **SignPath Foundation /
SignPath.io**, not an exportable PFX in GitHub Actions.

Why:

- `aems-agent` is a public AGPL repository and is a good fit for the free OSS
  SignPath Foundation program.
- New public code-signing certificates are typically issued with the private key
  on a hardware token or HSM-backed service, not as a blob you can base64 into
  `WIN_CODESIGN_CERT_PFX_BASE64`.
- The GitHub workflow in `.github/workflows/build.yml` therefore prefers
  SignPath and only keeps the PFX inputs as a legacy fallback.

## What the workflow expects

The Windows build job resolves the signing method in this order:

1. SignPath
2. Legacy exportable PFX
3. Unsigned

SignPath is enabled when the following are configured in GitHub:

- Repository secret: `SIGNPATH_API_TOKEN`
- Repository variable: `SIGNPATH_ORGANIZATION_ID`

The workflow hardcodes the following SignPath slugs:

- Project slug: `aems-agent`
- Artifact configuration slug: `windows-installer`
- Signing policy slug:
  - `release-signing` for tag builds (`refs/tags/v*`)
  - `test-signing` for `workflow_dispatch`

The artifact configuration committed in
`.signpath/artifact-configurations/windows-installer.xml` signs the root-level
`aems-agent-setup.exe` file inside the GitHub Actions artifact ZIP.

## One-time SignPath setup

1. Apply for OSS signing at `https://signpath.org/`.
2. After approval, create a SignPath project for this repository.
3. Use repository URL `https://github.com/aems-app/aems-agent`.
4. Set the project slug to `aems-agent`.
5. Link the trusted build system `GitHub.com`.
6. Install the SignPath GitHub App for the repository if SignPath requires it
   for your plan/policies.
7. Add the artifact configuration from
   `.signpath/artifact-configurations/windows-installer.xml` with slug
   `windows-installer`.
8. Create signing policies:
   - `test-signing`
   - `release-signing`
9. Create a SignPath API token for a submitter account.
10. Add GitHub Actions settings:
    - secret `SIGNPATH_API_TOKEN`
    - variable `SIGNPATH_ORGANIZATION_ID`

## First verification run

After the SignPath project and GitHub settings exist:

1. Run `workflow_dispatch` on `Build AEMS Agent`.
2. Confirm the Windows job logs:
   - `Resolve Windows signing method` -> SignPath selected
   - `Upload unsigned Windows installer for SignPath`
   - `Submit Windows signing request (SignPath)`
   - `Verify Windows installer signature`
3. Download the Windows artifact and confirm in PowerShell:

```powershell
Get-AuthenticodeSignature .\aems-agent-setup.exe | Format-List Status,SignerCertificate
```

`Status` must be `Valid`.

## Legacy fallback

`WIN_CODESIGN_CERT_PFX_BASE64` and `WIN_CODESIGN_CERT_PASSWORD` are still
supported only for older/exportable certificates. Do not buy a new public
certificate expecting that path to work.

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_readme_documents_current_linux_installer_flow() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "aems-agent-linux-x86_64.tar.gz" in readme
    assert "run `./install.sh`" in readme
    assert "Privacy & Storage" in readme


def test_readme_summary_row_points_macos_users_to_versioned_first_launch_steps() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "First launch: right-click the app -> Open" not in readme
    assert "Open AEMS Agent (first launch).command" in readme
    assert "removes the browser quarantine flag" in readme


def test_release_workflow_uses_built_linux_tarball_and_python_distributions() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")

    assert "dist/aems-agent-linux-*.tar.gz" in workflow
    assert "python -m build --sdist --wheel --outdir artifacts/python-dist" in workflow
    assert (
        "tar -czf artifacts/aems-agent-linux.tar.gz -C artifacts aems-agent-linux" not in workflow
    )

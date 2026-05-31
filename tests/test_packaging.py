"""Packaging regressions that broke the 0.4.8 release artifacts."""

from __future__ import annotations

from pathlib import Path
import tomllib


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

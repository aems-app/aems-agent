"""Tests for CLI commands."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from aems_agent import cli as cli_module
from aems_agent.config import load_config


def test_token_command_displays_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config_dir", lambda: tmp_path)
    from aems_agent.config import ensure_auth_token

    token = ensure_auth_token(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["token"])
    assert result.exit_code == 0
    assert token in result.output


def test_set_path_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config_dir", lambda: tmp_path)
    storage = tmp_path / "exam_storage"
    storage.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["set-path", str(storage)])
    assert result.exit_code == 0
    config = load_config(tmp_path)
    assert config.storage_path == str(storage.resolve())


def test_config_dir_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config_dir", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["config-dir"])
    assert result.exit_code == 0
    assert str(tmp_path) in result.output



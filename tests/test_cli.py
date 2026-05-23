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




def test_ensure_stdio_streams_substitutes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windowed PyInstaller bundles set sys.stdout=None; verify we patch it.

    Reproduces the v0.4.0 URI-launch crash (uvicorn ColourizedFormatter calls
    sys.stdout.isatty() during logging config and trips AttributeError).
    """
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    cli_module._ensure_stdio_streams()

    assert sys.stdout is not None
    assert sys.stderr is not None
    assert sys.stdout.isatty() is False
    assert sys.stderr.isatty() is False
    # write/flush are no-ops that don't raise
    sys.stdout.write("ignored")
    sys.stdout.flush()
    sys.stderr.write("ignored")
    sys.stderr.flush()


def test_ensure_stdio_streams_idempotent_when_streams_exist(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Don't clobber the real streams when they're already valid."""
    cli_module._ensure_stdio_streams()
    # The real (or pytest-captured) stream should still be in place.
    print("hello", flush=True)
    captured = capsys.readouterr()
    assert "hello" in captured.out

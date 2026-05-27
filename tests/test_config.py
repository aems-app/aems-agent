"""Tests for agent configuration management."""

import platform
from pathlib import Path

import pytest

from aems_agent.config import (
    AgentConfig,
    ensure_auth_token,
    get_auth_token,
    get_config_dir,
    load_config,
    save_config,
)


class TestGetConfigDir:
    """Tests for get_config_dir()."""

    def test_returns_path(self) -> None:
        result = get_config_dir()
        assert isinstance(result, Path)
        assert result.is_absolute()

    def test_platform_specific(self) -> None:
        result = get_config_dir()
        if platform.system() == "Windows":
            assert "AEMS" in str(result)
            assert "agent" in str(result)
        else:
            result_str = str(result)
            assert ".config" in result_str or "aems" in result_str.lower()


class TestGetConfigDirCrossPlatform:
    """Tests for get_config_dir() cross-platform behavior."""

    def test_darwin_uses_library(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = get_config_dir()
        assert "Library" in str(result)
        assert "Application Support" in str(result)
        assert "AEMS" in str(result)

    def test_linux_default(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        result = get_config_dir()
        assert ".config" in str(result)
        assert "aems" in str(result)

    def test_linux_xdg_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("platform.system", lambda: "Linux")
        custom_xdg = str(tmp_path / "custom_xdg")
        monkeypatch.setenv("XDG_CONFIG_HOME", custom_xdg)
        result = get_config_dir()
        assert custom_xdg in str(result)

    def test_windows(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("platform.system", lambda: "Windows")
        appdata = str(tmp_path / "AppData")
        monkeypatch.setenv("APPDATA", appdata)
        result = get_config_dir()
        assert appdata in str(result)
        assert "AEMS" in str(result)

    def test_darwin_migration(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        # Create old config dir with a file
        old_path = tmp_path / ".config" / "aems" / "agent"
        old_path.mkdir(parents=True)
        (old_path / "config.json").write_text("{}", encoding="utf-8")
        result = get_config_dir()
        new_path = tmp_path / "Library" / "Application Support" / "AEMS" / "agent"
        assert result == new_path
        assert (new_path / "config.json").exists()


class TestAgentConfig:
    """Tests for AgentConfig model."""

    def test_defaults(self) -> None:
        config = AgentConfig()
        assert config.storage_path is None
        assert config.port == 61234
        assert config.host == "127.0.0.1"
        assert len(config.allowed_origins) > 0
        assert "https://api.aems.app" in config.allowed_origins

    def test_custom_values(self) -> None:
        config = AgentConfig(
            storage_path="D:\\Exams" if platform.system() == "Windows" else "/tmp/exams",
            port=9999,
            host="0.0.0.0",
        )
        assert config.port == 9999
        assert config.host == "0.0.0.0"

    def test_storage_path_must_be_absolute(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            AgentConfig(storage_path="relative/path")

    def test_port_bounds(self) -> None:
        with pytest.raises(ValueError):
            AgentConfig(port=80)
        with pytest.raises(ValueError):
            AgentConfig(port=99999)


class TestLoadSaveConfig:
    """Tests for load_config/save_config."""

    def test_load_default_when_no_file(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "nonexistent")
        assert config.storage_path is None
        assert config.port == 61234

    def test_roundtrip(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()

        original = AgentConfig(
            storage_path=str(tmp_path / "storage"),
            port=12345,
        )
        save_config(original, config_dir)

        loaded = load_config(config_dir)
        assert loaded.storage_path == original.storage_path
        assert loaded.port == original.port

    def test_save_creates_dir(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "new" / "dir"
        save_config(AgentConfig(), config_dir)
        assert (config_dir / "config.json").exists()

    def test_load_corrupted_file(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        (config_dir / "config.json").write_text("not json", encoding="utf-8")

        config = load_config(config_dir)
        assert config.port == 61234  # Falls back to defaults


class TestAuthToken:
    """Tests for ensure_auth_token/get_auth_token."""

    def test_ensure_creates_token(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()

        token = ensure_auth_token(config_dir)
        assert len(token) > 20

    def test_ensure_returns_same_token(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()

        token1 = ensure_auth_token(config_dir)
        token2 = ensure_auth_token(config_dir)
        assert token1 == token2

    def test_get_returns_none_when_no_token(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()

        assert get_auth_token(config_dir) is None

    def test_get_returns_token_after_ensure(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()

        token = ensure_auth_token(config_dir)
        assert get_auth_token(config_dir) == token

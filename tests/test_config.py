"""Tests for agent configuration management."""

import platform
from pathlib import Path

import pytest

from aems_agent.config import (
    AgentConfig,
    ConfigLoadError,
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

    def test_canvas_hosts_are_normalized_and_deduplicated(self) -> None:
        config = AgentConfig(
            canvas_allowed_hosts=[
                "Canvas.Example.EDU.",
                "canvas.example.edu",
                "2001:db8::1",
            ]
        )
        assert config.canvas_allowed_hosts == ["canvas.example.edu", "2001:db8::1"]

    @pytest.mark.parametrize(
        "host",
        ["https://canvas.example.edu", "canvas.example.edu:443", "canvas.example.edu/path"],
    )
    def test_canvas_hosts_reject_urls_ports_and_paths(self, host: str) -> None:
        with pytest.raises(ValueError, match="hostname only"):
            AgentConfig(canvas_allowed_hosts=[host])


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

    def test_load_corrupted_file_fails_loudly_without_rewrite(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        config_file = config_dir / "config.json"
        original = b"not json"
        config_file.write_bytes(original)

        with pytest.raises(ConfigLoadError, match="invalid JSON"):
            load_config(config_dir)
        assert config_file.read_bytes() == original

    def test_load_utf8_bom_preserves_existing_values(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        config_file = config_dir / "config.json"
        config_file.write_bytes(
            b'\xef\xbb\xbf{"storage_path": null, "port": 61235, '
            b'"paired_origins": ["https://aems.example"]}'
        )

        config = load_config(config_dir)

        assert config.port == 61235
        assert config.paired_origins == ["https://aems.example"]

    def test_invalid_values_fail_loudly_without_rewrite(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        config_file = config_dir / "config.json"
        original = b'{"port": 80, "paired_origins": ["https://aems.example"]}'
        config_file.write_bytes(original)

        with pytest.raises(ConfigLoadError, match="failed validation"):
            load_config(config_dir)
        assert config_file.read_bytes() == original

    def test_non_object_json_fails_loudly_without_rewrite(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        config_file = config_dir / "config.json"
        original = b"[]"
        config_file.write_bytes(original)

        with pytest.raises(ConfigLoadError, match="root must be an object"):
            load_config(config_dir)
        assert config_file.read_bytes() == original


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


class TestSecretFilePermissions:
    """Token and config files are created owner-only, not chmod'd after."""

    def test_auth_token_created_owner_only(self, tmp_path: Path) -> None:
        import os

        if os.name == "nt":
            pytest.skip("POSIX permission semantics only")

        from aems_agent.config import ensure_auth_token

        ensure_auth_token(tmp_path)
        mode = (tmp_path / "auth_token").stat().st_mode & 0o777
        assert mode == 0o600

    def test_legacy_token_permissions_tightened_on_read(self, tmp_path: Path) -> None:
        import os

        if os.name == "nt":
            pytest.skip("POSIX permission semantics only")

        from aems_agent.config import ensure_auth_token

        token_file = tmp_path / "auth_token"
        token_file.write_text("legacy-token", encoding="utf-8")
        token_file.chmod(0o644)

        assert ensure_auth_token(tmp_path) == "legacy-token"
        assert token_file.stat().st_mode & 0o777 == 0o600

    def test_config_json_created_owner_only(self, tmp_path: Path) -> None:
        import os

        if os.name == "nt":
            pytest.skip("POSIX permission semantics only")

        from aems_agent.config import AgentConfig, save_config

        save_config(AgentConfig(), tmp_path)
        mode = (tmp_path / "config.json").stat().st_mode & 0o777
        assert mode == 0o600

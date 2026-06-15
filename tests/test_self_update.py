"""Tests for POST /self-update."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


def _skip_if_no_fastapi() -> None:
    if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("httpx") is None:
        pytest.skip("fastapi/httpx not installed")


class TestSelfUpdateValidation:
    """Payload + state checks that don't require network."""

    def test_rejects_missing_auth(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        r = agent_client.post("/self-update", json={"version": "0.4.24"})
        assert r.status_code == 401

    def test_rejects_invalid_token(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        r = agent_client.post(
            "/self-update",
            json={"version": "0.4.24"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 403

    def test_rejects_malformed_version(self, agent_client: Any, auth_headers: dict) -> None:
        _skip_if_no_fastapi()
        for bad in ["", "abc", "0.4", "0.4.x", "1", "v"]:
            r = agent_client.post("/self-update", json={"version": bad}, headers=auth_headers)
            assert r.status_code == 422, f"expected 422 for version={bad!r}, got {r.status_code}"

    def test_accepts_v_prefix(
        self, agent_client: Any, auth_headers: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The route should normalize a leading 'v' before validation."""
        _skip_if_no_fastapi()
        # Make sums URL fail so we don't actually try to download — we just want
        # to confirm validation accepted "v0.4.24" (otherwise it'd 422).
        from aems_agent import routes

        def _boom(url: str, timeout: float = 30.0) -> str:
            raise RuntimeError("network disabled in unit test")

        monkeypatch.setattr(routes, "_fetch_text", _boom)
        # Force a platform we support so we don't get 501 before validation runs
        monkeypatch.setitem(
            routes._SELF_UPDATE_ASSET_BY_PLATFORM, sys.platform, "aems-agent-setup.exe"
        )
        r = agent_client.post("/self-update", json={"version": "v0.4.24"}, headers=auth_headers)
        assert r.status_code == 502  # got past validation, network shim blocked the fetch
        assert r.json()["detail"]["code"] == "sums_unreachable"


class TestSelfUpdatePlatformGate:
    """501 with helpful payload on platforms we don't auto-update yet."""

    def test_unsupported_platform_returns_501(
        self,
        agent_client: Any,
        auth_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _skip_if_no_fastapi()
        from aems_agent import routes

        # Pretend we're on an unsupported platform by clearing the map for this run.
        monkeypatch.setattr(routes, "_SELF_UPDATE_ASSET_BY_PLATFORM", {})
        r = agent_client.post("/self-update", json={"version": "0.4.24"}, headers=auth_headers)
        assert r.status_code == 501
        body = r.json()["detail"]
        assert body["code"] == "platform_unsupported"
        assert "release_url" in body
        assert body["release_url"].endswith("/v0.4.24")


class TestSelfUpdateShaVerification:
    """The agent must reject a tampered installer."""

    def test_sha_mismatch_aborts(
        self,
        agent_client: Any,
        auth_headers: dict,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _skip_if_no_fastapi()
        from aems_agent import routes

        monkeypatch.setitem(
            routes._SELF_UPDATE_ASSET_BY_PLATFORM, sys.platform, "aems-agent-setup.exe"
        )

        good_bytes = b"this is the real installer payload"
        bad_bytes = b"this is a tampered payload of different content"
        # sha256sums.txt claims the SHA of good_bytes...
        good_sha = hashlib.sha256(good_bytes).hexdigest()
        sums_text = f"{good_sha}  aems-agent-setup.exe\n"

        def _fetch_text(url: str, timeout: float = 30.0) -> str:
            assert url.endswith("/sha256sums.txt")
            return sums_text

        def _download_to(url: str, dest: Path, timeout: float = 120.0) -> int:
            # ...but the download serves a different payload.
            dest.write_bytes(bad_bytes)
            return len(bad_bytes)

        spawn_called = {"yes": False}

        def _spawn_installer_detached(p: Path) -> int:
            spawn_called["yes"] = True
            return 99999

        monkeypatch.setattr(routes, "_fetch_text", _fetch_text)
        monkeypatch.setattr(routes, "_download_to", _download_to)
        monkeypatch.setattr(routes, "_spawn_installer_detached", _spawn_installer_detached)

        r = agent_client.post("/self-update", json={"version": "0.4.24"}, headers=auth_headers)
        assert r.status_code == 502
        body = r.json()["detail"]
        assert body["code"] == "sha_mismatch"
        assert body["expected"] == good_sha
        assert body["actual"] == hashlib.sha256(bad_bytes).hexdigest()
        assert spawn_called["yes"] is False, "must not spawn installer when SHA fails"


class TestSelfUpdateHappyPath:
    """Full happy path with the spawn step stubbed (do NOT actually invoke an installer)."""

    def test_spawns_installer_on_sha_match(
        self,
        agent_client: Any,
        auth_headers: dict,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _skip_if_no_fastapi()
        from aems_agent import routes

        monkeypatch.setitem(
            routes._SELF_UPDATE_ASSET_BY_PLATFORM, sys.platform, "aems-agent-setup.exe"
        )

        payload = b"%PDF-1.4 not really an installer but treat as one"
        sha = hashlib.sha256(payload).hexdigest()
        sums_text = f"{sha} *aems-agent-setup.exe\nffff  some-other-asset.tar.gz\n"

        def _fetch_text(url: str, timeout: float = 30.0) -> str:
            return sums_text

        def _download_to(url: str, dest: Path, timeout: float = 120.0) -> int:
            dest.write_bytes(payload)
            return len(payload)

        spawned = {}

        def _spawn_installer_detached(p: Path) -> int:
            spawned["path"] = str(p)
            return 12345

        monkeypatch.setattr(routes, "_fetch_text", _fetch_text)
        monkeypatch.setattr(routes, "_download_to", _download_to)
        monkeypatch.setattr(routes, "_spawn_installer_detached", _spawn_installer_detached)

        r = agent_client.post("/self-update", json={"version": "0.4.24"}, headers=auth_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "spawned"
        assert body["version"] == "0.4.24"
        assert body["asset"] == "aems-agent-setup.exe"
        assert body["installer_pid"] == 12345
        assert body["installer_size_bytes"] == len(payload)
        assert spawned["path"].endswith("aems-agent-setup.exe")


class TestMacOSSelfUpdate:
    """macOS .dmg self-update path — implemented in v0.4.33.

    The handover for v0.4.32 explicitly flagged macOS as ``return 501``
    with a manual download fallback. v0.4.33 wires the full flow by
    adding ``darwin`` to ``_SELF_UPDATE_ASSET_BY_PLATFORM`` and
    extending ``_spawn_installer_detached`` to emit a relaunch helper
    script. These tests do not require a real macOS box — they verify
    the plumbing without invoking ``hdiutil``.
    """

    def test_darwin_is_in_self_update_asset_map(self) -> None:
        """macOS must no longer 501; the route must download AEMS-Agent.dmg."""
        from aems_agent import routes

        assert routes._SELF_UPDATE_ASSET_BY_PLATFORM.get("darwin") == "AEMS-Agent.dmg"

    def test_build_macos_relaunch_script_contains_required_steps(self) -> None:
        """Static inspection of the generated bash. Pinning the shape so a
        well-meaning refactor cannot silently drop the quit / mount /
        replace / detach / relaunch sequence.
        """
        from aems_agent import routes

        script = routes._build_macos_relaunch_script(Path("/tmp/AEMS-Agent.dmg"))
        # Quit the running tray BEFORE replacing the .app.
        assert "osascript" in script and "quit" in script
        assert "pkill -f aems-agent" in script
        # Mount the DMG read-only without a Finder window.
        assert "hdiutil attach" in script
        assert "-nobrowse" in script
        assert "-readonly" in script
        # Replace the installed bundle.
        assert "/Applications/" in script
        assert routes._MACOS_APP_BUNDLE_NAME in script
        # ditto, not cp -R — preserves extended attributes.
        assert "ditto" in script
        # Detach the DMG (best-effort, the script tolerates a busy mount).
        assert "hdiutil detach" in script
        # Re-open the new app.
        assert "open -a" in script
        # The HTTP response must flush before kill — sleep gate at the top.
        assert "sleep 2" in script

    def test_build_macos_relaunch_script_quotes_dmg_path_with_spaces(self, tmp_path: Path) -> None:
        """Defends against path-with-spaces shell injection / mis-parsing.

        Uses ``tmp_path`` so the path is platform-correct (POSIX slashes
        on macOS/Linux, backslashes on Windows). The relaunch script is
        invoked by ``/bin/bash`` on macOS only, but the unit test runs on
        every CI platform — pin on the ``shlex.quote`` invariant instead
        of a hard-coded POSIX path.
        """
        import shlex

        from aems_agent import routes

        spaced_dir = tmp_path / "with space"
        spaced_dir.mkdir()
        dmg = spaced_dir / "AEMS-Agent.dmg"

        script = routes._build_macos_relaunch_script(dmg)

        expected_quoted = shlex.quote(str(dmg))
        # If the path contains a space, shlex.quote wraps it in single
        # quotes. The script must use that exact quoted form so bash
        # tokenizes the DMG path as a single argument.
        assert (
            expected_quoted in script
        ), f"script must contain shlex-quoted DMG path {expected_quoted!r}"
        # And the raw unquoted form must NOT appear outside the quoted
        # context — otherwise ``hdiutil attach $DMG`` would split on the
        # space and try to mount the wrong file.
        bare_path = str(dmg)
        stripped = script.replace(expected_quoted, "")
        assert (
            bare_path not in stripped
        ), "raw unquoted path must not appear outside the shlex-quoted context"

    def test_spawn_macos_relaunch_writes_executable_script(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_spawn_macos_relaunch_script`` writes a 0o700 helper next to
        the DMG and spawns ``/bin/bash <script>`` in a new session.
        """
        import subprocess

        from aems_agent import routes

        dmg = tmp_path / "AEMS-Agent.dmg"
        dmg.write_bytes(b"fake dmg bytes")

        captured: dict[str, Any] = {}

        class _FakePopen:
            pid = 4242

            def __init__(self, args: list[str], **kwargs: Any) -> None:
                captured["args"] = args
                captured["kwargs"] = kwargs

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)

        pid = routes._spawn_macos_relaunch_script(dmg)

        assert pid == 4242
        # bash + path-to-our-script
        assert captured["args"][0] == "/bin/bash"
        script_path = Path(captured["args"][1])
        assert script_path.exists(), "helper script must be on disk before spawn"
        assert script_path.parent == dmg.parent
        # Owner-execute bit must be set (chmod 0o700). Skip on Windows where
        # POSIX mode bits don't fully apply.
        if sys.platform != "win32":
            import stat as _stat

            mode = script_path.stat().st_mode & 0o777
            assert mode & _stat.S_IXUSR, f"script mode 0o{mode:o} lacks owner-execute"
        # start_new_session=True so SIGTERM to this process doesn't cascade.
        assert captured["kwargs"].get("start_new_session") is True
        # close_fds + DEVNULL for full detachment.
        assert captured["kwargs"].get("close_fds") is True

    def test_spawn_installer_detached_routes_darwin_to_relaunch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_spawn_installer_detached`` on darwin must delegate to the
        macOS helper, NOT to the Windows ``Popen(installer.exe, /S)`` path.
        """
        from aems_agent import routes

        called: dict[str, Any] = {}

        def _fake_relaunch(p: Path) -> int:
            called["path"] = str(p)
            return 7777

        monkeypatch.setattr(routes, "_spawn_macos_relaunch_script", _fake_relaunch)
        monkeypatch.setattr(sys, "platform", "darwin")

        dmg = tmp_path / "AEMS-Agent.dmg"
        dmg.write_bytes(b"x")

        pid = routes._spawn_installer_detached(dmg)
        assert pid == 7777
        assert called["path"] == str(dmg)


class TestSumsParser:
    """The sha256sums.txt parser must accept both ' ' and ' *' separators."""

    def test_plain_separator(self) -> None:
        from aems_agent import routes

        text = "abc123  some-file.exe\n"
        # 64-hex required
        assert routes._parse_sums_line(text, "some-file.exe") is None  # too short

    def test_64_hex_plain(self) -> None:
        from aems_agent import routes

        sha = "a" * 64
        text = f"{sha}  aems-agent-setup.exe\n"
        assert routes._parse_sums_line(text, "aems-agent-setup.exe") == sha

    def test_64_hex_with_star(self) -> None:
        from aems_agent import routes

        sha = "b" * 64
        text = f"{sha} *aems-agent-setup.exe\n"
        assert routes._parse_sums_line(text, "aems-agent-setup.exe") == sha

    def test_missing_target_returns_none(self) -> None:
        from aems_agent import routes

        sha = "c" * 64
        text = f"{sha}  other.tar.gz\n"
        assert routes._parse_sums_line(text, "aems-agent-setup.exe") is None

    def test_ignores_comments_and_blanks(self) -> None:
        from aems_agent import routes

        sha = "d" * 64
        text = f"# header comment\n\n{sha}  aems-agent-setup.exe\n"
        assert routes._parse_sums_line(text, "aems-agent-setup.exe") == sha

    def test_accepts_prefixed_path(self) -> None:
        """The release CI emits paths like ``artifacts/aems-agent-windows/aems-agent-setup.exe``
        because it concatenates downloaded artifact directories before hashing.
        The parser compares on basename so the path prefix is harmless."""
        from aems_agent import routes

        sha = "e" * 64
        text = f"{sha}  artifacts/aems-agent-windows/aems-agent-setup.exe\n"
        assert routes._parse_sums_line(text, "aems-agent-setup.exe") == sha

    def test_accepts_prefixed_path_with_binary_star(self) -> None:
        """Combine both quirks: prefix + binary-mode asterisk."""
        from aems_agent import routes

        sha = "f" * 64
        text = f"{sha} *artifacts/aems-agent-macos/AEMS-Agent.dmg\n"
        assert routes._parse_sums_line(text, "AEMS-Agent.dmg") == sha

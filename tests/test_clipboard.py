"""Clipboard helpers must use native platform tools, not Tk ownership hacks."""

from __future__ import annotations

import platform
import subprocess
from types import SimpleNamespace

import pytest


def test_copy_text_to_clipboard_uses_text_mode_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aems_agent import clipboard

    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        captured["argv"] = argv
        captured["input"] = kwargs.get("input")
        captured["text"] = kwargs.get("text")
        captured["encoding"] = kwargs.get("encoding")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert clipboard.copy_text_to_clipboard("123456") is True
    argv = captured["argv"]
    assert isinstance(argv, list) and len(argv) == 1
    # Absolute System32 path, not a bare name (CWD-relative resolution on
    # Windows would allow binary planting).
    assert str(argv[0]).lower().endswith("clip.exe")
    assert "system32" in str(argv[0]).lower()
    assert captured["input"] == "123456"
    assert captured["text"] is True
    assert captured["encoding"] is None


def test_copy_text_to_clipboard_falls_back_from_wl_copy_to_xclip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aems_agent import clipboard

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        if argv == ["wl-copy"]:
            raise FileNotFoundError("wl-copy missing")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert clipboard.copy_text_to_clipboard("token") is True
    assert calls == [["wl-copy"], ["xclip", "-selection", "clipboard"]]

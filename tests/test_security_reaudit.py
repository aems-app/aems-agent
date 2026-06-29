# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression tests for the 2026-06-24 adversarial security re-audit.

Each test class pins a defect class surfaced (and confirmed) by the re-audit so
the fix cannot silently regress. Docstrings name the defect.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Fix #1 — pre-auth unbounded request-body buffering (MEDIUM, browser-reachable)
# ---------------------------------------------------------------------------


class TestBodySizeCap:
    """Model-bound endpoints buffered the whole body before auth/validation.

    A page that can reach 127.0.0.1 could POST an arbitrarily large body to the
    unauthenticated /pair/* endpoints and force unbounded memory use. A global
    body-size middleware now bounds every request body.
    """

    def test_pair_initiate_rejects_oversize_body(self, agent_client: Any) -> None:
        # 17 MiB > the 16 MiB JSON cap → 413 before the body is buffered/parsed.
        oversize = b" " * (17 * 1024 * 1024)
        resp = agent_client.post(
            "/pair/initiate",
            content=oversize,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 413

    def test_self_update_oversize_body_rejected_even_unauthenticated(
        self, agent_client: Any
    ) -> None:
        # The cap runs before the route's auth dependency, so an oversize body
        # is rejected with 413 (not 401) — no auth needed to prove the bound.
        oversize = b" " * (17 * 1024 * 1024)
        resp = agent_client.post("/self-update", content=oversize)
        assert resp.status_code == 413

    def test_small_pairing_body_passes_size_gate(self, agent_client: Any) -> None:
        resp = agent_client.post(
            "/pair/initiate",
            json={"origin": "http://127.0.0.1:8080"},
            headers={"Origin": "http://127.0.0.1:8080"},
        )
        # Whatever the pairing outcome, it must NOT be a size rejection.
        assert resp.status_code != 413

    def test_body_limit_is_path_aware(self) -> None:
        from aems_agent.app import (
            _JSON_BODY_LIMIT_BYTES,
            _UPLOAD_BODY_LIMIT_BYTES,
            _body_limit_for,
        )

        upload_scope = {"method": "PUT", "path": "/files/asg1/sub1"}
        json_scope = {"method": "POST", "path": "/pair/initiate"}
        assert _body_limit_for(upload_scope) == _UPLOAD_BODY_LIMIT_BYTES
        assert _body_limit_for(json_scope) == _JSON_BODY_LIMIT_BYTES
        # The upload cap must clear the 200 MB PDF cap enforced in routes.py.
        assert _UPLOAD_BODY_LIMIT_BYTES > 200 * 1024 * 1024


# ---------------------------------------------------------------------------
# Fix #2 — PDF→pixmap memory bomb (MEDIUM, post-auth)
# ---------------------------------------------------------------------------


class TestPixmapClamp:
    """A tiny PDF with an enormous MediaBox forced a multi-GB get_pixmap alloc.

    _safe_render_dpi scales the effective dpi down so a pathological page stays
    under the pixel cap, while leaving every realistic exam page untouched.
    """

    def test_normal_pages_unchanged(self) -> None:
        from aems_agent.grading_bundle import _safe_render_dpi

        # A4 (595x842 pt) and Letter (612x792 pt) at high dpi stay under the cap.
        assert _safe_render_dpi(595, 842, 150) == 150
        assert _safe_render_dpi(595, 842, 600) == 600
        assert _safe_render_dpi(612, 792, 300) == 300

    def test_oversized_page_is_clamped(self) -> None:
        from aems_agent.grading_bundle import _MAX_RENDER_PIXELS, _safe_render_dpi

        dpi = _safe_render_dpi(50000, 50000, 150)
        assert dpi < 150
        zoom = dpi / 72.0
        rendered_pixels = (50000 * zoom) * (50000 * zoom)
        assert rendered_pixels <= _MAX_RENDER_PIXELS

    def test_clamp_applies_end_to_end(self, tmp_path: Path, monkeypatch: Any) -> None:
        import fitz

        from aems_agent import grading_bundle

        # Shrink the cap so a normal A4 page exercises the clamp path cheaply.
        monkeypatch.setattr(grading_bundle, "_MAX_RENDER_PIXELS", 250_000)

        pdf_path = tmp_path / "exam.pdf"
        doc = fitz.open()
        doc.new_page(width=595, height=842)
        doc.save(str(pdf_path))
        doc.close()

        bundle = grading_bundle.generate_bundle(pdf_path, strategy="multimodal", dpi=300)
        page = bundle["pages"][0]
        assert page.get("image_base64")

        import base64
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(base64.b64decode(page["image_base64"])))
        assert img.width * img.height <= 250_000


# ---------------------------------------------------------------------------
# Fix #3 — Canvas download redirect SSRF (LOW, post-auth)
# ---------------------------------------------------------------------------


def _redirect_manifest() -> dict[str, Any]:
    return {
        "canvas_base_url": "https://u.instructure.com",
        "canvas_token": "fake-token",
        "assignment_id": "asg1",
        "submissions": [{"submission_id": "sub1", "download_url": "/files/1/download"}],
    }


def _redirect_client(redirect_to: str, final: bytes = b"%PDF-1.4 ok") -> httpx.AsyncClient:
    from aems_agent.canvas_download import _redirect_ssrf_guard

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "u.instructure.com":
            return httpx.Response(302, headers={"location": redirect_to})
        return httpx.Response(200, content=final)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        event_hooks={"response": [_redirect_ssrf_guard]},
        max_redirects=5,
    )


class TestRedirectSsrfGuard:
    """Canvas downloads followed redirects with no post-redirect revalidation.

    An open-redirect on an allowlisted Canvas host could send the request to an
    internal address (e.g. 169.254.169.254). The redirect guard now blocks any
    redirect to a non-HTTPS or non-public target.
    """

    def test_is_public_ip(self) -> None:
        import ipaddress

        from aems_agent.canvas_download import _is_public_ip

        assert _is_public_ip(ipaddress.ip_address("8.8.8.8")) is True
        assert _is_public_ip(ipaddress.ip_address("2001:4860:4860::8888")) is True
        assert _is_public_ip(ipaddress.ip_address("127.0.0.1")) is False
        assert _is_public_ip(ipaddress.ip_address("10.0.0.5")) is False
        assert _is_public_ip(ipaddress.ip_address("100.64.0.1")) is False
        assert _is_public_ip(ipaddress.ip_address("169.254.169.254")) is False
        assert _is_public_ip(ipaddress.ip_address("192.168.1.1")) is False
        assert _is_public_ip(ipaddress.ip_address("224.0.0.1")) is False

    @pytest.mark.asyncio
    async def test_safe_redirect_target_scheme_and_ip(self) -> None:
        from aems_agent.canvas_download import _is_safe_redirect_target

        assert await _is_safe_redirect_target(httpx.URL("https://8.8.8.8/x")) is True
        assert await _is_safe_redirect_target(httpx.URL("http://8.8.8.8/x")) is False
        assert await _is_safe_redirect_target(httpx.URL("https://100.64.0.1/x")) is False
        assert await _is_safe_redirect_target(httpx.URL("https://127.0.0.1/x")) is False
        assert await _is_safe_redirect_target(httpx.URL("https://169.254.169.254/x")) is False
        assert await _is_safe_redirect_target(httpx.URL("https://224.0.0.1/x")) is False

    @pytest.mark.asyncio
    async def test_redirect_to_loopback_blocked(self, tmp_path: Path) -> None:
        from aems_agent.canvas_download import download_submissions

        client = _redirect_client("https://127.0.0.1/secret")
        async with client:
            results = await download_submissions(_redirect_manifest(), tmp_path, http_client=client)
        assert results[0].status == "failed"
        assert "redirect" in results[0].error.lower()
        # The internal target must NOT have been written to disk.
        assert not (tmp_path / "asg1" / "sub1" / "submission.pdf").exists()

    @pytest.mark.asyncio
    async def test_redirect_to_metadata_endpoint_blocked(self, tmp_path: Path) -> None:
        from aems_agent.canvas_download import download_submissions

        client = _redirect_client("https://169.254.169.254/latest/meta-data/")
        async with client:
            results = await download_submissions(_redirect_manifest(), tmp_path, http_client=client)
        assert results[0].status == "failed"

    @pytest.mark.asyncio
    async def test_redirect_to_public_cdn_allowed(self, tmp_path: Path) -> None:
        from aems_agent.canvas_download import download_submissions

        # Canvas legitimately 302s to a pre-signed CDN/S3 URL on a public host.
        client = _redirect_client("https://93.184.216.34/real.pdf", final=b"%PDF-1.4 real")
        async with client:
            results = await download_submissions(_redirect_manifest(), tmp_path, http_client=client)
        assert results[0].status == "downloaded"
        assert (tmp_path / "asg1" / "sub1" / "submission.pdf").exists()


# ---------------------------------------------------------------------------
# Fix #4 — self-update downgrade floor (LOW, post-auth)
# ---------------------------------------------------------------------------


class TestSelfUpdateDowngradeFloor:
    """Self-update accepted any valid semver; AGENT_VERSION was never compared,
    so a paired caller could roll the agent back to an older release."""

    def test_version_release_tuple(self) -> None:
        from aems_agent.routes import _version_release_tuple

        assert _version_release_tuple("0.4.35") == (0, 4, 35)
        assert _version_release_tuple("v1.2.3") == (1, 2, 3)
        assert _version_release_tuple("1.2.3-rc1") == (1, 2, 3)
        assert _version_release_tuple("garbage") == (0, 0, 0)

    def test_downgrade_blocked(
        self, agent_client: Any, auth_headers: dict, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr("aems_agent.routes.AGENT_VERSION", "1.2.3")
        resp = agent_client.post("/self-update", json={"version": "1.2.2"}, headers=auth_headers)
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "downgrade_blocked"

    def test_upgrade_passes_floor(
        self, agent_client: Any, auth_headers: dict, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr("aems_agent.routes.AGENT_VERSION", "1.2.3")

        def _boom(*_a: Any, **_k: Any) -> str:
            raise RuntimeError("network disabled in test")

        monkeypatch.setattr("aems_agent.routes._fetch_text", _boom)
        resp = agent_client.post("/self-update", json={"version": "9.9.9"}, headers=auth_headers)
        # Past the floor: anything but the downgrade rejection (501 unsupported
        # platform, or 502 once the mocked fetch fails).
        assert resp.status_code != 400

    def test_same_version_reinstall_allowed(
        self, agent_client: Any, auth_headers: dict, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr("aems_agent.routes.AGENT_VERSION", "1.2.3")

        def _boom(*_a: Any, **_k: Any) -> str:
            raise RuntimeError("network disabled in test")

        monkeypatch.setattr("aems_agent.routes._fetch_text", _boom)
        resp = agent_client.post("/self-update", json={"version": "1.2.3"}, headers=auth_headers)
        assert resp.status_code != 400


# ---------------------------------------------------------------------------
# Fix #5 — pairing compare_digest byte-safety + ASCII-only PIN (correctness)
# ---------------------------------------------------------------------------


class TestPairingConstantTimeCompare:
    """Pairing comparisons passed raw str to secrets.compare_digest, so a
    non-ASCII origin / challenge_id / Unicode-digit PIN raised TypeError→500."""

    def test_ct_eq_handles_non_ascii_without_raising(self) -> None:
        from aems_agent.routes import _ct_eq

        # The raw secrets.compare_digest(str, str) would TypeError on these.
        assert _ct_eq("café", "café") is True
        assert _ct_eq("café", "cafe") is False
        assert _ct_eq("٠١٢٣٤٥", "012345") is False
        lone_surrogate = chr(0xD800)
        assert _ct_eq(lone_surrogate, lone_surrogate) is True
        assert _ct_eq(lone_surrogate, "x") is False

    def test_unicode_digit_pin_rejected_at_validation(self, agent_client: Any) -> None:
        # Arabic-Indic digits satisfy \d but not [0-9]; must 422 at the model
        # layer, never reaching compare_digest as a 500.
        resp = agent_client.post(
            "/pair/complete",
            json={
                "challenge_id": "x",
                "origin": "http://127.0.0.1:8080",
                "pin": "٠١٢٣٤٥",
            },
            headers={"Origin": "http://127.0.0.1:8080"},
        )
        assert resp.status_code == 422

    def test_non_ascii_origin_body_does_not_500(self, agent_client: Any) -> None:
        # HTTP headers are ASCII-only, so the realistic non-ASCII vector is the
        # JSON body field. It reaches _ct_eq against the ASCII Origin header;
        # the old raw compare_digest(str, str) would TypeError → 500.
        resp = agent_client.post(
            "/pair/initiate",
            json={"origin": "http://café.example"},
            headers={"Origin": "http://127.0.0.1:8080"},
        )
        assert resp.status_code in (400, 403)

    def test_lone_surrogate_origin_body_does_not_500(self, agent_client: Any) -> None:
        # json.loads accepts escaped lone surrogates. Plain UTF-8 encoding would
        # raise UnicodeEncodeError in _ct_eq; the request must stay a clean 4xx.
        resp = agent_client.post(
            "/pair/initiate",
            content=b'{"origin":"http://\\ud800.example"}',
            headers={
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:8080",
            },
        )
        assert resp.status_code in (400, 403)


# ---------------------------------------------------------------------------
# Fix #6 — legacy private-key permissions not re-tightened on read (INFO)
# ---------------------------------------------------------------------------


class TestPrivateKeyPermissions:
    """ensure_keypair returned early when both key files existed, leaving a
    loosely-permissioned legacy private key untightened."""

    def test_legacy_private_key_tightened(self, tmp_path: Path) -> None:
        if os.name == "nt":
            pytest.skip("POSIX permission semantics only")

        from aems_agent.crypto import ensure_keypair

        ensure_keypair(tmp_path)
        priv = tmp_path / "agent_private.key"
        priv.chmod(0o644)  # simulate a key written by an older agent version

        ensure_keypair(tmp_path)
        assert priv.stat().st_mode & 0o777 == 0o600

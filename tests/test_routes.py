"""Tests for agent REST API endpoints."""

import hashlib
import importlib.util
import time
from pathlib import Path
from typing import Any

import pytest


def _skip_if_no_fastapi() -> None:
    """Skip test if FastAPI/httpx not installed."""
    if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("httpx") is None:
        pytest.skip("fastapi/httpx not installed")


def _reset_pairing_rate_limiters() -> None:
    """Reset module-level pairing rate limiters between tests."""
    from aems_agent import routes

    routes._rate_limiter.reset()
    routes._pairing_rate_limiter.reset()


class TestStatusEndpoint:
    """Tests for GET /status (no auth required)."""

    def test_status_returns_ok(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "aems-agent"

    def test_status_no_auth_needed(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/status")
        assert resp.status_code == 200


class TestHealthEndpoint:
    """Tests for GET /health (auth required)."""

    def test_health_requires_auth(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/health")
        assert resp.status_code == 401

    def test_health_invalid_token(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/health", headers={"Authorization": "Bearer bad-token"})
        assert resp.status_code == 403

    def test_health_ok(self, agent_client: Any, auth_headers: dict) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert data["storage_configured"] is True
        assert data["storage_exists"] is True
        assert data["storage_writable"] is True

    def test_health_shows_disk_space(self, agent_client: Any, auth_headers: dict) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/health", headers=auth_headers)
        data = resp.json()
        assert "disk_total_bytes" in data
        assert "disk_free_bytes" in data


class TestConfigPathEndpoints:
    """Tests for GET/PUT /config/path."""

    def test_get_path(self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/config/path", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["path"] == str(tmp_storage_path)

    def test_set_path(self, agent_client: Any, auth_headers: dict, tmp_path: Path) -> None:
        _skip_if_no_fastapi()
        new_path = tmp_path / "new_storage"
        new_path.mkdir()
        resp = agent_client.put(
            "/config/path",
            json={"path": str(new_path)},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["path"] == str(new_path)

    def test_set_path_creates_directory(
        self, agent_client: Any, auth_headers: dict, tmp_path: Path
    ) -> None:
        _skip_if_no_fastapi()
        new_path = tmp_path / "auto_created"
        resp = agent_client.put(
            "/config/path",
            json={"path": str(new_path)},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert new_path.exists()

    def test_set_relative_path_rejected(self, agent_client: Any, auth_headers: dict) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.put(
            "/config/path",
            json={"path": "relative/path"},
            headers=auth_headers,
        )
        assert resp.status_code == 422  # Pydantic validation error

class TestFileOperations:
    """Tests for file store/retrieve/delete endpoints."""

    def test_store_and_retrieve_pdf(
        self,
        agent_client: Any,
        auth_headers: dict,
        sample_pdf: bytes,
    ) -> None:
        _skip_if_no_fastapi()
        sha256 = hashlib.sha256(sample_pdf).hexdigest()

        # Store
        resp = agent_client.put(
            "/files/123/456",
            content=sample_pdf,
            headers={**auth_headers, "X-SHA256": sha256, "Content-Type": "application/pdf"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["sha256"] == sha256
        assert data["size"] == len(sample_pdf)

        # Retrieve
        resp = agent_client.get("/files/123/456", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.content == sample_pdf
        assert resp.headers["X-SHA256"] == sha256

    def test_store_sha256_mismatch(
        self,
        agent_client: Any,
        auth_headers: dict,
        sample_pdf: bytes,
    ) -> None:
        _skip_if_no_fastapi()
        # Use a valid hex format but wrong hash
        wrong_hash = "a" * 64
        resp = agent_client.put(
            "/files/123/456",
            content=sample_pdf,
            headers={**auth_headers, "X-SHA256": wrong_hash, "Content-Type": "application/pdf"},
        )
        assert resp.status_code == 400
        assert "mismatch" in resp.json()["detail"].lower()

    def test_store_non_pdf_rejected(self, agent_client: Any, auth_headers: dict) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.put(
            "/files/123/456",
            content=b"not a pdf",
            headers={**auth_headers, "Content-Type": "application/pdf"},
        )
        assert resp.status_code == 400
        assert "PDF" in resp.json()["detail"]

    def test_get_nonexistent_returns_404(self, agent_client: Any, auth_headers: dict) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/files/999/999", headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_submission(
        self,
        agent_client: Any,
        auth_headers: dict,
        sample_pdf: bytes,
    ) -> None:
        _skip_if_no_fastapi()
        # Store first
        agent_client.put(
            "/files/123/456",
            content=sample_pdf,
            headers={**auth_headers, "Content-Type": "application/pdf"},
        )

        # Delete
        resp = agent_client.delete("/files/123/456", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # Verify gone
        resp = agent_client.get("/files/123/456", headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, agent_client: Any, auth_headers: dict) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.delete("/files/999/999", headers=auth_headers)
        assert resp.status_code == 404


class TestListSubmissions:
    """Tests for GET /files/{assignment_id}."""

    def test_list_empty_assignment(self, agent_client: Any, auth_headers: dict) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/files/123", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["submissions"] == []

    def test_list_with_submissions(
        self,
        agent_client: Any,
        auth_headers: dict,
        sample_pdf: bytes,
    ) -> None:
        _skip_if_no_fastapi()
        # Store two submissions
        for sub_id in ["100", "200"]:
            agent_client.put(
                f"/files/123/{sub_id}",
                content=sample_pdf,
                headers={**auth_headers, "Content-Type": "application/pdf"},
            )

        resp = agent_client.get("/files/123", headers=auth_headers)
        assert resp.status_code == 200
        submissions = resp.json()["submissions"]
        assert len(submissions) == 2
        sub_ids = {s["submission_id"] for s in submissions}
        assert sub_ids == {"100", "200"}


class TestAnnotatedPDFs:
    """Tests for annotated PDF endpoints."""

    def test_store_and_retrieve_annotated(
        self,
        agent_client: Any,
        auth_headers: dict,
        sample_pdf: bytes,
    ) -> None:
        _skip_if_no_fastapi()
        # Store original first (creates the directory)
        agent_client.put(
            "/files/123/456",
            content=sample_pdf,
            headers={**auth_headers, "Content-Type": "application/pdf"},
        )

        annotated_pdf = b"%PDF-1.4 annotated content here"
        sha256 = hashlib.sha256(annotated_pdf).hexdigest()

        # Store annotated
        resp = agent_client.put(
            "/files/123/456/annotated",
            content=annotated_pdf,
            headers={**auth_headers, "X-SHA256": sha256, "Content-Type": "application/pdf"},
        )
        assert resp.status_code == 200

        # Retrieve annotated
        resp = agent_client.get("/files/123/456/annotated", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.content == annotated_pdf
        assert resp.headers["X-SHA256"] == sha256

    def test_get_annotated_not_found(self, agent_client: Any, auth_headers: dict) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/files/123/456/annotated", headers=auth_headers)
        assert resp.status_code == 404

    def test_list_shows_annotated(
        self,
        agent_client: Any,
        auth_headers: dict,
        sample_pdf: bytes,
    ) -> None:
        _skip_if_no_fastapi()
        # Store original
        agent_client.put(
            "/files/123/456",
            content=sample_pdf,
            headers={**auth_headers, "Content-Type": "application/pdf"},
        )
        # Store annotated
        agent_client.put(
            "/files/123/456/annotated",
            content=sample_pdf,
            headers={**auth_headers, "Content-Type": "application/pdf"},
        )

        resp = agent_client.get("/files/123", headers=auth_headers)
        submissions = resp.json()["submissions"]
        assert len(submissions) == 1
        assert submissions[0]["has_submission"] is True
        assert submissions[0]["has_annotated"] is True


class TestPathTraversal:
    """Tests for path traversal prevention."""

    def test_traversal_in_assignment_id(self, agent_client: Any, auth_headers: dict) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/files/..%2F..%2Fetc/passwd", headers=auth_headers)
        # FastAPI/Starlette decodes %2F and the path doesn't match routes → 404,
        # or the security layer catches it → 400/500. 200 must never be accepted.
        assert resp.status_code in (400, 404, 422)

    def test_traversal_in_submission_id(
        self,
        agent_client: Any,
        auth_headers: dict,
        sample_pdf: bytes,
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.put(
            "/files/123/..%2F..%2F..%2Fetc%2Fpasswd",
            content=sample_pdf,
            headers={**auth_headers, "Content-Type": "application/pdf"},
        )
        # Either routing rejects it (404) or security layer catches it (500)
        assert resp.status_code in (400, 404, 500)


class TestAuthentication:
    """Tests for authentication edge cases."""

    def test_missing_auth_header(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/health")
        assert resp.status_code == 401

    def test_invalid_auth_format(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/health", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert resp.status_code == 401

    def test_wrong_token(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/health", headers={"Authorization": "Bearer wrong-token"})
        assert resp.status_code == 403


class TestPairing:
    """Tests for pairing endpoints."""

    def _get_active_pin(self) -> str:
        """Helper: read the PIN from the active pairing challenge."""
        from aems_agent import routes

        assert routes._pairing_challenge is not None
        return routes._pairing_challenge["pin"]

    def test_pair_success(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        _reset_pairing_rate_limiters()
        origin = "http://127.0.0.1:8080"
        init_resp = agent_client.post(
            "/pair/initiate",
            json={"origin": origin},
            headers={"Origin": origin},
        )
        assert init_resp.status_code == 200
        challenge_id = init_resp.json()["challenge_id"]
        assert init_resp.json()["requires_pin"] is True
        pin = self._get_active_pin()

        complete_resp = agent_client.post(
            "/pair/complete",
            json={"challenge_id": challenge_id, "origin": origin, "pin": pin},
            headers={"Origin": origin},
        )
        assert complete_resp.status_code == 200
        payload = complete_resp.json()
        assert "token" in payload
        assert payload["token"]

    def test_pair_rejects_origin_mismatch(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        _reset_pairing_rate_limiters()
        init_resp = agent_client.post(
            "/pair/initiate",
            json={"origin": "http://127.0.0.1:8080"},
            headers={"Origin": "http://127.0.0.1:8080"},
        )
        assert init_resp.status_code == 200
        challenge_id = init_resp.json()["challenge_id"]

        complete_resp = agent_client.post(
            "/pair/complete",
            json={"challenge_id": challenge_id, "origin": "https://example.com", "pin": "000000"},
            headers={"Origin": "https://example.com"},
        )
        assert complete_resp.status_code == 403
        assert "origin mismatch" in complete_resp.json()["detail"].lower()

    def test_pair_rejects_body_header_origin_mismatch(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        _reset_pairing_rate_limiters()

        init_resp = agent_client.post(
            "/pair/initiate",
            json={"origin": "http://127.0.0.1:8080"},
            headers={"Origin": "http://localhost:8080"},
        )
        assert init_resp.status_code == 403
        assert "header mismatch" in init_resp.json()["detail"].lower()

    def test_pair_complete_no_prior_initiate(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        _reset_pairing_rate_limiters()
        from aems_agent import routes

        routes._pairing_challenge = None
        resp = agent_client.post(
            "/pair/complete",
            json={"challenge_id": "fake", "origin": "http://127.0.0.1:8080", "pin": "000000"},
            headers={"Origin": "http://127.0.0.1:8080"},
        )
        assert resp.status_code == 400

    def test_pair_complete_expired_challenge(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        _reset_pairing_rate_limiters()
        origin = "http://127.0.0.1:8080"
        init_resp = agent_client.post(
            "/pair/initiate",
            json={"origin": origin},
            headers={"Origin": origin},
        )
        assert init_resp.status_code == 200
        challenge_id = init_resp.json()["challenge_id"]

        from aems_agent import routes

        pin = routes._pairing_challenge["pin"]
        routes._pairing_challenge["expires_at"] = time.time() - 1

        resp = agent_client.post(
            "/pair/complete",
            json={"challenge_id": challenge_id, "origin": origin, "pin": pin},
            headers={"Origin": origin},
        )
        assert resp.status_code == 410

    def test_pair_wrong_challenge_id_clears_challenge(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        _reset_pairing_rate_limiters()
        origin = "http://127.0.0.1:8080"
        init_resp = agent_client.post(
            "/pair/initiate",
            json={"origin": origin},
            headers={"Origin": origin},
        )
        assert init_resp.status_code == 200
        challenge_id = init_resp.json()["challenge_id"]

        from aems_agent import routes

        pin = routes._pairing_challenge["pin"]

        bad_resp = agent_client.post(
            "/pair/complete",
            json={"challenge_id": "WRONG_ID", "origin": origin, "pin": pin},
            headers={"Origin": origin},
        )
        assert bad_resp.status_code == 403

        # Challenge should be consumed — correct ID should also fail now
        _reset_pairing_rate_limiters()
        retry_resp = agent_client.post(
            "/pair/complete",
            json={"challenge_id": challenge_id, "origin": origin, "pin": pin},
            headers={"Origin": origin},
        )
        assert retry_resp.status_code == 400


class TestPathValidation:
    """Tests for _validate_path_segment via HTTP requests."""

    def test_dot_in_assignment_id_rejected(self, agent_client: Any, auth_headers: dict) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/files/assign.ment", headers=auth_headers)
        assert resp.status_code == 400

    def test_special_chars_in_submission_id_rejected(
        self,
        agent_client: Any,
        auth_headers: dict,
        sample_pdf: bytes,
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.put(
            "/files/123/sub%2Aid",
            content=sample_pdf,
            headers={**auth_headers, "Content-Type": "application/pdf"},
        )
        assert resp.status_code in (400, 404, 422)

    def test_space_in_assignment_id_rejected(
        self, agent_client: Any, auth_headers: dict
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/files/assign%20ment", headers=auth_headers)
        assert resp.status_code in (400, 404, 422)


class TestSha256Validation:
    """Tests for X-SHA256 header validation."""

    def test_invalid_sha256_format_rejected(
        self,
        agent_client: Any,
        auth_headers: dict,
        sample_pdf: bytes,
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.put(
            "/files/123/456",
            content=sample_pdf,
            headers={
                **auth_headers,
                "X-SHA256": "not-a-valid-hex-string!!!",
                "Content-Type": "application/pdf",
            },
        )
        assert resp.status_code == 400
        assert "format" in resp.json()["detail"].lower()

    def test_sha256_mismatch_no_leak(
        self,
        agent_client: Any,
        auth_headers: dict,
        sample_pdf: bytes,
    ) -> None:
        _skip_if_no_fastapi()
        fake_hash = "a" * 64
        resp = agent_client.put(
            "/files/123/456",
            content=sample_pdf,
            headers={
                **auth_headers,
                "X-SHA256": fake_hash,
                "Content-Type": "application/pdf",
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        # Should NOT contain the actual or expected hash
        assert fake_hash not in detail

    def test_empty_body_rejected(
        self,
        agent_client: Any,
        auth_headers: dict,
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.put(
            "/files/123/456",
            content=b"",
            headers={**auth_headers, "Content-Type": "application/pdf"},
        )
        assert resp.status_code == 400



# ---------------------------------------------------------------------------
# C1 PIN Pairing Tests (Phase 2)
# ---------------------------------------------------------------------------


class TestPairingPIN:
    """Tests for PIN-based pairing confirmation (C1)."""

    def _get_active_pin(self) -> str:
        from aems_agent import routes

        assert routes._pairing_challenge is not None
        return routes._pairing_challenge["pin"]

    def test_pair_initiate_returns_requires_pin(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        origin = "http://127.0.0.1:8080"
        resp = agent_client.post(
            "/pair/initiate",
            json={"origin": origin},
            headers={"Origin": origin},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["requires_pin"] is True
        assert "challenge_id" in data

    def test_pair_complete_requires_pin_field(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        origin = "http://127.0.0.1:8080"
        init_resp = agent_client.post(
            "/pair/initiate",
            json={"origin": origin},
            headers={"Origin": origin},
        )
        challenge_id = init_resp.json()["challenge_id"]
        # Missing pin field → 422
        resp = agent_client.post(
            "/pair/complete",
            json={"challenge_id": challenge_id, "origin": origin},
            headers={"Origin": origin},
        )
        assert resp.status_code == 422

    def test_pair_complete_wrong_pin_rejected(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        origin = "http://127.0.0.1:8080"
        init_resp = agent_client.post(
            "/pair/initiate",
            json={"origin": origin},
            headers={"Origin": origin},
        )
        challenge_id = init_resp.json()["challenge_id"]
        resp = agent_client.post(
            "/pair/complete",
            json={"challenge_id": challenge_id, "origin": origin, "pin": "000000"},
            headers={"Origin": origin},
        )
        # Wrong PIN → 403 (unless 000000 happens to be the real pin, extremely unlikely)
        # The challenge is also consumed
        assert resp.status_code == 403
        assert "pin" in resp.json()["detail"].lower()

    def test_pair_complete_correct_pin_succeeds(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        origin = "http://127.0.0.1:8080"
        init_resp = agent_client.post(
            "/pair/initiate",
            json={"origin": origin},
            headers={"Origin": origin},
        )
        challenge_id = init_resp.json()["challenge_id"]
        pin = self._get_active_pin()
        resp = agent_client.post(
            "/pair/complete",
            json={"challenge_id": challenge_id, "origin": origin, "pin": pin},
            headers={"Origin": origin},
        )
        assert resp.status_code == 200
        assert "token" in resp.json()

    def test_pair_confirm_shows_pin(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        origin = "http://127.0.0.1:8080"
        agent_client.post(
            "/pair/initiate",
            json={"origin": origin},
            headers={"Origin": origin},
        )
        pin = self._get_active_pin()
        resp = agent_client.get("/pair/confirm")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is True
        assert data["pin"] == pin
        assert data["origin"] == origin
        assert "expires_in" in data

    def test_pair_confirm_no_active_challenge(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/pair/confirm")
        assert resp.status_code == 200
        assert resp.json()["active"] is False

    def test_pair_confirm_expired(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        from aems_agent import routes

        origin = "http://127.0.0.1:8080"
        agent_client.post(
            "/pair/initiate",
            json={"origin": origin},
            headers={"Origin": origin},
        )
        routes._pairing_challenge["expires_at"] = time.time() - 1
        resp = agent_client.get("/pair/confirm")
        assert resp.status_code == 200
        assert resp.json()["active"] is False

    def test_pin_consumed_after_use(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        origin = "http://127.0.0.1:8080"
        init_resp = agent_client.post(
            "/pair/initiate",
            json={"origin": origin},
            headers={"Origin": origin},
        )
        challenge_id = init_resp.json()["challenge_id"]
        pin = self._get_active_pin()
        # Complete successfully
        resp = agent_client.post(
            "/pair/complete",
            json={"challenge_id": challenge_id, "origin": origin, "pin": pin},
            headers={"Origin": origin},
        )
        assert resp.status_code == 200
        # Challenge consumed → confirm returns inactive
        confirm = agent_client.get("/pair/confirm")
        assert confirm.json()["active"] is False


# ---------------------------------------------------------------------------
# CORS dynamic origin tests
# ---------------------------------------------------------------------------


class TestCORSDynamicOrigins:
    """Verify CORS headers update after pairing completes."""

    def test_pairing_adds_cors_origin(self, agent_client: Any) -> None:
        """After successful pairing, the new origin gets CORS headers."""
        _skip_if_no_fastapi()
        _reset_pairing_rate_limiters()
        origin = "http://localhost:9999"  # not in default allowed_origins

        # Initiate pairing
        init_resp = agent_client.post(
            "/pair/initiate",
            json={"origin": origin},
            headers={"Origin": origin},
        )
        assert init_resp.status_code == 200
        challenge_id = init_resp.json()["challenge_id"]

        # Read PIN from internal state
        from aems_agent import routes

        pin = routes._pairing_challenge["pin"]

        # Complete pairing
        complete_resp = agent_client.post(
            "/pair/complete",
            json={"challenge_id": challenge_id, "origin": origin, "pin": pin},
            headers={"Origin": origin},
        )
        assert complete_resp.status_code == 200

        # Verify origin was added to live CORS list
        cors_origins = getattr(agent_client.app.state, "cors_origins", None)
        assert cors_origins is not None
        assert origin in cors_origins

    def test_localhost_origin_gets_cors_before_pairing(self, agent_client: Any) -> None:
        """Localhost origins get CORS headers via regex even before pairing."""
        _skip_if_no_fastapi()
        origin = "http://localhost:3000"
        resp = agent_client.get("/status", headers={"Origin": origin})
        assert resp.status_code == 200
        # CORSMiddleware should match via allow_origin_regex
        assert resp.headers.get("access-control-allow-origin") == origin


# ---------------------------------------------------------------------------
# Test Gap 2: _normalize_origin edge cases
# ---------------------------------------------------------------------------


class TestNormalizeOrigin:
    """Tests for _normalize_origin edge cases."""

    def test_ftp_rejected(self) -> None:
        from aems_agent.routes import _normalize_origin

        assert _normalize_origin("ftp://example.com") is None

    def test_empty_string(self) -> None:
        from aems_agent.routes import _normalize_origin

        assert _normalize_origin("") is None

    def test_none(self) -> None:
        from aems_agent.routes import _normalize_origin

        assert _normalize_origin(None) is None

    def test_javascript_scheme(self) -> None:
        from aems_agent.routes import _normalize_origin

        assert _normalize_origin("javascript:alert(1)") is None

    def test_whitespace_only(self) -> None:
        from aems_agent.routes import _normalize_origin

        assert _normalize_origin("   ") is None

    def test_valid_http(self) -> None:
        from aems_agent.routes import _normalize_origin

        assert _normalize_origin("http://example.com") == "http://example.com"

    def test_valid_https_with_port(self) -> None:
        from aems_agent.routes import _normalize_origin

        assert _normalize_origin("https://example.com:8080") == "https://example.com:8080"

    def test_path_rejected(self) -> None:
        from aems_agent.routes import _normalize_origin

        assert _normalize_origin("http://example.com/path") is None

    def test_query_rejected(self) -> None:
        from aems_agent.routes import _normalize_origin

        assert _normalize_origin("http://example.com?q=1") is None


# ---------------------------------------------------------------------------
# Test Gap 3: 503 paths (no storage)
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_client_no_storage(tmp_path: Path) -> Any:
    """Agent client with no storage_path configured."""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi not installed")
        return

    from aems_agent.app import create_app
    from aems_agent.config import AgentConfig, save_config, ensure_auth_token

    config = AgentConfig(storage_path=None, port=61234, host="127.0.0.1")
    config_dir = tmp_path / "no_storage_cfg"
    config_dir.mkdir()
    save_config(config, config_dir)
    ensure_auth_token(config_dir)

    app = create_app(config_dir=config_dir)
    return TestClient(app)


@pytest.fixture
def no_storage_auth_headers(tmp_path: Path) -> dict:
    from aems_agent.config import ensure_auth_token

    config_dir = tmp_path / "no_storage_cfg"
    token = ensure_auth_token(config_dir)
    return {"Authorization": f"Bearer {token}"}


class TestNoStoragePaths:
    """Tests for 503 responses when storage is not configured."""

    def test_list_returns_503(
        self, agent_client_no_storage: Any, no_storage_auth_headers: dict
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client_no_storage.get("/files/123", headers=no_storage_auth_headers)
        assert resp.status_code == 503

    def test_get_returns_503(
        self, agent_client_no_storage: Any, no_storage_auth_headers: dict
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client_no_storage.get("/files/123/456", headers=no_storage_auth_headers)
        assert resp.status_code == 503

    def test_store_returns_503(
        self, agent_client_no_storage: Any, no_storage_auth_headers: dict
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client_no_storage.put(
            "/files/123/456",
            content=b"%PDF-1.4 test",
            headers={**no_storage_auth_headers, "Content-Type": "application/pdf"},
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Test Gap 4: 413 upload size limit
# ---------------------------------------------------------------------------


class TestUploadSizeLimit:
    """Tests for 413 upload size limit."""

    def test_oversized_upload_rejected(
        self, agent_client: Any, auth_headers: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _skip_if_no_fastapi()
        from aems_agent import routes

        monkeypatch.setattr(routes, "_MAX_UPLOAD_BYTES", 100)
        big_pdf = b"%PDF-1.4 " + b"x" * 200
        resp = agent_client.put(
            "/files/123/456",
            content=big_pdf,
            headers={**auth_headers, "Content-Type": "application/pdf"},
        )
        assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Test Gap 7: Status endpoint exact fields
# ---------------------------------------------------------------------------


class TestStatusExactFields:
    """Tests for exact field set in /status response."""

    def test_status_exact_keys(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        expected_keys = {"status", "service", "version", "api_version", "min_client_version", "storage_configured"}
        assert set(data.keys()) == expected_keys


# ---------------------------------------------------------------------------
# Duplicate log handler regression test
# ---------------------------------------------------------------------------


class TestCreateAppLogHandlers:
    """Ensure repeated create_app() doesn't duplicate log handlers."""

    def test_no_duplicate_handlers(self, tmp_path: Path) -> None:
        _skip_if_no_fastapi()
        import logging
        import logging.handlers

        from aems_agent.app import create_app
        from aems_agent.config import save_config, AgentConfig

        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        save_config(AgentConfig(), config_dir)

        agent_logger = logging.getLogger("aems_agent")
        initial_count = len([
            h for h in agent_logger.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ])

        create_app(config_dir)
        create_app(config_dir)

        rotating_count = len([
            h for h in agent_logger.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ])
        # At most one new RotatingFileHandler should exist
        assert rotating_count <= initial_count + 1

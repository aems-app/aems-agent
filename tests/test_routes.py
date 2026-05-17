"""Tests for agent REST API endpoints."""

import hashlib
import inspect
import importlib.util
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest


def _skip_if_no_fastapi() -> None:
    """Skip test if FastAPI/httpx not installed."""
    if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("httpx") is None:
        pytest.skip("fastapi/httpx not installed")


def _supports_private_network_cors() -> bool:
    """Return True when this Starlette CORSMiddleware supports PNA preflights."""
    from fastapi.middleware.cors import CORSMiddleware

    return "allow_private_network" in inspect.signature(CORSMiddleware.__init__).parameters


def _reset_pairing_rate_limiters() -> None:
    """Reset module-level pairing rate limiters between tests."""
    from aems_agent import routes

    routes._rate_limiter.reset()
    routes._pairing_rate_limiter.reset()


class TestCapabilitiesEndpoint:
    """Tests for GET /capabilities (no auth required)."""

    def test_capabilities_returns_agent_info(self, agent_client: Any) -> None:
        """GET /capabilities returns version, contract versions, and key ID."""
        _skip_if_no_fastapi()
        resp = agent_client.get("/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        assert "agent_version" in data
        assert data["supported_contract_versions"] == [1]
        assert data["supported_annotation_contract_versions"] == [1]
        assert "encryption_key_id" in data
        assert "public_key_base64" in data
        assert len(data["encryption_key_id"]) == 16

    def test_capabilities_no_auth_required(self, agent_client: Any) -> None:
        """GET /capabilities works without bearer token."""
        _skip_if_no_fastapi()
        resp = agent_client.get("/capabilities")  # no auth headers
        assert resp.status_code == 200

    def test_capabilities_has_features(self, agent_client: Any) -> None:
        """GET /capabilities includes features list."""
        _skip_if_no_fastapi()
        resp = agent_client.get("/capabilities")
        data = resp.json()
        assert "features" in data
        assert "file_storage" in data["features"]
        assert "canvas_download" in data["features"]

    def test_capabilities_public_key_is_valid_base64(self, agent_client: Any) -> None:
        """GET /capabilities returns a valid base64-encoded public key."""
        _skip_if_no_fastapi()
        import base64

        resp = agent_client.get("/capabilities")
        data = resp.json()
        raw = base64.b64decode(data["public_key_base64"])
        # X25519 public key is 32 bytes
        assert len(raw) == 32


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


class TestInfoEndpoint:
    """Tests for GET /info (auth required, minimal metadata)."""

    def test_info_requires_auth(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/info")
        assert resp.status_code == 401

    def test_info_invalid_token(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/info", headers={"Authorization": "Bearer bad-token"})
        assert resp.status_code == 403

    def test_info_returns_minimal_fields(self, agent_client: Any, auth_headers: dict) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/info", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"version", "api_version", "min_client_version"}


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


class TestDeleteAssignmentFiles:
    """Tests for DELETE /files/{assignment_id}."""

    def test_delete_assignment_removes_submission_data_and_cache(
        self,
        agent_client: Any,
        auth_headers: dict,
        tmp_storage_path: Path,
        sample_pdf: bytes,
    ) -> None:
        _skip_if_no_fastapi()
        agent_client.put(
            "/files/a1/s1",
            content=sample_pdf,
            headers={**auth_headers, "Content-Type": "application/pdf"},
        )
        agent_client.put(
            "/files/a1/s1/annotated",
            content=sample_pdf,
            headers={**auth_headers, "Content-Type": "application/pdf"},
        )

        results_dir = tmp_storage_path / "_data" / "a1" / "results"
        results_dir.mkdir(parents=True)
        (results_dir / "s1.json").write_text("{}", encoding="utf-8")
        (tmp_storage_path / "_data" / "a1" / "assignment.json").write_text(
            "{}",
            encoding="utf-8",
        )

        cache_dir = tmp_storage_path / "_cache" / "bundles" / "a1" / "s1"
        cache_dir.mkdir(parents=True)
        (cache_dir / "bundle.json").write_text("{}", encoding="utf-8")

        resp = agent_client.delete("/files/a1", headers=auth_headers)
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["success"] is True
        assert payload["assignment_deleted"] is True
        assert payload["data_deleted"] is True
        assert payload["cache_deleted"] is True
        assert not (tmp_storage_path / "a1").exists()
        assert not (tmp_storage_path / "_data" / "a1").exists()
        assert not (tmp_storage_path / "_cache" / "bundles" / "a1").exists()

    def test_delete_assignment_with_only_data_succeeds(
        self,
        agent_client: Any,
        auth_headers: dict,
        tmp_storage_path: Path,
    ) -> None:
        _skip_if_no_fastapi()
        data_dir = tmp_storage_path / "_data" / "a1"
        data_dir.mkdir(parents=True)
        (data_dir / "assignment.json").write_text("{}", encoding="utf-8")

        resp = agent_client.delete("/files/a1", headers=auth_headers)
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["assignment_deleted"] is False
        assert payload["data_deleted"] is True
        assert payload["cache_deleted"] is False
        assert not data_dir.exists()

    def test_delete_assignment_nonexistent_returns_404(
        self,
        agent_client: Any,
        auth_headers: dict,
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.delete("/files/missing", headers=auth_headers)
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

    def test_pair_confirm_hides_pin_and_origin(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        origin = "http://127.0.0.1:8080"
        agent_client.post(
            "/pair/initiate",
            json={"origin": origin},
            headers={"Origin": origin},
        )
        resp = agent_client.get("/pair/confirm")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is True
        assert "expires_in" in data
        assert "pin" not in data
        assert "origin" not in data

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

    def test_pair_initiate_omits_storage_metadata(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        origin = "http://127.0.0.1:8080"
        resp = agent_client.post(
            "/pair/initiate",
            json={"origin": origin},
            headers={"Origin": origin},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "storage_configured" not in data


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

    def test_private_network_preflight_is_allowed_for_localhost_origin(self, agent_client: Any) -> None:
        """Loopback origins must pass Private Network Access preflights."""
        _skip_if_no_fastapi()
        if not _supports_private_network_cors():
            pytest.skip("Installed CORSMiddleware does not support allow_private_network")
        origin = "https://127.0.0.1:8080"
        resp = agent_client.options(
            "/files/test-assignment/test-submission",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "authorization,content-type",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == origin
        assert resp.headers.get("access-control-allow-private-network") == "true"


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
        expected_keys = {"status", "service", "version", "api_version", "min_client_version"}
        assert set(data.keys()) == expected_keys


# ---------------------------------------------------------------------------
# Duplicate log handler regression test
# ---------------------------------------------------------------------------


class TestDataJsonEndpoints:
    """Tests for /data/ JSON storage endpoints."""

    def test_put_get_result_json(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        """PUT then GET a grading result JSON."""
        _skip_if_no_fastapi()
        payload = {"mark_results": [{"verdict": "PASS"}], "annotation_contract_version": 1}
        resp = agent_client.put(
            "/data/100/results/200.json",
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "receipt" in data  # SHA-256 of written content
        assert "written_at" in data

        resp2 = agent_client.get("/data/100/results/200.json", headers=auth_headers)
        assert resp2.status_code == 200
        assert resp2.json() == payload

    def test_put_get_result_json_with_string_ids(
        self, agent_client: Any, auth_headers: dict
    ) -> None:
        _skip_if_no_fastapi()
        payload = {"feedback_items": [], "annotation_contract_version": 1}
        resp = agent_client.put(
            "/data/assign-1/results/sub-1.json",
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 200

        resp2 = agent_client.get("/data/assign-1/results/sub-1.json", headers=auth_headers)
        assert resp2.status_code == 200
        assert resp2.json() == payload

    def test_list_results(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        """GET /data/{aid}/results/ lists all stored result files."""
        _skip_if_no_fastapi()
        for sid in [201, 202, 203]:
            agent_client.put(
                f"/data/100/results/{sid}.json",
                json={"sid": sid},
                headers=auth_headers,
            )
        resp = agent_client.get("/data/100/results/", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 3

    def test_put_get_assignment_json(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        """PUT then GET assignment metadata."""
        _skip_if_no_fastapi()
        payload = {"name": "Exam 1", "course_id": 42}
        resp = agent_client.put(
            "/data/100/assignment.json",
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 200
        resp2 = agent_client.get("/data/100/assignment.json", headers=auth_headers)
        assert resp2.json() == payload

    def test_data_requires_auth(self, agent_client: Any) -> None:
        """Data endpoints require bearer token."""
        _skip_if_no_fastapi()
        resp = agent_client.get("/data/100/results/200.json")
        assert resp.status_code == 401

    def test_data_path_traversal_blocked(
        self, agent_client: Any, auth_headers: dict
    ) -> None:
        """Path traversal attempts are rejected."""
        _skip_if_no_fastapi()
        resp = agent_client.get("/data/../secrets/200.json", headers=auth_headers)
        assert resp.status_code in (400, 403, 404, 422)

    def test_result_not_found(self, agent_client: Any, auth_headers: dict) -> None:
        """GET for non-existent result returns 404."""
        _skip_if_no_fastapi()
        resp = agent_client.get("/data/999/results/999.json", headers=auth_headers)
        assert resp.status_code == 404

    def test_assignment_not_found(self, agent_client: Any, auth_headers: dict) -> None:
        """GET for non-existent assignment returns 404."""
        _skip_if_no_fastapi()
        resp = agent_client.get("/data/999/assignment.json", headers=auth_headers)
        assert resp.status_code == 404

    def test_list_empty_results(self, agent_client: Any, auth_headers: dict) -> None:
        """GET /data/{aid}/results/ with no stored results returns empty list."""
        _skip_if_no_fastapi()
        resp = agent_client.get("/data/999/results/", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_put_result_receipt_is_sha256(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        """Write receipt contains valid SHA-256 hex digest."""
        _skip_if_no_fastapi()
        import json as json_mod

        payload = {"test": True}
        resp = agent_client.put(
            "/data/100/results/300.json",
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        # Receipt should be SHA-256 of the written content
        expected_content = json_mod.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        expected_sha = hashlib.sha256(expected_content).hexdigest()
        assert data["receipt"] == expected_sha

    def test_put_result_invalidates_existing_annotated_pdf(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        _skip_if_no_fastapi()
        annotated_path = tmp_storage_path / "assign-1" / "sub-1" / "submission_annotated.pdf"
        annotated_path.parent.mkdir(parents=True, exist_ok=True)
        annotated_path.write_bytes(b"%PDF-1.4 stale")

        resp = agent_client.put(
            "/data/assign-1/results/sub-1.json",
            json={"feedback_items": [], "annotation_contract_version": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert not annotated_path.exists()

    def test_put_result_json_rejects_malformed_body(
        self, agent_client: Any, auth_headers: dict
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.put(
            "/data/100/results/200.json",
            content=b"{not-json",
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "json" in resp.json()["detail"].lower()

    def test_put_assignment_json_rejects_malformed_body(
        self, agent_client: Any, auth_headers: dict
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.put(
            "/data/100/assignment.json",
            content=b"{not-json",
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "json" in resp.json()["detail"].lower()


class TestResultWriteIdempotency:
    """Tests for delivery_id-based idempotency on PUT /data/{aid}/results/{sid}.json."""

    def _setup_results_dir(self, storage_path: Path, aid: str) -> Path:
        results_dir = storage_path / "_data" / aid / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        return results_dir

    def test_first_write_stores_delivery_sidecar(
        self, agent_client: Any, tmp_storage_path: Path, auth_headers: dict
    ) -> None:
        """First write with delivery_id creates a .delivery sidecar file."""
        _skip_if_no_fastapi()
        self._setup_results_dir(tmp_storage_path, "a1")
        delivery_id = str(uuid.uuid4())
        resp = agent_client.put(
            "/data/a1/results/s1.json",
            json={"score": 5},
            headers={**auth_headers, "X-AEMS-Delivery-Id": delivery_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "receipt" in data

        # Verify sidecar exists
        sidecar = tmp_storage_path / "_data" / "a1" / "results" / "s1.delivery"
        assert sidecar.exists()

    def test_same_delivery_id_returns_original_receipt(
        self, agent_client: Any, tmp_storage_path: Path, auth_headers: dict
    ) -> None:
        """Repeated PUT with the same delivery_id returns the original receipt unchanged."""
        _skip_if_no_fastapi()
        self._setup_results_dir(tmp_storage_path, "a1")
        delivery_id = str(uuid.uuid4())
        headers = {**auth_headers, "X-AEMS-Delivery-Id": delivery_id}

        resp1 = agent_client.put("/data/a1/results/s1.json", json={"score": 5}, headers=headers)
        assert resp1.status_code == 200
        receipt1 = resp1.json()

        resp2 = agent_client.put("/data/a1/results/s1.json", json={"score": 5}, headers=headers)
        assert resp2.status_code == 200
        receipt2 = resp2.json()

        assert receipt1["receipt"] == receipt2["receipt"]
        assert receipt1["written_at"] == receipt2["written_at"]

    def test_different_delivery_id_writes_new(
        self, agent_client: Any, tmp_storage_path: Path, auth_headers: dict
    ) -> None:
        """A different delivery_id causes a normal write with a new receipt."""
        _skip_if_no_fastapi()
        self._setup_results_dir(tmp_storage_path, "a1")
        id1 = str(uuid.uuid4())
        id2 = str(uuid.uuid4())

        resp1 = agent_client.put(
            "/data/a1/results/s1.json",
            json={"score": 5},
            headers={**auth_headers, "X-AEMS-Delivery-Id": id1},
        )
        assert resp1.status_code == 200

        resp2 = agent_client.put(
            "/data/a1/results/s1.json",
            json={"score": 7},
            headers={**auth_headers, "X-AEMS-Delivery-Id": id2},
        )
        assert resp2.status_code == 200

        assert resp2.json()["receipt"] != resp1.json()["receipt"]

    def test_no_delivery_id_header_still_works(
        self, agent_client: Any, tmp_storage_path: Path, auth_headers: dict
    ) -> None:
        """PUT without X-AEMS-Delivery-Id succeeds and creates no sidecar."""
        _skip_if_no_fastapi()
        self._setup_results_dir(tmp_storage_path, "a1")
        resp = agent_client.put(
            "/data/a1/results/s1.json",
            json={"score": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        # No sidecar should be created
        sidecar = tmp_storage_path / "_data" / "a1" / "results" / "s1.delivery"
        assert not sidecar.exists()


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


# ---------------------------------------------------------------------------
# Canvas Download Route Tests
# ---------------------------------------------------------------------------


class TestCanvasDownloadRoutes:
    """Tests for POST /canvas/download-submissions and GET /canvas/download-jobs/{job_id}."""

    def test_canvas_download_rejects_invalid_payload(
        self, agent_client: Any, auth_headers: dict
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.post(
            "/canvas/download-submissions",
            json={"encrypted_payload": "not-valid-base64!!!"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "decrypt" in resp.json()["detail"].lower()

    def test_canvas_download_job_not_found(
        self, agent_client: Any, auth_headers: dict
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/canvas/download-jobs/missing-job", headers=auth_headers)
        assert resp.status_code == 404

    def test_canvas_download_requires_auth(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.post(
            "/canvas/download-submissions",
            json={"encrypted_payload": "dGVzdA=="},
        )
        assert resp.status_code == 401

    def test_canvas_download_job_status_requires_auth(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/canvas/download-jobs/some-job")
        assert resp.status_code == 401

    def test_canvas_download_missing_payload_field(
        self, agent_client: Any, auth_headers: dict
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.post(
            "/canvas/download-submissions",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 422  # Pydantic validation error

    def test_canvas_download_accepts_encrypted_manifest(
        self, agent_client: Any, auth_headers: dict, agent_config_dir: Path
    ) -> None:
        """POST with properly encrypted manifest returns job_id."""
        _skip_if_no_fastapi()
        import base64
        import json as json_mod

        from nacl.public import PublicKey, SealedBox

        from aems_agent.crypto import get_key_id, load_public_key

        # Build manifest with the agent's real key ID
        agent_key_id = get_key_id(agent_config_dir)
        manifest = {
            "canvas_base_url": "https://university.instructure.com",
            "canvas_token": "test_token",
            "assignment_id": 100,
            "manifest_version": 1,
            "expires_at": time.time() + 300,
            "nonce": "test-nonce",
            "audience_key_id": agent_key_id,
            "submissions": [
                {"submission_id": 1001, "file_id": 569, "download_url": "/files/569/download"}
            ],
        }

        # Encrypt with agent's public key
        pub_bytes = load_public_key(agent_config_dir)
        pub_key = PublicKey(pub_bytes)
        box = SealedBox(pub_key)
        ciphertext = box.encrypt(json_mod.dumps(manifest).encode())
        payload_b64 = base64.b64encode(ciphertext).decode()

        resp = agent_client.post(
            "/canvas/download-submissions",
            json={"encrypted_payload": payload_b64},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "job_id" in data
        assert data["status"] == "pending"
        assert data["total_submissions"] == 1

    def test_canvas_download_rejects_expired_manifest(
        self, agent_client: Any, auth_headers: dict, agent_config_dir: Path
    ) -> None:
        """POST with expired manifest returns 403."""
        _skip_if_no_fastapi()
        import base64
        import json as json_mod

        from nacl.public import PublicKey, SealedBox

        from aems_agent.crypto import get_key_id, load_public_key

        agent_key_id = get_key_id(agent_config_dir)
        manifest = {
            "canvas_base_url": "https://university.instructure.com",
            "canvas_token": "test_token",
            "assignment_id": 100,
            "manifest_version": 1,
            "expires_at": time.time() - 10,  # expired
            "nonce": "test-nonce",
            "audience_key_id": agent_key_id,
            "submissions": [
                {"submission_id": 1001, "file_id": 569, "download_url": "/files/569/download"}
            ],
        }

        pub_bytes = load_public_key(agent_config_dir)
        pub_key = PublicKey(pub_bytes)
        box = SealedBox(pub_key)
        ciphertext = box.encrypt(json_mod.dumps(manifest).encode())
        payload_b64 = base64.b64encode(ciphertext).decode()

        resp = agent_client.post(
            "/canvas/download-submissions",
            json={"encrypted_payload": payload_b64},
            headers=auth_headers,
        )
        assert resp.status_code == 403
        assert "manifest validation failed" in resp.json()["detail"].lower()

    def test_canvas_download_rejects_unapproved_custom_host(
        self, agent_client: Any, auth_headers: dict, agent_config_dir: Path
    ) -> None:
        """Self-hosted Canvas domains must be explicitly allowlisted."""
        _skip_if_no_fastapi()
        import base64
        import json as json_mod

        from nacl.public import PublicKey, SealedBox

        from aems_agent.crypto import get_key_id, load_public_key

        agent_key_id = get_key_id(agent_config_dir)
        manifest = {
            "canvas_base_url": "https://canvas.example.edu",
            "canvas_token": "test_token",
            "assignment_id": 100,
            "manifest_version": 1,
            "expires_at": time.time() + 300,
            "nonce": "test-nonce",
            "audience_key_id": agent_key_id,
            "submissions": [
                {"submission_id": 1001, "file_id": 569, "download_url": "/files/569/download"}
            ],
        }

        pub_bytes = load_public_key(agent_config_dir)
        box = SealedBox(PublicKey(pub_bytes))
        ciphertext = box.encrypt(json_mod.dumps(manifest).encode())
        payload_b64 = base64.b64encode(ciphertext).decode()

        resp = agent_client.post(
            "/canvas/download-submissions",
            json={"encrypted_payload": payload_b64},
            headers=auth_headers,
        )
        assert resp.status_code == 403
        assert "manifest validation failed" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Annotation CRUD Route Tests
# ---------------------------------------------------------------------------


def _create_annotated_pdf(tmp_storage_path: Path, aid: str, sid: str) -> Path:
    """Create a submission dir with submission.pdf + submission_annotated.pdf containing annotations."""
    from aems_pdf_annotator._fitz import fitz
    from aems_pdf_annotator import (
        PDFAnnotator,
        PDFAnnotation,
        BBox,
        AnnotationType,
        AnnotationColor,
        AnnotationSource,
    )

    sub_dir = tmp_storage_path / aid / sid
    sub_dir.mkdir(parents=True, exist_ok=True)

    # Create base submission PDF
    base_path = sub_dir / "submission.pdf"
    doc = fitz.open()
    doc.new_page(width=612, height=792)
    doc.new_page(width=612, height=792)
    doc.save(str(base_path))
    doc.close()

    # Create annotated PDF with annotations
    ann_path = sub_dir / "submission_annotated.pdf"
    doc = fitz.open()
    doc.new_page(width=612, height=792)
    doc.new_page(width=612, height=792)
    doc.save(str(ann_path))
    doc.close()

    annotations = [
        PDFAnnotation(
            page_index=0,
            bbox=BBox(x0=50, y0=700, x1=200, y1=750),
            kind=AnnotationType.TEXT,
            color=AnnotationColor.GREEN,
            comment="Correct approach",
            source=AnnotationSource.AI,
            grader_name="AI Grader",
            is_verdict=False,
        ),
        PDFAnnotation(
            page_index=0,
            bbox=BBox(x0=50, y0=500, x1=200, y1=550),
            kind=AnnotationType.TEXT,
            color=AnnotationColor.RED,
            comment="Sign error in step 3",
            source=AnnotationSource.AI,
            grader_name="AI Grader",
            is_verdict=False,
        ),
        PDFAnnotation(
            page_index=1,
            bbox=BBox(x0=50, y0=600, x1=300, y1=650),
            kind=AnnotationType.TEXT,
            color=AnnotationColor.AMBER,
            comment="Task 2: 5/10",
            source=AnnotationSource.AI,
            grader_name="AI Grader",
            is_verdict=True,
        ),
    ]

    with PDFAnnotator(ann_path) as annotator:
        for ann in annotations:
            annotator.add_annotation(ann)
        annotator.save()

    return ann_path


def _annotation_headers(auth_headers: dict) -> dict:
    """Return auth headers merged with the required annotation contract version header."""
    return {**auth_headers, "X-AEMS-Annotation-Contract-Version": "1"}


class TestAnnotationCrudEndpoints:
    """Route-level tests for annotation CRUD endpoints."""

    def test_list_annotations(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        """GET /annotations/{aid}/{sid} returns grouped annotations."""
        _skip_if_no_fastapi()
        _create_annotated_pdf(tmp_storage_path, "a1", "s1")
        headers = _annotation_headers(auth_headers)
        resp = agent_client.get("/annotations/a1/s1", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        annotations = data["annotations"]
        # Page 0 has 2, page 1 has 1
        assert len(annotations["0"]) == 2
        assert len(annotations["1"]) == 1

    def test_list_annotations_404_when_no_annotated_pdf(
        self, agent_client: Any, auth_headers: dict
    ) -> None:
        """GET /annotations/{aid}/{sid} returns 404 when annotated PDF missing."""
        _skip_if_no_fastapi()
        headers = _annotation_headers(auth_headers)
        resp = agent_client.get("/annotations/nonexist/none", headers=headers)
        assert resp.status_code == 404

    def test_get_version(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        """GET /annotations/{aid}/{sid}/version returns a version token."""
        _skip_if_no_fastapi()
        _create_annotated_pdf(tmp_storage_path, "a1", "s1")
        headers = _annotation_headers(auth_headers)
        resp = agent_client.get("/annotations/a1/s1/version", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "version" in data
        # Version token has format "mtime_ns:size"
        assert ":" in data["version"]

    def test_create_annotation(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        """POST /annotations/{aid}/{sid} creates a new annotation."""
        _skip_if_no_fastapi()
        _create_annotated_pdf(tmp_storage_path, "a1", "s1")
        headers = _annotation_headers(auth_headers)

        payload = {
            "page_index": 0,
            "content": "New teacher comment",
            "kind": "text",
            "color": "amber",
            "rect": [100, 100, 250, 150],
        }
        resp = agent_client.post(
            "/annotations/a1/s1",
            json=payload,
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["success"] is True
        assert "annotation" in data
        assert data["annotation"]["content"] == "New teacher comment"

    def test_create_annotation_returns_201(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        """POST /annotations returns HTTP 201 Created."""
        _skip_if_no_fastapi()
        _create_annotated_pdf(tmp_storage_path, "a1", "s1")
        headers = _annotation_headers(auth_headers)

        resp = agent_client.post(
            "/annotations/a1/s1",
            json={"page_index": 0, "content": "Test", "kind": "text", "color": "green"},
            headers=headers,
        )
        assert resp.status_code == 201

    def test_update_annotation_content(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        """PUT /annotations/{aid}/{sid}/{annot_id} updates content."""
        _skip_if_no_fastapi()
        _create_annotated_pdf(tmp_storage_path, "a1", "s1")
        headers = _annotation_headers(auth_headers)

        # List annotations to find a valid ID
        list_resp = agent_client.get("/annotations/a1/s1", headers=headers)
        annotations = list_resp.json()["annotations"]
        first_ann = annotations["0"][0]
        ann_id = first_ann["id"]

        # Update content
        resp = agent_client.put(
            f"/annotations/a1/s1/{ann_id}",
            json={"content": "Updated comment"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_update_annotation_cross_page_move(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        """PUT with page_index moves annotation to a different page."""
        _skip_if_no_fastapi()
        _create_annotated_pdf(tmp_storage_path, "a1", "s1")
        headers = _annotation_headers(auth_headers)

        # Get annotation on page 0
        list_resp = agent_client.get("/annotations/a1/s1", headers=headers)
        annotations = list_resp.json()["annotations"]
        first_ann = annotations["0"][0]
        ann_id = first_ann["id"]

        # Move to page 1
        resp = agent_client.put(
            f"/annotations/a1/s1/{ann_id}",
            json={"page_index": 1, "content": "Moved to page 1"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_delete_annotation(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        """DELETE /annotations/{aid}/{sid}/{annot_id} removes the annotation."""
        _skip_if_no_fastapi()
        _create_annotated_pdf(tmp_storage_path, "a1", "s1")
        headers = _annotation_headers(auth_headers)

        # List to get annotation count and ID
        list_resp = agent_client.get("/annotations/a1/s1", headers=headers)
        annotations = list_resp.json()["annotations"]
        page0_count = len(annotations["0"])
        ann_id = annotations["0"][0]["id"]

        # Delete it
        resp = agent_client.delete(
            f"/annotations/a1/s1/{ann_id}",
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

        # Verify count decreased
        list_resp2 = agent_client.get("/annotations/a1/s1", headers=headers)
        annotations2 = list_resp2.json()["annotations"]
        page0_after = len(annotations2.get("0", []))
        assert page0_after == page0_count - 1

    def test_delete_annotation_404(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        """DELETE with non-existent annotation ID returns 404."""
        _skip_if_no_fastapi()
        _create_annotated_pdf(tmp_storage_path, "a1", "s1")
        headers = _annotation_headers(auth_headers)

        resp = agent_client.delete(
            "/annotations/a1/s1/nonexistent-id-9999",
            headers=headers,
        )
        assert resp.status_code == 404

    def test_annotation_endpoints_require_auth(
        self, agent_client: Any, tmp_storage_path: Path
    ) -> None:
        """All annotation endpoints require authentication."""
        _skip_if_no_fastapi()
        _create_annotated_pdf(tmp_storage_path, "a1", "s1")
        contract_header = {"X-AEMS-Annotation-Contract-Version": "1"}

        # GET list
        resp = agent_client.get("/annotations/a1/s1", headers=contract_header)
        assert resp.status_code == 401

        # GET version
        resp = agent_client.get("/annotations/a1/s1/version", headers=contract_header)
        assert resp.status_code == 401

        # POST create
        resp = agent_client.post(
            "/annotations/a1/s1",
            json={"page_index": 0, "content": "x"},
            headers=contract_header,
        )
        assert resp.status_code == 401

        # PUT update
        resp = agent_client.put(
            "/annotations/a1/s1/some-id",
            json={"content": "x"},
            headers=contract_header,
        )
        assert resp.status_code == 401

        # DELETE
        resp = agent_client.delete(
            "/annotations/a1/s1/some-id",
            headers=contract_header,
        )
        assert resp.status_code == 401

    def test_annotation_endpoints_require_supported_contract_header(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        """All annotation endpoints require X-AEMS-Annotation-Contract-Version: 1."""
        _skip_if_no_fastapi()
        _create_annotated_pdf(tmp_storage_path, "a1", "s1")

        # No contract header at all
        resp = agent_client.get("/annotations/a1/s1", headers=auth_headers)
        assert resp.status_code == 409
        assert "contract version" in resp.json()["detail"].lower()

        # Wrong version
        bad_headers = {**auth_headers, "X-AEMS-Annotation-Contract-Version": "99"}
        resp = agent_client.get("/annotations/a1/s1", headers=bad_headers)
        assert resp.status_code == 409

        # POST with wrong version
        resp = agent_client.post(
            "/annotations/a1/s1",
            json={"page_index": 0, "content": "x"},
            headers=bad_headers,
        )
        assert resp.status_code == 409

        # PUT with wrong version
        resp = agent_client.put(
            "/annotations/a1/s1/some-id",
            json={"content": "x"},
            headers=bad_headers,
        )
        assert resp.status_code == 409

        # DELETE with wrong version
        resp = agent_client.delete(
            "/annotations/a1/s1/some-id",
            headers=bad_headers,
        )
        assert resp.status_code == 409

    def test_capabilities_includes_annotation_crud(self, agent_client: Any) -> None:
        """GET /capabilities includes annotation_crud in features."""
        _skip_if_no_fastapi()
        resp = agent_client.get("/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        assert "annotation_crud" in data["features"]


# ---------------------------------------------------------------------------
# Grading Bundle Route Tests
# ---------------------------------------------------------------------------


class TestGradingBundleEndpoint:
    """Tests for POST /grading-bundle/{aid}/{sid}."""

    def _create_submission_pdf(self, storage_path: Path, aid: str, sid: str) -> None:
        """Helper to create a test submission PDF."""
        import fitz
        pdf_dir = storage_path / aid / sid
        pdf_dir.mkdir(parents=True, exist_ok=True)
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 100), "Test student answer", fontsize=12)
        doc.save(str(pdf_dir / "submission.pdf"))
        doc.close()

    def test_generates_bundle(
        self, agent_client: Any, tmp_storage_path: Path, auth_headers: dict
    ) -> None:
        _skip_if_no_fastapi()
        self._create_submission_pdf(tmp_storage_path, "a1", "s1")
        resp = agent_client.post(
            "/grading-bundle/a1/s1",
            json={"strategy": "text_only", "dpi": 72},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["bundle_version"] == 1
        assert len(data["pages"]) > 0
        assert data["assignment_id"] == "a1"
        assert data["submission_id"] == "s1"

    def test_404_missing_pdf(self, agent_client: Any, auth_headers: dict) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.post(
            "/grading-bundle/a1/nonexistent",
            json={"strategy": "text_only"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_requires_auth(self, agent_client: Any) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.post("/grading-bundle/a1/s1", json={"strategy": "text_only"})
        assert resp.status_code == 401

    def test_invalid_strategy_returns_400(
        self, agent_client: Any, tmp_storage_path: Path, auth_headers: dict
    ) -> None:
        _skip_if_no_fastapi()
        self._create_submission_pdf(tmp_storage_path, "a1", "s1")
        resp = agent_client.post(
            "/grading-bundle/a1/s1",
            json={"strategy": "invalid_strategy"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "strategy" in resp.json()["detail"].lower()

    def test_capabilities_includes_grading_bundle(
        self, agent_client: Any, auth_headers: dict
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.get("/capabilities")
        data = resp.json()
        assert "grading_bundle" in data["features"]
        assert 1 in data["supported_bundle_versions"]


class TestPairingClipboard:
    """Best-effort clipboard hand-off for the pairing PIN.

    The Windows tray toast is non-interactive, so the agent puts the PIN
    on the clipboard before showing the toast.  These tests verify that
    `_copy_pin_to_clipboard` calls the platform-appropriate command and
    that the tray notifier is invoked with the resulting flag.
    """

    def test_copy_pin_to_clipboard_uses_clip_on_windows(self, monkeypatch) -> None:
        from aems_agent import routes

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["input"] = kwargs.get("input")
            captured["encoding"] = kwargs.get("encoding")
            class _R:
                returncode = 0
            return _R()

        # _copy_pin_to_clipboard imports platform + subprocess lazily.
        import platform as _p
        import subprocess as _sp
        monkeypatch.setattr(_p, "system", lambda: "Windows")
        monkeypatch.setattr(_sp, "run", fake_run)

        ok = routes._copy_pin_to_clipboard("123456")
        assert ok is True
        assert captured["argv"] == ["clip"]
        assert captured["input"] == "123456"
        assert captured["encoding"] == "utf-16-le"

    def test_copy_pin_to_clipboard_returns_false_on_unsupported_platform(
        self, monkeypatch
    ) -> None:
        from aems_agent import routes

        import platform as _p
        monkeypatch.setattr(_p, "system", lambda: "Plan9")
        assert routes._copy_pin_to_clipboard("123456") is False

    def test_notify_pairing_pin_forwards_clipboard_flag(self) -> None:
        from types import SimpleNamespace
        from aems_agent import routes

        captured = {}

        def notifier(pin: str, clipboard_ok: bool = False) -> None:
            captured["pin"] = pin
            captured["clipboard_ok"] = clipboard_ok

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(tray_notifier=notifier)))
        routes._notify_pairing_pin(request, "654321", clipboard_ok=True)

        assert captured == {"pin": "654321", "clipboard_ok": True}

    def test_notify_pairing_pin_falls_back_to_legacy_notifier(self) -> None:
        from types import SimpleNamespace
        from aems_agent import routes

        captured = {}

        def legacy_notifier(pin: str) -> None:
            captured["pin"] = pin

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(tray_notifier=legacy_notifier)))
        routes._notify_pairing_pin(request, "111222", clipboard_ok=True)

        assert captured == {"pin": "111222"}

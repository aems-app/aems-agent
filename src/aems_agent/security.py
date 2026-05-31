# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Security utilities for the AEMS Local Bridge Agent.

Provides:
- Path traversal validation (all file paths must resolve within storage_path)
- In-memory rate limiting
"""

import logging
import threading
import time
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)


def validate_path_within_storage(
    storage_path: Path,
    *parts: str,
) -> Path:
    """
    Validate that the resolved path stays within the storage directory.

    Joins the parts onto storage_path, resolves symlinks, and ensures the
    result is still a descendant of storage_path.

    Args:
        storage_path: The root storage directory.
        *parts: Path components to join (e.g., assignment_id, submission_id).

    Returns:
        The resolved, validated path.

    Raises:
        ValueError: If the resolved path escapes the storage directory.
    """
    resolved_root = storage_path.resolve()
    target = resolved_root.joinpath(*parts).resolve()

    # Ensure target is within the storage root
    try:
        target.relative_to(resolved_root)
    except ValueError:
        raise ValueError("Path traversal detected: resolved path is outside storage directory")

    return target


class RateLimiter:
    """
    Simple in-memory rate limiter using a sliding window.

    Thread-safe. Tracks requests per-key (e.g., IP or token) within
    a configurable time window.
    """

    def __init__(
        self,
        max_requests: int = 100,
        window_seconds: float = 60.0,
        cleanup_interval: float = 300.0,
        max_keys: int = 10000,
    ) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._cleanup_interval = cleanup_interval
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._tracker: Dict[str, List[float]] = {}
        self._last_cleanup = 0.0

    def is_allowed(self, key: str) -> bool:
        """
        Check if the request should be allowed.

        Args:
            key: Identifier for rate limiting (e.g., IP or token hash).

        Returns:
            True if the request is within rate limits.
        """
        now = time.time()

        with self._lock:
            # Periodic cleanup
            if now - self._last_cleanup > self._cleanup_interval:
                stale_keys = [
                    k
                    for k, timestamps in self._tracker.items()
                    if not timestamps or (now - max(timestamps)) > self._window_seconds
                ]
                for k in stale_keys:
                    del self._tracker[k]
                self._last_cleanup = now

            timestamps = self._tracker.get(key, [])
            timestamps = [t for t in timestamps if now - t < self._window_seconds]

            if len(timestamps) >= self._max_requests:
                self._tracker[key] = timestamps
                return False

            # Evict oldest keys if tracker is full (memory bound)
            if len(self._tracker) >= self._max_keys and key not in self._tracker:
                oldest_key = min(
                    self._tracker, key=lambda k: max(self._tracker[k]) if self._tracker[k] else 0
                )
                del self._tracker[oldest_key]

            timestamps.append(now)
            self._tracker[key] = timestamps
            return True

    def reset(self) -> None:
        """Clear all tracking data."""
        with self._lock:
            self._tracker.clear()
            self._last_cleanup = 0.0

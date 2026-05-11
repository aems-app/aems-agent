# SPDX-License-Identifier: AGPL-3.0-or-later

"""
FastAPI application assembly for the AEMS Local Bridge Agent.

Creates and configures the FastAPI app with:
- CORS middleware for browser access
- Bearer token authentication
- Router from routes.py
- Global error handler with structured JSON responses
- Rotating log file
- Startup validation of storage path
"""

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Awaitable, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from .config import AGENT_VERSION, API_VERSION, ensure_auth_token, get_config_dir, load_config
from .crypto import ensure_keypair
from .routes import router, set_agent_globals

logger = logging.getLogger(__name__)


def _setup_logging(config_dir: Path) -> None:
    """Configure rotating file logging for the agent."""
    log_file = config_dir / "agent.log"
    config_dir.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root_logger = logging.getLogger("aems_agent")
    # Guard against duplicate handlers when create_app() is called multiple
    # times (tests, hot-reload).  Only add if no RotatingFileHandler exists.
    has_rotating = any(
        isinstance(h, logging.handlers.RotatingFileHandler) for h in root_logger.handlers
    )
    if not has_rotating:
        root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


def _validate_storage(config_dir: Path) -> None:
    """Validate that the storage path exists and is writable (if configured)."""
    config = load_config(config_dir)
    if not config.storage_path:
        logger.warning("Storage path not configured. Set it via CLI or Settings page.")
        return

    path = Path(config.storage_path)
    if not path.exists():
        logger.warning("Storage path does not exist: %s", path)
        return

    if not os.access(path, os.W_OK):
        logger.warning("Storage path is not writable: %s", path)


def create_app(
    config_dir: Optional[Path] = None,
) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        config_dir: Override config directory (for testing).

    Returns:
        Configured FastAPI app instance.
    """
    if config_dir is None:
        config_dir = get_config_dir()

    config = load_config(config_dir)
    auth_token = ensure_auth_token(config_dir)
    ensure_keypair(config_dir)

    # Set up file logging
    _setup_logging(config_dir)

    # Validate storage on startup
    _validate_storage(config_dir)

    # Set module-level globals for route handlers
    set_agent_globals(config_dir, auth_token)

    # Merge paired origins into allowed origins for CORS.
    # Use a mutable list so paired origins added at runtime (via /pair/complete)
    # are reflected immediately without restart.
    all_origins: list[str] = sorted(set(config.allowed_origins + config.paired_origins))

    # Allow localhost/127.0.0.1 on any port (http or https) so the pairing
    # handshake works before the origin is formally added to paired_origins.
    _localhost_origin_re = r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$"

    app = FastAPI(
        title="AEMS Local Bridge Agent",
        description="Local filesystem access for AEMS exam PDFs",
        version=AGENT_VERSION,
    )

    # Global exception handler for structured JSON errors
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "Unhandled error on %s %s: %s",
            request.method,
            request.url.path,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error",
            },
        )

    # Version header middleware — inject agent/API version on every response
    # and log warning if client version is incompatible.
    class _VersionHeaderMiddleware(BaseHTTPMiddleware):
        async def dispatch(
            self,
            request: Request,
            call_next: Callable[[Request], Awaitable[StarletteResponse]],
        ) -> StarletteResponse:
            response: StarletteResponse = await call_next(request)
            response.headers["X-AEMS-Agent-Version"] = AGENT_VERSION
            response.headers["X-AEMS-API-Version"] = API_VERSION
            client_version = request.headers.get("X-AEMS-Client-Version")
            if client_version:
                try:
                    client_major = int(client_version.split(".")[0])
                    api_major = int(API_VERSION.split(".")[0])
                    if client_major != api_major:
                        logger.warning(
                            "Client version %s incompatible with API version %s",
                            client_version,
                            API_VERSION,
                        )
                except (ValueError, IndexError):
                    logger.warning("Invalid X-AEMS-Client-Version: %s", client_version)
            return response

    app.add_middleware(_VersionHeaderMiddleware)

    # CORS middleware for browser access.
    # all_origins is a mutable list — routes.py appends to it after pairing,
    # which takes effect immediately because CORSMiddleware checks
    # `origin in self.allow_origins` on every request.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=all_origins,
        allow_origin_regex=_localhost_origin_re,
        allow_credentials=False,
        allow_private_network=True,
        allow_methods=["GET", "PUT", "POST", "DELETE", "HEAD", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-SHA256",
            "X-AEMS-Client-Version",
            "X-AEMS-Annotation-Contract-Version",
            "X-AEMS-Delivery-Id",
        ],
        expose_headers=["X-SHA256", "X-AEMS-Agent-Version", "X-AEMS-API-Version"],
    )

    # Store origins list on app.state so routes.py can append after pairing.
    app.state.cors_origins = all_origins

    app.include_router(router)

    return app

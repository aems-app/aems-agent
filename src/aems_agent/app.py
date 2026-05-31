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

import inspect
import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

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


def _format_host_header_name(host: str) -> str:
    """Return *host* normalized for direct Host header comparison."""
    value = host.strip().lower()
    if not value:
        return value
    if value.startswith("[") and value.endswith("]"):
        return value
    if value.count(":") >= 2:
        return f"[{value}]"
    return value


def _allowed_host_headers(host: str, port: int) -> set[str]:
    """Return the exact Host header values accepted by the local agent."""
    allowed_names = {
        "127.0.0.1",
        "localhost",
        "[::1]",
    }
    normalized_host = _format_host_header_name(host)
    if normalized_host and normalized_host not in {"0.0.0.0", "[::]", "::"}:
        allowed_names.add(normalized_host)

    return {f"{name}:{port}" for name in allowed_names}


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

    class _HostHeaderMiddleware(BaseHTTPMiddleware):
        def __init__(self, app: FastAPI, allowed_hosts: set[str]) -> None:
            super().__init__(app)
            self._allowed_hosts = allowed_hosts

        async def dispatch(
            self,
            request: Request,
            call_next: Callable[[Request], Awaitable[StarletteResponse]],
        ) -> StarletteResponse:
            host = request.headers.get("host", "").strip().lower()
            if host not in self._allowed_hosts:
                return JSONResponse(status_code=400, content={"detail": "Invalid Host header"})
            return await call_next(request)

    class _LNAAllowMiddleware(BaseHTTPMiddleware):
        """Inject Access-Control-Allow-Private-Network / -Local-Network on
        OPTIONS preflights that asked for them.

        Chrome 130+ Local Network Access (LNA, the successor to Private
        Network Access aka PNA) blocks every fetch from a public origin
        to a loopback address unless the target's preflight responds with
        the matching allow-header. Starlette's CORSMiddleware accepted
        ``allow_private_network=True`` in 0.27-0.44 but the parameter is
        gone in 0.45+, so the feature-detect above is silently false on
        the bundled Starlette 0.45.3. Without this middleware, the entire
        hosted-to-agent path (status badge, file listing, manifest fetch,
        annotated-PDF push) is blocked in any current Chrome.
        """

        async def dispatch(
            self,
            request: Request,
            call_next: Callable[[Request], Awaitable[StarletteResponse]],
        ) -> StarletteResponse:
            response = await call_next(request)
            if request.method == "OPTIONS":
                wants_pna = (
                    request.headers.get("access-control-request-private-network", "").lower()
                    == "true"
                )
                wants_lna = (
                    request.headers.get("access-control-request-local-network", "").lower()
                    == "true"
                )
                if wants_pna:
                    response.headers["Access-Control-Allow-Private-Network"] = "true"
                if wants_lna:
                    response.headers["Access-Control-Allow-Local-Network"] = "true"
            return response

    app.add_middleware(_VersionHeaderMiddleware)

    # CORS middleware for browser access.
    # all_origins is a mutable list — routes.py appends to it after pairing,
    # which takes effect immediately because CORSMiddleware checks
    # `origin in self.allow_origins` on every request.
    cors_kwargs: Dict[str, Any] = {
        "allow_origins": all_origins,
        "allow_origin_regex": _localhost_origin_re,
        "allow_credentials": False,
        "allow_methods": ["GET", "PUT", "POST", "DELETE", "HEAD", "OPTIONS"],
        "allow_headers": [
            "Authorization",
            "Content-Type",
            "X-SHA256",
            "X-AEMS-Client-Version",
            "X-AEMS-Annotation-Contract-Version",
            "X-AEMS-Delivery-Id",
        ],
        "expose_headers": ["X-SHA256", "X-AEMS-Agent-Version", "X-AEMS-API-Version"],
    }
    if "allow_private_network" in inspect.signature(CORSMiddleware.__init__).parameters:
        cors_kwargs["allow_private_network"] = True

    app.add_middleware(CORSMiddleware, **cors_kwargs)
    # Starlette's _MiddlewareFactory typing models ASGI-style middlewares;
    # _HostHeaderMiddleware uses Starlette's BaseHTTPMiddleware which has
    # a different __init__ shape. add_middleware still calls cls(app, **kw)
    # at runtime so the wiring is correct.
    app.add_middleware(
        _HostHeaderMiddleware,  # type: ignore[arg-type]
        allowed_hosts=_allowed_host_headers(config.host, config.port),
    )
    # LNA must be the outermost middleware so it sees the final response
    # headers (after CORSMiddleware has set Access-Control-Allow-Origin).
    app.add_middleware(_LNAAllowMiddleware)

    # Store origins list on app.state so routes.py can append after pairing.
    app.state.cors_origins = all_origins

    app.include_router(router)

    return app

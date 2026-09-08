"""Common browser security policy and redacted error presentation."""
import logging
import re
import secrets
import uuid

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from dockbench.core.errors import (DataRootError, DockerCommandError, WorkstationContainerExists,
    WorkspaceRootError, WorkstationError, WorkstationGPUConflict, WorkstationRebuildRequired,
    WorkstationReplaceRequired)
from dockbench.core.recipes import RecipeError

LOG = logging.getLogger(__name__)

_SENSITIVE_LOG_VALUE = re.compile(r"(?i)\b(password|token|secret|authorization|cookie)\b\s*([=:])\s*[^\s,;]+")
_SENSITIVE_DOCKER_VALUE = re.compile(
    r"(?i)\b(password|token|secret|authorization|cookie|credential|api[_-]?key)\b\s*([=:])\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_SENSITIVE_AUTHORIZATION = re.compile(
    r"(?i)\bauthorization\b\s*([=:])\s*(?:(?:bearer|basic)\s+)?[^\s,;]+"
)
_DOCKER_ERROR_PREFIX = re.compile(r"(?i)^(?:docker:\s*)?(?:error response from daemon:\s*)+")
_ABSOLUTE_PATH = re.compile(r"(?<![\w.-])/(?:[^\s/'\"`]+/?)+")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _redact_image_log(value: str) -> str:
    """Keep job logs useful without making Dockbench a secret sink."""
    return _SENSITIVE_LOG_VALUE.sub(r"\1\2[REDACTED]", value)


def _safe_docker_error(value: str) -> str:
    """Turn an untrusted Docker daemon message into a brief UI-safe cause.

    Daemon stderr can include credentials, socket or host paths, and verbose
    diagnostics.  Keep the human-readable cause while redacting those details,
    collapsing it to one bounded line, and append a consistent next step.
    """
    cause = _ANSI_ESCAPE.sub("", value)
    cause = _DOCKER_ERROR_PREFIX.sub("", " ".join(cause.split()))
    cause = _SENSITIVE_AUTHORIZATION.sub(r"authorization\1[REDACTED]", cause)
    cause = _SENSITIVE_DOCKER_VALUE.sub(r"\1\2[REDACTED]", cause)
    cause = _ABSOLUTE_PATH.sub("[PATH]", cause).strip(" .")
    if not cause:
        cause = "Docker did not provide a usable failure reason"
    if len(cause) > 300:
        cause = f"{cause[:297].rstrip()}..."
    return (
        f"Docker could not complete the request: {cause}. "
        "Check that Docker is running and that the selected image and resources are available, then try again."
    )


def _safe_diagnostic_detail(exc: Exception) -> str:
    """Return a bounded, redacted exception summary suitable for server logs."""
    detail = _ANSI_ESCAPE.sub("", str(exc))
    detail = " ".join(detail.split())
    detail = _SENSITIVE_AUTHORIZATION.sub(r"authorization\1[REDACTED]", detail)
    detail = _SENSITIVE_DOCKER_VALUE.sub(r"\1\2[REDACTED]", detail)
    detail = _ABSOLUTE_PATH.sub("[PATH]", detail).strip(" .")
    if not detail:
        return "no usable diagnostic detail"
    return detail[:500]


def safe_error(exc: Exception) -> JSONResponse:
    correlation_id = uuid.uuid4().hex
    def response(status: int, code: str, message: str) -> JSONResponse:
        LOG.warning(
            "dockbench request failed id=%s code=%s kind=%s detail=%s",
            correlation_id,
            code,
            type(exc).__name__,
            _safe_diagnostic_detail(exc),
        )
        return JSONResponse(
            status_code=status,
            content={"code": code, "message": message, "correlation_id": correlation_id},
        )

    if isinstance(exc, RecipeError):
        status = 409 if "already exists" in str(exc) else 422
        return response(status, "invalid_recipe", str(exc))
    if isinstance(exc, WorkstationContainerExists):
        return response(
            409,
            "container_exists",
            (
                f"A Docker container named {exc.name} already exists. "
                "Choose another name, or remove or rename the existing container before trying again."
            ),
        )
    if isinstance(exc, WorkspaceRootError):
        return response(422, "invalid_workspace_root", "The selected workspace root does not exist or is not a directory.")
    if isinstance(exc, DataRootError):
        return response(422, "invalid_data_root", "The selected data root does not exist or is not a directory.")
    if isinstance(exc, WorkstationReplaceRequired):
        return response(409, "workstation_replace_required", "The requested image or GPU selection differs. Replacing keeps the workspace mount and /state but discards the old container filesystem.")
    if isinstance(exc, WorkstationRebuildRequired):
        return response(
            409,
            "workstation_rebuild_required",
            "The workstation image or launch settings changed. Run `uv run dockbench image rebuild`, then try again.",
        )
    if isinstance(exc, WorkstationGPUConflict):
        return response(409, "gpu_reserved", f"GPU {exc.gpu_uuid} is reserved by running container {exc.owner}.")
    if isinstance(exc, DockerCommandError):
        return response(503, "docker_error", _safe_docker_error(str(exc)))
    if isinstance(exc, WorkstationError):
        return response(503, "workstation_unavailable", "Dockbench is unavailable. Check its status and try again.")
    return response(500, "internal_error", "Dockbench could not complete the request.")


def _origin_for(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host', '')}"


def _require_csrf(request: Request, csrf_cookie: str | None) -> None:
    # Browser-only mutations must have a matching double-submit token and an
    # exact same-origin Origin header. It intentionally rejects native clients.
    if not csrf_cookie or not secrets.compare_digest(csrf_cookie, request.headers.get("x-csrf-token", "")):
        raise HTTPException(403, "CSRF validation failed")
    if request.headers.get("origin") != _origin_for(request):
        raise HTTPException(403, "Same-origin request required")


def _issue_csrf(response: Response, current: str | None) -> str:
    """Reuse the browser-wide token so opening another Dockbench tab cannot invalidate it."""
    token = current if current and 20 <= len(current) <= 256 else secrets.token_urlsafe(32)
    response.set_cookie("dockbench_csrf", token, httponly=False, samesite="strict", secure=False, path="/")
    return token


def install_security(app: FastAPI) -> None:
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; connect-src 'self' ws:; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'",
        })
        return response


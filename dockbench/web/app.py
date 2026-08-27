"""Same-origin FastAPI host for the desktop-first Dockbench browser app."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import re
import secrets
import struct
import tempfile
import termios
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import Cookie, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dockbench.core.defaults import DEFAULT_IMAGE
from dockbench.core.errors import DockerCommandError
from dockbench.core.workstation import (
    Workstation,
    WorkstationError,
    WorkstationGPUConflict,
    WorkstationRebuildRequired,
    WorkstationReplaceRequired,
)
from dockbench.core.host_inventory import HostInventory
from dockbench.core.recipes import RecipeError

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
WEB_DIST = ROOT / "apps" / "workbench" / "dist"
SESSION_TTL_SECONDS = 60
MAX_IMAGE_JOB_LOG_LINES = 2000


@dataclass
class DesktopSession:
    port: int
    expires_at: float
    container_name: str = "dockbench"
    used: bool = False


@dataclass
class TerminalSession:
    container_name: str
    expires_at: float
    used: bool = False


@dataclass
class ImageJob:
    id: str
    kind: str
    state: str = "running"
    message: str = ""
    created_at: float = 0
    logs: list[str] = field(default_factory=list)
    code: str | None = None


_SENSITIVE_LOG_VALUE = re.compile(r"(?i)\b(password|token|secret|authorization|cookie)\b\s*([=:])\s*[^\s,;]+")
_SENSITIVE_DOCKER_VALUE = re.compile(
    r"(?i)\b(password|token|secret|authorization|cookie|credential|api[_-]?key)\b\s*([=:])\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
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


class SessionRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class PasswordResetRequest(BaseModel):
    password: str = Field(min_length=6, max_length=8)


class StartRequest(BaseModel):
    image: str | None = Field(default=None, min_length=1, max_length=512)
    gpu_uuids: list[str] = Field(default_factory=list, max_length=64)
    all_gpus: bool | None = None
    replace: bool = False


class ContainerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=96, pattern=r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")
    image: str = Field(min_length=1, max_length=512)
    gpu_uuids: list[str] = Field(default_factory=list, max_length=64)
    all_gpus: bool = False


class ImageBuildRequest(BaseModel):
    recipe_id: str = Field(default="android-ws", min_length=1, max_length=96,
                           pattern=r"[a-z0-9]+(?:-[a-z0-9]+)*")
    tag: str | None = Field(default=None, min_length=1, max_length=512)
    target: str | None = Field(default=None, min_length=1, max_length=128)
    platform: str | None = Field(default=None, min_length=1, max_length=128)
    no_cache: bool = False


class RecipeCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=96, pattern=r"[a-z0-9]+(?:-[a-z0-9]+)*")
    dockerfile: str = Field(min_length=1, max_length=1_048_576)
    tag: str = Field(min_length=1, max_length=512)
    target: str | None = Field(default=None, max_length=128)
    platform: str = Field(default="linux/amd64", min_length=1, max_length=128)


class RecipeReviseRequest(BaseModel):
    dockerfile: str = Field(min_length=1, max_length=1_048_576)
    tag: str | None = Field(default=None, max_length=512)
    target: str | None = Field(default=None, max_length=128)
    platform: str | None = Field(default=None, max_length=128)


class DesktopSessions:
    def __init__(self) -> None:
        self._sessions: dict[str, DesktopSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, port: int, container_name: str = "dockbench") -> str:
        async with self._lock:
            self._purge()
            session_id = secrets.token_urlsafe(32)
            self._sessions[session_id] = DesktopSession(port, time.monotonic() + SESSION_TTL_SECONDS, container_name)
            return session_id

    async def consume(self, session_id: str) -> DesktopSession | None:
        async with self._lock:
            self._purge()
            session = self._sessions.get(session_id)
            if session is None or session.used:
                return None
            session.used = True
            return session

    def _purge(self) -> None:
        now = time.monotonic()
        self._sessions = {key: value for key, value in self._sessions.items() if value.expires_at > now and not value.used}


class TerminalSessions:
    """Short-lived capabilities so terminal WebSockets cannot name arbitrary containers."""

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, container_name: str) -> str:
        async with self._lock:
            self._purge()
            session_id = secrets.token_urlsafe(32)
            self._sessions[session_id] = TerminalSession(container_name, time.monotonic() + SESSION_TTL_SECONDS)
            return session_id

    async def consume(self, session_id: str) -> TerminalSession | None:
        async with self._lock:
            self._purge()
            session = self._sessions.get(session_id)
            if session is None or session.used:
                return None
            session.used = True
            return session

    def _purge(self) -> None:
        now = time.monotonic()
        self._sessions = {key: value for key, value in self._sessions.items() if value.expires_at > now and not value.used}


def safe_error(exc: Exception) -> JSONResponse:
    correlation_id = uuid.uuid4().hex
    LOG.warning("dockbench request failed id=%s kind=%s", correlation_id, type(exc).__name__)
    if isinstance(exc, RecipeError):
        status = 409 if "already exists" in str(exc) else 422
        return JSONResponse(status_code=status, content={"code": "invalid_recipe", "message": str(exc), "correlation_id": correlation_id})
    if isinstance(exc, WorkstationReplaceRequired):
        return JSONResponse(status_code=409, content={"code": "workstation_replace_required", "message": "The requested image or GPU selection differs. Replacing keeps host code roots and /state but discards the old container filesystem.", "correlation_id": correlation_id})
    if isinstance(exc, WorkstationRebuildRequired):
        return JSONResponse(
            status_code=409,
            content={
                "code": "workstation_rebuild_required",
                "message": (
                    "The workstation image or launch settings changed. Run "
                    "`uv run dockbench image rebuild`, then try again."
                ),
                "correlation_id": correlation_id,
            },
        )
    if isinstance(exc, WorkstationGPUConflict):
        return JSONResponse(
            status_code=409,
            content={
                "code": "gpu_reserved",
                "message": f"GPU {exc.gpu_uuid} is reserved by running container {exc.owner}.",
                "correlation_id": correlation_id,
            },
        )
    if isinstance(exc, DockerCommandError):
        return JSONResponse(
            status_code=503,
            content={
                "code": "docker_error",
                "message": _safe_docker_error(str(exc)),
                "correlation_id": correlation_id,
            },
        )
    if isinstance(exc, WorkstationError):
        return JSONResponse(status_code=503, content={"code": "workstation_unavailable", "message": "Dockbench is unavailable. Check its status and try again.", "correlation_id": correlation_id})
    return JSONResponse(status_code=500, content={"code": "internal_error", "message": "Dockbench could not complete the request.", "correlation_id": correlation_id})


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


def create_app(workstation: Workstation | None = None, fleet: Any | None = None,
               recipes: Any | None = None, image_builder: Any | None = None,
               image_verifier: Any | None = None) -> FastAPI:
    app = FastAPI(title="Dockbench", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.workstation = workstation
    app.state.fleet = fleet
    app.state.sessions = DesktopSessions()
    app.state.terminal_sessions = TerminalSessions()
    app.state.desktop_sockets: set[WebSocket] = set()
    app.state.desktop_socket_containers: dict[WebSocket, str] = {}
    app.state.image_jobs: dict[str, ImageJob] = {}
    app.state.image_job_lock = asyncio.Lock()
    app.state.recipes = recipes
    app.state.image_builder = image_builder
    app.state.image_verifier = image_verifier

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

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        """Report that the HTTP server is ready without contacting Docker.

        Deployment and SSH-tunnel clients use this endpoint as their readiness
        probe.  It deliberately avoids the workstation and fleet services so a
        reachable Dockbench server can still report ready while Docker is stopped or
        temporarily unavailable.
        """
        return {"status": "ok"}

    def ws() -> Workstation:
        return app.state.workstation or Workstation()

    def managed_fleet() -> Any:
        """Return the shared fleet manager without making legacy callers pay for it.

        Tests and downstream embedders can inject a fleet directly.  Production
        constructs one from the exact runner/configuration used by the legacy
        default workstation, so the API cannot accidentally use a different
        Docker context.
        """
        if app.state.fleet is not None:
            return app.state.fleet
        workstation = ws()
        if not hasattr(workstation, "config"):
            raise WorkstationError("managed fleet is unavailable")
        try:
            from dockbench.core.workstation import FleetManager
        except ImportError as exc:  # pragma: no cover - protects partial installs
            raise WorkstationError("managed fleet is unavailable") from exc
        app.state.fleet = FleetManager(workstation.config, runner=workstation.docker, inventory=workstation.inventory)
        return app.state.fleet

    def _public(status: Any) -> dict[str, Any]:
        return status.public() if hasattr(status, "public") else dict(status)

    async def _fleet_call(method: str, *args: Any, **kwargs: Any) -> Any:
        return await run_in_threadpool(getattr(managed_fleet(), method), *args, **kwargs)

    def recipe_catalog() -> Any:
        """The web layer only adapts the core recipe service; it owns no files."""
        if app.state.recipes is None:
            from dockbench.core.recipes import RecipeCatalog
            app.state.recipes = RecipeCatalog(managed_fleet().config.repository_root / "assets" / "images")
        return app.state.recipes

    def recipe_builder() -> Any:
        if app.state.image_builder is None:
            from dockbench.core.image_builder import ImageBuilder
            app.state.image_builder = ImageBuilder(managed_fleet().docker)
        return app.state.image_builder

    def image_verifier() -> Any:
        if app.state.image_verifier is None:
            from dockbench.core.image_verifier import ImageVerifier
            app.state.image_verifier = ImageVerifier(managed_fleet().docker)
        return app.state.image_verifier

    def recipe_public(recipe: Any) -> dict[str, Any]:
        manifest = getattr(recipe, "manifest", recipe)
        return {
            "id": getattr(manifest, "id"),
            "revision": getattr(manifest, "revision"),
            "dockerfile": getattr(recipe, "dockerfile", getattr(manifest, "dockerfile", None)),
            "tag": getattr(manifest, "tag"),
            "target": getattr(manifest, "target", None),
            "platform": getattr(manifest, "platform", None),
        }

    async def _close_desktop_sockets(container_name: str | None = None) -> None:
        # Sessions are container-scoped.  A stopped/replaced container only
        # disconnects its own VNC clients, never another desktop in the fleet.
        for socket in tuple(app.state.desktop_sockets):
            if container_name is None or app.state.desktop_socket_containers.get(socket) == container_name:
                await socket.close(code=1012)

    @app.get("/api/workstation")
    async def workstation_status(response: Response, dockbench_csrf: str | None = Cookie(default=None)):
        token = _issue_csrf(response, dockbench_csrf)
        try:
            return {**ws().status().public(), "csrf_token": token}
        except Exception as exc:
            return safe_error(exc)

    @app.get("/api/host/inventory")
    async def host_inventory():
        try:
            return {**await inventory_with_reservations(), "default_all_gpus": True}
        except Exception as exc:
            return safe_error(exc)

    async def inventory_with_reservations() -> dict[str, Any]:
        result = await _fleet_call("inventory")
        # FleetManager owns reservation decisions.  The UI calls its display
        # field `owner`; retain `reservation` for external consumers that used
        # the core representation directly.
        result["gpus"] = [{**gpu, "owner": gpu.get("reservation")} for gpu in result["gpus"]]
        return result

    def container_public(status: Any) -> dict[str, Any]:
        result = _public(status)
        result["name"] = result.get("container_name", "")
        return result

    @app.get("/api/containers")
    async def containers(response: Response, dockbench_csrf: str | None = Cookie(default=None)):
        token = _issue_csrf(response, dockbench_csrf)
        try:
            items = await _fleet_call("containers")
            return {"containers": [container_public(item) for item in items], "csrf_token": token}
        except Exception as exc:
            return safe_error(exc)

    @app.get("/api/containers/{name}")
    async def container(name: str):
        try:
            return container_public(await _fleet_call("container", name))
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers")
    async def create_container(body: ContainerCreateRequest, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            status = await _fleet_call("create", body.name, body.image, tuple(body.gpu_uuids), body.all_gpus)
            return container_public(status)
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers/{name}/start")
    async def start_container(name: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            return container_public(await _fleet_call("start", name))
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers/{name}/stop")
    async def stop_container(name: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            status = await _fleet_call("stop", name)
            await _close_desktop_sockets(name)
            return container_public(status)
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers/{name}/remove")
    async def remove_container(name: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            await _fleet_call("remove", name)
            await _close_desktop_sockets(name)
            return {"removed": True, "name": name}
        except Exception as exc:
            return safe_error(exc)

    @app.delete("/api/containers/{name}")
    async def remove_container_delete(name: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        return await remove_container(name, request, dockbench_csrf)

    @app.delete("/api/containers/{name}/state")
    async def delete_container_state(name: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            await _fleet_call("delete_state", name)
            return {"state_deleted": True, "name": name}
        except Exception as exc:
            return safe_error(exc)

    @app.get("/api/container-states")
    async def container_states():
        try:
            names = await _fleet_call("orphaned_states")
            return {"container_states": [{"name": name} for name in names]}
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers/{name}/recreate")
    async def recreate_container(name: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            status = await _fleet_call("recreate", name)
            await _close_desktop_sockets(name)
            return container_public(status)
        except Exception as exc:
            return safe_error(exc)

    @app.get("/api/images")
    async def images():
        try:
            data = await inventory_with_reservations()
            containers = await _fleet_call("containers")
            dependents: dict[str, list[str]] = {}
            stale: dict[str, list[str]] = {}
            for item in containers:
                image_id = item.image_id
                if image_id:
                    dependents.setdefault(image_id, []).append(item.container_name)
                    if getattr(item, "stale", False): stale.setdefault(image_id, []).append(item.container_name)
            data["images"] = [{**image, "dependent_containers": dependents.get(str(image["id"]), []), "stale_dependents": stale.get(str(image["id"]), [])} for image in data["images"]]
            return {"images": data["images"]}
        except Exception as exc:
            return safe_error(exc)

    @app.get("/api/gpus")
    async def gpus():
        try:
            data = await inventory_with_reservations()
            return {"gpus": data["gpus"], "gpu_diagnostic": data["gpu_diagnostic"]}
        except Exception as exc:
            return safe_error(exc)

    @app.get("/api/image-recipes")
    async def image_recipes(response: Response, dockbench_csrf: str | None = Cookie(default=None)):
        token = _issue_csrf(response, dockbench_csrf)
        try:
            items = await run_in_threadpool(recipe_catalog().list)
            return {"recipes": [recipe_public(item) for item in items], "csrf_token": token}
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/image-recipes")
    async def create_image_recipe(body: RecipeCreateRequest, request: Request,
                                  dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            recipe = await run_in_threadpool(
                recipe_catalog().create, body.id, body.dockerfile,
                tag=body.tag, target=body.target, platform=body.platform,
            )
            return recipe_public(recipe)
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/image-recipes/{recipe_id}/revisions")
    async def revise_image_recipe(recipe_id: str, body: RecipeReviseRequest, request: Request,
                                  dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            # Only pass requested defaults: omitted fields retain their prior
            # manifest values while an explicit null target clears the target.
            defaults = {field: getattr(body, field) for field in ("tag", "target", "platform")
                        if field in body.model_fields_set}
            recipe = await run_in_threadpool(recipe_catalog().revise, recipe_id, body.dockerfile, **defaults)
            return recipe_public(recipe)
        except Exception as exc:
            return safe_error(exc)

    def start_image_job(kind: str, operation: Callable[[Callable[[str], None]], Any]) -> ImageJob:
        queued = app.state.image_job_lock.locked()
        job = ImageJob(uuid.uuid4().hex, kind, state="queued" if queued else "running",
                       message="Waiting for another image operation." if queued else "Starting image operation.",
                       created_at=time.time(), logs=["queued" if queued else "starting"])
        app.state.image_jobs[job.id] = job

        def report(value: str) -> None:
            lines = _redact_image_log(value).splitlines() or [""]
            job.logs.extend(lines)
            if len(job.logs) > MAX_IMAGE_JOB_LOG_LINES:
                del job.logs[:-MAX_IMAGE_JOB_LOG_LINES]

        async def run() -> None:
            async with app.state.image_job_lock:
                job.state = "running"
                job.message = "Image operation is running."
                report("running")
                try:
                    output = await run_in_threadpool(operation, report)
                except Exception as exc:
                    LOG.warning("image job failed id=%s kind=%s error=%s", job.id, kind, type(exc).__name__)
                    job.state = "failed"
                    if isinstance(exc, DockerCommandError):
                        job.code = "docker_error"
                        job.message = _safe_docker_error(str(exc))
                        report(job.message)
                    else:
                        job.message = "Image operation failed. Check Dockbench logs and try again."
                    report("failed")
                else:
                    if isinstance(output, str) and output:
                        report(output)
                    job.state = "completed"
                    job.message = "Image operation completed."
                    report("completed")

        asyncio.create_task(run())
        return job

    @app.post("/api/images/build")
    async def build_image(body: ImageBuildRequest, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            recipe = await run_in_threadpool(recipe_catalog().get, body.recipe_id)

            overrides = {field: getattr(body, field) for field in ("tag", "target", "platform")
                         if field in body.model_fields_set}

            def build(report: Callable[[str], None]) -> Any:
                return recipe_builder().build(recipe, no_cache=body.no_cache,
                                              on_progress=report, **overrides)

            job = start_image_job("no-cache build" if body.no_cache else "build", build)
            return {"id": job.id, "kind": job.kind, "state": job.state}
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/images/{image_id}/verify")
    async def verify_image(image_id: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            job = start_image_job("verify", lambda _report: image_verifier().verify(image_id))
            return {"id": job.id, "kind": job.kind, "state": job.state}
        except Exception as exc:
            return safe_error(exc)

    @app.get("/api/image-jobs/{job_id}")
    async def image_job(job_id: str):
        job = app.state.image_jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Image job not found")
        return {"id": job.id, "kind": job.kind, "state": job.state, "message": job.message,
                "code": job.code, "created_at": job.created_at, "logs": job.logs}

    @app.post("/api/images/load")
    async def load_image(request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        temporary = tempfile.NamedTemporaryFile(prefix="dockbench-image-", suffix=".tar", delete=False)
        temporary_path = Path(temporary.name)
        try:
            async for chunk in request.stream():
                temporary.write(chunk)
            temporary.close()
            if temporary_path.stat().st_size == 0:
                temporary_path.unlink(missing_ok=True)
                raise HTTPException(422, "Image archive is empty")
            manager = managed_fleet()

            def load(_report: Callable[[str], None]) -> None:
                try:
                    return manager.docker.run(["image", "load", "--input", str(temporary_path)], capture=True)
                finally:
                    temporary_path.unlink(missing_ok=True)

            job = start_image_job("load", load)
            return {"id": job.id, "kind": job.kind, "state": job.state}
        except HTTPException:
            raise
        except Exception as exc:
            temporary.close()
            temporary_path.unlink(missing_ok=True)
            return safe_error(exc)

    @app.get("/api/images/{image_id}/package")
    async def package_image(image_id: str):
        temporary_path: Path | None = None
        try:
            manager = managed_fleet()
            image = await run_in_threadpool(HostInventory(manager.docker).resolve_image, image_id)
            temporary = tempfile.NamedTemporaryFile(prefix="dockbench-image-", suffix=".tar", delete=False)
            temporary_path = Path(temporary.name)
            temporary.close()
            await run_in_threadpool(manager.docker.run, ["image", "save", "--output", str(temporary_path), image.id])
            filename = f"{image.display_reference.replace('/', '_').replace(':', '_')}.tar"
            return FileResponse(temporary_path, media_type="application/x-tar", filename=filename, background=BackgroundTask(temporary_path.unlink, missing_ok=True))
        except Exception as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            return safe_error(exc)

    @app.post("/api/workstation/start")
    async def start_workstation(request: Request, body: StartRequest | None = None, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            if body is None:
                return (await run_in_threadpool(ws().start)).public()
            return (await run_in_threadpool(ws().start, body.image, tuple(body.gpu_uuids), body.all_gpus, body.replace)).public()
        except Exception as exc: return safe_error(exc)

    @app.post("/api/workstation/stop")
    async def stop_workstation(request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            result = await run_in_threadpool(ws().stop)
            # A stopped container invalidates every active VNC transport.
            for socket in tuple(app.state.desktop_sockets):
                await socket.close(code=1012)
            return result.public()
        except Exception as exc: return safe_error(exc)

    @app.post("/api/desktop/sessions")
    async def create_desktop_session(body: SessionRequest, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            endpoint = await run_in_threadpool(ws().ensure_desktop, body.password)
            session_id = await app.state.sessions.create(endpoint.port)
            return {"session_id": session_id, "expires_in": SESSION_TTL_SECONDS}
        except Exception as exc: return safe_error(exc)

    @app.post("/api/desktop/password")
    async def reset_desktop_password(body: PasswordResetRequest, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            return (await run_in_threadpool(ws().reset_vnc_password, body.password)).public()
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers/{name}/desktop/sessions")
    async def create_container_desktop_session(name: str, body: SessionRequest, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            endpoint = await _fleet_call("ensure_desktop", name, body.password)
            session_id = await app.state.sessions.create(endpoint.port, name)
            return {"session_id": session_id, "expires_in": SESSION_TTL_SECONDS}
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers/{name}/desktop/password")
    async def reset_container_desktop_password(name: str, body: PasswordResetRequest, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            return container_public(await _fleet_call("reset_vnc_password", name, body.password))
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers/{name}/terminals")
    async def create_terminal(name: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            status = await _fleet_call("container", name)
            if status.state != "running":
                raise WorkstationError("container is not running; start it before opening a terminal")
            session_id = await app.state.terminal_sessions.create(name)
            return {"session_id": session_id, "expires_in": SESSION_TTL_SECONDS}
        except Exception as exc:
            return safe_error(exc)

    @app.websocket("/api/desktop/sessions/{session_id}/ws")
    async def desktop_proxy(socket: WebSocket, session_id: str):
        host = socket.headers.get("host", "")
        expected = f"http://{host}"
        if socket.headers.get("origin") != expected:
            await socket.close(code=1008); return
        session = await app.state.sessions.consume(session_id)
        if session is None:
            await socket.close(code=1008); return
        await socket.accept()
        app.state.desktop_sockets.add(socket)
        app.state.desktop_socket_containers[socket] = session.container_name
        writer = None
        tasks: list[asyncio.Task[None]] = []
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", session.port)
            async def browser_to_vnc() -> None:
                while True:
                    message = await socket.receive()
                    if message["type"] == "websocket.disconnect": return
                    payload = message.get("bytes")
                    if payload is not None:
                        writer.write(payload)
                        await writer.drain()

            async def vnc_to_browser() -> None:
                while data := await reader.read(65536):
                    await socket.send_bytes(data)

            tasks = [asyncio.create_task(browser_to_vnc()), asyncio.create_task(vnc_to_browser())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        except OSError:
            await socket.close(code=1011)
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            app.state.desktop_sockets.discard(socket)
            app.state.desktop_socket_containers.pop(socket, None)
            if writer is not None:
                writer.close()
                try: await writer.wait_closed()
                except OSError: pass

    @app.websocket("/api/containers/{name}/desktop/sessions/{session_id}/ws")
    async def container_desktop_proxy(socket: WebSocket, name: str, session_id: str):
        host = socket.headers.get("host", "")
        if socket.headers.get("origin") != f"http://{host}":
            await socket.close(code=1008); return
        session = await app.state.sessions.consume(session_id)
        if session is None or session.container_name != name:
            await socket.close(code=1008); return
        await socket.accept()
        app.state.desktop_sockets.add(socket)
        app.state.desktop_socket_containers[socket] = name
        writer = None
        tasks: list[asyncio.Task[None]] = []
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", session.port)

            async def browser_to_vnc() -> None:
                while True:
                    message = await socket.receive()
                    if message["type"] == "websocket.disconnect": return
                    payload = message.get("bytes")
                    if payload is not None:
                        writer.write(payload)
                        await writer.drain()

            async def vnc_to_browser() -> None:
                while data := await reader.read(65536):
                    await socket.send_bytes(data)

            tasks = [asyncio.create_task(browser_to_vnc()), asyncio.create_task(vnc_to_browser())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        except OSError:
            await socket.close(code=1011)
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            app.state.desktop_sockets.discard(socket)
            app.state.desktop_socket_containers.pop(socket, None)
            if writer is not None:
                writer.close()
                try: await writer.wait_closed()
                except OSError: pass

    @app.websocket("/api/terminals/{session_id}/ws")
    async def terminal_proxy(socket: WebSocket, session_id: str):
        host = socket.headers.get("host", "")
        if socket.headers.get("origin") != f"http://{host}":
            await socket.close(code=1008); return
        session = await app.state.terminal_sessions.consume(session_id)
        if session is None:
            await socket.close(code=1008); return
        master_fd, slave_fd = pty.openpty()
        try:
            config = managed_fleet().config
            process = await asyncio.create_subprocess_exec(
                config.docker_command, "exec", "-it", "--user", "root", "--workdir", "/workspace",
                session.container_name, "/bin/sh", "-lc",
                "if command -v bash >/dev/null 2>&1; then exec bash -l; else exec /bin/sh; fi",
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, preexec_fn=os.setsid,
            )
        except (OSError, WorkstationError):
            os.close(master_fd); os.close(slave_fd)
            await socket.close(code=1011); return
        os.close(slave_fd)
        await socket.accept()
        tasks: list[asyncio.Task[None]] = []
        try:
            def resize(rows: int, columns: int) -> None:
                if not 1 <= rows <= 1000 or not 1 <= columns <= 1000:
                    return
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

            async def browser_to_shell() -> None:
                while True:
                    message = await socket.receive()
                    if message["type"] == "websocket.disconnect": return
                    raw = message.get("text")
                    if raw is None:
                        continue
                    try:
                        message_data = json.loads(raw)
                    except json.JSONDecodeError:
                        message_data = raw
                    if isinstance(message_data, dict) and message_data.get("type") == "resize":
                        try:
                            resize(int(message_data.get("rows", 0)), int(message_data.get("cols", 0)))
                        except (TypeError, ValueError):
                            pass
                        continue
                    data = message_data.get("data", raw) if isinstance(message_data, dict) else message_data
                    if not isinstance(data, str):
                        continue
                    os.write(master_fd, data.encode())

            async def shell_to_browser() -> None:
                while True:
                    try:
                        data = await asyncio.to_thread(os.read, master_fd, 65536)
                    except OSError:
                        return
                    if not data:
                        return
                    await socket.send_text(data.decode(errors="replace"))

            tasks = [asyncio.create_task(browser_to_shell()), asyncio.create_task(shell_to_browser())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try: os.close(master_fd)
            except OSError: pass
            if process.returncode is None:
                process.terminate()
                try: await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError: process.kill()

    if WEB_DIST.is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}")
        async def frontend(path: str):
            candidate = WEB_DIST / path
            return FileResponse(candidate if path and candidate.is_file() else WEB_DIST / "index.html")
    else:
        @app.get("/")
        async def unavailable_frontend():
            return JSONResponse(status_code=503, content={"code": "frontend_not_built", "message": "Build the Dockbench frontend with npm run build."})
    return app


app = create_app()

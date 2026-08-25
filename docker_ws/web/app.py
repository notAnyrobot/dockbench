"""Same-origin FastAPI host for the desktop-first Workbench."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from docker_ws.core.workstation import (
    Workstation,
    WorkstationError,
    WorkstationRebuildRequired,
    WorkstationReplaceRequired,
)
from docker_ws.core.host_inventory import HostInventory

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
WEB_DIST = ROOT / "apps" / "workbench" / "dist"
SESSION_TTL_SECONDS = 60


@dataclass
class DesktopSession:
    port: int
    expires_at: float
    used: bool = False


class SessionRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class PasswordResetRequest(BaseModel):
    password: str = Field(min_length=6, max_length=8)


class StartRequest(BaseModel):
    image: str | None = Field(default=None, min_length=1, max_length=512)
    gpu_uuids: list[str] = Field(default_factory=list, max_length=64)
    all_gpus: bool = False
    replace: bool = False


class DesktopSessions:
    def __init__(self) -> None:
        self._sessions: dict[str, DesktopSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, port: int) -> str:
        async with self._lock:
            self._purge()
            session_id = secrets.token_urlsafe(32)
            self._sessions[session_id] = DesktopSession(port, time.monotonic() + SESSION_TTL_SECONDS)
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


def safe_error(exc: Exception) -> JSONResponse:
    correlation_id = uuid.uuid4().hex
    LOG.warning("workbench request failed id=%s kind=%s", correlation_id, type(exc).__name__)
    if isinstance(exc, WorkstationReplaceRequired):
        return JSONResponse(status_code=409, content={"code": "workstation_replace_required", "message": "The requested image or GPU selection differs. Replacing keeps /workspace and /state but discards the old container filesystem.", "correlation_id": correlation_id})
    if isinstance(exc, WorkstationRebuildRequired):
        return JSONResponse(
            status_code=409,
            content={
                "code": "workstation_rebuild_required",
                "message": (
                    "The workstation image or launch settings changed. Run "
                    "`uv run docker-ws image rebuild`, then try again."
                ),
                "correlation_id": correlation_id,
            },
        )
    if isinstance(exc, WorkstationError):
        return JSONResponse(status_code=503, content={"code": "workstation_unavailable", "message": "Docker Workstation is unavailable. Check its status and try again.", "correlation_id": correlation_id})
    return JSONResponse(status_code=500, content={"code": "internal_error", "message": "Workbench could not complete the request.", "correlation_id": correlation_id})


def _origin_for(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host', '')}"


def _require_csrf(request: Request, csrf_cookie: str | None) -> None:
    # Browser-only mutations must have a matching double-submit token and an
    # exact same-origin Origin header. It intentionally rejects native clients.
    if not csrf_cookie or not secrets.compare_digest(csrf_cookie, request.headers.get("x-csrf-token", "")):
        raise HTTPException(403, "CSRF validation failed")
    if request.headers.get("origin") != _origin_for(request):
        raise HTTPException(403, "Same-origin request required")


def create_app(workstation: Workstation | None = None) -> FastAPI:
    app = FastAPI(title="Docker Workstation Workbench", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.workstation = workstation
    app.state.sessions = DesktopSessions()
    app.state.desktop_sockets: set[WebSocket] = set()

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

    def ws() -> Workstation:
        return app.state.workstation or Workstation()

    @app.get("/api/workstation")
    async def workstation_status(response: Response):
        token = secrets.token_urlsafe(32)
        response.set_cookie("workbench_csrf", token, httponly=False, samesite="strict", secure=False, path="/")
        try:
            return {**ws().status().public(), "csrf_token": token}
        except Exception as exc:
            return safe_error(exc)

    @app.get("/api/host/inventory")
    async def host_inventory():
        try:
            return await run_in_threadpool(lambda: HostInventory(ws().docker).inventory().public())
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/workstation/start")
    async def start_workstation(request: Request, body: StartRequest | None = None, workbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, workbench_csrf)
        try:
            if body is None:
                return (await run_in_threadpool(ws().start)).public()
            return (await run_in_threadpool(ws().start, body.image, tuple(body.gpu_uuids), body.all_gpus, body.replace)).public()
        except Exception as exc: return safe_error(exc)

    @app.post("/api/workstation/stop")
    async def stop_workstation(request: Request, workbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, workbench_csrf)
        try:
            result = await run_in_threadpool(ws().stop)
            # A stopped container invalidates every active VNC transport.
            for socket in tuple(app.state.desktop_sockets):
                await socket.close(code=1012)
            return result.public()
        except Exception as exc: return safe_error(exc)

    @app.post("/api/desktop/sessions")
    async def create_desktop_session(body: SessionRequest, request: Request, workbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, workbench_csrf)
        try:
            endpoint = await run_in_threadpool(ws().ensure_desktop, body.password)
            session_id = await app.state.sessions.create(endpoint.port)
            return {"session_id": session_id, "expires_in": SESSION_TTL_SECONDS}
        except Exception as exc: return safe_error(exc)

    @app.post("/api/desktop/password")
    async def reset_desktop_password(body: PasswordResetRequest, request: Request, workbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, workbench_csrf)
        try:
            return (await run_in_threadpool(ws().reset_vnc_password, body.password)).public()
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
            if writer is not None:
                writer.close()
                try: await writer.wait_closed()
                except OSError: pass

    if WEB_DIST.is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}")
        async def frontend(path: str):
            candidate = WEB_DIST / path
            return FileResponse(candidate if path and candidate.is_file() else WEB_DIST / "index.html")
    else:
        @app.get("/")
        async def unavailable_frontend():
            return JSONResponse(status_code=503, content={"code": "frontend_not_built", "message": "Build the Workbench frontend with npm run build."})
    return app


app = create_app()

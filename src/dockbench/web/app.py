"""Same-origin FastAPI host for the desktop-first Dockbench browser app."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import secrets
import struct
import termios
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import Cookie, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dockbench.web.containers import register_container_routes, container_public
from dockbench.core.backend import Backend
from dockbench.core.defaults import DEFAULT_IMAGE
from dockbench.core.errors import DataRootError, DockerCommandError, WorkstationContainerExists, WorkspaceRootError
from dockbench.core.workstation import (
    Workstation,
    WorkstationError,
    WorkstationGPUConflict,
    WorkstationRebuildRequired,
    WorkstationReplaceRequired,
)

from dockbench.web.image_jobs import ImageJobs
from dockbench.web.archives import register_archive_routes
from dockbench.web.recipes import register_recipe_routes

from dockbench.web.security import (safe_error, _require_csrf, _issue_csrf,
    _redact_image_log, install_security)

LOG = logging.getLogger(__name__)
SESSION_TTL_SECONDS = 60


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




class SessionRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class PasswordResetRequest(BaseModel):
    password: str = Field(min_length=6, max_length=8)


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


def create_app(workstation: Workstation | None = None, fleet: Any | None = None,
               recipes: Any | None = None, image_builder: Any | None = None,
               image_verifier: Any | None = None, *, repository_root: Path | None = None,
               backend: Backend | None = None) -> FastAPI:
    backend = backend if backend is not None else Backend(repository_root=repository_root)
    resources = backend.resources
    app = FastAPI(title="Dockbench", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.workstation = workstation
    app.state.fleet = fleet
    app.state.sessions = DesktopSessions()
    app.state.terminal_sessions = TerminalSessions()
    app.state.desktop_sockets: set[WebSocket] = set()
    app.state.desktop_socket_containers: dict[WebSocket, str] = {}
    jobs = ImageJobs(app)
    app.state.recipes = recipes
    app.state.image_builder = image_builder
    app.state.image_verifier = image_verifier

    install_security(app)

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
        return workstation if workstation is not None else backend.workstation

    def managed_fleet() -> Any:
        return fleet if fleet is not None else backend.fleet

    def _public(status: Any) -> dict[str, Any]:
        return status.public() if hasattr(status, "public") else dict(status)

    async def _fleet_call(method: str, *args: Any, **kwargs: Any) -> Any:
        return await run_in_threadpool(getattr(managed_fleet(), method), *args, **kwargs)

    async def _close_desktop_sockets(container_name: str | None = None) -> None:
        # Sessions are container-scoped.  A stopped/replaced container only
        # disconnects its own VNC clients, never another desktop in the fleet.
        for socket in tuple(app.state.desktop_sockets):
            if container_name is None or app.state.desktop_socket_containers.get(socket) == container_name:
                await socket.close(code=1012)

    register_container_routes(app, ws, managed_fleet, backend.host_defaults, _close_desktop_sockets)

    register_recipe_routes(app, backend, jobs, recipes=recipes, image_builder=image_builder,
                           image_verifier=image_verifier)

    register_archive_routes(app, backend, jobs)

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
        await socket.accept()
        initial_input: str | None = None
        initial_rows, initial_columns = 24, 80
        try:
            initial_message = await asyncio.wait_for(socket.receive(), timeout=2)
        except asyncio.TimeoutError:
            initial_message = None
        if initial_message is not None:
            if initial_message["type"] == "websocket.disconnect": return
            initial_raw = initial_message.get("text")
            if initial_raw is not None:
                try:
                    initial_data = json.loads(initial_raw)
                except json.JSONDecodeError:
                    initial_data = initial_raw
                if isinstance(initial_data, dict) and initial_data.get("type") == "resize":
                    try:
                        rows = int(initial_data.get("rows", 0))
                        columns = int(initial_data.get("cols", 0))
                        if 1 <= rows <= 1000 and 1 <= columns <= 1000:
                            initial_rows, initial_columns = rows, columns
                    except (TypeError, ValueError):
                        pass
                else:
                    data = initial_data.get("data", initial_raw) if isinstance(initial_data, dict) else initial_data
                    if isinstance(data, str):
                        initial_input = data
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(
            master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", initial_rows, initial_columns, 0, 0),
        )
        try:

            def attach_controlling_terminal() -> None:
                os.setsid()
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

            process = await asyncio.create_subprocess_exec(
                backend.docker_command, "exec", "-it", "--user", "root", "--workdir", "/workspace",
                session.container_name, "/bin/sh", "-lc",
                "if command -v bash >/dev/null 2>&1; then exec bash -l; else exec /bin/sh; fi",
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, env=backend.execution_environment,
                preexec_fn=attach_controlling_terminal,
            )
        except (OSError, WorkstationError):
            os.close(master_fd); os.close(slave_fd)
            await socket.close(code=1011); return
        os.close(slave_fd)
        tasks: list[asyncio.Task[None]] = []
        try:
            def resize(rows: int, columns: int) -> None:
                if not 1 <= rows <= 1000 or not 1 <= columns <= 1000:
                    return
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

            async def browser_to_shell() -> None:
                if initial_input is not None:
                    os.write(master_fd, initial_input.encode())
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

    if resources.frontend_dist.is_dir():
        app.mount("/assets", StaticFiles(directory=resources.frontend_dist / "assets"), name="assets")

        @app.get("/{path:path}")
        async def frontend(path: str):
            candidate = resources.frontend_dist / path
            return FileResponse(candidate if path and candidate.is_file() else resources.frontend_dist / "index.html")
    else:
        @app.get("/")
        async def unavailable_frontend():
            return JSONResponse(status_code=503, content={"code": "frontend_not_built", "message": "Build the Dockbench frontend with npm run build."})
    return app

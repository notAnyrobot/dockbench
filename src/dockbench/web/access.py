"""Browser access routes; owns transport lifetime and scoped desktop shutdown."""

from __future__ import annotations
import asyncio
from anyio import CancelScope
import json
from typing import Any, Callable, Awaitable
from fastapi import Cookie, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from dockbench.core.backend import Backend
from dockbench.core.workstation import Workstation
from dockbench.core.errors import WorkstationError
from dockbench.core.access import generic_shell
from dockbench.web.terminal import open_terminal
from dockbench.web.security import safe_error, _require_csrf
from dockbench.web.containers import container_public
from dockbench.web.sessions import (
    SESSION_TTL_SECONDS,
    DesktopSessions,
    TerminalSessions,
)


class SessionRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class PasswordResetRequest(BaseModel):
    password: str = Field(min_length=6, max_length=8)


def register_access_routes(
    app: FastAPI,
    ws: Callable[[], Workstation],
    managed_fleet: Callable[[], Any],
    backend: Backend,
) -> Callable[[str | None], Awaitable[None]]:
    app.state.sessions = DesktopSessions()
    app.state.terminal_sessions = TerminalSessions()
    app.state.desktop_sockets: set[WebSocket] = set()
    app.state.desktop_socket_containers: dict[WebSocket, str] = {}

    async def _close_desktop_sockets(container_name: str | None = None) -> None:
        # Sessions are container-scoped.  A stopped/replaced container only
        # disconnects its own VNC clients, never another desktop in the fleet.
        for socket in tuple(app.state.desktop_sockets):
            if (
                container_name is None
                or app.state.desktop_socket_containers.get(socket) == container_name
            ):
                await socket.close(code=1012)

    async def _fleet_call(method: str, *args: Any, **kwargs: Any) -> Any:
        return await run_in_threadpool(
            getattr(managed_fleet(), method), *args, **kwargs
        )

    @app.post("/api/desktop/sessions")
    async def create_desktop_session(
        body: SessionRequest,
        request: Request,
        dockbench_csrf: str | None = Cookie(default=None),
    ):
        _require_csrf(request, dockbench_csrf)
        try:
            endpoint = await run_in_threadpool(ws().ensure_desktop, body.password)
            session_id = await app.state.sessions.create(endpoint.port)
            return {"session_id": session_id, "expires_in": SESSION_TTL_SECONDS}
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/desktop/password")
    async def reset_desktop_password(
        body: PasswordResetRequest,
        request: Request,
        dockbench_csrf: str | None = Cookie(default=None),
    ):
        _require_csrf(request, dockbench_csrf)
        try:
            return (
                await run_in_threadpool(ws().reset_vnc_password, body.password)
            ).public()
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers/{name}/desktop/sessions")
    async def create_container_desktop_session(
        name: str,
        body: SessionRequest,
        request: Request,
        dockbench_csrf: str | None = Cookie(default=None),
    ):
        _require_csrf(request, dockbench_csrf)
        try:
            endpoint = await _fleet_call("ensure_desktop", name, body.password)
            session_id = await app.state.sessions.create(endpoint.port, name)
            return {"session_id": session_id, "expires_in": SESSION_TTL_SECONDS}
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers/{name}/desktop/password")
    async def reset_container_desktop_password(
        name: str,
        body: PasswordResetRequest,
        request: Request,
        dockbench_csrf: str | None = Cookie(default=None),
    ):
        _require_csrf(request, dockbench_csrf)
        try:
            return container_public(
                await _fleet_call("reset_vnc_password", name, body.password)
            )
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers/{name}/terminals")
    async def create_terminal(
        name: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)
    ):
        _require_csrf(request, dockbench_csrf)
        try:
            status = await _fleet_call("container", name)
            if status.state != "running":
                raise WorkstationError(
                    "container is not running; start it before opening a terminal"
                )
            session_id = await app.state.terminal_sessions.create(name)
            return {"session_id": session_id, "expires_in": SESSION_TTL_SECONDS}
        except Exception as exc:
            return safe_error(exc)

    async def forward_desktop(socket: WebSocket, session) -> None:
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
                    if message["type"] == "websocket.disconnect":
                        return
                    payload = message.get("bytes")
                    if payload is not None:
                        writer.write(payload)
                        await writer.drain()

            async def vnc_to_browser() -> None:
                while data := await reader.read(65536):
                    await socket.send_bytes(data)

            tasks = [
                asyncio.create_task(browser_to_vnc()),
                asyncio.create_task(vnc_to_browser()),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        except OSError:
            await socket.close(code=1011)
        finally:
            with CancelScope(shield=True):
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                app.state.desktop_sockets.discard(socket)
                app.state.desktop_socket_containers.pop(socket, None)
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass

    async def desktop_connection(
        socket: WebSocket, session_id: str, name: str | None = None
    ) -> None:
        host = socket.headers.get("host", "")
        if socket.headers.get("origin") != f"http://{host}":
            await socket.close(code=1008)
            return
        session = await app.state.sessions.consume(session_id)
        if session is None or (name is not None and session.container_name != name):
            await socket.close(code=1008)
            return
        await forward_desktop(socket, session)

    @app.websocket("/api/desktop/sessions/{session_id}/ws")
    async def desktop_proxy(socket: WebSocket, session_id: str):
        await desktop_connection(socket, session_id)

    @app.websocket("/api/containers/{name}/desktop/sessions/{session_id}/ws")
    async def container_desktop_proxy(socket: WebSocket, name: str, session_id: str):
        await desktop_connection(socket, session_id, name)

    @app.websocket("/api/terminals/{session_id}/ws")
    async def terminal_proxy(socket: WebSocket, session_id: str):
        host = socket.headers.get("host", "")
        if socket.headers.get("origin") != f"http://{host}":
            await socket.close(code=1008)
            return
        session = await app.state.terminal_sessions.consume(session_id)
        if session is None:
            await socket.close(code=1008)
            return
        await socket.accept()
        initial_input: str | None = None
        initial_rows, initial_columns = 24, 80
        try:
            initial_message = await asyncio.wait_for(socket.receive(), timeout=2)
        except asyncio.TimeoutError:
            initial_message = None
        if initial_message is not None:
            if initial_message["type"] == "websocket.disconnect":
                return
            initial_raw = initial_message.get("text")
            if initial_raw is not None:
                try:
                    initial_data = json.loads(initial_raw)
                except json.JSONDecodeError:
                    initial_data = initial_raw
                if (
                    isinstance(initial_data, dict)
                    and initial_data.get("type") == "resize"
                ):
                    try:
                        rows = int(initial_data.get("rows", 0))
                        columns = int(initial_data.get("cols", 0))
                        if 1 <= rows <= 1000 and 1 <= columns <= 1000:
                            initial_rows, initial_columns = rows, columns
                    except (TypeError, ValueError):
                        pass
                else:
                    data = (
                        initial_data.get("data", initial_raw)
                        if isinstance(initial_data, dict)
                        else initial_data
                    )
                    if isinstance(data, str):
                        initial_input = data
        tasks: list[asyncio.Task[None]] = []
        try:
            async with open_terminal(
                [
                    backend.docker_command,
                    *generic_shell(session.container_name).arguments,
                ],
                backend.execution_environment,
                initial_rows,
                initial_columns,
            ) as terminal:

                async def browser_to_shell() -> None:
                    if initial_input is not None:
                        await terminal.write(initial_input)
                    while True:
                        message = await socket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        raw = message.get("text")
                        if raw is None:
                            continue
                        try:
                            message_data = json.loads(raw)
                        except json.JSONDecodeError:
                            message_data = raw
                        if (
                            isinstance(message_data, dict)
                            and message_data.get("type") == "resize"
                        ):
                            try:
                                terminal.resize(
                                    int(message_data.get("rows", 0)),
                                    int(message_data.get("cols", 0)),
                                )
                            except (TypeError, ValueError):
                                pass
                            continue
                        data = (
                            message_data.get("data", raw)
                            if isinstance(message_data, dict)
                            else message_data
                        )
                        if not isinstance(data, str):
                            continue
                        await terminal.write(data)

                async def shell_to_browser() -> None:
                    while True:
                        try:
                            data = await terminal.read()
                        except OSError:
                            return
                        if not data:
                            return
                        await socket.send_text(data.decode(errors="replace"))

                try:
                    tasks = [
                        asyncio.create_task(browser_to_shell()),
                        asyncio.create_task(shell_to_browser()),
                    ]
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    with CancelScope(shield=True):
                        await asyncio.gather(*tasks, return_exceptions=True)
        except WebSocketDisconnect:
            pass
        except (OSError, WorkstationError):
            await socket.close(code=1011)

    return _close_desktop_sockets

"""Browser access protocols and their resource lifetimes."""
import asyncio
import os

import pytest
from starlette.websockets import WebSocketDisconnect

from fastapi.testclient import TestClient

from dockbench.core.backend import Backend
from dockbench.web.app import create_app
from web_fakes import FakeWorkstation, FakeFleet
from dockbench.web.sessions import DesktopSessions


def test_vnc_password_can_be_reset_with_csrf_protection():
    workstation = FakeWorkstation()
    client = TestClient(create_app(workstation))
    status = client.get("/api/workstation")
    token = status.json()["csrf_token"]

    assert client.post("/api/desktop/password", json={"password": "new-pass"}).status_code == 403
    response = client.post(
        "/api/desktop/password",
        json={"password": "new-pass"},
        headers={"origin": "http://testserver", "x-csrf-token": token},
    )

    assert response.status_code == 200
    assert response.json()["desktop_ready"] is True
    assert workstation.reset_password == "new-pass"


def test_desktop_sessions_are_single_use():
    sessions = DesktopSessions()

    async def consume_once():
        token = await sessions.create(5901)
        first = await sessions.consume(token)
        second = await sessions.consume(token)
        return first, second

    first, second = asyncio.run(consume_once())
    assert first is not None and first.port == 5901
    assert second is None


def test_tcp_open_failure_removes_the_desktop_socket():
    app = create_app(FakeWorkstation())
    token = asyncio.run(app.state.sessions.create(1))  # no listener is expected on port 1
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/desktop/sessions/{token}/ws", headers={"origin": "http://testserver"}) as socket:
            socket.receive_bytes()
    assert app.state.desktop_sockets == set()


def test_terminal_resize_reaches_the_interactive_child(tmp_path):
    docker_probe = tmp_path / "docker-probe"
    docker_probe.write_text(
        "#!/bin/sh\n"
        "trap 'printf \"RESIZED \"; stty size' WINCH\n"
        "printf 'READY\\r\\n'\n"
        "sleep 1\n"
        "printf 'DONE\\r\\n'\n"
    )
    docker_probe.chmod(0o755)

    fleet = FakeFleet()
    fleet.config = type(
        "Config",
        (),
        {
            "workspace_root": "/data/atom7/workspace",
            "data_mounts": (),
            "docker_command": os.fspath(docker_probe),
        },
    )()
    app = create_app(FakeWorkstation(), fleet=fleet, backend=Backend(environment={"DOCKBENCH_DOCKER": str(docker_probe)}))
    session_id = asyncio.run(app.state.terminal_sessions.create("alpha"))

    output = ""
    with TestClient(app).websocket_connect(
        f"/api/terminals/{session_id}/ws",
        headers={"origin": "http://testserver"},
    ) as socket:
        socket.send_json({"type": "resize", "rows": 24, "cols": 80})
        while "READY" not in output:
            output += socket.receive_text()
        socket.send_json({"type": "resize", "rows": 37, "cols": 120})
        while "DONE" not in output:
            output += socket.receive_text()

    assert "RESIZED 37 120" in output


def test_terminal_has_browser_dimensions_before_interactive_child_starts(tmp_path, monkeypatch):
    docker_probe = tmp_path / "docker-probe"
    docker_probe.write_text("#!/bin/sh\nprintf 'READY\\r\\n'\nsleep 1\nprintf 'DONE\\r\\n'\n")
    docker_probe.chmod(0o755)

    fleet = FakeFleet()
    fleet.config = type(
        "Config",
        (),
        {
            "workspace_root": "/data/atom7/workspace",
            "data_mounts": (),
            "docker_command": os.fspath(docker_probe),
        },
    )()
    app = create_app(FakeWorkstation(), fleet=fleet, backend=Backend(environment={"DOCKBENCH_DOCKER": str(docker_probe)}))
    session_id = asyncio.run(app.state.terminal_sessions.create("alpha"))
    spawned_sizes = []
    spawned_commands = []
    create_subprocess_exec = asyncio.create_subprocess_exec

    async def capture_spawn_size(*args, **kwargs):
        spawned_commands.append(args)
        spawned_sizes.append(os.get_terminal_size(kwargs["stdin"]))
        return await create_subprocess_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn_size)

    output = ""
    with TestClient(app).websocket_connect(
        f"/api/terminals/{session_id}/ws",
        headers={"origin": "http://testserver"},
    ) as socket:
        socket.send_json({"type": "resize", "rows": 37, "cols": 120})
        while "DONE" not in output:
            output += socket.receive_text()

    assert spawned_sizes == [os.terminal_size((120, 37))]
    assert spawned_commands == [(
        str(docker_probe), "exec", "-it", "--user", "root", "--workdir", "/workspace",
        "alpha", "/bin/sh", "-lc",
        "if command -v bash >/dev/null 2>&1; then exec bash -l; else exec /bin/sh; fi",
    )]


def test_terminal_cancelled_launch_releases_pty(tmp_path, monkeypatch):
    """Cancellation while launching must close the transport's allocated PTY."""
    import concurrent.futures
    descriptors = []

    async def cancelled_launch(*args, **kwargs):
        descriptors.append(kwargs["stdin"])
        raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cancelled_launch)
    app = create_app(FakeWorkstation(), fleet=FakeFleet())
    token = asyncio.run(app.state.terminal_sessions.create("alpha"))
    try:
        with TestClient(app).websocket_connect(
            f"/api/terminals/{token}/ws", headers={"origin": "http://testserver"}
        ) as socket:
            socket.send_json({"type": "resize", "rows": 24, "cols": 80})
            socket.receive_text()
    except (concurrent.futures.CancelledError, WebSocketDisconnect):
        pass
    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_terminal_disconnect_reaps_child_that_ignores_termination(tmp_path):
    import sys
    docker_probe = tmp_path / "docker-probe"
    docker_probe.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "print(os.getpid(), flush=True)\n"
        "while True: time.sleep(1)\n"
    )
    docker_probe.chmod(0o755)
    app = create_app(FakeWorkstation(), fleet=FakeFleet(),
                     backend=Backend(environment={"DOCKBENCH_DOCKER": str(docker_probe)}))
    token = asyncio.run(app.state.terminal_sessions.create("alpha"))
    with TestClient(app).websocket_connect(
        f"/api/terminals/{token}/ws", headers={"origin": "http://testserver"}
    ) as socket:
        socket.send_json({"type": "resize", "rows": 24, "cols": 80})
        child_pid = int(socket.receive_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_desktop_disconnect_closes_tcp_connection_and_socket_registration():
    import socket as tcp
    import threading
    disconnected = threading.Event()
    with tcp.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(3)

        def serve():
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(3)
                connection.sendall(b"READY")
                if connection.recv(1) == b"":
                    disconnected.set()

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        app = create_app(FakeWorkstation())
        token = asyncio.run(app.state.sessions.create(listener.getsockname()[1], "alpha"))
        with TestClient(app).websocket_connect(
            f"/api/containers/alpha/desktop/sessions/{token}/ws",
            headers={"origin": "http://testserver"},
        ) as socket:
            assert socket.receive_bytes() == b"READY"
        worker.join(timeout=3)
    assert disconnected.is_set()
    assert app.state.desktop_sockets == set()
    assert app.state.desktop_socket_containers == {}


def test_named_desktop_token_cannot_cross_containers_or_replay():
    app = create_app(FakeWorkstation())
    token = asyncio.run(app.state.sessions.create(5901, "alpha"))
    client = TestClient(app)
    for name in ("beta", "alpha"):
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                f"/api/containers/{name}/desktop/sessions/{token}/ws",
                headers={"origin": "http://testserver"},
            ):
                pass
        assert rejected.value.code == 1008


def test_expired_terminal_token_cannot_launch_a_child(monkeypatch):
    from dockbench.web import sessions
    monkeypatch.setattr(sessions, "SESSION_TTL_SECONDS", 0)
    app = create_app(FakeWorkstation())
    token = asyncio.run(app.state.terminal_sessions.create("alpha"))
    with pytest.raises(WebSocketDisconnect) as rejected:
        with TestClient(app).websocket_connect(
            f"/api/terminals/{token}/ws", headers={"origin": "http://testserver"}
        ):
            pass
    assert rejected.value.code == 1008

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from docker_ws.core.workstation import (
    WorkstationError,
    WorkstationRebuildRequired,
    WorkstationStatus,
)
from docker_ws.web.app import DesktopSessions, create_app


class FakeWorkstation:
    reset_password: str | None = None
    def status(self): return WorkstationStatus("stopped", False, "test", "docker-ws", "/workspace")
    def start(self, *args):
        self.start_args = args
        return WorkstationStatus("running", False, "test", "docker-ws", "/workspace")
    def stop(self): return WorkstationStatus("stopped", False, "test", "docker-ws", "/workspace")
    def reset_vnc_password(self, password):
        self.reset_password = password
        return WorkstationStatus("running", True, "test", "docker-ws", "/workspace")


def test_status_issues_csrf_and_mutations_require_it():
    client = TestClient(create_app(FakeWorkstation()))
    status = client.get("/api/workstation")
    assert status.status_code == 200
    assert client.post("/api/workstation/start").status_code == 403
    token = status.json()["csrf_token"]
    started = client.post("/api/workstation/start", headers={"origin": "http://testserver", "x-csrf-token": token})
    assert started.status_code == 200
    assert started.json()["state"] == "running"


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


def test_workstation_errors_are_redacted_from_the_response():
    class FailingWorkstation(FakeWorkstation):
        def start(self): raise WorkstationError("docker token=secret should never reach the browser")

    client = TestClient(create_app(FailingWorkstation()))
    status = client.get("/api/workstation")
    response = client.post("/api/workstation/start", headers={"origin": "http://testserver", "x-csrf-token": status.json()["csrf_token"]})
    assert response.status_code == 503
    assert response.json()["message"] == "Docker Workstation is unavailable. Check its status and try again."
    assert "secret" not in response.text


def test_stale_workstation_returns_actionable_rebuild_error():
    class StaleWorkstation(FakeWorkstation):
        def start(self):
            raise WorkstationRebuildRequired("stale image details remain private")

    client = TestClient(create_app(StaleWorkstation()))
    status = client.get("/api/workstation")
    response = client.post(
        "/api/workstation/start",
        headers={
            "origin": "http://testserver",
            "x-csrf-token": status.json()["csrf_token"],
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "workstation_rebuild_required"
    assert response.json()["message"] == (
        "The workstation image or launch settings changed. Run "
        "`uv run docker-ws image rebuild`, then try again."
    )
    assert "private" not in response.text


def test_tcp_open_failure_removes_the_desktop_socket():
    app = create_app(FakeWorkstation())
    token = asyncio.run(app.state.sessions.create(1))  # no listener is expected on port 1
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/desktop/sessions/{token}/ws", headers={"origin": "http://testserver"}) as socket:
            socket.receive_bytes()
    assert app.state.desktop_sockets == set()


def test_start_accepts_image_gpu_and_replace_fields():
    workstation = FakeWorkstation()
    client = TestClient(create_app(workstation))
    token = client.get("/api/workstation").json()["csrf_token"]
    response = client.post("/api/workstation/start", json={"image": "ubuntu:24.04", "gpu_uuids": ["GPU-a"], "replace": True}, headers={"origin": "http://testserver", "x-csrf-token": token})
    assert response.status_code == 200
    assert workstation.start_args == ("ubuntu:24.04", ("GPU-a",), False, True)

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from docker_ws.core.workstation import (
    WorkstationError,
    WorkstationRebuildRequired,
    WorkstationStatus,
)
from docker_ws.core.errors import WorkstationGPUConflict
from docker_ws.web.app import DesktopSessions, _redact_image_log, create_app
from docker_ws.core.recipes import RecipeError


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


class FakeFleet:
    def __init__(self):
        self.created = None
        self.items = [WorkstationStatus("running", False, "desktop", "alpha", "/workspace", image_id="sha256:one", image_ref="desktop:latest", gpu_uuids=("GPU-a",), desktop_capable=True)]

    def containers(self): return tuple(self.items)
    def container(self, name): return next(item for item in self.items if item.container_name == name)
    def create(self, name, image, gpu_uuids, all_gpus):
        self.created = (name, image, gpu_uuids, all_gpus)
        return WorkstationStatus("running", False, image, name, "/workspace", image_id="sha256:new", image_ref=image, gpu_uuids=gpu_uuids)
    def start(self, name): return self.container(name)
    def stop(self, name): return WorkstationStatus("stopped", False, "desktop", name, "/workspace")
    def remove(self, name): self.removed = name
    def delete_state(self, name): self.deleted_state = name
    def recreate(self, name): return self.container(name)
    def orphaned_states(self): return ("removed-alpha",)
    def inventory(self):
        return {"images": [{"id": "sha256:one", "display_reference": "desktop:latest", "desktop_capable": True}], "gpus": [{"uuid": "GPU-a", "index": 0, "name": "GPU", "memory_total_mib": 100, "reservation": "alpha", "available": False}], "gpu_diagnostic": None, "default_image": "desktop:latest", "containers": []}


class FakeManifest:
    def __init__(self, recipe_id="android-ws", revision=1, tag="android-ws:test", target="desktop", platform="linux/amd64"):
        self.id, self.revision, self.dockerfile = recipe_id, revision, f"Dockerfile.{recipe_id}-v{revision}"
        self.tag, self.target, self.platform = tag, target, platform


class FakeRecipe:
    def __init__(self, *args, **kwargs): self.manifest = FakeManifest(*args, **kwargs)


class FakeRecipes:
    def __init__(self): self.items = {"android-ws": FakeRecipe()}; self.created = None; self.revised = None
    def list(self): return tuple(self.items.values())
    def get(self, recipe_id): return self.items[recipe_id]
    def create(self, recipe_id, dockerfile, **kwargs):
        if recipe_id in self.items: raise RecipeError(f"recipe already exists: {recipe_id}")
        self.created = (recipe_id, dockerfile, kwargs); item = FakeRecipe(recipe_id, **kwargs); self.items[recipe_id] = item; return item
    def revise(self, recipe_id, dockerfile, **kwargs):
        self.revised = (recipe_id, dockerfile, kwargs); old = self.items[recipe_id].manifest; item = FakeRecipe(recipe_id, old.revision + 1, kwargs.get("tag", old.tag), kwargs.get("target", old.target), kwargs.get("platform", old.platform)); self.items[recipe_id] = item; return item


class FakeImageBuilder:
    def __init__(self): self.calls = []
    def build(self, recipe, **kwargs): self.calls.append((recipe, kwargs)); return "build completed"


class FakeImageVerifier:
    def __init__(self): self.calls = []
    def verify(self, image): self.calls.append(image); return "verified"


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
    assert workstation.start_args == ("ubuntu:24.04", ("GPU-a",), None, True)


def test_fleet_inventory_and_container_lifecycle_routes_are_scoped_and_csrf_protected():
    fleet = FakeFleet()
    client = TestClient(create_app(FakeWorkstation(), fleet=fleet))
    response = client.get("/api/containers")
    assert response.status_code == 200
    assert response.json()["containers"][0]["name"] == "alpha"
    token = response.json()["csrf_token"]
    headers = {"origin": "http://testserver", "x-csrf-token": token}

    inventory = client.get("/api/host/inventory")
    assert inventory.json()["gpus"][0]["owner"] == "alpha"
    created = client.post("/api/containers", json={"name": "beta", "image": "ubuntu:24.04", "gpu_uuids": []}, headers=headers)
    assert created.status_code == 200
    assert fleet.created == ("beta", "ubuntu:24.04", (), False)
    removed = client.post("/api/containers/alpha/remove", headers=headers)
    assert removed.json() == {"removed": True, "name": "alpha"}
    assert fleet.removed == "alpha"


def test_csrf_token_remains_valid_when_another_workbench_tab_initializes():
    client = TestClient(create_app(FakeWorkstation(), fleet=FakeFleet()))
    first_token = client.get("/api/containers").json()["csrf_token"]
    second_token = client.get("/api/containers").json()["csrf_token"]

    assert second_token == first_token
    response = client.post(
        "/api/containers/alpha/stop",
        headers={"origin": "http://testserver", "x-csrf-token": first_token},
    )
    assert response.status_code == 200


def test_gpu_reservation_conflict_is_a_safe_actionable_409_response():
    class ConflictedFleet(FakeFleet):
        def create(self, name, image, gpu_uuids, all_gpus):
            raise WorkstationGPUConflict("GPU-a", "alpha")

    client = TestClient(create_app(FakeWorkstation(), fleet=ConflictedFleet()))
    token = client.get("/api/containers").json()["csrf_token"]
    response = client.post(
        "/api/containers",
        json={"name": "beta", "image": "desktop:latest", "gpu_uuids": ["GPU-a"]},
        headers={"origin": "http://testserver", "x-csrf-token": token},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "gpu_reserved"
    assert response.json()["message"] == "GPU GPU-a is reserved by running container alpha."


def test_orphaned_container_states_are_listed_for_explicit_cleanup():
    client = TestClient(create_app(FakeWorkstation(), fleet=FakeFleet()))
    assert client.get("/api/container-states").json() == {
        "container_states": [{"name": "removed-alpha"}],
    }


def test_image_job_log_redaction_removes_secret_values():
    log = _redact_image_log("build token=private password:also-private normal output")
    assert "private" not in log
    assert "token=[REDACTED]" in log
    assert "password:[REDACTED]" in log


def test_recipe_api_requires_csrf_and_creates_revisions_without_starting_builds():
    recipes = FakeRecipes()
    builder = FakeImageBuilder()
    client = TestClient(create_app(FakeWorkstation(), fleet=FakeFleet(), recipes=recipes, image_builder=builder, image_verifier=FakeImageVerifier()))
    listed = client.get("/api/image-recipes")
    assert listed.status_code == 200
    assert listed.json()["recipes"] == [{"id": "android-ws", "revision": 1, "dockerfile": "Dockerfile.android-ws-v1", "tag": "android-ws:test", "target": "desktop", "platform": "linux/amd64"}]
    payload = {"id": "custom-ws", "dockerfile": "FROM ubuntu:24.04\n", "tag": "custom:one", "target": None, "platform": "linux/amd64"}
    assert client.post("/api/image-recipes", json=payload).status_code == 403
    headers = {"origin": "http://testserver", "x-csrf-token": listed.json()["csrf_token"]}
    created = client.post("/api/image-recipes", json=payload, headers=headers)
    assert created.status_code == 200
    assert recipes.created == ("custom-ws", "FROM ubuntu:24.04\n", {"tag": "custom:one", "target": None, "platform": "linux/amd64"})
    assert builder.calls == []
    revised = client.post("/api/image-recipes/custom-ws/revisions", json={"dockerfile": "FROM ubuntu:24.04\nRUN true\n", "target": "desktop"}, headers=headers)
    assert revised.status_code == 200
    assert recipes.revised == ("custom-ws", "FROM ubuntu:24.04\nRUN true\n", {"target": "desktop"})


def test_build_and_verify_are_csrf_protected_serialized_image_jobs():
    recipes, builder, verifier = FakeRecipes(), FakeImageBuilder(), FakeImageVerifier()
    app = create_app(FakeWorkstation(), fleet=FakeFleet(), recipes=recipes,
                     image_builder=builder, image_verifier=verifier)
    with TestClient(app) as client:
        token = client.get("/api/image-recipes").json()["csrf_token"]
        headers = {"origin": "http://testserver", "x-csrf-token": token}
        assert client.post("/api/images/build", json={}).status_code == 403
        build = client.post("/api/images/build", json={"recipe_id": "android-ws", "tag": "custom:two", "target": None, "no_cache": True}, headers=headers)
        assert build.status_code == 200
        build_job = client.get(f"/api/image-jobs/{build.json()['id']}")
        assert build_job.json()["state"] == "completed"
        assert builder.calls and builder.calls[0][1] == {"tag": "custom:two", "target": None, "no_cache": True}
        verify = client.post("/api/images/sha256:one/verify", headers=headers)
        assert verify.status_code == 200
        verify_job = client.get(f"/api/image-jobs/{verify.json()['id']}")
        assert verify_job.json()["state"] == "completed"
        assert verifier.calls == ["sha256:one"]

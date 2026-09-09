"""Application readiness, HTTP lifecycle, validation, and resource contracts."""
from fastapi.testclient import TestClient

from dockbench.core.backend import Backend
from dockbench.web.app import create_app
from web_fakes import FakeWorkstation, FakeFleet
from dockbench.core.errors import (
    WorkstationError, WorkstationRebuildRequired, DockerCommandError, DataRootError,
    WorkstationContainerExists, WorkstationGPUConflict, WorkspaceRootError,
)


def test_health_is_a_minimal_docker_independent_readiness_probe():
    class UnavailableWorkstation:
        def status(self):
            raise AssertionError("health must not query Docker or workstation status")

    response = TestClient(create_app(UnavailableWorkstation())).get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_status_issues_csrf_and_mutations_require_it():
    client = TestClient(create_app(FakeWorkstation()))
    status = client.get("/api/workstation")
    assert status.status_code == 200
    assert client.post("/api/workstation/start").status_code == 403
    token = status.json()["csrf_token"]
    started = client.post("/api/workstation/start", headers={"origin": "http://testserver", "x-csrf-token": token})
    assert started.status_code == 200
    assert started.json()["state"] == "running"


def test_workstation_errors_are_redacted_from_the_response():
    class FailingWorkstation(FakeWorkstation):
        def start(self): raise WorkstationError("docker token=secret should never reach the browser")

    client = TestClient(create_app(FailingWorkstation()))
    status = client.get("/api/workstation")
    response = client.post("/api/workstation/start", headers={"origin": "http://testserver", "x-csrf-token": status.json()["csrf_token"]})
    assert response.status_code == 503
    assert response.json()["message"] == "Dockbench is unavailable. Check its status and try again."
    assert "secret" not in response.text


def test_docker_errors_include_a_sanitized_cause_and_remediation():
    class FailingWorkstation(FakeWorkstation):
        def start(self):
            raise DockerCommandError(
                "docker: Error response from daemon: failed to create task: "
                "insufficient memory; token=super-secret; socket=/run/user/1000/docker.sock"
            )

    client = TestClient(create_app(FailingWorkstation()))
    status = client.get("/api/workstation")
    response = client.post(
        "/api/workstation/start",
        headers={"origin": "http://testserver", "x-csrf-token": status.json()["csrf_token"]},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "docker_error"
    assert "insufficient memory" in response.json()["message"]
    assert "Check that Docker is running" in response.json()["message"]
    assert "super-secret" not in response.text
    assert "/run/user/1000/docker.sock" not in response.text


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
        "`uv run dockbench image rebuild`, then try again."
    )
    assert "private" not in response.text


def test_start_accepts_image_gpu_and_replace_fields():
    workstation = FakeWorkstation()
    client = TestClient(create_app(workstation))
    token = client.get("/api/workstation").json()["csrf_token"]
    response = client.post("/api/workstation/start", json={"image": "ubuntu:24.04", "gpu_uuids": ["GPU-a"], "replace": True}, headers={"origin": "http://testserver", "x-csrf-token": token})
    assert response.status_code == 200
    assert workstation.start_args == ("ubuntu:24.04", ("GPU-a",), None, True)


def test_fleet_inventory_and_container_lifecycle_routes_are_scoped_and_csrf_protected():
    fleet = FakeFleet()
    backend = Backend(config=type("Config", (), {"repository_root": None, "docker_command": "docker", "workspace_root": "/data/atom7/workspace", "data_mounts": (("/data/share/motion_datasets", "/data/motions"),)})())
    client = TestClient(create_app(FakeWorkstation(), fleet=fleet, backend=backend))
    response = client.get("/api/containers")
    assert response.status_code == 200
    assert response.json()["containers"][0]["name"] == "alpha"
    token = response.json()["csrf_token"]
    headers = {"origin": "http://testserver", "x-csrf-token": token}

    inventory = client.get("/api/host/inventory")
    assert inventory.json()["gpus"][0]["owner"] == "alpha"
    assert inventory.json()["workspace_root"] == "/data/atom7/workspace"
    assert inventory.json()["data_root"] == "/data/share/motion_datasets"
    created = client.post("/api/containers", json={"name": "beta", "image": "ubuntu:24.04", "gpu_uuids": []}, headers=headers)
    assert created.status_code == 200
    assert fleet.created == ("beta", "ubuntu:24.04", (), False, None, None)

    custom = client.post("/api/containers", json={"name": "gamma", "image": "ubuntu:24.04", "gpu_uuids": [], "workspace_root": "/custom/code"}, headers=headers)
    assert custom.status_code == 200
    assert fleet.created == ("gamma", "ubuntu:24.04", (), False, "/custom/code", None)
    custom_data = client.post("/api/containers", json={"name": "delta", "image": "ubuntu:24.04", "data_root": "/datasets/motions"}, headers=headers)
    assert custom_data.status_code == 200
    assert fleet.created == ("delta", "ubuntu:24.04", (), False, None, "/datasets/motions")
    removed = client.post("/api/containers/alpha/remove", headers=headers)
    assert removed.json() == {"removed": True, "name": "alpha"}
    assert fleet.removed == "alpha"


def test_csrf_token_remains_valid_when_another_dockbench_tab_initializes():
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
        def create(self, name, image, gpu_uuids, all_gpus, workspace_root=None, data_root=None):
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


def test_invalid_custom_code_root_is_an_actionable_422_response():
    class InvalidWorkspaceFleet(FakeFleet):
        def create(self, name, image, gpu_uuids, all_gpus, workspace_root=None, data_root=None):
            raise WorkspaceRootError(f"workspace root does not exist: {workspace_root}")

    client = TestClient(create_app(FakeWorkstation(), fleet=InvalidWorkspaceFleet()))
    token = client.get("/api/containers").json()["csrf_token"]
    response = client.post(
        "/api/containers",
        json={"name": "beta", "image": "desktop:latest", "workspace_root": "/missing"},
        headers={"origin": "http://testserver", "x-csrf-token": token},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_workspace_root"
    assert response.json()["message"] == "The selected workspace root does not exist or is not a directory."


def test_invalid_custom_data_root_is_an_actionable_422_response():
    class InvalidDataFleet(FakeFleet):
        def create(self, name, image, gpu_uuids, all_gpus, workspace_root=None, data_root=None):
            raise DataRootError(f"data root does not exist: {data_root}")

    client = TestClient(create_app(FakeWorkstation(), fleet=InvalidDataFleet()))
    token = client.get("/api/containers").json()["csrf_token"]
    response = client.post(
        "/api/containers",
        json={"name": "beta", "image": "desktop:latest", "data_root": "/missing"},
        headers={"origin": "http://testserver", "x-csrf-token": token},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_data_root"
    assert response.json()["message"] == "The selected data root does not exist or is not a directory."


def test_create_container_name_collision_is_a_safe_actionable_409_response(caplog):
    class ExistingNameFleet(FakeFleet):
        def create(self, name, image, gpu_uuids, all_gpus, workspace_root=None, data_root=None):
            raise WorkstationContainerExists(name)

    client = TestClient(create_app(FakeWorkstation(), fleet=ExistingNameFleet()))
    token = client.get("/api/containers").json()["csrf_token"]

    with caplog.at_level("WARNING", logger="dockbench.web.app"):
        response = client.post(
            "/api/containers",
            json={"name": "workstation-8gpu", "image": "desktop:latest", "gpu_uuids": []},
            headers={"origin": "http://testserver", "x-csrf-token": token},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "container_exists"
    assert response.json()["message"] == (
        "A Docker container named workstation-8gpu already exists. "
        "Choose another name, or remove or rename the existing container before trying again."
    )
    correlation_id = response.json()["correlation_id"]
    assert correlation_id in caplog.text
    assert "container_exists" in caplog.text


def test_unexpected_workstation_error_stays_redacted_but_logs_sanitized_diagnostic(caplog):
    class FailingFleet(FakeFleet):
        def create(self, name, image, gpu_uuids, all_gpus, workspace_root=None, data_root=None):
            raise WorkstationError(
                "Docker setup failed: token=private-token "
                "Authorization: Bearer private-bearer path=/state/private"
            )

    client = TestClient(create_app(FakeWorkstation(), fleet=FailingFleet()))
    token = client.get("/api/containers").json()["csrf_token"]

    with caplog.at_level("WARNING", logger="dockbench.web.app"):
        response = client.post(
            "/api/containers",
            json={"name": "workstation-8gpu", "image": "desktop:latest", "gpu_uuids": []},
            headers={"origin": "http://testserver", "x-csrf-token": token},
        )

    assert response.status_code == 503
    assert response.json()["message"] == "Dockbench is unavailable. Check its status and try again."
    assert "private-token" not in response.text
    assert "private-token" not in caplog.text
    assert "private-bearer" not in caplog.text
    assert "Docker setup failed" in caplog.text
    assert "[PATH]" in caplog.text


def test_orphaned_container_states_are_listed_for_explicit_cleanup():
    client = TestClient(create_app(FakeWorkstation(), fleet=FakeFleet()))
    assert client.get("/api/container-states").json() == {
        "container_states": [{"name": "removed-alpha"}],
    }


def test_frontend_uses_explicit_checkout_outside_working_directory(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    dist = root / "src/dockbench/web/frontend/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("checkout frontend")
    (dist / "assets/app.js").write_text("checkout asset")
    monkeypatch.chdir(tmp_path)

    client = TestClient(create_app(repository_root=root))

    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/nested/route").text == "checkout frontend"
    assert client.get("/assets/app.js").text == "checkout asset"


def test_explicit_checkout_also_selects_default_backend_recipes(tmp_path, monkeypatch):
    from pathlib import Path

    fake_docker = Path(__file__).resolve().parents[1] / "helpers/fake-docker"
    monkeypatch.setenv("DOCKBENCH_DOCKER", str(fake_docker))
    monkeypatch.setenv("DOCKBENCH_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(tmp_path / "docker.log"))
    root = tmp_path / "empty-checkout"
    (root / "assets/images").mkdir(parents=True)
    client = TestClient(create_app(repository_root=root))

    response = client.get("/api/image-recipes")

    assert response.status_code == 200
    assert response.json()["recipes"] == []

"""Lifecycle contracts exercised through both real adapters and controlled Docker."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from dockbench.cli.main import main
from dockbench.core.backend import Backend
from dockbench.core.errors import WorkstationError
from dockbench.web.app import create_app
from test_fleet import Docker, Inventory, config


@pytest.mark.parametrize("adapter", ["cli", "http"])
def test_default_lifecycle_retains_request_conflicts_and_state(tmp_path, capsys, adapter):
    configuration = replace(config(tmp_path), image="demo:image")
    docker = Docker()
    backend = Backend(config=configuration, runner=docker)
    backend.inventory = Inventory()
    client = TestClient(create_app(backend=backend))
    token = client.get("/api/workstation").json()["csrf_token"]
    headers = {"X-CSRF-Token": token, "origin": "http://testserver"}
    if adapter == "cli":
        assert main(["start", "--gpu", "none"], backend=backend) == 0
        assert "dockbench: running" in capsys.readouterr().out
        assert main(["start", "--gpu", "GPU-a"], backend=backend) == 1
        assert "--replace" in capsys.readouterr().err
        assert main(["stop"], backend=backend) == 0
        assert "dockbench: stopped" in capsys.readouterr().out
    else:
        response = client.post("/api/workstation/start", json={"all_gpus": False}, headers=headers)
        assert response.status_code == 200
        assert response.json()["state"] == "running"
        response = client.post("/api/workstation/start", json={"gpu_uuids": ["GPU-a"]}, headers=headers)
        assert response.status_code == 409
        assert response.json()["code"] == "workstation_replace_required"
        response = client.post("/api/workstation/stop", headers=headers)
        assert response.status_code == 200
        assert response.json()["state"] == "stopped"
    assert docker.containers["dockbench"]["state"] == "exited"
    assert configuration.state_root.is_dir()
    assert not any(command[0] == "rm" for command in docker.commands)


def test_http_failed_named_start_cleans_container_and_retains_state(tmp_path):
    class FailedStartDocker(Docker):
        def run(self, args, **kwargs):
            result = super().run(args, **kwargs)
            if args[:2] == ["run", "-d"]:
                self.containers[args[args.index("--name") + 1]]["state"] = "created"
                raise WorkstationError("failed to start")
            return result

    configuration = config(tmp_path)
    docker = FailedStartDocker()
    backend = Backend(config=configuration, runner=docker)
    backend.inventory = Inventory()
    client = TestClient(create_app(backend=backend))
    token = client.get("/api/containers").json()["csrf_token"]
    response = client.post("/api/containers", json={"name": "new", "image": "demo:image"},
                           headers={"X-CSRF-Token": token, "origin": "http://testserver"})
    assert response.status_code == 503
    assert "new" not in docker.containers
    assert (configuration.state_root / "containers" / "new").is_dir()
    assert client.get("/api/container-states").json() == {"container_states": [{"name": "new"}]}


def test_shell_preserves_long_configured_default_but_validates_explicit_names(tmp_path, capsys):
    name = "d" * 64
    configuration = replace(config(tmp_path), container_name=name)
    docker = Docker()
    backend = Backend(config=configuration, runner=docker)
    backend.inventory = Inventory()
    assert main(["start", "--gpu", "none"], backend=backend) == 0
    assert main(["shell"], backend=backend) == 0
    assert any(command[:2] == ["exec", "-it"] and name in command for command in docker.commands)
    assert main(["shell", name], backend=backend) == 1
    assert "container names must be 1–63" in capsys.readouterr().err

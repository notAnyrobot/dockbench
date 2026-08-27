import subprocess

import pytest

from dockbench.core import images
from dockbench.core.workstation import WorkstationError


def test_package_uses_configured_image_and_stable_archive_name(tmp_path, monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(images.shutil, "which", lambda command: command)
    result = images.WorkstationImages("fake-docker", "dockbench:custom", run).package(tmp_path)
    assert result.archive == tmp_path / images.ARCHIVE_NAME
    assert commands == [
        ["fake-docker", "image", "inspect", "dockbench:custom"],
        ["fake-docker", "save", "--output", str(result.archive), "dockbench:custom"],
    ]


def test_load_rejects_missing_archive_before_docker_call(tmp_path, monkeypatch):
    monkeypatch.setattr(images.shutil, "which", lambda command: command)
    with pytest.raises(WorkstationError, match="tar file does not exist"):
        images.WorkstationImages("fake-docker").load([tmp_path / "missing.tar"])


def test_image_transfer_uses_only_dockbench_environment_variables(monkeypatch):
    monkeypatch.setenv("DOCKBENCH_DOCKER", "dockbench-docker")
    monkeypatch.setenv("DOCKBENCH_IMAGE", "dockbench:image")
    monkeypatch.setenv("ROBOTICS_WS_DOCKER", "former-docker")
    monkeypatch.setenv("ROBOTICS_WS_DESKTOP_IMAGE", "former:image")

    configured = images.WorkstationImages()

    assert configured.docker_command == "dockbench-docker"
    assert configured.image == "dockbench:image"

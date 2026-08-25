import subprocess

import pytest

from docker_ws.core import images
from docker_ws.core.workstation import WorkstationError


def test_package_uses_configured_image_and_stable_archive_name(tmp_path, monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(images.shutil, "which", lambda command: command)
    result = images.WorkstationImages("fake-docker", "docker-ws:custom", run).package(tmp_path)
    assert result.archive == tmp_path / images.ARCHIVE_NAME
    assert commands == [
        ["fake-docker", "image", "inspect", "docker-ws:custom"],
        ["fake-docker", "save", "--output", str(result.archive), "docker-ws:custom"],
    ]


def test_load_rejects_missing_archive_before_docker_call(tmp_path, monkeypatch):
    monkeypatch.setattr(images.shutil, "which", lambda command: command)
    with pytest.raises(WorkstationError, match="tar file does not exist"):
        images.WorkstationImages("fake-docker").load([tmp_path / "missing.tar"])

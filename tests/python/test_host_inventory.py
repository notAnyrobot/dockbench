import json
import subprocess

import pytest

from docker_ws.core.errors import WorkstationError
from docker_ws.core.host_inventory import HostInventory


class Docker:
    def run(self, args, **kwargs):
        if args[:2] == ["image", "ls"]: return "sha256:short\tubuntu\t24.04\nsha256:short\tubuntu\tlatest"
        if args[:2] == ["image", "inspect"]:
            return json.dumps({"Id": "sha256:full", "RepoTags": ["ubuntu:24.04", "ubuntu:latest"], "Size": 42, "Created": "now", "Architecture": "amd64", "Config": {"Labels": {}}})
        if args[:2] == ["info", "--format"]: return '{"nvidia":{}}'
        raise AssertionError(args)


def test_images_are_tagged_grouped_and_resolved(monkeypatch):
    inventory = HostInventory(Docker())
    images = inventory.images()
    assert len(images) == 1 and images[0].references == ("ubuntu:24.04", "ubuntu:latest")
    assert inventory.resolve_image("ubuntu:latest").id == "sha256:full"


def test_gpu_diagnostic_is_safe_when_nvidia_smi_is_absent(monkeypatch):
    monkeypatch.setattr("docker_ws.core.host_inventory.shutil.which", lambda value: None)
    gpus, diagnostic = HostInventory(Docker()).gpus()
    assert gpus == () and "nvidia-smi" in diagnostic


def test_gpu_uuid_index_resolution_and_duplicate_rejection(monkeypatch):
    monkeypatch.setattr("docker_ws.core.host_inventory.shutil.which", lambda value: "/usr/bin/nvidia-smi")
    def run(*args, **kwargs): return subprocess.CompletedProcess(args[0], 0, "0, GPU-a, Test, 1024\n1, GPU-b, Test 2, 2048\n", "")
    inventory = HostInventory(Docker(), run)
    assert [gpu.uuid for gpu in inventory.resolve_gpus(("0", "GPU-b"))] == ["GPU-a", "GPU-b"]
    with pytest.raises(WorkstationError, match="more than once"):
        inventory.resolve_gpus(("0", "GPU-a"))

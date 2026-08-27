import json
import subprocess

import pytest

from dockbench.core.errors import WorkstationError
from dockbench.core.host_inventory import DESKTOP_CONTRACT_LABEL, HostInventory


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


def test_dockbench_desktop_label_marks_an_image_desktop_capable():
    class DesktopDocker(Docker):
        def run(self, args, **kwargs):
            if args[:2] == ["image", "ls"]: return "sha256:short\tandroid-ws\tu22.04-cu12.8-v2"
            if args[:2] == ["image", "inspect"]:
                return json.dumps({"Id": "sha256:desktop", "RepoTags": ["android-ws:u22.04-cu12.8-v2"], "Size": 42, "Created": "now", "Architecture": "amd64", "Config": {"Labels": {DESKTOP_CONTRACT_LABEL: "v1"}}})
            return super().run(args, **kwargs)

    assert HostInventory(DesktopDocker()).images()[0].desktop_capable is True


def test_former_desktop_label_is_not_accepted():
    class FormerDesktopDocker(Docker):
        def run(self, args, **kwargs):
            if args[:2] == ["image", "ls"]:
                return "sha256:short\tandroid-ws\tu22.04-cu12.8-v2"
            if args[:2] == ["image", "inspect"]:
                return json.dumps({"Id": "sha256:desktop", "RepoTags": ["android-ws:u22.04-cu12.8-v2"], "Size": 42, "Created": "now", "Architecture": "amd64", "Config": {"Labels": {"io.docker-workstation.desktop-contract": "v1"}}})
            return super().run(args, **kwargs)

    assert HostInventory(FormerDesktopDocker()).images()[0].desktop_capable is False


def test_gpu_diagnostic_is_safe_when_nvidia_smi_is_absent(monkeypatch):
    monkeypatch.setattr("dockbench.core.host_inventory.shutil.which", lambda value: None)
    gpus, diagnostic = HostInventory(Docker()).gpus()
    assert gpus == () and "nvidia-smi" in diagnostic


def test_gpu_uuid_index_resolution_and_duplicate_rejection(monkeypatch):
    monkeypatch.setattr("dockbench.core.host_inventory.shutil.which", lambda value: "/usr/bin/nvidia-smi")
    def run(*args, **kwargs): return subprocess.CompletedProcess(args[0], 0, "0, GPU-a, Test, 1024\n1, GPU-b, Test 2, 2048\n", "")
    inventory = HostInventory(Docker(), run)
    assert [gpu.uuid for gpu in inventory.resolve_gpus(("0", "GPU-b"))] == ["GPU-a", "GPU-b"]
    with pytest.raises(WorkstationError, match="more than once"):
        inventory.resolve_gpus(("0", "GPU-a"))

from pathlib import Path

import pytest

from docker_ws.core.errors import WorkstationError, WorkstationReplaceRequired
from docker_ws.core.defaults import DEFAULT_IMAGE
from docker_ws.core.host_inventory import GPU, LocalImage
from docker_ws.core.workstation import Workstation, WorkstationConfig


class FakeInventory:
    def __init__(self, desktop=False):
        self.image = LocalImage("sha256:image", (DEFAULT_IMAGE, "test:image"), 1, "now", "amd64", "v1" if desktop else None)
        self.gpu = GPU("GPU-abc", 0, "Test GPU", 1024)
    def resolve_image(self, selection):
        if not selection: raise WorkstationError("an image is required")
        if selection not in {DEFAULT_IMAGE, "test:image"}: raise WorkstationError("image is not available locally")
        return self.image
    def resolve_gpus(self, selected, all_gpus=False):
        if all_gpus: return (self.gpu,)
        result = []
        for value in selected:
            if value not in {"0", "GPU-abc"}: raise WorkstationError("GPU is not available")
            if self.gpu in result: raise WorkstationError("GPU selected more than once")
            result.append(self.gpu)
        return tuple(result)


class FakeDocker:
    def __init__(self, config):
        self.config = config; self.state = ""; self.commands = []; self.inputs = []; self.launch_spec = ""; self.vnc = False
    def run(self, args, *, input=None, capture=False, check=True):
        self.commands.append(args); self.inputs.append(input); command = " ".join(args)
        if args[:2] == ["container", "inspect"]:
            if not self.state: raise WorkstationError("not found")
            if "docker-ws.launch-spec" in command: return self.launch_spec
            if "{{.Image}}" in args: return "sha256:image"
            return self.state
        if args[:2] == ["run", "-d"]:
            self.state = "running"
            self.launch_spec = next((value.split("=", 1)[1] for value in args if value.startswith("docker-ws.launch-spec=")), "")
        if args[:1] == ["start"]: self.state = "running"
        if args[:1] == ["stop"]: self.state = "exited"
        if args[:1] == ["rm"]: self.state = ""
        if args[:2] == ["exec", "-d"]: self.vnc = True
        if "vncserver -list" in command and not self.vnc: raise WorkstationError("no vnc")
        if "test -s /state/home/.vnc/passwd" in command: raise WorkstationError("no password")
        return ""


def config(tmp_path: Path, image=None):
    code = tmp_path / "Code"; code.mkdir()
    return WorkstationConfig(tmp_path, "docker", code, tmp_path / ".robotics-ws", "8g", 1234, 5678, "robot", image, "robot-ws", 5901, "vncviewer", "rootful", 1234, 5678)


def test_creation_defaults_to_desktop_image_and_all_gpus(tmp_path):
    fake = FakeDocker(config(tmp_path, DEFAULT_IMAGE)); ws = Workstation(fake.config, fake, FakeInventory())
    result = ws.start()
    command = next(command for command in fake.commands if command[:2] == ["run", "-d"])
    assert result.state == "running" and result.image_ref == DEFAULT_IMAGE and result.gpu_uuids == ("GPU-abc",)
    assert ["--gpus", "device=GPU-abc"] == command[command.index("--gpus"):command.index("--gpus") + 2]
    assert ["--user", "root"] == command[command.index("--user"):command.index("--user") + 2]
    assert command[-3:] == ["sha256:image", "-lc", "exec sleep infinity"]
    assert "--entrypoint" in command and any("dst=/workspace" in item for item in command)


def test_creation_can_explicitly_select_cpu_only(tmp_path):
    fake = FakeDocker(config(tmp_path, DEFAULT_IMAGE)); ws = Workstation(fake.config, fake, FakeInventory())
    result = ws.start(all_gpus=False)
    command = next(command for command in fake.commands if command[:2] == ["run", "-d"])
    assert result.gpu_uuids == ()
    assert "--gpus" not in command


def test_selected_gpu_is_persisted_by_uuid_and_replace_is_explicit(tmp_path):
    fake = FakeDocker(config(tmp_path)); ws = Workstation(fake.config, fake, FakeInventory())
    assert ws.start(image="test:image", gpus=("0",)).gpu_uuids == ("GPU-abc",)
    run = next(command for command in fake.commands if command[:2] == ["run", "-d"])
    assert ["--gpus", "device=GPU-abc"] == run[run.index("--gpus"):run.index("--gpus") + 2]
    with pytest.raises(WorkstationReplaceRequired, match="--replace"):
        ws.start(image="test:image", all_gpus=False)
    ws.start(image="test:image", all_gpus=False, replace=True)
    assert ["rm", "robot-ws"] in fake.commands


def test_duplicate_gpu_is_rejected_before_container_creation(tmp_path):
    fake = FakeDocker(config(tmp_path)); ws = Workstation(fake.config, fake, FakeInventory())
    with pytest.raises(WorkstationError, match="more than once"):
        ws.start(image="test:image", gpus=("0", "GPU-abc"))
    assert not any(command[:2] == ["run", "-d"] for command in fake.commands)


def test_legacy_container_restarts_without_new_labels(tmp_path):
    fake = FakeDocker(config(tmp_path)); fake.state = "exited"; ws = Workstation(fake.config, fake, FakeInventory())
    assert ws.start().state == "running"
    assert ["start", "robot-ws"] in fake.commands


def test_shell_images_reject_desktop_without_running_vnc(tmp_path):
    fake = FakeDocker(config(tmp_path)); ws = Workstation(fake.config, fake, FakeInventory())
    ws.start(image="test:image")
    with pytest.raises(WorkstationError, match="desktop contract"):
        ws.ensure_desktop("password")
    assert not any(command[:2] == ["exec", "-d"] for command in fake.commands)


def test_desktop_contract_exposes_desktop_endpoint(tmp_path):
    fake = FakeDocker(config(tmp_path)); ws = Workstation(fake.config, fake, FakeInventory(desktop=True))
    ws.start(image="test:image")
    endpoint = ws.ensure_desktop("password")
    assert (endpoint.host, endpoint.port) == ("127.0.0.1", 5901)
    assert any(command[:2] == ["exec", "-d"] for command in fake.commands)
    start = next(command for command in fake.commands if command[:2] == ["exec", "-d"])
    assert "start-vnc" not in start
    assert start[-1] == 'exec vncserver "${VNC_DISPLAY:-:1}" -fg -localhost no -geometry "${VNC_GEOMETRY:-1920x1080}" -depth "${VNC_DEPTH:-24}"'
    startup = next(payload for payload in fake.inputs if payload and "dbus-launch" in payload)
    assert "exec dbus-launch --exit-with-session startxfce4" in startup


def test_desktop_contract_restores_host_user_and_secure_vnc_paths(tmp_path):
    fake = FakeDocker(config(tmp_path)); ws = Workstation(fake.config, fake, FakeInventory(desktop=True))
    ws.start(image="test:image")
    provisioning = "\n".join(payload or "" for payload in fake.inputs)
    assert "NOPASSWD: ALL" in provisioning and 'ownership_marker="/state/.owner-${requested_uid}-${requested_gid}"' in provisioning
    ws.enter()
    assert any(command[:2] == ["exec", "-it"] and "1234:5678" in command and "HOME=/state/home" in command for command in fake.commands)
    ws.ensure_desktop("password")
    assert any("AcceptPointerEvents" in " ".join(command) and "AcceptKeyEvents" in " ".join(command) for command in fake.commands)
    ws.reset_vnc_password("new-pass")
    reset = next(command for command in fake.commands if "temporary_file" in " ".join(command))
    assert reset[:2] == ["exec", "-i"]

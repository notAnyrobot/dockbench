from pathlib import Path

import pytest

from docker_ws.core.workstation import (
    Workstation,
    WorkstationConfig,
    WorkstationError,
    WorkstationRebuildRequired,
)


class FakeDocker:
    def __init__(self, config: WorkstationConfig):
        self.config = config
        self.state = ""
        self.commands: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.vnc = False
        self.password_exists = False
        self.container_image = "sha256:image"
        self.current_image = "sha256:image"

    def run(self, args, *, input=None, capture=False, check=True):
        self.commands.append(args)
        self.inputs.append(input)
        command = " ".join(args)
        if args[:2] == ["container", "inspect"]:
            if not self.state: raise WorkstationError("not found")
            if "{{.Image}}" in args: return self.container_image
            if "robotics-ws.launch-config" in command: return self.config.launch_config
            return self.state
        if args[:2] == ["image", "inspect"]: return self.current_image
        if args[:2] == ["run", "-d"]: self.state = "running"
        if args[:1] == ["start"]: self.state = "running"
        if args[:1] == ["stop"]: self.state = "exited"
        if args[:1] == ["rm"]: self.state = ""
        if args[:2] == ["exec", "-d"]: self.vnc = True
        if "vncserver -list" in command and not self.vnc: raise WorkstationError("no vnc")
        if "test -s /state/home/.vnc/passwd" in command and not self.password_exists:
            raise WorkstationError("missing password")
        return ""


def config(tmp_path: Path) -> WorkstationConfig:
    code = tmp_path / "Code"; code.mkdir()
    return WorkstationConfig(tmp_path, "docker", code, tmp_path / ".robotics-ws", "8g", 1234, 5678,
        "robot", "docker-ws:test", "robot-ws", 5901, "vncviewer", "rootful", 1234, 5678)


def test_start_uses_workspace_mount_and_is_idempotent(tmp_path):
    cfg = config(tmp_path); fake = FakeDocker(cfg); workstation = Workstation(cfg, fake)
    assert workstation.start().state == "running"
    assert any("dst=/workspace" in argument for command in fake.commands for argument in command)
    assert workstation.start().state == "running"


def test_start_grants_the_desktop_user_unrestricted_sudo(tmp_path):
    cfg = config(tmp_path); fake = FakeDocker(cfg)

    Workstation(cfg, fake).start()

    provisioning = "\n".join(payload or "" for payload in fake.inputs)
    assert "NOPASSWD: ALL" in provisioning
    assert "/etc/sudoers.d/docker-ws-user" in provisioning
    assert "visudo -cf" in provisioning


def test_start_rejects_stale_container_with_rebuild_action(tmp_path):
    cfg = config(tmp_path)
    fake = FakeDocker(cfg)
    fake.state = "exited"
    fake.container_image = "sha256:old"
    fake.current_image = "sha256:new"

    with pytest.raises(WorkstationRebuildRequired, match="uv run docker-ws image rebuild"):
        Workstation(cfg, fake).start()

    assert ["start", "robot-ws"] not in fake.commands


def test_rebuild_only_replaces_container_after_build(tmp_path):
    cfg = config(tmp_path); fake = FakeDocker(cfg); fake.state = "running"; workstation = Workstation(cfg, fake)
    workstation.rebuild()
    assert ["buildx", "build", "--platform", "linux/amd64", "--file", str(tmp_path / "assets/docker/Dockerfile"), "--target", "desktop", "--load", "--tag", "docker-ws:test", str(tmp_path)] in fake.commands
    assert ["stop", "robot-ws"] in fake.commands


def test_desktop_autostarts_and_returns_loopback_endpoint(tmp_path):
    cfg = config(tmp_path); fake = FakeDocker(cfg)
    endpoint = Workstation(cfg, fake).ensure_desktop("password")
    assert (endpoint.host, endpoint.port) == ("127.0.0.1", 5901)
    assert fake.state == "running"
    assert any(command[:2] == ["exec", "-d"] for command in fake.commands)


def test_existing_dual_vnc_password_is_normalized_to_full_control(tmp_path):
    cfg = config(tmp_path); fake = FakeDocker(cfg); fake.state = "running"; fake.password_exists = True

    Workstation(cfg, fake).ensure_desktop("view-only-password")

    assert any(
        "truncate -s 8 /state/home/.vnc/passwd" in " ".join(command)
        and "vncserver -kill :1" in " ".join(command)
        for command in fake.commands
    )


def test_reset_vnc_password_replaces_credential_and_restarts_desktop(tmp_path):
    cfg = config(tmp_path); fake = FakeDocker(cfg); fake.state = "running"; fake.password_exists = True; fake.vnc = True

    result = Workstation(cfg, fake).reset_vnc_password("new-pass")

    reset = next(command for command in fake.commands if "vncpasswd -f" in " ".join(command))
    assert reset[:2] == ["exec", "-i"]
    assert result.desktop_ready is True

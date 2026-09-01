from dataclasses import replace
from pathlib import Path
import subprocess
import threading

import pytest

from dockbench.core.errors import DockerCommandError, WorkstationError, WorkstationReplaceRequired
from dockbench.core.defaults import DEFAULT_IMAGE
from dockbench.core.host_inventory import GPU, LocalImage
from dockbench.core.workstation import SubprocessDockerRunner, Workstation, WorkstationConfig


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
    def run(self, args, *, input=None, capture=False, check=True, on_output=None):
        self.commands.append(args); self.inputs.append(input); command = " ".join(args)
        if on_output is not None: on_output("build progress")
        if args[:2] == ["container", "inspect"]:
            if not self.state: raise WorkstationError("not found")
            if "io.github.notanyrobot.dockbench.launch-spec" in command: return self.launch_spec
            if "{{.Image}}" in args: return "sha256:image"
            return self.state
        if args[:2] == ["run", "-d"]:
            self.state = "running"
            self.launch_spec = next((value.split("=", 1)[1] for value in args if value.startswith("io.github.notanyrobot.dockbench.launch-spec=")), "")
        if args[:1] == ["start"]: self.state = "running"
        if args[:1] == ["stop"]: self.state = "exited"
        if args[:1] == ["rm"]: self.state = ""
        if args[:2] == ["exec", "-d"]: self.vnc = True
        if "vncserver -list" in command and not self.vnc: raise WorkstationError("no vnc")
        if "test -s /state/home/.vnc/passwd" in command: raise WorkstationError("no password")
        return ""


def config(tmp_path: Path, image=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return WorkstationConfig(tmp_path, "docker", workspace, tmp_path / ".dockbench", "8g", 1234, 5678, "robot", image, "dockbench", 5901, "vncviewer", "rootful", 1234, 5678)


def test_config_uses_explicit_workspace_root(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("DOCKBENCH_WORKSPACE", str(workspace))
    monkeypatch.setenv("DOCKBENCH_STATE_ROOT", str(tmp_path / ".dockbench"))
    monkeypatch.setattr("dockbench.core.workstation.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr("dockbench.core.workstation.SubprocessDockerRunner.run", lambda *args, **kwargs: "[]")

    result = WorkstationConfig.from_environment(tmp_path)

    assert result.workspace_root == workspace
    assert result.state_root == tmp_path / ".dockbench"
    assert result.container_name == "dockbench"


def test_config_uses_default_workspace_root(tmp_path, monkeypatch):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.delenv("DOCKBENCH_WORKSPACE", raising=False)
    monkeypatch.setattr("dockbench.core.workstation.default_workspace_root", lambda: fallback)
    monkeypatch.setattr("dockbench.core.workstation.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr("dockbench.core.workstation.SubprocessDockerRunner.run", lambda *args, **kwargs: "[]")

    result = WorkstationConfig.from_environment(tmp_path)

    assert result.workspace_root == fallback


def test_docker_runner_retains_daemon_stderr_for_sanitized_workbench_errors(monkeypatch):
    calls = []

    def failed_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 1, stderr="Error response from daemon: insufficient memory")

    monkeypatch.setattr("dockbench.core.workstation.subprocess.run", failed_run)

    with pytest.raises(DockerCommandError, match="insufficient memory"):
        SubprocessDockerRunner("docker").run(["run", "example:image"])

    assert calls[0][1]["stderr"] is subprocess.PIPE


def test_docker_runner_streams_combined_output_and_retains_failure_tail():
    progress = []
    errors = []
    received = threading.Event()
    release = threading.Event()

    def report(line):
        progress.append(line)
        if line == "step one":
            received.set()
            release.wait(timeout=2)

    def run():
        try:
            SubprocessDockerRunner("bash").run(
                ["-c", "printf 'step one\\n' >&2; printf 'fatal build failure\\n' >&2; exit 7"],
                on_output=report,
            )
        except DockerCommandError as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert received.wait(timeout=1), "first progress line was not streamed"
    assert thread.is_alive(), "runner waited for process exit before reporting progress"
    release.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert progress == ["step one", "fatal build failure"]
    assert len(errors) == 1 and "fatal build failure" in str(errors[0])


def test_creation_defaults_to_desktop_image_and_all_gpus(tmp_path):
    fake = FakeDocker(config(tmp_path, DEFAULT_IMAGE)); ws = Workstation(fake.config, fake, FakeInventory())
    result = ws.start()
    command = next(command for command in fake.commands if command[:2] == ["run", "-d"])
    assert result.state == "running" and result.image_ref == DEFAULT_IMAGE and result.gpu_uuids == ("GPU-abc",)
    assert ["--gpus", "device=GPU-abc"] == command[command.index("--gpus"):command.index("--gpus") + 2]
    assert ["--user", "root"] == command[command.index("--user"):command.index("--user") + 2]
    assert ["--env", "UV_CACHE_DIR=/state/cache/uv", "--env", "TMPDIR=/state/tmp", "--env", "UV_PYTHON_INSTALL_DIR=/workspace/.local/share/uv/python"] == command[command.index("--env"):command.index("--env") + 6]
    assert command[-3:] == ["sha256:image", "-lc", "exec sleep infinity"]
    assert "--entrypoint" in command
    assert any(f"src={fake.config.workspace_root},dst=/workspace" in item for item in command)
    assert (fake.config.state_root / "cache/uv").is_dir()
    assert (fake.config.state_root / "tmp").is_dir()
    assert (fake.config.workspace_root / ".local/share/uv/python").is_dir()


def test_creation_mounts_discovered_motion_data_at_data_motions(tmp_path):
    configuration = config(tmp_path, DEFAULT_IMAGE)
    motion_root = tmp_path / "motion_datasets"
    motion_root.mkdir()
    configuration = replace(configuration, data_mounts=((motion_root, "/data/motions"),))
    fake = FakeDocker(configuration)

    Workstation(configuration, fake, FakeInventory()).start()

    command = next(command for command in fake.commands if command[:2] == ["run", "-d"])
    assert f"type=bind,src={motion_root},dst=/data/motions" in command


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
    assert ["rm", "dockbench"] in fake.commands


def test_multiple_selected_gpus_are_quoted_for_docker_csv_parsing(tmp_path):
    fake = FakeDocker(config(tmp_path))
    inventory = FakeInventory()
    selected = (GPU("GPU-first", 0, "First GPU", 1024), GPU("GPU-second", 1, "Second GPU", 1024))
    inventory.resolve_gpus = lambda values, all_gpus=False: selected
    ws = Workstation(fake.config, fake, inventory)

    result = ws.start(image="test:image", gpus=("GPU-first", "GPU-second"))

    run = next(command for command in fake.commands if command[:2] == ["run", "-d"])
    assert result.gpu_uuids == ("GPU-first", "GPU-second")
    assert ["--gpus", '"device=GPU-first,GPU-second"'] == run[run.index("--gpus"):run.index("--gpus") + 2]


def test_duplicate_gpu_is_rejected_before_container_creation(tmp_path):
    fake = FakeDocker(config(tmp_path)); ws = Workstation(fake.config, fake, FakeInventory())
    with pytest.raises(WorkstationError, match="more than once"):
        ws.start(image="test:image", gpus=("0", "GPU-abc"))
    assert not any(command[:2] == ["run", "-d"] for command in fake.commands)


def test_existing_default_container_restarts_without_replacement(tmp_path):
    fake = FakeDocker(config(tmp_path)); fake.state = "exited"; ws = Workstation(fake.config, fake, FakeInventory())
    assert ws.start().state == "running"
    assert ["start", "dockbench"] in fake.commands


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

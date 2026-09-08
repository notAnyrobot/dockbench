#!/usr/bin/env python3
"""Lifecycle management for one managed Docker workstation instance."""
from __future__ import annotations

import contextlib
from collections import deque
import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol

from dockbench.core.access import ContainerAccess, DesktopEndpoint
from dockbench.core.resources import CheckoutResources
from dockbench.core.defaults import DEFAULT_IMAGE, default_data_mounts, default_state_root, default_workspace_root, workspace_root_from_value
from dockbench.core.image_builder import ImageBuilder, ImageBuildResult
from dockbench.core.recipes import RecipeCatalog
from dockbench.core.errors import DockerCommandError, WorkstationContainerExists, WorkstationError, WorkstationGPUConflict, WorkstationRebuildRequired, WorkstationReplaceRequired
from dockbench.core.host_inventory import HostInventory


class DockerRunner(Protocol):
    def run(self, args: list[str], *, input: str | None = None, capture: bool = False,
            check: bool = True, on_output: Callable[[str], None] | None = None) -> str: ...


class SubprocessDockerRunner:
    """Docker adapter that never composes a shell command."""
    def __init__(self, command: str, *, environment: Mapping[str, str] | None = None) -> None:
        self.command = command
        self.environment = dict(environment) if environment is not None else None
    def run(self, args: list[str], *, input: str | None = None, capture: bool = False,
            check: bool = True, on_output: Callable[[str], None] | None = None) -> str:
        if on_output is not None:
            if input is not None or capture:
                raise WorkstationError("streamed Docker output cannot be combined with input or captured output")
            return self._run_streamed(args, check=check, on_output=on_output)
        try:
            result = subprocess.run([self.command, *args], input=input, text=True, env=self.environment, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE, check=False)
        except FileNotFoundError as exc: raise WorkstationError(f"Docker command not found: {self.command}") from exc
        if check and result.returncode:
            raise DockerCommandError((result.stderr or "").strip() or f"Docker command failed ({result.returncode})")
        return result.stdout.strip() if capture else ""

    def _run_streamed(self, args: list[str], *, check: bool,
                      on_output: Callable[[str], None]) -> str:
        try:
            process = subprocess.Popen(
                [self.command, *args], text=True, env=self.environment, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=1,
            )
        except FileNotFoundError as exc:
            raise WorkstationError(f"Docker command not found: {self.command}") from exc
        tail: deque[str] = deque(maxlen=20)
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                tail.append(line[-2000:])
                on_output(line)
            returncode = process.wait()
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            raise
        if check and returncode:
            detail = "\n".join(tail).strip() or f"Docker command failed ({returncode})"
            raise DockerCommandError(detail)
        return ""


@dataclass(frozen=True)
class WorkstationConfig:
    repository_root: Path; docker_command: str; workspace_root: Path; state_root: Path; shm_size: str
    host_uid: int; host_gid: int; host_user: str; image: str | None; container_name: str
    vnc_port: int; vncviewer_command: str; docker_mode: str; container_uid: int; container_gid: int
    dynamic_vnc_port: bool = False
    data_mounts: tuple[tuple[Path, str], ...] = ()
    @property
    def launch_config(self) -> str: return json.dumps({"workspace_root": str(self.workspace_root), "data_mounts": [{"source": str(source), "destination": destination} for source, destination in self.data_mounts], "state_root": str(self.state_root), "shm_size": self.shm_size, "host_uid": self.host_uid, "host_gid": self.host_gid, "docker_mode": self.docker_mode, "vnc_port": self.vnc_port}, separators=(",", ":"), sort_keys=True)
    @classmethod
    def from_environment(cls, repository_root: Path | None = None, *, environment: Mapping[str, str] | None = None, runner: DockerRunner | None = None) -> "WorkstationConfig":
        env = os.environ if environment is None else environment; root = CheckoutResources.discover(repository_root).repository_root
        workspace_value = env.get("DOCKBENCH_WORKSPACE")
        workspace_root = workspace_root_from_value(workspace_value) if workspace_value else default_workspace_root()
        if workspace_root is None:
            raise WorkstationError("workspace root not found; create ~/workspace (or /data/$USER/workspace on remote hosts), or set DOCKBENCH_WORKSPACE")
        state_root = Path(env.get("DOCKBENCH_STATE_ROOT", str(default_state_root()))).expanduser(); port = env.get("DOCKBENCH_VNC_PORT", "5901")
        if not re.fullmatch(r"[1-9][0-9]*", port): raise WorkstationError(f"VNC port must be a positive integer: {port}")
        docker = env.get("DOCKBENCH_DOCKER", "docker")
        if runner is None and shutil.which(docker) is None: raise WorkstationError(f"Docker command not found: {docker}")
        security = (runner if runner is not None else SubprocessDockerRunner(docker)).run(["info", "--format", "{{json .SecurityOptions}}"], capture=True); rootless = "rootless" in security
        uid = int(env.get("DOCKBENCH_HOST_UID", str(os.getuid()))); gid = int(env.get("DOCKBENCH_HOST_GID", str(os.getgid())))
        return cls(root, docker, workspace_root, state_root, env.get("DOCKBENCH_SHM_SIZE", "32g"), uid, gid, env.get("DOCKBENCH_HOST_USER", "user"), env.get("DOCKBENCH_IMAGE", DEFAULT_IMAGE), env.get("DOCKBENCH_CONTAINER", "dockbench"), int(port), env.get("DOCKBENCH_VNC_VIEWER", "vncviewer"), "rootless" if rootless else "rootful", 0 if rootless else uid, 0 if rootless else gid, data_mounts=default_data_mounts())


@dataclass(frozen=True)
class LaunchSpecification:
    """Immutable creation request, persisted in a container label."""
    image_id: str; image_ref: str; gpu_uuids: tuple[str, ...]; desktop_contract: str | None
    @property
    def desktop_capable(self) -> bool: return self.desktop_contract == "v1"
    def label_value(self) -> str: return json.dumps({"image_id": self.image_id, "image_ref": self.image_ref, "gpu_uuids": list(self.gpu_uuids), "desktop_contract": self.desktop_contract}, separators=(",", ":"), sort_keys=True)
    @classmethod
    def from_label(cls, value: str) -> "LaunchSpecification | None":
        try:
            data = json.loads(value); return cls(data["image_id"], data["image_ref"], tuple(data.get("gpu_uuids", ())), data.get("desktop_contract"))
        except (TypeError, ValueError, KeyError): return None


@dataclass(frozen=True)
class WorkstationStatus:
    state: str; desktop_ready: bool; image: str; container_name: str; workspace: str
    image_id: str | None = None; image_ref: str | None = None; gpu_uuids: tuple[str, ...] = (); desktop_capable: bool = False; message: str | None = None
    vnc_port: int | None = None; stale: bool = False
    def public(self) -> dict[str, object]:
        result = asdict(self); result["gpu_uuids"] = list(self.gpu_uuids); return result




class Workstation:
    """Deep lifecycle API; HostInventory owns image/GPU discovery and resolution."""
    def __init__(self, config: WorkstationConfig | None = None, runner: DockerRunner | None = None, inventory: HostInventory | None = None, *,
                 recipes: RecipeCatalog | None = None, image_builder: ImageBuilder | None = None) -> None:
        self.config = config or WorkstationConfig.from_environment(); self.docker = runner or SubprocessDockerRunner(self.config.docker_command); self.inventory = inventory or HostInventory(self.docker)
        self.recipes = recipes if recipes is not None else RecipeCatalog.for_repository(self.config.repository_root)
        self.image_builder = image_builder if image_builder is not None else ImageBuilder(self.docker)
    @contextlib.contextmanager
    def locked(self) -> Iterable[None]:
        self.config.state_root.mkdir(parents=True, exist_ok=True)
        with (self.config.state_root / ".workstation.lock").open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    def _container_status(self) -> str:
        try: return self.docker.run(["container", "inspect", "--format", "{{.State.Status}}", self.config.container_name], capture=True)
        except WorkstationError: return ""
    def _launch_spec(self) -> LaunchSpecification | None:
        if not self._container_status(): return None
        try: return LaunchSpecification.from_label(self.docker.run(["container", "inspect", "--format", '{{index .Config.Labels "io.github.notanyrobot.dockbench.launch-spec"}}', self.config.container_name], capture=True))
        except WorkstationError: return None
    def _container_image_id(self) -> str | None:
        try: return self.docker.run(["container", "inspect", "--format", "{{.Image}}", self.config.container_name], capture=True)
        except WorkstationError: return None
    def status(self) -> WorkstationStatus:
        raw = self._container_status(); state = "absent" if not raw else "running" if raw == "running" else "stopped" if raw in {"created", "exited"} else "unavailable"; spec = self._launch_spec()
        image_id = spec.image_id if spec else (self._container_image_id() if raw else None); image_ref = spec.image_ref if spec else self.config.image; desktop = spec.desktop_capable if spec else False; gpu_uuids = spec.gpu_uuids if spec else ()
        return WorkstationStatus(state, self.access.desktop_ready() if state == "running" and desktop else False, image_ref or image_id or "", self.config.container_name, "/workspace", image_id, image_ref, gpu_uuids, desktop, None if state != "unavailable" else f"Docker state: {raw}")
    def _specification(self, image: str | None, gpus: tuple[str, ...], all_gpus: bool) -> LaunchSpecification:
        selected = self.inventory.resolve_image(image or self.config.image or ""); selected_gpus = self.inventory.resolve_gpus(gpus, all_gpus)
        return LaunchSpecification(selected.id, selected.display_reference, tuple(gpu.uuid for gpu in selected_gpus), selected.desktop_contract)
    def _preflight(self, spec: LaunchSpecification) -> None:
        self.docker.run(["run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", spec.image_id, "-lc", "command -v sleep >/dev/null && sleep 0"])
    def _create(self, spec: LaunchSpecification) -> None:
        c = self.config; c.state_root.mkdir(parents=True, exist_ok=True)
        (c.state_root / "tmp").mkdir(parents=True, exist_ok=True)
        (c.workspace_root / ".cache/uv").mkdir(parents=True, exist_ok=True)
        (c.workspace_root / ".local/share/uv/python").mkdir(parents=True, exist_ok=True)
        # Never force a platform for an arbitrary local image: Docker has
        # already resolved the image's locally available architecture.
        args = ["run", "-d", "--name", c.container_name, "--hostname", c.container_name, "--user", "root", "--shm-size", c.shm_size, "--restart", "unless-stopped", "--env", "UV_CACHE_DIR=/workspace/.cache/uv", "--env", "TMPDIR=/state/tmp", "--env", "UV_PYTHON_INSTALL_DIR=/workspace/.local/share/uv/python", "--label", "io.github.notanyrobot.dockbench.managed=true", "--label", f"io.github.notanyrobot.dockbench.state-root={c.state_root}", "--label", f"io.github.notanyrobot.dockbench.launch-spec={spec.label_value()}", "--label", f"io.github.notanyrobot.dockbench.image-id={spec.image_id}", "--label", f"io.github.notanyrobot.dockbench.image-ref={spec.image_ref}", "--label", f"io.github.notanyrobot.dockbench.gpus={','.join(spec.gpu_uuids)}", "--label", f"io.github.notanyrobot.dockbench.launch-config={c.launch_config}"]
        args.extend(["--mount", f"type=bind,src={c.workspace_root},dst=/workspace"])
        for source, destination in c.data_mounts:
            args.extend(["--mount", f"type=bind,src={source},dst={destination}"])
        args.extend(["--mount", f"type=bind,src={c.state_root},dst=/state"])
        if spec.gpu_uuids:
            device_request = f"device={','.join(spec.gpu_uuids)}"
            # Docker parses --gpus as CSV. Preserve literal double quotes when
            # commas separate multiple device IDs; subprocess does not add the
            # shell quoting shown in Docker's CLI examples for us.
            if len(spec.gpu_uuids) > 1:
                device_request = f'"{device_request}"'
            args += ["--gpus", device_request]
        if spec.desktop_capable:
            # Fleet containers use Docker's ephemeral host ports.  This makes
            # several desktops possible without exposing VNC beyond loopback.
            args += ["-p", "127.0.0.1::5901" if c.dynamic_vnc_port else f"127.0.0.1:{c.vnc_port}:5901"]
        self.docker.run(args + ["--entrypoint", "/bin/sh", spec.image_id, "-lc", "exec sleep infinity"])
    def _replace(self) -> None:
        raw = self._container_status()
        if raw == "running": self.docker.run(["stop", self.config.container_name])
        if raw in {"running", "created", "exited"}: self.docker.run(["rm", self.config.container_name])
        elif raw: raise WorkstationError(f"unsupported container state for replacement: {raw}")
    def start(self, image: str | None = None, gpus: tuple[str, ...] = (), all_gpus: bool | None = None, replace: bool = False) -> WorkstationStatus:
        with self.locked():
            raw = self._container_status(); existing = self._launch_spec(); requested = image is not None or bool(gpus) or all_gpus is not None
            if raw and not requested and not replace:
                if raw in {"created", "exited"}: self.docker.run(["start", self.config.container_name])
                elif raw != "running": raise WorkstationError(f"unsupported container state for --start: {raw}")
                if self.status().desktop_capable: self.access.prepare_user()
                return self.status()
            spec = self._specification(image, gpus, (not gpus) if all_gpus is None else all_gpus)
            if raw:
                if existing == spec and not replace:
                    if raw in {"created", "exited"}: self.docker.run(["start", self.config.container_name])
                    elif raw != "running": raise WorkstationError(f"unsupported container state for --start: {raw}")
                    if spec.desktop_capable: self.access.prepare_user()
                    return self.status()
                if not replace: raise WorkstationReplaceRequired("The requested image or GPU selection differs from the managed workstation. Re-run with --replace; the old container filesystem will be discarded while /workspace and /state are preserved.")
                self._replace()
            self._preflight(spec); self._create(spec)
            if spec.desktop_capable: self.access.prepare_user()
            return self.status()
    def build(self, on_progress: Callable[[str], None] | None = None) -> ImageBuildResult:
        # Rebuild has historically selected these values independently of recipe
        # defaults. Keep that contract explicit while using the validated context.
        return self.image_builder.build(
            self.recipes.get("android-ws"), tag=self.config.image or DEFAULT_IMAGE,
            target="desktop", platform="linux/amd64", on_progress=on_progress,
        )

    def rebuild(self, on_progress: Callable[[str], None] | None = None, *,
                on_built: Callable[[ImageBuildResult], None] | None = None) -> WorkstationStatus:
        built = self.build(on_progress)
        if on_built is not None:
            on_built(built)
        return self.start(image=built.tag, replace=True)

    def stop(self) -> WorkstationStatus:
        with self.locked():
            raw = self._container_status()
            if not raw: raise WorkstationError(f"{self.config.container_name} does not exist; use `dockbench start` first")
            if raw == "running": self.docker.run(["stop", self.config.container_name])
            elif raw not in {"created", "exited"}: raise WorkstationError(f"unsupported container state for --stop: {raw}")
            return self.status()
    @property
    def access(self) -> ContainerAccess:
        return ContainerAccess(self)

    def ensure_desktop(self, password: str | None = None) -> DesktopEndpoint:
        return self.access.ensure_desktop(password)

    def reset_vnc_password(self, password: str) -> WorkstationStatus:
        return self.access.reset_vnc_password(password)

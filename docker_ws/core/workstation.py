#!/usr/bin/env python3
"""Lifecycle management for one managed Docker workstation instance."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Protocol

from docker_ws.core.errors import WorkstationError, WorkstationRebuildRequired, WorkstationReplaceRequired
from docker_ws.core.host_inventory import HostInventory


class DockerRunner(Protocol):
    def run(self, args: list[str], *, input: str | None = None, capture: bool = False, check: bool = True) -> str: ...


class SubprocessDockerRunner:
    """Docker adapter that never composes a shell command."""
    def __init__(self, command: str) -> None: self.command = command
    def run(self, args: list[str], *, input: str | None = None, capture: bool = False, check: bool = True) -> str:
        try:
            result = subprocess.run([self.command, *args], input=input, text=True, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None, check=False)
        except FileNotFoundError as exc: raise WorkstationError(f"Docker command not found: {self.command}") from exc
        if check and result.returncode: raise WorkstationError((result.stderr or "").strip() if capture else f"Docker command failed ({result.returncode})")
        return result.stdout.strip() if capture else ""


@dataclass(frozen=True)
class WorkstationConfig:
    repository_root: Path; docker_command: str; code_root: Path; state_root: Path; shm_size: str
    host_uid: int; host_gid: int; host_user: str; image: str | None; container_name: str
    vnc_port: int; vncviewer_command: str; docker_mode: str; container_uid: int; container_gid: int
    @property
    def launch_config(self) -> str: return "|".join((str(self.code_root), str(self.state_root), self.shm_size, str(self.host_uid), str(self.host_gid), self.docker_mode, str(self.vnc_port)))
    @classmethod
    def from_environment(cls, repository_root: Path | None = None) -> "WorkstationConfig":
        env = os.environ; root = repository_root or Path(__file__).resolve().parents[2]
        code_root = Path(env.get("ROBOTICS_WS_CODE_ROOT", str(Path.home() / "Code"))).expanduser(); state_root = Path(env.get("ROBOTICS_WS_STATE_ROOT", str(code_root.parent / ".robotics-ws"))).expanduser(); port = env.get("ROBOTICS_WS_VNC_PORT", "5901")
        if not re.fullmatch(r"[1-9][0-9]*", port): raise WorkstationError(f"VNC port must be a positive integer: {port}")
        docker = env.get("ROBOTICS_WS_DOCKER", "docker")
        if shutil.which(docker) is None: raise WorkstationError(f"Docker command not found: {docker}")
        if not code_root.is_dir(): raise WorkstationError(f"code root does not exist: {code_root}")
        security = SubprocessDockerRunner(docker).run(["info", "--format", "{{json .SecurityOptions}}"], capture=True); rootless = "rootless" in security
        uid = int(env.get("ROBOTICS_WS_HOST_UID", str(os.getuid()))); gid = int(env.get("ROBOTICS_WS_HOST_GID", str(os.getgid())))
        return cls(root, docker, code_root, state_root, env.get("ROBOTICS_WS_SHM_SIZE", "32g"), uid, gid, env.get("ROBOTICS_WS_HOST_USER", "user"), env.get("ROBOTICS_WS_DESKTOP_IMAGE"), env.get("ROBOTICS_WS_DESKTOP_NAME", "docker-ws"), int(port), env.get("ROBOTICS_WS_VNCVIEWER", "vncviewer"), "rootless" if rootless else "rootful", 0 if rootless else uid, 0 if rootless else gid)


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
    def public(self) -> dict[str, object]:
        result = asdict(self); result["gpu_uuids"] = list(self.gpu_uuids); return result


@dataclass(frozen=True)
class DesktopEndpoint: host: str; port: int


class Workstation:
    """Deep lifecycle API; HostInventory owns image/GPU discovery and resolution."""
    def __init__(self, config: WorkstationConfig | None = None, runner: DockerRunner | None = None, inventory: HostInventory | None = None) -> None:
        self.config = config or WorkstationConfig.from_environment(); self.docker = runner or SubprocessDockerRunner(self.config.docker_command); self.inventory = inventory or HostInventory(self.docker)
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
        try: return LaunchSpecification.from_label(self.docker.run(["container", "inspect", "--format", '{{index .Config.Labels "docker-ws.launch-spec"}}', self.config.container_name], capture=True))
        except WorkstationError: return None
    def _container_image_id(self) -> str | None:
        try: return self.docker.run(["container", "inspect", "--format", "{{.Image}}", self.config.container_name], capture=True)
        except WorkstationError: return None
    def status(self) -> WorkstationStatus:
        raw = self._container_status(); state = "absent" if not raw else "running" if raw == "running" else "stopped" if raw in {"created", "exited"} else "unavailable"; spec = self._launch_spec()
        image_id = spec.image_id if spec else (self._container_image_id() if raw else None); image_ref = spec.image_ref if spec else self.config.image; desktop = spec.desktop_capable if spec else bool(self.config.image)
        return WorkstationStatus(state, self._vnc_running() if state == "running" and desktop else False, image_ref or image_id or "", self.config.container_name, "/workspace", image_id, image_ref, spec.gpu_uuids if spec else (), desktop, None if state != "unavailable" else f"Docker state: {raw}")
    def _specification(self, image: str | None, gpus: tuple[str, ...], all_gpus: bool) -> LaunchSpecification:
        selected = self.inventory.resolve_image(image or self.config.image or ""); selected_gpus = self.inventory.resolve_gpus(gpus, all_gpus)
        return LaunchSpecification(selected.id, selected.display_reference, tuple(gpu.uuid for gpu in selected_gpus), selected.desktop_contract)
    def _preflight(self, spec: LaunchSpecification) -> None:
        self.docker.run(["run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", spec.image_id, "-lc", "command -v sleep >/dev/null && sleep 0"])
    def _create(self, spec: LaunchSpecification) -> None:
        c = self.config; c.state_root.mkdir(parents=True, exist_ok=True)
        args = ["run", "-d", "--name", c.container_name, "--hostname", c.container_name, "--platform", "linux/amd64", "--shm-size", c.shm_size, "--restart", "unless-stopped", "--label", f"docker-ws.launch-spec={spec.label_value()}", "--label", f"docker-ws.image-id={spec.image_id}", "--label", f"docker-ws.image-ref={spec.image_ref}", "--label", f"docker-ws.gpus={','.join(spec.gpu_uuids)}", "--label", f"robotics-ws.launch-config={c.launch_config}", "--mount", f"type=bind,src={c.code_root},dst=/workspace", "--mount", f"type=bind,src={c.state_root},dst=/state"]
        if spec.gpu_uuids: args += ["--gpus", f"device={','.join(spec.gpu_uuids)}"]
        if spec.desktop_capable: args += ["-p", f"127.0.0.1:{c.vnc_port}:5901"]
        self.docker.run(args + ["--entrypoint", "/bin/sh", spec.image_id, "-lc", "exec sleep infinity"])
    def _replace(self) -> None:
        raw = self._container_status()
        if raw == "running": self.docker.run(["stop", self.config.container_name])
        if raw in {"running", "created", "exited"}: self.docker.run(["rm", self.config.container_name])
        elif raw: raise WorkstationError(f"unsupported container state for replacement: {raw}")
    def start(self, image: str | None = None, gpus: tuple[str, ...] = (), all_gpus: bool = False, replace: bool = False) -> WorkstationStatus:
        with self.locked():
            raw = self._container_status(); existing = self._launch_spec(); requested = image is not None or bool(gpus) or all_gpus
            if raw and not requested and not replace:
                if raw in {"created", "exited"}: self.docker.run(["start", self.config.container_name])
                elif raw != "running": raise WorkstationError(f"unsupported container state for --start: {raw}")
                return self.status()
            spec = self._specification(image, gpus, all_gpus)
            if raw:
                if existing == spec and not replace:
                    if raw in {"created", "exited"}: self.docker.run(["start", self.config.container_name])
                    elif raw != "running": raise WorkstationError(f"unsupported container state for --start: {raw}")
                    return self.status()
                if not replace: raise WorkstationReplaceRequired("The requested image or GPU selection differs from the managed workstation. Re-run with --replace; the old container filesystem will be discarded while /workspace and /state are preserved.")
                self._replace()
            self._preflight(spec); self._create(spec); return self.status()
    def build(self) -> None:
        image = self.config.image or "docker-ws:u22.04-cu12.8.1-v1-desktop"; self.docker.run(["buildx", "build", "--platform", "linux/amd64", "--file", str(self.config.repository_root / "assets/docker/Dockerfile"), "--target", "desktop", "--load", "--tag", image, str(self.config.repository_root)]); print(f"{image}: image built")
    def rebuild(self) -> WorkstationStatus:
        self.build(); return self.start(image=self.config.image or "docker-ws:u22.04-cu12.8.1-v1-desktop", replace=True)
    def enter(self) -> None:
        if self._container_status() != "running": raise WorkstationError(f"{self.config.container_name} is not running; use --start first")
        self.docker.run(["exec", "-it", "--user", "root", "--workdir", "/workspace", self.config.container_name, "/bin/sh", "-lc", "if command -v bash >/dev/null 2>&1; then exec bash -l; else exec /bin/sh; fi"])
    def stop(self) -> WorkstationStatus:
        with self.locked():
            raw = self._container_status()
            if not raw: raise WorkstationError(f"{self.config.container_name} does not exist; use --start first")
            if raw == "running": self.docker.run(["stop", self.config.container_name])
            elif raw not in {"created", "exited"}: raise WorkstationError(f"unsupported container state for --stop: {raw}")
            return self.status()
    def _require_desktop(self) -> None:
        if not self.status().desktop_capable: raise WorkstationError("The selected image does not advertise Docker Workstation desktop contract v1; shell access remains available with docker-ws enter.")
    def _password_exists(self) -> bool:
        try: self.docker.run(["exec", self.config.container_name, "/bin/bash", "-c", "test -s /state/home/.vnc/passwd"]); return True
        except WorkstationError: return False
    def _ensure_vnc_password(self, password: str | None = None, prompt: bool = False) -> None:
        if self._password_exists(): return
        password = password or os.environ.get("ROBOTICS_WS_VNC_PASSWORD")
        if password: self.docker.run(["exec", "-i", self.config.container_name, "/bin/bash", "-c", "mkdir -p /state/home/.vnc && vncpasswd -f > /state/home/.vnc/passwd && chmod 600 /state/home/.vnc/passwd"], input=password + "\n")
        elif prompt: self.docker.run(["exec", "-it", self.config.container_name, "vncpasswd", "/state/home/.vnc/passwd"])
        else: raise WorkstationError("VNC password must be provided before opening the desktop")
    def _vnc_running(self) -> bool:
        try: self.docker.run(["exec", self.config.container_name, "/bin/bash", "-c", "vncserver -list | grep -q 5901"]); return True
        except WorkstationError: return False
    def _start_vnc(self) -> None:
        if not self._vnc_running(): self.docker.run(["exec", "-d", self.config.container_name, "start-vnc"])
    def _wait_for_vnc(self) -> None:
        for _ in range(100):
            if self._vnc_running(): return
            time.sleep(.1)
        raise WorkstationError("VNC server did not become ready within 10 seconds")
    def ensure_desktop(self, password: str | None = None) -> DesktopEndpoint:
        self._require_desktop()
        if self._container_status() != "running": self.start()
        with self.locked():
            self._ensure_vnc_password(password); self._start_vnc(); self._wait_for_vnc(); return DesktopEndpoint("127.0.0.1", self.config.vnc_port)
    def reset_vnc_password(self, password: str) -> WorkstationStatus:
        if not 6 <= len(password) <= 8: raise WorkstationError("VNC passwords must contain 6 to 8 characters")
        self._require_desktop()
        if self._container_status() != "running": self.start()
        with self.locked():
            self.docker.run(["exec", "-i", self.config.container_name, "/bin/bash", "-c", "mkdir -p /state/home/.vnc && umask 077 && vncpasswd -f > /state/home/.vnc/passwd && HOME=/state/home vncserver -kill :1 >/dev/null 2>&1 || true"], input=password + "\n"); self._start_vnc(); self._wait_for_vnc(); return self.status()
    def open_vnc(self) -> None:
        if shutil.which(self.config.vncviewer_command) is None: raise WorkstationError(f"VNC viewer not found: {self.config.vncviewer_command}")
        self._require_desktop()
        if self._container_status() != "running": raise WorkstationError(f"{self.config.container_name} is not running; use --start first")
        with self.locked(): self._ensure_vnc_password(prompt=True); self._start_vnc(); self._wait_for_vnc()
        subprocess.run([self.config.vncviewer_command, f"127.0.0.1:{self.config.vnc_port}"], check=False)


def run_cli(action: str) -> int:
    try:
        ws = Workstation(); result = getattr(ws, {"vnc": "open_vnc"}.get(action, action))()
        if action == "status": print(f"{ws.config.container_name}: {result.state}")
        return 0
    except WorkstationError as exc: print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(run_cli(sys.argv[1]) if len(sys.argv) == 2 else 1)

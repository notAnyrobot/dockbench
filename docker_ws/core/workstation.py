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
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable, Protocol

from docker_ws.core.defaults import DEFAULT_IMAGE
from docker_ws.core.errors import WorkstationError, WorkstationGPUConflict, WorkstationRebuildRequired, WorkstationReplaceRequired
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
    dynamic_vnc_port: bool = False
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
        return cls(root, docker, code_root, state_root, env.get("ROBOTICS_WS_SHM_SIZE", "32g"), uid, gid, env.get("ROBOTICS_WS_HOST_USER", "user"), env.get("ROBOTICS_WS_DESKTOP_IMAGE", DEFAULT_IMAGE), env.get("ROBOTICS_WS_DESKTOP_NAME", "docker-ws"), int(port), env.get("ROBOTICS_WS_VNCVIEWER", "vncviewer"), "rootless" if rootless else "rootful", 0 if rootless else uid, 0 if rootless else gid)


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
    def _legacy_image(self):
        """Resolve the actual image behind a pre-launch-spec container."""
        if not self._container_status(): return None
        try:
            image_id = self._container_image_id()
            return next((image for image in getattr(self.inventory, "images")() if image.id == image_id), None)
        except (AttributeError, WorkstationError):
            return None
    def _legacy_gpu_uuids(self) -> tuple[str, ...]:
        """Recover GPU allocation for pre-launch-spec containers from Docker's device request."""
        try:
            raw = self.docker.run(
                ["container", "inspect", "--format", "{{json .HostConfig.DeviceRequests}}", self.config.container_name],
                capture=True,
            )
            requests = json.loads(raw) if raw else []
        except (WorkstationError, json.JSONDecodeError, TypeError):
            return ()
        if not isinstance(requests, list):
            return ()
        selected: list[str] = []
        for request in requests:
            if not isinstance(request, dict):
                continue
            capabilities = request.get("Capabilities") or []
            if "gpu" not in {str(capability).lower() for group in capabilities if isinstance(group, list) for capability in group}:
                continue
            device_ids = request.get("DeviceIDs") or []
            if device_ids:
                try:
                    selected.extend(gpu.uuid for gpu in self.inventory.resolve_gpus(tuple(map(str, device_ids)), False))
                except (AttributeError, WorkstationError):
                    continue
            elif request.get("Count") == -1:
                try:
                    host = self.inventory.inventory().public()
                    selected.extend(str(gpu["uuid"]) for gpu in host.get("gpus", []) if gpu.get("uuid"))
                except (AttributeError, WorkstationError, TypeError):
                    continue
        return tuple(dict.fromkeys(selected))
    def status(self) -> WorkstationStatus:
        raw = self._container_status(); state = "absent" if not raw else "running" if raw == "running" else "stopped" if raw in {"created", "exited"} else "unavailable"; spec = self._launch_spec()
        legacy_image = self._legacy_image() if raw and spec is None else None
        image_id = spec.image_id if spec else (self._container_image_id() if raw else None); image_ref = spec.image_ref if spec else (legacy_image.display_reference if legacy_image else self.config.image); desktop = spec.desktop_capable if spec else bool(legacy_image and any(ref.startswith("docker-ws:") for ref in legacy_image.references)); gpu_uuids = spec.gpu_uuids if spec else (self._legacy_gpu_uuids() if raw else ())
        return WorkstationStatus(state, self._vnc_running() if state == "running" and desktop else False, image_ref or image_id or "", self.config.container_name, "/workspace", image_id, image_ref, gpu_uuids, desktop, None if state != "unavailable" else f"Docker state: {raw}")
    def _specification(self, image: str | None, gpus: tuple[str, ...], all_gpus: bool) -> LaunchSpecification:
        selected = self.inventory.resolve_image(image or self.config.image or ""); selected_gpus = self.inventory.resolve_gpus(gpus, all_gpus)
        return LaunchSpecification(selected.id, selected.display_reference, tuple(gpu.uuid for gpu in selected_gpus), selected.desktop_contract)
    def _preflight(self, spec: LaunchSpecification) -> None:
        self.docker.run(["run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", spec.image_id, "-lc", "command -v sleep >/dev/null && sleep 0"])
    def _create(self, spec: LaunchSpecification) -> None:
        c = self.config; c.state_root.mkdir(parents=True, exist_ok=True)
        # Never force a platform for an arbitrary local image: Docker has
        # already resolved the image's locally available architecture.
        args = ["run", "-d", "--name", c.container_name, "--hostname", c.container_name, "--shm-size", c.shm_size, "--restart", "unless-stopped", "--label", "docker-ws.managed=true", "--label", f"docker-ws.state-root={c.state_root}", "--label", f"docker-ws.launch-spec={spec.label_value()}", "--label", f"docker-ws.image-id={spec.image_id}", "--label", f"docker-ws.image-ref={spec.image_ref}", "--label", f"docker-ws.gpus={','.join(spec.gpu_uuids)}", "--label", f"robotics-ws.launch-config={c.launch_config}", "--mount", f"type=bind,src={c.code_root},dst=/workspace", "--mount", f"type=bind,src={c.state_root},dst=/state"]
        if spec.gpu_uuids: args += ["--gpus", f"device={','.join(spec.gpu_uuids)}"]
        if spec.desktop_capable:
            # Fleet containers use Docker's ephemeral host ports.  This makes
            # several desktops possible without exposing VNC beyond loopback.
            args += ["-p", "127.0.0.1::5901" if c.dynamic_vnc_port else f"127.0.0.1:{c.vnc_port}:5901"]
        self.docker.run(args + ["--entrypoint", "/bin/sh", spec.image_id, "-lc", "exec sleep infinity"])
    def _prepare_user(self) -> None:
        """Prepare the historical desktop-user contract, never generic shells."""
        c = self.config
        script = '''set -euo pipefail
requested_user="$1"; requested_uid="$2"; requested_gid="$3"; docker_mode="$4"
if test "$docker_mode" = rootful; then
  if group_entry="$(getent group "$requested_gid")"; then container_group="${group_entry%%:*}"; else container_group="$requested_user"; if getent group "$container_group" >/dev/null; then container_group="${requested_user}-${requested_gid}"; fi; groupadd --gid "$requested_gid" "$container_group"; fi
  if ! getent passwd "$requested_uid" >/dev/null; then container_user="$requested_user"; if getent passwd "$container_user" >/dev/null; then container_user="${requested_user}-${requested_uid}"; fi; useradd --uid "$requested_uid" --gid "$requested_gid" --home-dir /state/home --shell /bin/bash --no-create-home "$container_user"; fi
  container_user="$(getent passwd "$requested_uid" | cut -d: -f1)"
  printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$container_user" >/etc/sudoers.d/docker-ws-user
  chmod 0440 /etc/sudoers.d/docker-ws-user
  visudo -cf /etc/sudoers.d/docker-ws-user >/dev/null
fi
ownership_marker="/state/.owner-${requested_uid}-${requested_gid}"
if test "$docker_mode" = rootful && test ! -e "$ownership_marker"; then chown -R "$requested_uid:$requested_gid" /state; touch "$ownership_marker"; chown "$requested_uid:$requested_gid" "$ownership_marker"; fi
cat >/state/.robotics-ws-bashrc <<'BASHRC'
test -r /etc/bash.bashrc && source /etc/bash.bashrc
test -r "$HOME/.bashrc" && source "$HOME/.bashrc"
PS1="${ROBOTICS_WS_PROMPT_USER:-user}@\\h:\\w\\$ "
BASHRC
chown "$requested_uid:$requested_gid" /state/.robotics-ws-bashrc
'''
        self.docker.run(["exec", "-i", "--user", "root", c.container_name, "/bin/bash", "-s", "--", c.host_user, str(c.container_uid), str(c.container_gid), c.docker_mode], input=script)
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
                if self.status().desktop_capable: self._prepare_user()
                return self.status()
            spec = self._specification(image, gpus, (not gpus) if all_gpus is None else all_gpus)
            if raw:
                if existing == spec and not replace:
                    if raw in {"created", "exited"}: self.docker.run(["start", self.config.container_name])
                    elif raw != "running": raise WorkstationError(f"unsupported container state for --start: {raw}")
                    if spec.desktop_capable: self._prepare_user()
                    return self.status()
                if not replace: raise WorkstationReplaceRequired("The requested image or GPU selection differs from the managed workstation. Re-run with --replace; the old container filesystem will be discarded while /workspace and /state are preserved.")
                self._replace()
            self._preflight(spec); self._create(spec)
            if spec.desktop_capable: self._prepare_user()
            return self.status()
    def build(self) -> None:
        image = self.config.image or DEFAULT_IMAGE; self.docker.run(["buildx", "build", "--platform", "linux/amd64", "--file", str(self.config.repository_root / "assets/docker/Dockerfile"), "--target", "desktop", "--load", "--tag", image, str(self.config.repository_root)]); print(f"{image}: image built")
    def rebuild(self) -> WorkstationStatus:
        self.build(); return self.start(image=self.config.image or DEFAULT_IMAGE, replace=True)
    def enter(self) -> None:
        if self._container_status() != "running": raise WorkstationError(f"{self.config.container_name} is not running; use docker-ws container start first")
        c = self.config
        if self.status().desktop_capable:
            self._prepare_user()
            self.docker.run(["exec", "-it", "--user", f"{c.container_uid}:{c.container_gid}", "--workdir", "/workspace", "--env", "HOME=/state/home", "--env", f"USER={c.host_user}", "--env", f"LOGNAME={c.host_user}", "--env", f"ROBOTICS_WS_PROMPT_USER={c.host_user}", c.container_name, "/bin/bash", "--rcfile", "/state/.robotics-ws-bashrc"])
        else:
            self.docker.run(["exec", "-it", "--user", "root", "--workdir", "/workspace", c.container_name, "/bin/sh", "-lc", "if command -v bash >/dev/null 2>&1; then exec bash -l; else exec /bin/sh; fi"])
    def stop(self) -> WorkstationStatus:
        with self.locked():
            raw = self._container_status()
            if not raw: raise WorkstationError(f"{self.config.container_name} does not exist; use docker-ws container start first")
            if raw == "running": self.docker.run(["stop", self.config.container_name])
            elif raw not in {"created", "exited"}: raise WorkstationError(f"unsupported container state for --stop: {raw}")
            return self.status()
    def _require_desktop(self) -> None:
        if not self.status().desktop_capable: raise WorkstationError("The selected image does not advertise Docker Workstation desktop contract v1; shell access remains available with docker-ws container enter.")
    def _password_exists(self) -> bool:
        c = self.config
        try: self.docker.run(["exec", "--user", f"{c.container_uid}:{c.container_gid}", c.container_name, "/bin/bash", "-c", "test -s /state/home/.vnc/passwd"]); return True
        except WorkstationError: return False
    def _ensure_vnc_password(self, password: str | None = None, prompt: bool = False) -> None:
        c = self.config
        if self._password_exists():
            self.docker.run(["exec", "--user", f"{c.container_uid}:{c.container_gid}", c.container_name, "/bin/bash", "-c", 'if test "$(wc -c < /state/home/.vnc/passwd)" -gt 8; then truncate -s 8 /state/home/.vnc/passwd; HOME=/state/home vncserver -kill :1 >/dev/null 2>&1 || true; fi'])
            return
        self.docker.run(["exec", "--user", "root", c.container_name, "install", "-d", "-m", "700", "-o", str(c.container_uid), "-g", str(c.container_gid), "/state/home/.vnc"])
        password = password or os.environ.get("ROBOTICS_WS_VNC_PASSWORD")
        if password: self.docker.run(["exec", "-i", "--user", f"{c.container_uid}:{c.container_gid}", c.container_name, "/bin/bash", "-c", "vncpasswd -f > /state/home/.vnc/passwd && chmod 600 /state/home/.vnc/passwd"], input=password + "\n")
        elif prompt: self.docker.run(["exec", "-it", "--user", f"{c.container_uid}:{c.container_gid}", c.container_name, "vncpasswd", "/state/home/.vnc/passwd"])
        else: raise WorkstationError("VNC password must be provided before opening the desktop")
    def _vnc_running(self) -> bool:
        c = self.config
        try: self.docker.run(["exec", "--user", f"{c.container_uid}:{c.container_gid}", c.container_name, "/bin/bash", "-c", 'vncserver -list | grep -Fv stale | grep -Eq "^[[:space:]]*1[[:space:]]+5901"']); return True
        except WorkstationError: return False
    def _start_vnc(self) -> None:
        if not self._vnc_running(): self.docker.run(["exec", "-d", "--user", f"{self.config.container_uid}:{self.config.container_gid}", self.config.container_name, "start-vnc"])
    def _wait_for_vnc(self) -> None:
        c = self.config; probe = 'test "$(tigervncconfig -display :1 -get AcceptPointerEvents 2>/dev/null)" = 1 && test "$(tigervncconfig -display :1 -get AcceptKeyEvents 2>/dev/null)" = 1'
        for _ in range(100):
            try: self.docker.run(["exec", "--user", f"{c.container_uid}:{c.container_gid}", c.container_name, "/bin/bash", "-c", probe]); return
            except WorkstationError: time.sleep(.1)
        raise WorkstationError("VNC server did not become ready within 10 seconds")
    def ensure_desktop(self, password: str | None = None) -> DesktopEndpoint:
        self._require_desktop()
        if self._container_status() != "running": self.start()
        with self.locked():
            self._prepare_user(); self._ensure_vnc_password(password); self._start_vnc(); self._wait_for_vnc(); return DesktopEndpoint("127.0.0.1", self._desktop_port())

    def _desktop_port(self) -> int:
        if not self.config.dynamic_vnc_port:
            return self.config.vnc_port
        try:
            value = self.docker.run(["container", "inspect", "--format", '{{(index (index .NetworkSettings.Ports "5901/tcp") 0).HostPort}}', self.config.container_name], capture=True)
            port = int(value)
            if port > 0:
                return port
        except (ValueError, WorkstationError):
            pass
        raise WorkstationError("Docker did not allocate a loopback VNC port for this desktop")
    def reset_vnc_password(self, password: str) -> WorkstationStatus:
        if not 6 <= len(password) <= 8: raise WorkstationError("VNC passwords must contain 6 to 8 characters")
        self._require_desktop()
        if self._container_status() != "running": self.start()
        with self.locked():
            self._prepare_user(); c = self.config
            self.docker.run(["exec", "--user", "root", c.container_name, "install", "-d", "-m", "700", "-o", str(c.container_uid), "-g", str(c.container_gid), "/state/home/.vnc"])
            script = '''set -euo pipefail
password_file=/state/home/.vnc/passwd
temporary_file="${password_file}.new"
trap 'rm -f "$temporary_file"' EXIT
umask 077
vncpasswd -f > "$temporary_file"
mv "$temporary_file" "$password_file"
trap - EXIT
HOME=/state/home vncserver -kill :1 >/dev/null 2>&1 || true
'''
            self.docker.run(["exec", "-i", "--user", f"{c.container_uid}:{c.container_gid}", c.container_name, "/bin/bash", "-c", script], input=password + "\n")
            self._start_vnc(); self._wait_for_vnc(); return self.status()
    def open_vnc(self) -> None:
        if shutil.which(self.config.vncviewer_command) is None: raise WorkstationError(f"VNC viewer not found: {self.config.vncviewer_command}")
        self._require_desktop()
        if self._container_status() != "running": raise WorkstationError(f"{self.config.container_name} is not running; use docker-ws container start first")
        with self.locked(): self._prepare_user(); self._ensure_vnc_password(prompt=True); self._start_vnc(); self._wait_for_vnc()
        password_file = self.config.state_root / "home/.vnc/passwd"
        if not os.access(password_file, os.R_OK): raise WorkstationError(f"VNC password file is not readable from the host: {password_file}")
        subprocess.run([self.config.vncviewer_command, "-SecurityTypes=VncAuth", f"-PasswordFile={password_file}", "-ViewOnly=0", f"127.0.0.1:{self.config.vnc_port}"], check=False)


class FleetManager:
    """Managed-only multi-container lifecycle facade used by Workbench.

    CLI commands keep using ``Workstation`` and the configured default name.
    Labels form the fleet discovery boundary; the legacy default is the sole
    compatibility exception, so unrelated Docker containers are never shown.
    """
    managed_label = "docker-ws.managed=true"
    _name = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

    def __init__(self, config: WorkstationConfig | None = None, runner: DockerRunner | None = None,
                 inventory: HostInventory | None = None) -> None:
        self.config = config or WorkstationConfig.from_environment()
        self.docker = runner or SubprocessDockerRunner(self.config.docker_command)
        self.host_inventory = inventory or HostInventory(self.docker)

    @contextlib.contextmanager
    def locked(self) -> Iterable[None]:
        self.config.state_root.mkdir(parents=True, exist_ok=True)
        with (self.config.state_root / ".fleet.lock").open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _validate_name(self, name: str) -> str:
        if not self._name.fullmatch(name):
            raise WorkstationError("container names must be 1–63 letters, numbers, dots, underscores, or dashes")
        return name

    def _exists(self, name: str) -> bool:
        try:
            self.docker.run(["container", "inspect", "--format", "{{.State.Status}}", name], capture=True)
            return True
        except WorkstationError:
            return False

    def _managed_names(self) -> tuple[str, ...]:
        try:
            output = self.docker.run(["container", "ls", "-a", "--filter", f"label={self.managed_label}", "--format", "{{.Names}}"], capture=True)
            names = {line.strip() for line in output.splitlines() if self._name.fullmatch(line.strip())}
        except WorkstationError:
            names = set()
        if self._is_legacy_default():
            names.add(self.config.container_name)
        return tuple(sorted(names))

    def _is_legacy_default(self) -> bool:
        """Recognize the historical default only when its workstation labels prove ownership.

        Container names are user-controlled Docker namespace.  In particular,
        an unrelated container named ``docker-ws`` must not become manageable
        merely because it happens to match our default name.
        """
        name = self.config.container_name
        if not self._exists(name):
            return False
        try:
            launch_spec = self.docker.run(
                ["container", "inspect", "--format", '{{index .Config.Labels "docker-ws.launch-spec"}}', name],
                capture=True,
            )
            if LaunchSpecification.from_label(launch_spec) is not None:
                return True
            launch_config = self.docker.run(
                ["container", "inspect", "--format", '{{index .Config.Labels "robotics-ws.launch-config"}}', name],
                capture=True,
            )
            return launch_config == self.config.launch_config
        except WorkstationError:
            return False

    def _config_for(self, name: str) -> WorkstationConfig:
        self._validate_name(name)
        if name == self.config.container_name:
            return self.config
        return replace(self.config, container_name=name,
                       state_root=self.config.state_root / "containers" / name,
                       vnc_port=0, dynamic_vnc_port=True)

    def _workstation(self, name: str) -> Workstation:
        return Workstation(self._config_for(name), self.docker, self.host_inventory)

    def _is_stale(self, status: WorkstationStatus) -> bool:
        if not status.image_id:
            return False
        # The persisted reference records the image selection. A rebuild may
        # leave the old image ID locally present, so the reference must still
        # resolve to the recorded ID.
        if status.image_ref:
            try:
                return self.host_inventory.resolve_image(status.image_ref).id != status.image_id
            except WorkstationError:
                return True
        try: return all(image.id != status.image_id for image in self.host_inventory.images())
        except WorkstationError: return False

    def _status(self, name: str) -> WorkstationStatus:
        status = self._workstation(name).status()
        return replace(status, stale=self._is_stale(status))

    def containers(self) -> tuple[WorkstationStatus, ...]:
        return tuple(self._status(name) for name in self._managed_names())

    def container(self, name: str) -> WorkstationStatus:
        self._validate_name(name)
        if name not in self._managed_names():
            raise WorkstationError("container is not managed by Docker Workstation")
        return self._status(name)

    def _reserved_gpus(self, excluding: str | None = None) -> dict[str, str]:
        reservations: dict[str, str] = {}
        for status in self.containers():
            if (status.container_name != excluding and status.state == "running"
                    and self._name.fullmatch(status.container_name)):
                reservations.update({gpu: status.container_name for gpu in status.gpu_uuids})
        return reservations

    def _ensure_gpus_available(self, gpus: tuple[str, ...], excluding: str | None = None) -> None:
        reservations = self._reserved_gpus(excluding)
        for gpu in gpus:
            if gpu in reservations:
                raise WorkstationGPUConflict(gpu, reservations[gpu])

    def create(self, name: str, image: str, gpu_uuids: tuple[str, ...] = (), all_gpus: bool = False) -> WorkstationStatus:
        name = self._validate_name(name)
        with self.locked():
            if self._exists(name): raise WorkstationError(f"container already exists: {name}")
            selected = self.host_inventory.resolve_gpus(gpu_uuids, all_gpus)
            self._ensure_gpus_available(tuple(gpu.uuid for gpu in selected))
            self._workstation(name).start(image=image, gpus=tuple(gpu.uuid for gpu in selected), all_gpus=False)
            return self._status(name)

    def start(self, name: str) -> WorkstationStatus:
        with self.locked():
            status = self.container(name)
            self._ensure_gpus_available(status.gpu_uuids, excluding=name)
            self._workstation(name).start()
            return self._status(name)

    def stop(self, name: str) -> WorkstationStatus:
        with self.locked():
            self.container(name)
            return self._workstation(name).stop()

    def remove(self, name: str) -> None:
        with self.locked():
            self.container(name)
            raw = self._workstation(name)._container_status()
            if raw == "running": self.docker.run(["stop", name])
            if raw: self.docker.run(["rm", name])

    def delete_state(self, name: str) -> None:
        name = self._validate_name(name)
        if self._exists(name): raise WorkstationError("remove the container before deleting its persistent state")
        if name == self.config.container_name: raise WorkstationError("the legacy default state cannot be deleted from Workbench")
        path = self._config_for(name).state_root
        if path.is_dir(): shutil.rmtree(path)

    def orphaned_states(self) -> tuple[str, ...]:
        """Return named persistent-state directories whose container is gone."""
        root = self.config.state_root / "containers"
        if not root.is_dir():
            return ()
        return tuple(sorted(
            entry.name for entry in root.iterdir()
            if entry.is_dir() and self._name.fullmatch(entry.name) and not self._exists(entry.name)
        ))

    def recreate(self, name: str) -> WorkstationStatus:
        with self.locked():
            status = self.container(name)
            if not status.stale: raise WorkstationError("container image is still available; recreation is only needed for stale containers")
            image = status.image_ref or status.image_id
            if not image: raise WorkstationError("stale container has no recorded image reference")
            ws = self._workstation(name)
            raw = ws._container_status()
            if raw == "running": self.docker.run(["stop", name])
            if raw: self.docker.run(["rm", name])
            selected = self.host_inventory.resolve_gpus(status.gpu_uuids, False)
            self._ensure_gpus_available(tuple(gpu.uuid for gpu in selected))
            ws.start(image=image, gpus=tuple(gpu.uuid for gpu in selected), all_gpus=False)
            return self._status(name)

    def ensure_desktop(self, name: str, password: str | None = None) -> DesktopEndpoint:
        with self.locked():
            status = self.container(name)
            if status.state != "running":
                self._ensure_gpus_available(status.gpu_uuids, excluding=name)
            return self._workstation(name).ensure_desktop(password)

    def reset_vnc_password(self, name: str, password: str) -> WorkstationStatus:
        with self.locked():
            status = self.container(name)
            if status.state != "running":
                self._ensure_gpus_available(status.gpu_uuids, excluding=name)
            return self._workstation(name).reset_vnc_password(password)

    def inventory(self) -> dict[str, object]:
        host = self.host_inventory.inventory().public()
        reservations = self._reserved_gpus()
        host["gpus"] = [{**gpu, "reservation": reservations.get(str(gpu["uuid"])), "available": str(gpu["uuid"]) not in reservations} for gpu in host["gpus"]]  # type: ignore[index]
        host["containers"] = [status.public() for status in self.containers()]
        host["default_image"] = self.config.image or DEFAULT_IMAGE
        return host


def run_cli(action: str) -> int:
    try:
        ws = Workstation(); result = getattr(ws, {"vnc": "open_vnc"}.get(action, action))()
        if action == "status": print(f"{ws.config.container_name}: {result.state}")
        return 0
    except WorkstationError as exc: print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(run_cli(sys.argv[1]) if len(sys.argv) == 2 else 1)

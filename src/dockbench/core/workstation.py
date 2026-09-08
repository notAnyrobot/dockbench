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

from dockbench.core.resources import CheckoutResources
from dockbench.core.defaults import DEFAULT_IMAGE, default_data_mounts, default_state_root, default_workspace_root, workspace_root_from_value
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
        try: return LaunchSpecification.from_label(self.docker.run(["container", "inspect", "--format", '{{index .Config.Labels "io.github.notanyrobot.dockbench.launch-spec"}}', self.config.container_name], capture=True))
        except WorkstationError: return None
    def _container_image_id(self) -> str | None:
        try: return self.docker.run(["container", "inspect", "--format", "{{.Image}}", self.config.container_name], capture=True)
        except WorkstationError: return None
    def status(self) -> WorkstationStatus:
        raw = self._container_status(); state = "absent" if not raw else "running" if raw == "running" else "stopped" if raw in {"created", "exited"} else "unavailable"; spec = self._launch_spec()
        image_id = spec.image_id if spec else (self._container_image_id() if raw else None); image_ref = spec.image_ref if spec else self.config.image; desktop = spec.desktop_capable if spec else False; gpu_uuids = spec.gpu_uuids if spec else ()
        return WorkstationStatus(state, self._vnc_running() if state == "running" and desktop else False, image_ref or image_id or "", self.config.container_name, "/workspace", image_id, image_ref, gpu_uuids, desktop, None if state != "unavailable" else f"Docker state: {raw}")
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
    def _prepare_user(self) -> None:
        """Prepare the historical desktop-user contract, never generic shells."""
        c = self.config
        script = '''set -euo pipefail
requested_user="$1"; requested_uid="$2"; requested_gid="$3"; docker_mode="$4"
if test "$docker_mode" = rootful; then
  if group_entry="$(getent group "$requested_gid")"; then container_group="${group_entry%%:*}"; else container_group="$requested_user"; if getent group "$container_group" >/dev/null; then container_group="${requested_user}-${requested_gid}"; fi; groupadd --gid "$requested_gid" "$container_group"; fi
  if ! getent passwd "$requested_uid" >/dev/null; then container_user="$requested_user"; if getent passwd "$container_user" >/dev/null; then container_user="${requested_user}-${requested_uid}"; fi; useradd --uid "$requested_uid" --gid "$requested_gid" --home-dir /state/home --shell /bin/bash --no-create-home "$container_user"; fi
  container_user="$(getent passwd "$requested_uid" | cut -d: -f1)"
  printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$container_user" >/etc/sudoers.d/dockbench-user
  chmod 0440 /etc/sudoers.d/dockbench-user
  visudo -cf /etc/sudoers.d/dockbench-user >/dev/null
fi
ownership_marker="/state/.owner-${requested_uid}-${requested_gid}"
if test "$docker_mode" = rootful && test ! -e "$ownership_marker"; then chown -R "$requested_uid:$requested_gid" /state; touch "$ownership_marker"; chown "$requested_uid:$requested_gid" "$ownership_marker"; fi
cat >/state/.dockbench-bashrc <<'BASHRC'
test -r /etc/bash.bashrc && source /etc/bash.bashrc
test -r "$HOME/.bashrc" && source "$HOME/.bashrc"
PS1="${DOCKBENCH_PROMPT_USER:-user}@\\h:\\w\\$ "
BASHRC
chown "$requested_uid:$requested_gid" /state/.dockbench-bashrc
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
    def build(self, on_progress: Callable[[str], None] | None = None) -> None:
        image = self.config.image or DEFAULT_IMAGE
        recipe_dir = CheckoutResources.discover(self.config.repository_root).images / "android-ws"
        report = on_progress or (lambda line: print(line, flush=True))
        self.docker.run(["buildx", "build", "--progress=plain", "--platform", "linux/amd64", "--file", str(recipe_dir / "Dockerfile.android-ws-v2"), "--target", "desktop", "--load", "--tag", image, str(recipe_dir)], on_output=report)
        print(f"{image}: image built")
    def rebuild(self) -> WorkstationStatus:
        self.build(); return self.start(image=self.config.image or DEFAULT_IMAGE, replace=True)
    def enter(self) -> None:
        if self._container_status() != "running": raise WorkstationError(f"{self.config.container_name} is not running; use `dockbench start` first")
        c = self.config
        if self.status().desktop_capable:
            self._prepare_user()
            self.docker.run(["exec", "-it", "--user", f"{c.container_uid}:{c.container_gid}", "--workdir", "/workspace", "--env", "HOME=/state/home", "--env", f"USER={c.host_user}", "--env", f"LOGNAME={c.host_user}", "--env", f"DOCKBENCH_PROMPT_USER={c.host_user}", c.container_name, "/bin/bash", "--rcfile", "/state/.dockbench-bashrc"])
        else:
            self.docker.run(["exec", "-it", "--user", "root", "--workdir", "/workspace", c.container_name, "/bin/sh", "-lc", "if command -v bash >/dev/null 2>&1; then exec bash -l; else exec /bin/sh; fi"])
    def stop(self) -> WorkstationStatus:
        with self.locked():
            raw = self._container_status()
            if not raw: raise WorkstationError(f"{self.config.container_name} does not exist; use `dockbench start` first")
            if raw == "running": self.docker.run(["stop", self.config.container_name])
            elif raw not in {"created", "exited"}: raise WorkstationError(f"unsupported container state for --stop: {raw}")
            return self.status()
    def _require_desktop(self) -> None:
        if not self.status().desktop_capable: raise WorkstationError("The selected image does not advertise the Dockbench desktop contract v1; shell access remains available with `dockbench shell`.")
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
        password = password or os.environ.get("DOCKBENCH_VNC_PASSWORD")
        if password: self.docker.run(["exec", "-i", "--user", f"{c.container_uid}:{c.container_gid}", c.container_name, "/bin/bash", "-c", "vncpasswd -f > /state/home/.vnc/passwd && chmod 600 /state/home/.vnc/passwd"], input=password + "\n")
        elif prompt: self.docker.run(["exec", "-it", "--user", f"{c.container_uid}:{c.container_gid}", c.container_name, "vncpasswd", "/state/home/.vnc/passwd"])
        else: raise WorkstationError("VNC password must be provided before opening the desktop")
    def _vnc_running(self) -> bool:
        c = self.config
        try: self.docker.run(["exec", "--user", f"{c.container_uid}:{c.container_gid}", c.container_name, "/bin/bash", "-c", 'vncserver -list | grep -Fv stale | grep -Eq "^[[:space:]]*1[[:space:]]+5901"']); return True
        except WorkstationError: return False
    def _write_vnc_xstartup(self) -> None:
        """Install the desktop session launcher without depending on image helpers."""
        c = self.config
        script = '''set -eu
vnc_dir="$HOME/.vnc"
xstartup="$vnc_dir/xstartup"
mkdir -p "$vnc_dir"
chmod 700 "$vnc_dir"
if test ! -f "$xstartup"; then
  cat >"$xstartup" <<'XSTARTUP'
#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
exec dbus-launch --exit-with-session startxfce4
XSTARTUP
  chmod 700 "$xstartup"
fi
'''
        self.docker.run(["exec", "-i", "--user", f"{c.container_uid}:{c.container_gid}", "--env", "HOME=/state/home", c.container_name, "/bin/sh", "-s"], input=script)

    def _start_vnc(self) -> None:
        if self._vnc_running():
            return
        self._write_vnc_xstartup()
        c = self.config
        self.docker.run([
            "exec", "-d", "--user", f"{c.container_uid}:{c.container_gid}", "--env", "HOME=/state/home",
            c.container_name, "/bin/sh", "-c",
            'exec vncserver "${VNC_DISPLAY:-:1}" -fg -localhost no -geometry "${VNC_GEOMETRY:-1920x1080}" -depth "${VNC_DEPTH:-24}"',
        ])
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
        if self._container_status() != "running": raise WorkstationError(f"{self.config.container_name} is not running; use `dockbench start` first")
        with self.locked(): self._prepare_user(); self._ensure_vnc_password(prompt=True); self._start_vnc(); self._wait_for_vnc()
        password_file = self.config.state_root / "home/.vnc/passwd"
        if not os.access(password_file, os.R_OK): raise WorkstationError(f"VNC password file is not readable from the host: {password_file}")
        subprocess.run([self.config.vncviewer_command, "-SecurityTypes=VncAuth", f"-PasswordFile={password_file}", "-ViewOnly=0", f"127.0.0.1:{self.config.vnc_port}"], check=False)

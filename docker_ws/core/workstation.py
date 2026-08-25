#!/usr/bin/env python3
"""Lifecycle management for the single Docker workstation.

This is deliberately dependency-free so the CLI remains usable on a freshly
provisioned host. The web application imports :class:`Workstation` rather
than invoking the command-line client.
"""
from __future__ import annotations

import contextlib
import fcntl
import getpass
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Protocol


class WorkstationError(RuntimeError):
    """An expected, safe-to-display workstation failure."""


class WorkstationRebuildRequired(WorkstationError):
    """The persisted container no longer matches its image or launch settings."""


class DockerRunner(Protocol):
    def run(self, args: list[str], *, input: str | None = None, capture: bool = False,
            check: bool = True) -> str: ...


class SubprocessDockerRunner:
    """Docker adapter that never composes a shell command."""

    def __init__(self, command: str) -> None:
        self.command = command

    def run(self, args: list[str], *, input: str | None = None, capture: bool = False,
            check: bool = True) -> str:
        try:
            completed = subprocess.run([self.command, *args], input=input, text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None, check=False)
        except FileNotFoundError as exc:
            raise WorkstationError(f"Docker command not found: {self.command}") from exc
        if check and completed.returncode:
            detail = (completed.stderr or "").strip() if capture else ""
            raise WorkstationError(detail or f"Docker command failed ({completed.returncode})")
        return completed.stdout.strip() if capture else ""


@dataclass(frozen=True)
class WorkstationConfig:
    repository_root: Path; docker_command: str; code_root: Path; state_root: Path
    shm_size: str; host_uid: int; host_gid: int; host_user: str; image: str
    container_name: str; vnc_port: int; vncviewer_command: str; docker_mode: str
    container_uid: int; container_gid: int

    @property
    def launch_config(self) -> str:
        return "|".join((str(self.code_root), str(self.state_root), self.shm_size,
            str(self.host_uid), str(self.host_gid), self.docker_mode, str(self.vnc_port)))

    @classmethod
    def from_environment(cls, repository_root: Path | None = None) -> "WorkstationConfig":
        env = os.environ; root = repository_root or Path(__file__).resolve().parents[2]
        code_root = Path(env.get("ROBOTICS_WS_CODE_ROOT", str(Path.home() / "Code"))).expanduser()
        state_root = Path(env.get("ROBOTICS_WS_STATE_ROOT", str(code_root.parent / ".robotics-ws"))).expanduser()
        port = env.get("ROBOTICS_WS_VNC_PORT", "5901")
        if not re.fullmatch(r"[1-9][0-9]*", port): raise WorkstationError(f"VNC port must be a positive integer: {port}")
        docker_command = env.get("ROBOTICS_WS_DOCKER", "docker")
        if shutil.which(docker_command) is None: raise WorkstationError(f"Docker command not found: {docker_command}")
        if not code_root.is_dir(): raise WorkstationError(f"code root does not exist: {code_root}")
        security = SubprocessDockerRunner(docker_command).run(["info", "--format", "{{json .SecurityOptions}}"], capture=True)
        rootless = "rootless" in security; uid = int(env.get("ROBOTICS_WS_HOST_UID", str(os.getuid()))); gid = int(env.get("ROBOTICS_WS_HOST_GID", str(os.getgid())))
        return cls(root, docker_command, code_root, state_root, env.get("ROBOTICS_WS_SHM_SIZE", "32g"), uid, gid,
            env.get("ROBOTICS_WS_HOST_USER", getpass.getuser()), env.get("ROBOTICS_WS_DESKTOP_IMAGE", "docker-ws:u22.04-cu12.8.1-v1-desktop"),
            env.get("ROBOTICS_WS_DESKTOP_NAME", "docker-ws"), int(port), env.get("ROBOTICS_WS_VNCVIEWER", "vncviewer"),
            "rootless" if rootless else "rootful", 0 if rootless else uid, 0 if rootless else gid)


@dataclass(frozen=True)
class WorkstationStatus:
    state: str; desktop_ready: bool; image: str; container_name: str; workspace: str; message: str | None = None
    def public(self) -> dict[str, object]: return asdict(self)


@dataclass(frozen=True)
class DesktopEndpoint:
    host: str; port: int


class Workstation:
    """Deep lifecycle interface shared by the CLI and Workbench backend."""
    def __init__(self, config: WorkstationConfig | None = None, runner: DockerRunner | None = None) -> None:
        self.config = config or WorkstationConfig.from_environment(); self.docker = runner or SubprocessDockerRunner(self.config.docker_command)

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

    def status(self) -> WorkstationStatus:
        raw = self._container_status(); state = "absent" if not raw else "running" if raw == "running" else "stopped" if raw in {"created", "exited"} else "unavailable"
        return WorkstationStatus(state, self._vnc_running() if state == "running" else False, self.config.image, self.config.container_name, "/workspace", None if state != "unavailable" else f"Docker state: {raw}")

    def _require_current_image(self) -> None:
        container = self.docker.run(["container", "inspect", "--format", "{{.Image}}", self.config.container_name], capture=True)
        image = self.docker.run(["image", "inspect", "--format", "{{.Id}}", self.config.image], capture=True)
        if container != image:
            raise WorkstationRebuildRequired(
                f"{self.config.container_name} uses a stale image; rebuild it with: "
                "uv run docker-ws image rebuild"
            )

    def _require_current_config(self) -> None:
        actual = self.docker.run(["container", "inspect", "--format", '{{index .Config.Labels "robotics-ws.launch-config"}}', self.config.container_name], capture=True)
        if actual != self.config.launch_config:
            raise WorkstationRebuildRequired(
                f"{self.config.container_name} uses different launch settings; rebuild it with: "
                "uv run docker-ws image rebuild"
            )

    def build(self) -> None:
        self.docker.run(["buildx", "build", "--platform", "linux/amd64", "--file", str(self.config.repository_root / "assets/docker/Dockerfile"), "--target", "desktop", "--load", "--tag", self.config.image, str(self.config.repository_root)])
        print(f"{self.config.image}: image built")

    def rebuild(self) -> WorkstationStatus:
        with self.locked():
            self.build(); raw = self._container_status()
            if raw == "running": self.docker.run(["stop", self.config.container_name]); self.docker.run(["rm", self.config.container_name])
            elif raw in {"created", "exited"}: self.docker.run(["rm", self.config.container_name])
            elif raw: raise WorkstationError(f"unsupported container state for rebuild: {raw}")
            return self._start_unlocked()

    def _prepare_user(self) -> None:
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

    def _start_unlocked(self) -> WorkstationStatus:
        c = self.config; raw = self._container_status(); created = False
        if not raw:
            c.state_root.mkdir(parents=True, exist_ok=True)
            self.docker.run(["run", "-d", "--name", c.container_name, "--hostname", c.container_name, "--platform", "linux/amd64", "--gpus", "all", "--shm-size", c.shm_size, "--restart", "unless-stopped", "--label", f"robotics-ws.launch-config={c.launch_config}", "-p", f"127.0.0.1:{c.vnc_port}:5901", "--mount", f"type=bind,src={c.code_root},dst=/workspace", "--mount", f"type=bind,src={c.state_root},dst=/state", c.image]); created = True
        elif raw == "running": self._require_current_image(); self._require_current_config()
        elif raw in {"created", "exited"}: self._require_current_image(); self._require_current_config(); self.docker.run(["start", c.container_name])
        else: raise WorkstationError(f"unsupported container state for --start: {raw}")
        self._prepare_user(); print(f"{c.container_name}: {'created and running' if created else 'already running' if raw == 'running' else 'started'}")
        return self.status()

    def start(self) -> WorkstationStatus:
        with self.locked(): return self._start_unlocked()

    def enter(self) -> None:
        c = self.config
        if self._container_status() != "running": raise WorkstationError(f"{c.container_name} is not running; use --start first")
        self._require_current_image(); self._require_current_config()
        self.docker.run(["exec", "-it", "--user", f"{c.container_uid}:{c.container_gid}", "--workdir", "/workspace", "--env", "HOME=/state/home", "--env", f"USER={c.host_user}", "--env", f"LOGNAME={c.host_user}", "--env", f"ROBOTICS_WS_PROMPT_USER={c.host_user}", c.container_name, "/bin/bash", "--rcfile", "/state/.robotics-ws-bashrc"])

    def stop(self) -> WorkstationStatus:
        with self.locked():
            raw = self._container_status()
            if not raw: raise WorkstationError(f"{self.config.container_name} does not exist; use --start first")
            if raw == "running": self.docker.run(["stop", self.config.container_name]); print(f"{self.config.container_name}: stopped")
            elif raw in {"created", "exited"}: print(f"{self.config.container_name}: already stopped")
            else: raise WorkstationError(f"unsupported container state for --stop: {raw}")
            return self.status()

    def _password_exists(self) -> bool:
        c = self.config
        try: self.docker.run(["exec", "--user", f"{c.container_uid}:{c.container_gid}", c.container_name, "/bin/bash", "-c", "test -s /state/home/.vnc/passwd"]); return True
        except WorkstationError: return False

    def _ensure_vnc_password(self, password: str | None = None, prompt: bool = False) -> None:
        c = self.config
        if self._password_exists():
            # TigerVNC stores an optional view-only credential as a second
            # encrypted 8-byte block. A browser user can otherwise
            # authenticate successfully yet have every input event rejected.
            # Preserve the primary full-control credential and discard only
            # the optional view-only block from legacy password files. Restart
            # a live server because TigerVNC loads the credentials at startup.
            self.docker.run(["exec", "--user", f"{c.container_uid}:{c.container_gid}", c.container_name,
                "/bin/bash", "-c", 'if test "$(wc -c < /state/home/.vnc/passwd)" -gt 8; then truncate -s 8 /state/home/.vnc/passwd; HOME=/state/home vncserver -kill :1 >/dev/null 2>&1 || true; fi'])
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

    def reset_vnc_password(self, password: str) -> WorkstationStatus:
        """Replace the VNC credential and restart the desktop server."""
        if not 6 <= len(password) <= 8:
            raise WorkstationError("VNC passwords must contain 6 to 8 characters")
        with self.locked():
            if self._container_status() != "running": self._start_unlocked()
            self._require_current_image(); self._require_current_config(); self._prepare_user()
            c = self.config
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
            self.docker.run(["exec", "-i", "--user", f"{c.container_uid}:{c.container_gid}", c.container_name,
                "/bin/bash", "-c", script], input=password + "\n")
            self._start_vnc(); self._wait_for_vnc()
            return self.status()

    def _start_vnc(self) -> None:
        if not self._vnc_running(): self.docker.run(["exec", "-d", "--user", f"{self.config.container_uid}:{self.config.container_gid}", self.config.container_name, "start-vnc"])

    def _wait_for_vnc(self) -> None:
        c = self.config; probe = 'test "$(tigervncconfig -display :1 -get AcceptPointerEvents 2>/dev/null)" = 1 && test "$(tigervncconfig -display :1 -get AcceptKeyEvents 2>/dev/null)" = 1'
        for _ in range(100):
            try: self.docker.run(["exec", "--user", f"{c.container_uid}:{c.container_gid}", c.container_name, "/bin/bash", "-c", probe]); return
            except WorkstationError: time.sleep(.1)
        raise WorkstationError("VNC server did not become ready within 10 seconds")

    def ensure_desktop(self, password: str | None = None) -> DesktopEndpoint:
        with self.locked():
            if self._container_status() != "running": self._start_unlocked()
            self._require_current_image(); self._require_current_config(); self._prepare_user(); self._ensure_vnc_password(password, prompt=False); self._start_vnc(); self._wait_for_vnc()
            return DesktopEndpoint("127.0.0.1", self.config.vnc_port)

    def open_vnc(self) -> None:
        if shutil.which(self.config.vncviewer_command) is None: raise WorkstationError(f"VNC viewer not found: {self.config.vncviewer_command}")
        if self._container_status() != "running": raise WorkstationError(f"{self.config.container_name} is not running; use --start first")
        with self.locked(): self._require_current_image(); self._require_current_config(); self._prepare_user(); self._ensure_vnc_password(prompt=True); self._start_vnc(); self._wait_for_vnc()
        password_file = self.config.state_root / "home/.vnc/passwd"
        if not os.access(password_file, os.R_OK): raise WorkstationError(f"VNC password file is not readable from the host: {password_file}")
        subprocess.run([self.config.vncviewer_command, "-SecurityTypes=VncAuth", f"-PasswordFile={password_file}", "-ViewOnly=0", f"127.0.0.1:{self.config.vnc_port}"], check=False)


def run_cli(action: str) -> int:
    try:
        ws = Workstation(); result = getattr(ws, {"vnc": "open_vnc"}.get(action, action))()
        if action == "status": print(f"{ws.config.container_name}: {result.state}")
        return 0
    except WorkstationError as exc: print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"build", "rebuild", "start", "enter", "stop", "status", "vnc"}:
        print("ERROR: workstation_run requires an action", file=sys.stderr); raise SystemExit(1)
    raise SystemExit(run_cli(sys.argv[1]))

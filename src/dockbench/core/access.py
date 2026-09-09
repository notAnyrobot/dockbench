"""Managed-container shell and desktop preparation shared by both adapters.

Lifecycle owns selection and locking. This module owns user provisioning, shell
contracts, credentials, VNC readiness and endpoint discovery; adapters own the
interactive process or socket consuming the prepared result.
"""

from __future__ import annotations
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable
from dockbench.core.errors import WorkstationError

if TYPE_CHECKING:
    from dockbench.core.workstation import Workstation, WorkstationStatus


@dataclass(frozen=True)
class DesktopEndpoint:
    host: str
    port: int


@dataclass(frozen=True)
class ShellExecution:
    """Prepared Docker arguments; the consuming adapter owns execution."""

    arguments: tuple[str, ...]


def generic_shell(container_name: str) -> ShellExecution:
    """Shared root shell contract for CLI and browser terminals."""
    return ShellExecution(
        (
            "exec",
            "-it",
            "--user",
            "root",
            "--workdir",
            "/workspace",
            container_name,
            "/bin/sh",
            "-lc",
            "if command -v bash >/dev/null 2>&1; then exec bash -l; else exec /bin/sh; fi",
        )
    )


class ContainerAccess:
    def __init__(self, container: Workstation) -> None:
        self._container = container
        self.config = container.config
        self.docker = container.docker

    def prepare_user(self) -> None:
        """Prepare the historical desktop-user contract, never generic shells."""
        c = self.config
        script = """set -euo pipefail
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
"""
        self.docker.run(
            [
                "exec",
                "-i",
                "--user",
                "root",
                c.container_name,
                "/bin/bash",
                "-s",
                "--",
                c.host_user,
                str(c.container_uid),
                str(c.container_gid),
                c.docker_mode,
            ],
            input=script,
        )

    def shell(self) -> ShellExecution:
        if self._container._container_status() != "running":
            raise WorkstationError(
                f"{self.config.container_name} is not running; use `dockbench start` first"
            )
        return generic_shell(self.config.container_name)

    def _require_desktop(self) -> None:
        if not self._container.status().desktop_capable:
            raise WorkstationError(
                "The selected image does not advertise the Dockbench desktop contract v1; shell access remains available with `dockbench shell`."
            )

    def _password_exists(self) -> bool:
        c = self.config
        try:
            self.docker.run(
                [
                    "exec",
                    "--user",
                    f"{c.container_uid}:{c.container_gid}",
                    c.container_name,
                    "/bin/bash",
                    "-c",
                    "test -s /state/home/.vnc/passwd",
                ]
            )
            return True
        except WorkstationError:
            return False

    def _ensure_vnc_password(
        self,
        password: str | None = None,
        prompt: Callable[[list[str]], None] | None = None,
    ) -> None:
        c = self.config
        if self._password_exists():
            self.docker.run(
                [
                    "exec",
                    "--user",
                    f"{c.container_uid}:{c.container_gid}",
                    c.container_name,
                    "/bin/bash",
                    "-c",
                    'if test "$(wc -c < /state/home/.vnc/passwd)" -gt 8; then truncate -s 8 /state/home/.vnc/passwd; HOME=/state/home vncserver -kill :1 >/dev/null 2>&1 || true; fi',
                ]
            )
            return
        self.docker.run(
            [
                "exec",
                "--user",
                "root",
                c.container_name,
                "install",
                "-d",
                "-m",
                "700",
                "-o",
                str(c.container_uid),
                "-g",
                str(c.container_gid),
                "/state/home/.vnc",
            ]
        )
        password = password or os.environ.get("DOCKBENCH_VNC_PASSWORD")
        if password:
            self.docker.run(
                [
                    "exec",
                    "-i",
                    "--user",
                    f"{c.container_uid}:{c.container_gid}",
                    c.container_name,
                    "/bin/bash",
                    "-c",
                    "vncpasswd -f > /state/home/.vnc/passwd && chmod 600 /state/home/.vnc/passwd",
                ],
                input=password + "\n",
            )
        elif prompt:
            prompt(
                [
                    "exec",
                    "-it",
                    "--user",
                    f"{c.container_uid}:{c.container_gid}",
                    c.container_name,
                    "vncpasswd",
                    "/state/home/.vnc/passwd",
                ]
            )
        else:
            raise WorkstationError(
                "VNC password must be provided before opening the desktop"
            )

    def desktop_ready(self) -> bool:
        c = self.config
        try:
            self.docker.run(
                [
                    "exec",
                    "--user",
                    f"{c.container_uid}:{c.container_gid}",
                    c.container_name,
                    "/bin/bash",
                    "-c",
                    'vncserver -list | grep -Fv stale | grep -Eq "^[[:space:]]*1[[:space:]]+5901"',
                ]
            )
            return True
        except WorkstationError:
            return False

    def _write_vnc_xstartup(self) -> None:
        """Install the desktop session launcher without depending on image helpers."""
        c = self.config
        script = """set -eu
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
"""
        self.docker.run(
            [
                "exec",
                "-i",
                "--user",
                f"{c.container_uid}:{c.container_gid}",
                "--env",
                "HOME=/state/home",
                c.container_name,
                "/bin/sh",
                "-s",
            ],
            input=script,
        )

    def _start_vnc(self) -> None:
        if self.desktop_ready():
            return
        self._write_vnc_xstartup()
        c = self.config
        self.docker.run(
            [
                "exec",
                "-d",
                "--user",
                f"{c.container_uid}:{c.container_gid}",
                "--env",
                "HOME=/state/home",
                c.container_name,
                "/bin/sh",
                "-c",
                'exec vncserver "${VNC_DISPLAY:-:1}" -fg -localhost no -geometry "${VNC_GEOMETRY:-1920x1080}" -depth "${VNC_DEPTH:-24}"',
            ]
        )

    def _wait_for_vnc(self) -> None:
        c = self.config
        probe = 'test "$(tigervncconfig -display :1 -get AcceptPointerEvents 2>/dev/null)" = 1 && test "$(tigervncconfig -display :1 -get AcceptKeyEvents 2>/dev/null)" = 1'
        for _ in range(100):
            try:
                self.docker.run(
                    [
                        "exec",
                        "--user",
                        f"{c.container_uid}:{c.container_gid}",
                        c.container_name,
                        "/bin/bash",
                        "-c",
                        probe,
                    ]
                )
                return
            except WorkstationError:
                time.sleep(0.1)
        raise WorkstationError("VNC server did not become ready within 10 seconds")

    def ensure_desktop(self, password: str | None = None) -> DesktopEndpoint:
        self._require_desktop()
        if self._container._container_status() != "running":
            self._container.start()
        with self._container.locked():
            self.prepare_user()
            self._ensure_vnc_password(password)
            self._start_vnc()
            self._wait_for_vnc()
            return DesktopEndpoint("127.0.0.1", self._desktop_port())

    def _desktop_port(self) -> int:
        if not self.config.dynamic_vnc_port:
            return self.config.vnc_port
        try:
            value = self.docker.run(
                [
                    "container",
                    "inspect",
                    "--format",
                    '{{(index (index .NetworkSettings.Ports "5901/tcp") 0).HostPort}}',
                    self.config.container_name,
                ],
                capture=True,
            )
            port = int(value)
            if port > 0:
                return port
        except (ValueError, WorkstationError):
            pass
        raise WorkstationError(
            "Docker did not allocate a loopback VNC port for this desktop"
        )

    def reset_vnc_password(self, password: str) -> WorkstationStatus:
        if not 6 <= len(password) <= 8:
            raise WorkstationError("VNC passwords must contain 6 to 8 characters")
        self._require_desktop()
        if self._container._container_status() != "running":
            self._container.start()
        with self._container.locked():
            self.prepare_user()
            c = self.config
            self.docker.run(
                [
                    "exec",
                    "--user",
                    "root",
                    c.container_name,
                    "install",
                    "-d",
                    "-m",
                    "700",
                    "-o",
                    str(c.container_uid),
                    "-g",
                    str(c.container_gid),
                    "/state/home/.vnc",
                ]
            )
            script = """set -euo pipefail
password_file=/state/home/.vnc/passwd
temporary_file="${password_file}.new"
trap 'rm -f "$temporary_file"' EXIT
umask 077
vncpasswd -f > "$temporary_file"
mv "$temporary_file" "$password_file"
trap - EXIT
HOME=/state/home vncserver -kill :1 >/dev/null 2>&1 || true
"""
            self.docker.run(
                [
                    "exec",
                    "-i",
                    "--user",
                    f"{c.container_uid}:{c.container_gid}",
                    c.container_name,
                    "/bin/bash",
                    "-c",
                    script,
                ],
                input=password + "\n",
            )
            self._start_vnc()
            self._wait_for_vnc()
            return self._container.status()

    def native_desktop(self, prompt: Callable[[list[str]], None]) -> DesktopEndpoint:
        self._require_desktop()
        if self._container._container_status() != "running":
            raise WorkstationError(
                f"{self.config.container_name} is not running; use `dockbench start` first"
            )
        with self._container.locked():
            self.prepare_user()
            self._ensure_vnc_password(prompt=prompt)
            self._start_vnc()
            self._wait_for_vnc()
        return DesktopEndpoint("127.0.0.1", self.config.vnc_port)

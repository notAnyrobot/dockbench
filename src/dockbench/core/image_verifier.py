"""Capability-based validation for arbitrary local Docker images."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from dockbench.core.host_inventory import DESKTOP_CONTRACT_LABEL


class DockerRunner(Protocol):
    def run(self, args: list[str], *, input: str | None = None, capture: bool = False, check: bool = True) -> str: ...


GENERIC_SHELL_CHECK = "command -v sleep >/dev/null && sleep 0"
DESKTOP_V1_COMMANDS = (
    # User preparation and the sudoers path used by the lifecycle service.
    "bash", "getent", "groupadd", "useradd", "sudo", "visudo", "install", "chown", "chmod", "touch", "cut", "cat", "mkdir", "grep",
    # Password setup, VNC readiness, and the desktop server command line.
    "vncserver", "vncpasswd", "tigervncconfig", "truncate", "wc", "dbus-launch", "startxfce4",
)


@dataclass(frozen=True)
class ImageVerificationResult:
    image: str
    desktop_contract: str | None
    checks: tuple[str, ...]

    @property
    def desktop_capable(self) -> bool:
        return self.desktop_contract == "v1"


class ImageVerifier:
    """Verify only the generic and advertised desktop capabilities we launch."""

    def __init__(self, docker: DockerRunner) -> None:
        self.docker = docker

    def verify(self, image: str) -> ImageVerificationResult:
        desktop_contract = self.docker.run(
            ["image", "inspect", "--format", f'{{{{index .Config.Labels "{DESKTOP_CONTRACT_LABEL}"}}}}', image],
            capture=True,
        ).strip()
        if desktop_contract in {"", "<no value>"}:
            desktop_contract = None
        self.docker.run(["run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image, "-lc", GENERIC_SHELL_CHECK])
        checks = ["shell"]
        if desktop_contract == "v1":
            command_check = " && ".join(f"command -v {command} >/dev/null" for command in DESKTOP_V1_COMMANDS)
            self.docker.run(["run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image, "-lc", command_check])
            checks.append("desktop-v1")
        return ImageVerificationResult(image, desktop_contract, tuple(checks))

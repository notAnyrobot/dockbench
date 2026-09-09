"""Native shell and viewer adaptation for prepared container access."""

from __future__ import annotations
import argparse
import os
import shutil
import subprocess
from dockbench.core.backend import Backend
from dockbench.core.errors import WorkstationError


def shell(backend: Backend, name: str | None = None) -> None:
    backend.fleet.docker.run(list(backend.fleet.shell(name).arguments))


def desktop(backend: Backend) -> None:
    container = backend.workstation
    config = container.config
    if shutil.which(config.vncviewer_command) is None:
        raise WorkstationError(f"VNC viewer not found: {config.vncviewer_command}")
    endpoint = container.access.native_desktop(container.docker.run)
    password_file = config.state_root / "home/.vnc/passwd"
    if not os.access(password_file, os.R_OK):
        raise WorkstationError(
            f"VNC password file is not readable from the host: {password_file}"
        )
    subprocess.run(
        [
            config.vncviewer_command,
            "-SecurityTypes=VncAuth",
            f"-PasswordFile={password_file}",
            "-ViewOnly=0",
            f"{endpoint.host}:{endpoint.port}",
        ],
        check=False,
    )


def register(actions: argparse._SubParsersAction) -> None:
    shell = actions.add_parser(
        "shell",
        help="Open a shell in a running managed container as root.",
        description=(
            "Open a shell as root in /workspace, using Bash when available or /bin/sh. "
            "The VNC desktop retains its configured host user and persistent home."
        ),
    )
    shell.add_argument(
        "container",
        nargs="?",
        metavar="CONTAINER",
        help="Managed container name; defaults to the running configured default or sole running container.",
    )
    actions.add_parser(
        "desktop", help="Provision VNC if needed and open the native viewer."
    )

"""CLI adaptation for managed-container lifecycle and access commands."""
from __future__ import annotations

import argparse

from dockbench.core.backend import Backend
from dockbench.core.errors import WorkstationError
from dockbench.cli.common import fail as _fail


def run(action: str, image: str | None = None, gpus: list[str] | None = None,
                 replace: bool = False, container_name: str | None = None, *, backend: Backend | None = None) -> int:
    try:
        backend = backend if backend is not None else Backend()
        workstation = backend.workstation
        if action == "start":
            values = tuple(gpus or ())
            if "none" in values and len(values) != 1:
                raise WorkstationError("--gpu none cannot be combined with other GPU selections")
            result = workstation.start(
                image=image, gpus=tuple(value for value in values if value not in {"all", "none"}),
                all_gpus=None if gpus is None else "all" in values, replace=replace,
            )
        elif action == "shell":
            backend.fleet.enter(container_name)
            return 0
        else:
            result = getattr(workstation, {"desktop": "open_vnc", "shell": "enter"}.get(action, action))()
        if action in {"start", "stop", "status"}:
            print(f"{result.container_name}: {result.state}")
        return 0
    except WorkstationError as exc:
        return _fail(str(exc))


def register(actions: argparse._SubParsersAction) -> None:
    start = actions.add_parser("start", help="Create or start the managed container.")
    start.add_argument("--image", help="Tagged local image to use when creating the container.")
    start.add_argument("--gpu", action="append", default=None, metavar="UUID_OR_INDEX",
                       help="GPU UUID/index, 'all' (default), or 'none'; repeat for multiple GPUs.")
    start.add_argument("--replace", action="store_true",
                       help="Replace a container whose immutable image/GPU launch request differs.")
    shell = actions.add_parser("shell", help="Open Bash in a running managed container as the host user.")
    shell.add_argument("container", nargs="?", metavar="CONTAINER",
                       help="Managed container name; defaults to the sole running container.")
    actions.add_parser("desktop", help="Provision VNC if needed and open the native viewer.")
    actions.add_parser("stop", help="Stop the managed container without removing it.")
    actions.add_parser("status", help="Print managed container state.")

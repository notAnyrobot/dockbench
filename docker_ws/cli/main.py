"""Repository-first command line for Docker Workstation."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import uvicorn

from docker_ws.core.workstation import Workstation, WorkstationError
from docker_ws.core.images import WorkstationImages


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKBENCH_INDEX = REPOSITORY_ROOT / "apps/workbench/dist/index.html"


def _fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def _workstation(action: str) -> int:
    try:
        workstation = Workstation()
        result = getattr(workstation, {"vnc": "open_vnc"}.get(action, action))()
        if action == "status":
            print(f"{workstation.config.container_name}: {result.state}")
        return 0
    except WorkstationError as exc:
        return _fail(str(exc))


def _package_images(action: str, paths: list[str]) -> int:
    try:
        if action == "package":
            result = WorkstationImages().package(paths[0] if paths else None)
            print(f"Created:\n  {result.archive}")
            return 0
        WorkstationImages().load(paths)
        return 0
    except (OSError, subprocess.CalledProcessError, WorkstationError) as exc:
        return _fail(str(exc))


def _install_service() -> int:
    try:
        uv = shutil.which("uv")
        if uv is None:
            raise WorkstationError("uv command not found; install uv before installing the Workbench service")
        unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        template = (REPOSITORY_ROOT / "assets/systemd/docker-ws-workbench.service").read_text()
        unit = unit_dir / "docker-ws-workbench.service"
        unit.write_text(template.replace("__WORKBENCH_ROOT__", str(REPOSITORY_ROOT)).replace("__UV_EXECUTABLE__", uv))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "docker-ws-workbench.service"], check=True)
        print("Workbench is listening only at http://127.0.0.1:8787")
        return 0
    except (OSError, subprocess.CalledProcessError, WorkstationError) as exc:
        return _fail(str(exc))


def _workbench() -> int:
    if not WORKBENCH_INDEX.is_file():
        return _fail("Workbench frontend is not built. Run:\n  npm ci --prefix apps/workbench\n  npm run --prefix apps/workbench build")
    uvicorn.run("docker_ws.web.app:app", host="127.0.0.1", port=8787, proxy_headers=False)
    return 0


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="docker-ws", description="Manage the Docker Workstation.")
    actions = command.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for action, description in {
        "start": "Create or start the workstation without configuring VNC.",
        "enter": "Open Bash in the running workstation as the host user.",
        "stop": "Stop the workstation without removing it.",
        "status": "Print workstation state.",
        "vnc": "Provision VNC if needed and open the native viewer.",
        "workbench": "Serve the loopback-only web Workbench.",
    }.items():
        actions.add_parser(action, help=description, description=description)
    image = actions.add_parser("image", help="Build, replace, package, or load images.")
    image_actions = image.add_subparsers(dest="image_action", required=True, metavar="ACTION")
    image_actions.add_parser("build", help="Build or update the desktop image using Docker's layer cache.")
    image_actions.add_parser("rebuild", help="Build the image, replace the container, and start it again.")
    package = image_actions.add_parser("package", help="Save the desktop image as a Docker tar file.")
    package.add_argument("directory", nargs="*")
    load = image_actions.add_parser("load", help="Load one or more Docker image tar files.")
    load.add_argument("tarfile", nargs="+")
    service = actions.add_parser("service", help="Manage the Workbench user service.")
    service_actions = service.add_subparsers(dest="service_action", required=True, metavar="ACTION")
    service_actions.add_parser("install", help="Install and start the user-level Workbench service.")
    return command


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command in {"start", "enter", "stop", "status", "vnc"}:
        return _workstation(arguments.command)
    if arguments.command == "workbench":
        return _workbench()
    if arguments.command == "image":
        if arguments.image_action in {"build", "rebuild"}:
            return _workstation(arguments.image_action)
        if arguments.image_action == "package":
            if len(arguments.directory) > 1:
                return _fail("package accepts at most one output directory")
            paths = arguments.directory
        else:
            paths = arguments.tarfile
        return _package_images(arguments.image_action, paths)
    if arguments.command == "service":
        return _install_service()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

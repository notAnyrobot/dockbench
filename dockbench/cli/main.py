"""Command-line interface for Dockbench."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import uvicorn

from dockbench.core.image_builder import ImageBuilder
from dockbench.core.image_verifier import ImageVerifier
from dockbench.core.images import WorkstationImages
from dockbench.core.recipes import RecipeCatalog
from dockbench.core.server_connection import DEFAULT_SERVER_PORT, connect
from dockbench.core.server_deployment import DeploymentOptions, ServerDeployment, load_runtime_config
from dockbench.core.workstation import FleetManager, SubprocessDockerRunner, Workstation, WorkstationError


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SERVER_INDEX = REPOSITORY_ROOT / "apps/workbench/dist/index.html"


def _fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def _workstation(action: str, image: str | None = None, gpus: list[str] | None = None,
                 replace: bool = False, container_name: str | None = None) -> int:
    try:
        workstation = Workstation()
        if action == "start":
            values = tuple(gpus or ())
            if "none" in values and len(values) != 1:
                raise WorkstationError("--gpu none cannot be combined with other GPU selections")
            result = workstation.start(
                image=image, gpus=tuple(value for value in values if value not in {"all", "none"}),
                all_gpus=None if gpus is None else "all" in values, replace=replace,
            )
        elif action == "shell" and container_name is not None:
            FleetManager(workstation.config, runner=workstation.docker,
                         inventory=workstation.inventory).enter(container_name)
            return 0
        elif action == "shell" and workstation.status().state != "running":
            fleet = FleetManager(workstation.config, runner=workstation.docker,
                                 inventory=workstation.inventory)
            running = tuple(item for item in fleet.containers() if item.state == "running")
            if len(running) > 1:
                names = ", ".join(item.container_name for item in running)
                raise WorkstationError(
                    f"multiple managed containers are running ({names}); "
                    "specify one with `dockbench shell CONTAINER`"
                )
            if len(running) == 1:
                fleet.enter(running[0].container_name)
                return 0
            result = workstation.enter()
        else:
            result = getattr(workstation, {"desktop": "open_vnc", "shell": "enter"}.get(action, action))()
        if action in {"start", "stop", "status"}:
            print(f"{workstation.config.container_name}: {result.state}")
        return 0
    except WorkstationError as exc:
        return _fail(str(exc))


def _package_images(action: str, paths: list[str]) -> int:
    try:
        if action == "export":
            result = WorkstationImages().package(paths[0] if paths else None)
            print(f"Created:\n  {result.archive}")
            return 0
        WorkstationImages().load(paths)
        return 0
    except (OSError, subprocess.CalledProcessError, WorkstationError) as exc:
        return _fail(str(exc))


def _recipe_catalog() -> RecipeCatalog:
    return RecipeCatalog.for_repository(REPOSITORY_ROOT)


def _docker_runner() -> SubprocessDockerRunner:
    return SubprocessDockerRunner(os.environ.get("DOCKBENCH_DOCKER", "docker"))


def _build_recipe(recipe_id: str, *, tag: str | None = None, target: str | None = None,
                  platform: str | None = None, no_cache: bool = False) -> int:
    try:
        recipe = _recipe_catalog().get(recipe_id)
        overrides: dict[str, object] = {"no_cache": no_cache}
        for name, value in (("tag", tag), ("target", target), ("platform", platform)):
            if value is not None:
                overrides[name] = value
        result = ImageBuilder(_docker_runner()).build(
            recipe, on_progress=lambda line: print(line, flush=True), **overrides,
        )
        print(f"{result.tag}: image built from {recipe.id} revision {recipe.manifest.revision}")
        return 0
    except WorkstationError as exc:
        return _fail(str(exc))


def _verify_image(image: str) -> int:
    try:
        result = ImageVerifier(_docker_runner()).verify(image)
        print(f"{result.image}: verified ({', '.join(result.checks)})")
        return 0
    except WorkstationError as exc:
        return _fail(str(exc))


def _deployment(port: int = DEFAULT_SERVER_PORT, workspace_root: str | None = None,
                state_root: str | None = None, docker_command: str | None = None) -> ServerDeployment:
    return ServerDeployment(DeploymentOptions(
        repository_root=REPOSITORY_ROOT, port=port,
        workspace_root=Path(workspace_root).expanduser() if workspace_root else None,
        state_root=Path(state_root).expanduser() if state_root else None,
        docker_command=docker_command,
    ))


def _deploy(port: int = DEFAULT_SERVER_PORT, workspace_root: str | None = None,
            state_root: str | None = None, docker_command: str | None = None) -> int:
    try:
        print("Building and deploying Dockbench…")
        result = _deployment(port, workspace_root, state_root, docker_command).deploy()
        print(f"Dockbench deployed with {result.manager}: {result.url}")
        if result.log_path:
            print(f"Log: {result.log_path}")
        elif result.manager == "systemd":
            print("Logs: journalctl --user -u dockbench.service")
        return 0
    except (OSError, WorkstationError) as exc:
        return _fail(str(exc))


def _server_status(action: str) -> int:
    try:
        status = getattr(_deployment(), action)()
        print(f"Dockbench: {status.state} ({status.manager or 'unmanaged'}) — {status.message}")
        if status.log_path:
            print(f"Log: {status.log_path}")
        elif status.manager == "systemd":
            print("Logs: journalctl --user -u dockbench.service")
        return 0 if status.state in {"running", "stopped", "absent"} else 1
    except (OSError, WorkstationError) as exc:
        return _fail(str(exc))


def _connect(ssh_host: str, local_port: int | None, remote_port: int, open_browser: bool) -> int:
    try:
        result = connect(
            ssh_host, local_port=local_port, remote_port=remote_port, open_browser=open_browser,
            on_ready=lambda url: print(f"Dockbench: {url}\nPress Ctrl+C to close the SSH tunnel."),
        )
        return 0 if result.interrupted else 1
    except (OSError, WorkstationError) as exc:
        return _fail(str(exc))


def _serve(port: int = DEFAULT_SERVER_PORT, config: str | Path | None = None) -> int:
    if not SERVER_INDEX.is_file():
        return _fail("Dockbench frontend is not built. Run `dockbench deploy` or:\n"
                     "  npm ci --prefix apps/workbench\n  npm run --prefix apps/workbench build")
    try:
        if config is not None:
            os.environ.update(load_runtime_config(Path(config).expanduser()))
        uvicorn.run("dockbench.web.app:app", host="127.0.0.1", port=port, proxy_headers=False)
        return 0
    except (OSError, WorkstationError) as exc:
        return _fail(str(exc))


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer between 1 and 65535")
    return port


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="dockbench", description="Manage Dockbench.")
    actions = command.add_subparsers(dest="command", metavar="COMMAND")
    deploy = actions.add_parser("deploy", help="Build and deploy Dockbench on this Docker host.")
    deploy.add_argument("--port", type=_port, default=DEFAULT_SERVER_PORT)
    deploy.add_argument("--workspace", metavar="PATH",
                        help="Host workspace root mounted at /workspace (default: /data/$USER/workspace on remote hosts).")
    deploy.add_argument("--state-root", help="Host directory for persistent Dockbench state.")
    deploy.add_argument("--docker-command", help="Docker-compatible command used on the remote host.")
    connect_command = actions.add_parser("connect", help="Open a local SSH tunnel to a deployed Dockbench.")
    connect_command.add_argument("ssh_host", help="SSH host, user@host, or configured SSH alias.")
    connect_command.add_argument("--local-port", type=_port, default=None)
    connect_command.add_argument("--remote-port", type=_port, default=DEFAULT_SERVER_PORT)
    browser = connect_command.add_mutually_exclusive_group()
    browser.add_argument("--open-browser", action="store_true")
    browser.add_argument("--no-open", dest="open_browser", action="store_false", help=argparse.SUPPRESS)
    serve = actions.add_parser("serve", help="Serve Dockbench on loopback.")
    serve.add_argument("--port", type=_port, default=DEFAULT_SERVER_PORT)
    serve.add_argument("--config", help="Deployment runtime configuration file.")
    server = actions.add_parser("server", help="Manage an already deployed Dockbench server.")
    server_actions = server.add_subparsers(dest="server_action", required=True, metavar="ACTION")
    for action, description in {
        "start": "Start the deployed Dockbench server without rebuilding.",
        "status": "Show deployed Dockbench server status.",
        "stop": "Stop the deployed Dockbench server.",
    }.items():
        server_actions.add_parser(action, help=description)
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
    image = actions.add_parser("image", help="Build, verify, export, or import images.")
    image_actions = image.add_subparsers(dest="image_action", required=True, metavar="ACTION")
    build = image_actions.add_parser("build", help="Build a managed image recipe using Docker's layer cache.")
    build.add_argument("recipe", nargs="?", default="android-ws", help="Recipe id (default: android-ws).")
    build.add_argument("--tag", help="Override the recipe's output image tag for this build.")
    build.add_argument("--target", help="Override the recipe's Dockerfile target for this build.")
    build.add_argument("--platform", help="Override the recipe's target platform for this build.")
    build.add_argument("--no-cache", action="store_true", help="Build without using cached layers.")
    image_actions.add_parser("rebuild", help="Build the image, replace the container, and start it again.")
    verify = image_actions.add_parser("verify", help="Verify an image's advertised workstation capabilities.")
    verify.add_argument("image", help="Local image reference or id to verify.")
    export = image_actions.add_parser("export", help="Save the desktop image as a Docker tar file.")
    export.add_argument("directory", nargs="?")
    image_import = image_actions.add_parser("import", help="Load one or more Docker image tar files.")
    image_import.add_argument("tarfile", nargs="+")
    return command


def main(argv: Sequence[str] | None = None) -> int:
    command = parser()
    arguments = command.parse_args(argv)
    if arguments.command is None:
        command.print_help()
        return 0
    if arguments.command in {"start", "shell", "desktop", "stop", "status"}:
        if arguments.command == "start":
            return _workstation("start", arguments.image, arguments.gpu, arguments.replace)
        if arguments.command == "shell" and arguments.container is not None:
            return _workstation("shell", container_name=arguments.container)
        return _workstation(arguments.command)
    if arguments.command == "deploy":
        return _deploy(arguments.port, arguments.workspace, arguments.state_root, arguments.docker_command)
    if arguments.command == "connect":
        return _connect(arguments.ssh_host, arguments.local_port, arguments.remote_port, arguments.open_browser)
    if arguments.command == "serve":
        return _serve(arguments.port, arguments.config)
    if arguments.command == "server":
        return _server_status(arguments.server_action)
    if arguments.command == "image":
        if arguments.image_action == "build":
            return _build_recipe(arguments.recipe, tag=arguments.tag, target=arguments.target,
                                 platform=arguments.platform, no_cache=arguments.no_cache)
        if arguments.image_action == "rebuild":
            return _workstation("rebuild")
        if arguments.image_action == "verify":
            return _verify_image(arguments.image)
        if arguments.image_action == "export":
            return _package_images("export", [arguments.directory] if arguments.directory else [])
        return _package_images("import", arguments.tarfile)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

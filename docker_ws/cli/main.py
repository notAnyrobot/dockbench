"""Repository-first command line for Docker Workstation."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import uvicorn

from docker_ws.core.workstation import SubprocessDockerRunner, Workstation, WorkstationError
from docker_ws.core.host_inventory import HostInventory
from docker_ws.core.image_builder import ImageBuilder
from docker_ws.core.image_verifier import ImageVerifier
from docker_ws.core.images import WorkstationImages
from docker_ws.core.recipes import DEFAULT_PLATFORM, RecipeCatalog
from docker_ws.core.workbench_connection import DEFAULT_WORKBENCH_PORT, connect as connect_workbench
from docker_ws.core.workbench_deployment import DeploymentOptions, WorkbenchDeployment, load_runtime_config


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKBENCH_INDEX = REPOSITORY_ROOT / "apps/workbench/dist/index.html"


def _fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def _workstation(action: str, image: str | None = None, gpus: list[str] | None = None,
                 replace: bool = False) -> int:
    try:
        workstation = Workstation()
        if action == "start":
            values = tuple(gpus or ())
            if "none" in values and len(values) != 1:
                raise WorkstationError("--gpu none cannot be combined with other GPU selections")
            result = workstation.start(image=image, gpus=tuple(value for value in values if value not in {"all", "none"}),
                all_gpus=None if gpus is None else "all" in values, replace=replace)
        else:
            result = getattr(workstation, {"vnc": "open_vnc"}.get(action, action))()
        if action in {"start", "stop", "status"}:
            print(f"{workstation.config.container_name}: {result.state}")
        return 0
    except WorkstationError as exc:
        return _fail(str(exc))


def _host_inventory(kind: str, as_json: bool) -> int:
    try:
        workstation = Workstation()
        data = HostInventory(workstation.docker).inventory().public()
        value = data["images" if kind == "images" else "gpus"]
        if as_json:
            import json
            print(json.dumps(value, indent=2))
        elif kind == "images":
            for image in value:
                desktop = " desktop-v1" if image["desktop_capable"] else " shell-only"
                print(f"{image['display_reference']}\t{image['id']}\t{image['size']} bytes{desktop}")
        else:
            if data["gpu_diagnostic"]: print(f"GPU unavailable: {data['gpu_diagnostic']}")
            for gpu in value: print(f"{gpu['index']}\t{gpu['uuid']}\t{gpu['name']}\t{gpu['memory_total_mib']} MiB")
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


def _recipe_catalog() -> RecipeCatalog:
    return RecipeCatalog.for_repository(REPOSITORY_ROOT)


def _docker_runner() -> SubprocessDockerRunner:
    return SubprocessDockerRunner(os.environ.get("ROBOTICS_WS_DOCKER", "docker"))


def _recipe_action(action: str, recipe_id: str | None = None, dockerfile: str | None = None,
                   **defaults: object) -> int:
    try:
        catalog = _recipe_catalog()
        if action == "list":
            for recipe in catalog.list():
                manifest = recipe.manifest
                target = manifest.target or "default target"
                print(f"{manifest.id}\tv{manifest.revision}\t{manifest.tag}\t{target}\t{manifest.platform}")
            return 0
        if recipe_id is None or dockerfile is None:
            raise WorkstationError(f"recipe {action} requires an id and Dockerfile")
        content = Path(dockerfile).read_bytes()
        recipe = (catalog.create(recipe_id, content, **defaults)
                  if action == "add" else catalog.revise(recipe_id, content, **defaults))
        print(f"{recipe.id}: recipe revision {recipe.manifest.revision} saved")
        return 0
    except (OSError, WorkstationError) as exc:
        return _fail(str(exc))


def _build_recipe(recipe_id: str, *, tag: str | None = None, target: str | None = None,
                  platform: str | None = None, no_cache: bool = False) -> int:
    try:
        catalog = _recipe_catalog()
        recipe = catalog.get(recipe_id)
        overrides: dict[str, object] = {"no_cache": no_cache}
        if tag is not None:
            overrides["tag"] = tag
        if target is not None:
            overrides["target"] = target
        if platform is not None:
            overrides["platform"] = platform
        result = ImageBuilder(_docker_runner()).build(recipe, **overrides)
        print(f"{result.tag}: image built from {recipe.id} revision {recipe.manifest.revision}")
        return 0
    except WorkstationError as exc:
        return _fail(str(exc))


def _verify_image(image: str) -> int:
    try:
        result = ImageVerifier(_docker_runner()).verify(image)
        capabilities = ", ".join(result.checks)
        print(f"{result.image}: verified ({capabilities})")
        return 0
    except WorkstationError as exc:
        return _fail(str(exc))


def _deployment(port: int = DEFAULT_WORKBENCH_PORT, code_root: str | None = None,
                state_root: str | None = None, docker_command: str | None = None) -> WorkbenchDeployment:
    return WorkbenchDeployment(DeploymentOptions(
        repository_root=REPOSITORY_ROOT,
        port=port,
        code_root=Path(code_root).expanduser() if code_root else None,
        state_root=Path(state_root).expanduser() if state_root else None,
        docker_command=docker_command,
    ))


def _deploy_workbench(port: int = DEFAULT_WORKBENCH_PORT, code_root: str | None = None,
                      state_root: str | None = None, docker_command: str | None = None) -> int:
    try:
        print("Building and deploying Docker Workbench…")
        result = _deployment(port, code_root, state_root, docker_command).deploy()
        print(f"Docker Workbench deployed with {result.manager}: {result.url}")
        if result.log_path:
            print(f"Log: {result.log_path}")
        elif result.manager == "systemd":
            print("Logs: journalctl --user -u docker-ws-workbench.service")
        return 0
    except (OSError, WorkstationError) as exc:
        return _fail(str(exc))


def _workbench_status(action: str) -> int:
    try:
        deployment = _deployment()
        status = deployment.stop() if action == "stop" else deployment.status()
        print(f"Docker Workbench: {status.state} ({status.manager or 'unmanaged'}) — {status.message}")
        if status.log_path:
            print(f"Log: {status.log_path}")
        elif status.manager == "systemd":
            print("Logs: journalctl --user -u docker-ws-workbench.service")
        return 0 if status.state in {"running", "stopped", "absent"} else 1
    except (OSError, WorkstationError) as exc:
        return _fail(str(exc))


def _connect_workbench(ssh_host: str, local_port: int | None, remote_port: int,
                       open_browser: bool) -> int:
    try:
        result = connect_workbench(
            ssh_host,
            local_port=local_port,
            remote_port=remote_port,
            open_browser=open_browser,
            on_ready=lambda url: print(f"Docker Workbench: {url}\nPress Ctrl+C to close the SSH tunnel."),
        )
        return 0 if result.interrupted else 1
    except (OSError, WorkstationError) as exc:
        return _fail(str(exc))


def _workbench(port: int = DEFAULT_WORKBENCH_PORT, config: str | Path | None = None) -> int:
    if not WORKBENCH_INDEX.is_file():
        return _fail("Workbench frontend is not built. Run `docker-ws workbench deploy` or:\n  npm ci --prefix apps/workbench\n  npm run --prefix apps/workbench build")
    try:
        if config is not None:
            os.environ.update(load_runtime_config(Path(config).expanduser()))
        uvicorn.run("docker_ws.web.app:app", host="127.0.0.1", port=port, proxy_headers=False)
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
    command = argparse.ArgumentParser(prog="docker-ws", description="Manage the Docker Workstation.")
    actions = command.add_subparsers(dest="command", required=True, metavar="COMMAND")
    actions.add_parser("help", help="Show this help message.", description="Show the Docker Workstation command overview.")
    workbench = actions.add_parser("workbench", help="Deploy, serve, or connect to Docker Workbench.")
    workbench_actions = workbench.add_subparsers(dest="workbench_action", metavar="ACTION")
    workbench_actions.add_parser("help", help="Show Workbench command help.").set_defaults(help_parser=workbench)
    serve = workbench_actions.add_parser("serve", help="Serve Workbench on remote loopback.")
    serve.add_argument("--port", type=_port, default=DEFAULT_WORKBENCH_PORT)
    serve.add_argument("--config", help="Deployment runtime configuration file.")
    deploy = workbench_actions.add_parser("deploy", help="Build and deploy Workbench on this Docker host.")
    deploy.add_argument("--port", type=_port, default=DEFAULT_WORKBENCH_PORT)
    deploy.add_argument("--code-root", help="Host project directory mounted into managed containers.")
    deploy.add_argument("--state-root", help="Host directory for persistent Workstation state.")
    deploy.add_argument("--docker-command", help="Docker-compatible command used on the remote host.")
    connect = workbench_actions.add_parser("connect", help="Open a local SSH tunnel to a deployed Workbench.")
    connect.add_argument("ssh_host", help="SSH host, user@host, or configured SSH alias.")
    connect.add_argument("--local-port", type=_port, default=None, help="Explicit local loopback port; defaults to an available port.")
    connect.add_argument("--remote-port", type=_port, default=DEFAULT_WORKBENCH_PORT, help="Remote Workbench loopback port.")
    connect.add_argument("--no-open", action="store_true", help="Do not open the local browser automatically.")
    workbench_actions.add_parser("status", help="Show deployed Workbench service status.")
    workbench_actions.add_parser("stop", help="Stop the deployed Workbench service.")
    container = actions.add_parser("container", help="Manage the workstation container.")
    container_actions = container.add_subparsers(dest="container_action", required=True, metavar="ACTION")
    container_actions.add_parser("help", help="Show container command help.").set_defaults(help_parser=container)
    for action, description in {
        "start": "Create or start the workstation without configuring VNC.",
        "enter": "Open Bash in the running workstation as the host user.",
        "stop": "Stop the workstation without removing it.",
        "status": "Print workstation state.",
        "vnc": "Provision VNC if needed and open the native viewer.",
    }.items():
        command_action = container_actions.add_parser(action, help=description, description=description)
        if action == "start":
            command_action.add_argument("--image", help="Tagged local image to use when creating the workstation.")
            command_action.add_argument("--gpu", action="append", default=None, metavar="UUID_OR_INDEX", help="GPU UUID/index, 'all' (default), or 'none'; repeat for multiple GPUs.")
            command_action.add_argument("--replace", action="store_true", help="Replace a container whose immutable image/GPU launch request differs.")
    gpus = actions.add_parser("gpus", help="List NVIDIA GPUs available to Docker.", description="List NVIDIA GPUs available to Docker.")
    gpus.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    image = actions.add_parser("image", help="Build, replace, package, or load images.")
    image_actions = image.add_subparsers(dest="image_action", required=True, metavar="ACTION")
    image_actions.add_parser("help", help="Show image command help.").set_defaults(help_parser=image)
    image_list = image_actions.add_parser("list", help="List tagged local images.")
    image_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    build = image_actions.add_parser("build", help="Build a managed image recipe using Docker's layer cache.")
    build.add_argument("recipe", nargs="?", default="android-ws", help="Recipe id (default: android-ws).")
    build.add_argument("--tag", help="Override the recipe's output image tag for this build.")
    build.add_argument("--target", help="Override the recipe's Dockerfile target for this build.")
    build.add_argument("--platform", help="Override the recipe's target platform for this build.")
    build.add_argument("--no-cache", action="store_true", help="Build without using cached layers.")
    image_actions.add_parser("rebuild", help="Build the image, replace the container, and start it again.")
    verify = image_actions.add_parser("verify", help="Verify an image's advertised workstation capabilities.")
    verify.add_argument("image", help="Local image reference or id to verify.")
    recipe = image_actions.add_parser("recipe", help="List, add, or revise managed image recipes.")
    recipe_actions = recipe.add_subparsers(dest="recipe_action", required=True, metavar="ACTION")
    recipe_actions.add_parser("list", help="List managed image recipes.")
    add_recipe = recipe_actions.add_parser("add", help="Add a Dockerfile as a new managed recipe.")
    add_recipe.add_argument("id", help="Lowercase kebab-case recipe id.")
    add_recipe.add_argument("dockerfile", help="UTF-8 Dockerfile to import.")
    add_recipe.add_argument("--tag", required=True, help="Default output image tag.")
    add_recipe.add_argument("--target", default=None, help="Default Dockerfile target.")
    add_recipe.add_argument("--platform", default=DEFAULT_PLATFORM, help="Default target platform.")
    revise_recipe = recipe_actions.add_parser("revise", help="Add the next Dockerfile revision to a recipe.")
    revise_recipe.add_argument("id", help="Existing recipe id.")
    revise_recipe.add_argument("dockerfile", help="UTF-8 Dockerfile to import.")
    revise_recipe.add_argument("--tag", default=argparse.SUPPRESS, help="Replace the default output image tag.")
    revise_recipe.add_argument("--target", default=argparse.SUPPRESS, help="Replace the default Dockerfile target.")
    revise_recipe.add_argument("--platform", default=argparse.SUPPRESS, help="Replace the default target platform.")
    package = image_actions.add_parser("package", help="Save the desktop image as a Docker tar file.")
    package.add_argument("directory", nargs="*")
    load = image_actions.add_parser("load", help="Load one or more Docker image tar files.")
    load.add_argument("tarfile", nargs="+")
    service = actions.add_parser("service", help="Manage the Workbench user service.")
    service_actions = service.add_subparsers(dest="service_action", required=True, metavar="ACTION")
    service_actions.add_parser("help", help="Show service command help.").set_defaults(help_parser=service)
    service_actions.add_parser("install", help="Install and start the user-level Workbench service.")
    return command


def main(argv: Sequence[str] | None = None) -> int:
    command = parser()
    arguments = command.parse_args(argv)
    if arguments.command == "help":
        command.print_help()
        return 0
    if arguments.command == "container":
        if arguments.container_action == "help":
            arguments.help_parser.print_help()
            return 0
        if arguments.container_action == "start":
            return _workstation("start", arguments.image, arguments.gpu, arguments.replace)
        return _workstation(arguments.container_action)
    if arguments.command == "gpus":
        return _host_inventory("gpus", arguments.json)
    if arguments.command == "workbench":
        action = arguments.workbench_action or "serve"
        if action == "help":
            arguments.help_parser.print_help()
            return 0
        if action == "serve":
            return _workbench(getattr(arguments, "port", DEFAULT_WORKBENCH_PORT), getattr(arguments, "config", None))
        if action == "deploy":
            return _deploy_workbench(arguments.port, arguments.code_root, arguments.state_root, arguments.docker_command)
        if action == "connect":
            return _connect_workbench(arguments.ssh_host, arguments.local_port, arguments.remote_port, not arguments.no_open)
        return _workbench_status(action)
    if arguments.command == "image":
        if arguments.image_action == "help":
            arguments.help_parser.print_help()
            return 0
        if arguments.image_action == "list":
            return _host_inventory("images", arguments.json)
        if arguments.image_action == "build":
            return _build_recipe(arguments.recipe, tag=arguments.tag, target=arguments.target,
                                 platform=arguments.platform, no_cache=arguments.no_cache)
        if arguments.image_action == "rebuild":
            return _workstation("rebuild")
        if arguments.image_action == "verify":
            return _verify_image(arguments.image)
        if arguments.image_action == "recipe":
            if arguments.recipe_action == "list":
                return _recipe_action("list")
            defaults = {name: getattr(arguments, name) for name in ("tag", "target", "platform")
                        if hasattr(arguments, name)}
            return _recipe_action(arguments.recipe_action, arguments.id, arguments.dockerfile, **defaults)
        if arguments.image_action == "package":
            if len(arguments.directory) > 1:
                return _fail("package accepts at most one output directory")
            paths = arguments.directory
        else:
            paths = arguments.tarfile
        return _package_images(arguments.image_action, paths)
    if arguments.command == "service":
        if arguments.service_action == "help":
            arguments.help_parser.print_help()
            return 0
        return _deploy_workbench()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

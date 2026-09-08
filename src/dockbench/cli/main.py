"""Command-line interface for Dockbench."""
from __future__ import annotations

import argparse
import sys
from typing import Sequence

from dockbench.cli import archives, connect, containers, deploy, serve, server
from dockbench.cli.containers import run as _workstation

from dockbench.core.resources import CheckoutResources
from dockbench.core.backend import Backend
from dockbench.core.errors import WorkstationError


RESOURCES = CheckoutResources.discover()


def _fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1



def _build_recipe(recipe_id: str, *, tag: str | None = None, target: str | None = None,
                  platform: str | None = None, no_cache: bool = False, backend: Backend | None = None) -> int:
    try:
        backend = backend if backend is not None else Backend()
        recipe = backend.recipes.get(recipe_id)
        overrides: dict[str, object] = {"no_cache": no_cache}
        for name, value in (("tag", tag), ("target", target), ("platform", platform)):
            if value is not None:
                overrides[name] = value
        result = backend.image_builder.build(
            recipe, on_progress=lambda line: print(line, flush=True), **overrides,
        )
        print(f"{result.tag}: image built from {recipe.id} revision {recipe.manifest.revision}")
        return 0
    except WorkstationError as exc:
        return _fail(str(exc))


def _verify_image(image: str, *, backend: Backend | None = None) -> int:
    try:
        backend = backend if backend is not None else Backend()
        result = backend.image_verifier.verify(image)
        print(f"{result.image}: verified ({', '.join(result.checks)})")
        return 0
    except WorkstationError as exc:
        return _fail(str(exc))


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="dockbench", description="Manage Dockbench.")
    actions = command.add_subparsers(dest="command", metavar="COMMAND")
    for commands in (deploy, connect, serve, server):
        commands.register(actions)
    containers.register(actions)
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
    archives.register(image_actions)
    return command


def main(argv: Sequence[str] | None = None, *, backend: Backend | None = None) -> int:
    command = parser()
    arguments = command.parse_args(argv)
    if arguments.command is None:
        command.print_help()
        return 0
    if hasattr(arguments, "handler"):
        return arguments.handler(arguments)
    backend = backend if backend is not None else Backend()
    if arguments.command in {"start", "shell", "desktop", "stop", "status"}:
        if arguments.command == "start":
            return _workstation("start", arguments.image, arguments.gpu, arguments.replace, backend=backend)
        if arguments.command == "shell" and arguments.container is not None:
            return _workstation("shell", container_name=arguments.container, backend=backend)
        return _workstation(arguments.command, backend=backend)
    if arguments.command == "image":
        if arguments.image_action == "build":
            return _build_recipe(arguments.recipe, tag=arguments.tag, target=arguments.target,
                                 platform=arguments.platform, no_cache=arguments.no_cache, backend=backend)
        if arguments.image_action == "rebuild":
            return _workstation("rebuild", backend=backend)
        if arguments.image_action == "verify":
            return _verify_image(arguments.image, backend=backend)
        if arguments.image_action == "export":
            return archives.run("export", [arguments.directory] if arguments.directory else [], backend=backend)
        return archives.run("import", arguments.tarfile, backend=backend)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

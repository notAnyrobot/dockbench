"""Command-line interface for Dockbench."""
from __future__ import annotations

import argparse
from typing import Sequence

from dockbench.cli import archives, containers, recipes, server, web
from dockbench.cli.containers import run as _workstation
from dockbench.core.backend import Backend


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="dockbench", description="Manage Dockbench.")
    actions = command.add_subparsers(dest="command", metavar="COMMAND")
    for commands in (server, web):
        commands.register(actions)
    containers.register(actions)
    image = actions.add_parser("image", help="Build, verify, export, or import images.")
    image_actions = image.add_subparsers(dest="image_action", required=True, metavar="ACTION")
    recipes.register(image_actions)
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
            return recipes.build(arguments.recipe, tag=arguments.tag, target=arguments.target,
                                 platform=arguments.platform, no_cache=arguments.no_cache, backend=backend)
        if arguments.image_action == "rebuild":
            return recipes.rebuild(backend=backend)
        if arguments.image_action == "verify":
            return recipes.verify(arguments.image, backend=backend)
        if arguments.image_action == "export":
            return archives.run("export", [arguments.directory] if arguments.directory else [], backend=backend)
        return archives.run("import", arguments.tarfile, backend=backend)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

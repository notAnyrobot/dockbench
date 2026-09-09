"""Image archive command adaptation."""
import argparse
import subprocess

from dockbench.core.backend import Backend
from dockbench.core.errors import WorkstationError
from dockbench.cli.common import fail


def run(action: str, paths: list[str], *, backend: Backend | None = None) -> int:
    try:
        backend = backend if backend is not None else Backend()
        if action == "export":
            result = backend.images.package(paths[0] if paths else None)
            print(f"Created:\n  {result.archive}")
            return 0
        backend.images.load(paths)
        return 0
    except (OSError, subprocess.CalledProcessError, WorkstationError) as exc:
        return fail(str(exc))



def register(image_actions: argparse._SubParsersAction) -> None:
    export = image_actions.add_parser("export", help="Save the desktop image as a Docker tar file.")
    export.add_argument("directory", nargs="?")
    image_import = image_actions.add_parser("import", help="Load one or more Docker image tar files.")
    image_import.add_argument("tarfile", nargs="+")

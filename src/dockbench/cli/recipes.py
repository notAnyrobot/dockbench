"""CLI recipe builds, verification, and explicit default-container rebuilds."""
from __future__ import annotations

import argparse

from dockbench.cli.common import fail
from dockbench.core.backend import Backend
from dockbench.core.errors import WorkstationError


def build(recipe_id: str, *, tag: str | None = None, target: str | None = None,
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
        return fail(str(exc))


def verify(image: str, *, backend: Backend | None = None) -> int:
    try:
        backend = backend if backend is not None else Backend()
        result = backend.image_verifier.verify(image)
        print(f"{result.image}: verified ({', '.join(result.checks)})")
        return 0
    except WorkstationError as exc:
        return fail(str(exc))


def rebuild(*, backend: Backend) -> int:
    try:
        backend.workstation.rebuild(
            on_progress=lambda line: print(line, flush=True),
            on_built=lambda result: print(f"{result.tag}: image built"),
        )
        return 0
    except WorkstationError as exc:
        return fail(str(exc))


def register(actions: argparse._SubParsersAction) -> None:
    build = actions.add_parser("build", help="Build a managed image recipe using Docker's layer cache.")
    build.add_argument("recipe", nargs="?", default="android-ws", help="Recipe id (default: android-ws).")
    build.add_argument("--tag", help="Override the recipe's output image tag for this build.")
    build.add_argument("--target", help="Override the recipe's Dockerfile target for this build.")
    build.add_argument("--platform", help="Override the recipe's target platform for this build.")
    build.add_argument("--no-cache", action="store_true", help="Build without using cached layers.")
    actions.add_parser("rebuild", help="Build the image, replace the container, and start it again.")
    verify = actions.add_parser("verify", help="Verify an image's advertised workstation capabilities.")
    verify.add_argument("image", help="Local image reference or id to verify.")

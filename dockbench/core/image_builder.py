"""Shared Docker buildx invocation for managed image recipes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from dockbench.core.recipes import ImageRecipe, RecipeCatalog, RecipeError, UNSET, _Unset, resolve_manifest_overrides


class DockerRunner(Protocol):
    def run(self, args: list[str], *, input: str | None = None, capture: bool = False,
            check: bool = True, on_output: Callable[[str], None] | None = None) -> str: ...


@dataclass(frozen=True)
class ImageBuildResult:
    recipe: ImageRecipe
    tag: str
    target: str | None
    platform: str
    no_cache: bool
    command: tuple[str, ...]


class ImageBuilder:
    """Build a catalog recipe without allowing arbitrary build contexts."""

    def __init__(self, docker: DockerRunner) -> None:
        self.docker = docker

    def build(
        self,
        recipe: ImageRecipe,
        *,
        tag: str | _Unset = UNSET,
        target: str | None | _Unset = UNSET,
        platform: str | _Unset = UNSET,
        no_cache: bool = False,
        on_progress: Callable[[str], None] | None = None,
    ) -> ImageBuildResult:
        # The recipe itself carries a context directory.  Constructing a fresh
        # catalog from its parent prevents callers from accidentally building a
        # stale manifest or a directory that became unsafe after it was listed.
        recipe = RecipeCatalog(recipe.directory.parent).validate_recipe(recipe)
        if not isinstance(no_cache, bool):
            raise RecipeError("no_cache must be a boolean")
        # This public helper reuses the manifest validation used for uploaded
        # data, so Docker never receives unchecked web/CLI input.
        validated = resolve_manifest_overrides(recipe.manifest, tag=tag, target=target, platform=platform)
        command = ["buildx", "build", "--progress=plain", "--platform", validated.platform, "--file", str(recipe.dockerfile_path)]
        if validated.target is not None:
            command.extend(["--target", validated.target])
        if no_cache:
            command.append("--no-cache")
        command.extend(["--load", "--tag", validated.tag, str(recipe.directory)])
        self.docker.run(command, on_output=on_progress)
        return ImageBuildResult(recipe, validated.tag, validated.target, validated.platform, no_cache, tuple(command))

"""Safe, versioned Docker build recipes stored in ``assets/images``.

The catalog is intentionally independent of the CLI and HTTP layers.  Both
callers receive immutable recipe records and expected failures as
``WorkstationError`` instances, which are already safe to show to a user.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dockbench.core.errors import WorkstationError


RECIPE_SCHEMA_VERSION: Final = 1
DEFAULT_PLATFORM: Final = "linux/amd64"
MAX_DOCKERFILE_BYTES: Final = 1024 * 1024
MAX_MANIFEST_BYTES: Final = 64 * 1024
_RECIPE_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")


class RecipeError(WorkstationError):
    """A recipe was invalid, missing, or unsafe to use."""


class _Unset:
    pass


UNSET: Final = _Unset()


@dataclass(frozen=True)
class RecipeManifest:
    """The portable, JSON-serializable part of an image recipe."""

    schema_version: int
    id: str
    revision: int
    dockerfile: str
    tag: str
    target: str | None
    platform: str

    @property
    def expected_dockerfile(self) -> str:
        return dockerfile_filename(self.id, self.revision)

    def public(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "revision": self.revision,
            "dockerfile": self.dockerfile,
            "tag": self.tag,
            "target": self.target,
            "platform": self.platform,
        }


@dataclass(frozen=True)
class ImageRecipe:
    """A validated recipe manifest together with its safe build context."""

    manifest: RecipeManifest
    directory: Path

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def dockerfile_path(self) -> Path:
        return self.directory / self.manifest.dockerfile

    def public(self) -> dict[str, object]:
        return self.manifest.public()


def dockerfile_filename(recipe_id: str, revision: int) -> str:
    """Return the only permitted Dockerfile name for a recipe revision."""
    validate_recipe_id(recipe_id)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise RecipeError("recipe revision must be a positive integer")
    return f"Dockerfile.{recipe_id}-v{revision}"


def validate_recipe_id(recipe_id: str) -> str:
    if not isinstance(recipe_id, str) or not _RECIPE_ID.fullmatch(recipe_id):
        raise RecipeError("recipe id must be lowercase kebab-case")
    return recipe_id


def _validate_text(value: object, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or any(character.isspace() for character in value):
        suffix = " or null" if nullable else ""
        raise RecipeError(f"recipe {field} must be a non-empty whitespace-free string{suffix}")
    return value


def manifest_from_mapping(data: object) -> RecipeManifest:
    """Validate external JSON data before it reaches a Docker command."""
    if not isinstance(data, dict):
        raise RecipeError("recipe manifest must be a JSON object")
    expected = {"schema_version", "id", "revision", "dockerfile", "tag", "target", "platform"}
    if set(data) != expected:
        raise RecipeError("recipe manifest fields must be schema_version, id, revision, dockerfile, tag, target, and platform")
    schema_version = data["schema_version"]
    if isinstance(schema_version, bool) or schema_version != RECIPE_SCHEMA_VERSION:
        raise RecipeError(f"unsupported recipe schema version: {schema_version!r}")
    recipe_id = validate_recipe_id(data["id"])
    revision = data["revision"]
    filename = dockerfile_filename(recipe_id, revision)
    dockerfile = _validate_text(data["dockerfile"], "dockerfile")
    if dockerfile != filename:
        raise RecipeError(f"recipe dockerfile must be {filename}")
    return RecipeManifest(
        schema_version=RECIPE_SCHEMA_VERSION,
        id=recipe_id,
        revision=revision,
        dockerfile=dockerfile,
        tag=_validate_text(data["tag"], "tag"),  # type: ignore[arg-type]
        target=_validate_text(data["target"], "target", nullable=True),
        platform=_validate_text(data["platform"], "platform"),  # type: ignore[arg-type]
    )


def resolve_manifest_overrides(
    manifest: RecipeManifest,
    *,
    tag: str | _Unset = UNSET,
    target: str | None | _Unset = UNSET,
    platform: str | _Unset = UNSET,
) -> RecipeManifest:
    """Validate and apply build-time overrides without changing a recipe."""
    return manifest_from_mapping(
        {
            "schema_version": manifest.schema_version,
            "id": manifest.id,
            "revision": manifest.revision,
            "dockerfile": manifest.dockerfile,
            "tag": manifest.tag if tag is UNSET else tag,
            "target": manifest.target if target is UNSET else target,
            "platform": manifest.platform if platform is UNSET else platform,
        }
    )


def _decode_dockerfile(content: str | bytes) -> bytes:
    if isinstance(content, str):
        encoded = content.encode("utf-8")
    elif isinstance(content, bytes):
        encoded = content
        try:
            encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RecipeError("Dockerfile must be UTF-8 text") from exc
    else:
        raise RecipeError("Dockerfile must be UTF-8 text")
    if not encoded:
        raise RecipeError("Dockerfile must not be empty")
    if len(encoded) > MAX_DOCKERFILE_BYTES:
        raise RecipeError(f"Dockerfile exceeds the {MAX_DOCKERFILE_BYTES} byte limit")
    return encoded


class RecipeCatalog:
    """Read and safely mutate an ``assets/images`` recipe catalog."""

    def __init__(self, images_root: str | Path) -> None:
        self.images_root = Path(images_root)

    @classmethod
    def for_repository(cls, repository_root: str | Path) -> "RecipeCatalog":
        return cls(Path(repository_root) / "assets" / "images")

    def list(self) -> tuple[ImageRecipe, ...]:
        if not self.images_root.exists():
            return ()
        self._validate_images_root()
        recipes: list[ImageRecipe] = []
        for candidate in sorted(self.images_root.iterdir(), key=lambda path: path.name):
            if candidate.name.startswith("."):
                continue
            if candidate.is_symlink():
                raise RecipeError(f"recipe path must not be a symlink: {candidate.name}")
            if not candidate.is_dir():
                raise RecipeError(f"recipe catalog entry is not a directory: {candidate.name}")
            recipes.append(self._load(candidate.name))
        return tuple(recipes)

    def get(self, recipe_id: str) -> ImageRecipe:
        return self._load(validate_recipe_id(recipe_id))

    def create(
        self,
        recipe_id: str,
        dockerfile: str | bytes,
        *,
        tag: str,
        target: str | None = None,
        platform: str = DEFAULT_PLATFORM,
    ) -> ImageRecipe:
        recipe_id = validate_recipe_id(recipe_id)
        content = _decode_dockerfile(dockerfile)
        manifest = self._new_manifest(recipe_id, 1, tag=tag, target=target, platform=platform)
        self._ensure_images_root()
        final = self.images_root / recipe_id
        if final.exists() or final.is_symlink():
            raise RecipeError(f"recipe already exists: {recipe_id}")
        temporary = Path(tempfile.mkdtemp(prefix=f".{recipe_id}-", dir=self.images_root))
        try:
            self._write_file(temporary / manifest.dockerfile, content)
            self._write_manifest(temporary / "recipe.json", manifest)
            # Rename is atomic when source and destination share this parent.
            os.replace(temporary, final)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        return self._load(recipe_id)

    def revise(
        self,
        recipe_id: str,
        dockerfile: str | bytes,
        *,
        tag: str | _Unset = UNSET,
        target: str | None | _Unset = UNSET,
        platform: str | _Unset = UNSET,
    ) -> ImageRecipe:
        current = self.get(recipe_id)
        content = _decode_dockerfile(dockerfile)
        manifest = self._new_manifest(current.id, current.manifest.revision + 1, tag=tag, target=target, platform=platform,
                                      base=current.manifest)
        self._validate_recipe_directory(current.directory)
        destination = current.directory / manifest.dockerfile
        if destination.exists() or destination.is_symlink():
            raise RecipeError(f"revision Dockerfile already exists: {destination.name}")
        self._write_file(destination, content, overwrite=False)
        # The manifest is the commit point: readers see either revision N or N+1.
        self._write_manifest(current.directory / "recipe.json", manifest)
        return self._load(current.id)

    def validate_recipe(self, recipe: ImageRecipe) -> ImageRecipe:
        """Re-read and validate a recipe before handing its directory to Docker."""
        current = self._load(recipe.id)
        if current.directory != recipe.directory or current.manifest != recipe.manifest:
            raise RecipeError(f"recipe changed while preparing build: {recipe.id}")
        return current

    def _new_manifest(self, recipe_id: str, revision: int, *, tag: object, target: object, platform: object,
                      base: RecipeManifest | None = None) -> RecipeManifest:
        return manifest_from_mapping(
            {
                "schema_version": RECIPE_SCHEMA_VERSION,
                "id": recipe_id,
                "revision": revision,
                "dockerfile": dockerfile_filename(recipe_id, revision),
                "tag": base.tag if tag is UNSET and base else tag,
                "target": base.target if target is UNSET and base else target,
                "platform": base.platform if platform is UNSET and base else platform,
            }
        )

    def _ensure_images_root(self) -> None:
        self.images_root.mkdir(parents=True, exist_ok=True)
        self._validate_images_root()

    def _validate_images_root(self) -> None:
        if self.images_root.is_symlink() or not self.images_root.is_dir():
            raise RecipeError("recipe catalog root must be a non-symlink directory")

    def _load(self, recipe_id: str) -> ImageRecipe:
        self._validate_images_root()
        directory = self.images_root / recipe_id
        if directory.is_symlink() or not directory.is_dir():
            raise RecipeError(f"recipe does not exist: {recipe_id}")
        self._validate_recipe_directory(directory)
        manifest_path = directory / "recipe.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RecipeError(f"recipe manifest is missing: {recipe_id}")
        try:
            raw = manifest_path.read_bytes()
        except OSError as exc:
            raise RecipeError(f"unable to read recipe manifest: {recipe_id}") from exc
        if len(raw) > MAX_MANIFEST_BYTES:
            raise RecipeError("recipe manifest exceeds the size limit")
        try:
            manifest = manifest_from_mapping(json.loads(raw.decode("utf-8")))
        except UnicodeDecodeError as exc:
            raise RecipeError("recipe manifest must be UTF-8 JSON") from exc
        except json.JSONDecodeError as exc:
            raise RecipeError("recipe manifest is invalid JSON") from exc
        if manifest.id != recipe_id:
            raise RecipeError("recipe manifest id does not match its directory")
        dockerfile = directory / manifest.dockerfile
        if dockerfile.is_symlink() or not dockerfile.is_file():
            raise RecipeError(f"recipe Dockerfile is missing: {manifest.dockerfile}")
        return ImageRecipe(manifest, directory)

    def _validate_recipe_directory(self, directory: Path) -> None:
        """Reject any symlink in the context before Docker can follow it."""
        if directory.is_symlink() or not directory.is_dir():
            raise RecipeError("recipe directory must be a non-symlink directory")
        for parent, directories, files in os.walk(directory, followlinks=False):
            parent_path = Path(parent)
            for name in [*directories, *files]:
                path = parent_path / name
                if path.is_symlink():
                    raise RecipeError(f"recipe build context must not contain symlinks: {path.relative_to(directory)}")
                try:
                    path.resolve(strict=True).relative_to(directory.resolve(strict=True))
                except (OSError, ValueError) as exc:
                    raise RecipeError("recipe build context escapes its directory") from exc

    @staticmethod
    def _write_file(path: Path, content: bytes, *, overwrite: bool = True) -> None:
        temporary = path.with_name(f".{path.name}.tmp-{next(tempfile._get_candidate_names())}")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if overwrite:
                os.replace(temporary, path)
            else:
                # ``link`` atomically refuses to replace an existing revision.
                # Both paths are in the same recipe directory/filesystem.
                os.link(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @classmethod
    def _write_manifest(cls, path: Path, manifest: RecipeManifest) -> None:
        cls._write_file(path, (json.dumps(manifest.public(), indent=2, sort_keys=True) + "\n").encode("utf-8"))

import json
from pathlib import Path

import pytest

from dockbench.core.image_builder import ImageBuilder
from dockbench.core.image_verifier import DESKTOP_V1_COMMANDS, GENERIC_SHELL_CHECK, ImageVerifier
from dockbench.core.recipes import MAX_DOCKERFILE_BYTES, RecipeCatalog, RecipeError, UNSET, dockerfile_filename


class FakeDocker:
    def __init__(self, label=""):
        self.label = label
        self.commands = []
        self.progress = []

    def run(self, args, *, input=None, capture=False, check=True, on_output=None):
        self.commands.append(args)
        if on_output is not None:
            on_output("#1 loading build definition")
            self.progress.append("reported")
        if args[:2] == ["image", "inspect"]:
            return self.label
        return ""


def test_create_and_revise_preserve_prior_dockerfile_and_update_manifest_atomically(tmp_path):
    catalog = RecipeCatalog(tmp_path / "assets" / "images")
    initial = catalog.create("android-ws", "FROM ubuntu:24.04\n", tag="android-ws:v1", target="desktop")
    revised = catalog.revise("android-ws", b"FROM ubuntu:24.04\nRUN true\n", tag="android-ws:v2", target=None)

    assert initial.manifest.revision == 1
    assert revised.manifest.revision == 2
    assert revised.manifest.dockerfile == "Dockerfile.android-ws-v2"
    assert (revised.directory / "Dockerfile.android-ws-v1").read_text() == "FROM ubuntu:24.04\n"
    assert revised.manifest.target is None
    assert json.loads((revised.directory / "recipe.json").read_text()) == revised.public()


@pytest.mark.parametrize("recipe_id", ["Android", "android_thing", "android-", "../android", "android/ws"])
def test_recipe_id_is_strict_kebab_case(tmp_path, recipe_id):
    with pytest.raises(RecipeError, match="kebab-case"):
        RecipeCatalog(tmp_path / "images").create(recipe_id, "FROM scratch\n", tag="example:v1")


def test_create_rejects_collision_and_non_utf8_or_oversized_upload(tmp_path):
    catalog = RecipeCatalog(tmp_path / "images")
    catalog.create("demo", "FROM scratch\n", tag="demo:v1")
    with pytest.raises(RecipeError, match="already exists"):
        catalog.create("demo", "FROM scratch\n", tag="demo:v1")
    with pytest.raises(RecipeError, match="UTF-8"):
        catalog.create("binary", b"\xff", tag="binary:v1")
    with pytest.raises(RecipeError, match="exceeds"):
        catalog.create("large", b"x" * (MAX_DOCKERFILE_BYTES + 1), tag="large:v1")


def test_catalog_rejects_malformed_manifest_and_symlinked_build_context(tmp_path):
    catalog = RecipeCatalog(tmp_path / "images")
    recipe = catalog.create("demo", "FROM scratch\n", tag="demo:v1")
    (recipe.directory / "recipe.json").write_text("[]")
    with pytest.raises(RecipeError, match="JSON object"):
        catalog.get("demo")

    (recipe.directory / "recipe.json").write_text(json.dumps({
        "schema_version": 1, "id": "demo", "revision": 1,
        "dockerfile": "Dockerfile.demo-v1", "tag": "demo:v1", "target": None, "platform": "linux/amd64",
    }))
    (recipe.directory / "outside").symlink_to(tmp_path)
    with pytest.raises(RecipeError, match="must not contain symlinks"):
        catalog.get("demo")


def test_catalog_requires_derived_filename_and_rejects_recipe_directory_symlink(tmp_path):
    catalog = RecipeCatalog(tmp_path / "images")
    recipe = catalog.create("demo", "FROM scratch\n", tag="demo:v1")
    payload = recipe.public() | {"dockerfile": "Dockerfile"}
    (recipe.directory / "recipe.json").write_text(json.dumps(payload))
    with pytest.raises(RecipeError, match="must be Dockerfile.demo-v1"):
        catalog.get("demo")

    safe = tmp_path / "safe"
    safe.mkdir()
    (tmp_path / "linked-images").symlink_to(safe)
    with pytest.raises(RecipeError, match="non-symlink"):
        RecipeCatalog(tmp_path / "linked-images").create("demo", "FROM scratch\n", tag="demo:v1")


def test_build_uses_recipe_context_defaults_and_explicit_overrides(tmp_path):
    catalog = RecipeCatalog(tmp_path / "images")
    recipe = catalog.create("demo", "FROM scratch\n", tag="demo:v1", target="desktop", platform="linux/amd64")
    docker = FakeDocker()
    progress = []
    result = ImageBuilder(docker).build(recipe, no_cache=True, on_progress=progress.append)
    assert result.tag == "demo:v1"
    assert progress == ["#1 loading build definition"]
    assert docker.commands == [[
        "buildx", "build", "--progress=plain", "--platform", "linux/amd64", "--file", str(recipe.dockerfile_path),
        "--target", "desktop", "--no-cache", "--load", "--tag", "demo:v1", str(recipe.directory),
    ]]

    docker = FakeDocker()
    result = ImageBuilder(docker).build(recipe, tag="demo:custom", target=None, platform="linux/arm64")
    assert result.target is None
    assert "--target" not in docker.commands[0]
    assert docker.commands[0] == [
        "buildx", "build", "--progress=plain", "--platform", "linux/arm64", "--file", str(recipe.dockerfile_path),
        "--load", "--tag", "demo:custom", str(recipe.directory),
    ]


def test_build_detects_recipe_changes_and_rejects_invalid_override(tmp_path):
    catalog = RecipeCatalog(tmp_path / "images")
    recipe = catalog.create("demo", "FROM scratch\n", tag="demo:v1")
    catalog.revise("demo", "FROM scratch\nRUN true\n")
    with pytest.raises(RecipeError, match="changed while preparing build"):
        ImageBuilder(FakeDocker()).build(recipe)
    current = catalog.get("demo")
    with pytest.raises(RecipeError, match="whitespace-free"):
        ImageBuilder(FakeDocker()).build(current, tag="bad tag")


def test_verifier_checks_generic_shell_only_when_no_desktop_contract():
    docker = FakeDocker()
    result = ImageVerifier(docker).verify("shell:latest")
    assert result.checks == ("shell",)
    assert docker.commands == [
        ["image", "inspect", "--format", '{{index .Config.Labels "io.github.notanyrobot.dockbench.desktop-contract"}}', "shell:latest"],
        ["run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", "shell:latest", "-lc", GENERIC_SHELL_CHECK],
    ]


def test_verifier_adds_desktop_v1_capabilities_without_bundled_tool_requirements():
    docker = FakeDocker("v1")
    result = ImageVerifier(docker).verify("desktop:latest")
    assert result.desktop_capable
    assert result.checks == ("shell", "desktop-v1")
    assert docker.commands[-1] == [
        "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", "desktop:latest", "-lc",
        " && ".join(f"command -v {command} >/dev/null" for command in DESKTOP_V1_COMMANDS),
    ]
    assert "nvidia-smi" not in docker.commands[-1][-1]
    assert "conda" not in docker.commands[-1][-1]


def test_filename_derivation_requires_positive_revision():
    assert dockerfile_filename("android-ws", 2) == "Dockerfile.android-ws-v2"
    with pytest.raises(RecipeError):
        dockerfile_filename("android-ws", 0)

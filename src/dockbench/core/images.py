"""Shared image archive operations with explicit selection and output policies."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Sequence

from dockbench.core.defaults import DEFAULT_IMAGE
from dockbench.core.workstation import DockerRunner, WorkstationError
from dockbench.core.errors import DockerCommandError
from dockbench.core.host_inventory import HostInventory


ARCHIVE_NAME = "android-ws-u22.04-cu12.8-v2.tar"
Run = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ImagePackageResult:
    archive: Path
    reference: str = ""


class WorkstationImages:
    """Own image-transfer policy and Docker interaction behind a small API."""
    def __init__(self, docker_command: str | None = None, image: str | None = None,
                 run: Run = subprocess.run, *, runner: DockerRunner | None = None,
                 inventory: HostInventory | None = None,
                 capture_runner: DockerRunner | None = None) -> None:
        self.docker_command = docker_command or os.environ.get("DOCKBENCH_DOCKER", "docker")
        self.image = image or os.environ.get("DOCKBENCH_IMAGE", DEFAULT_IMAGE)
        self._run = run
        self._runner = runner
        self._capture_runner = capture_runner or runner
        self._inventory = inventory

    def _docker(self) -> str:
        if shutil.which(self.docker_command) is None:
            raise WorkstationError(f"Docker command not found: {self.docker_command}")
        return self.docker_command

    def package(self, directory: str | Path | None = None) -> ImagePackageResult:
        output_dir = Path(directory or "images")
        output_dir.mkdir(parents=True, exist_ok=True)
        if self._runner is not None:
            try:
                self._runner.run(["image", "inspect", self.image], capture=True)
            except DockerCommandError as exc:
                raise WorkstationError(f"image is not available locally: {self.image}") from exc
            archive = output_dir / ARCHIVE_NAME
            self._transfer("save", archive, self.image, output="inherit")
            return ImagePackageResult(archive)
        docker = self._docker()
        available = self._run([docker, "image", "inspect", self.image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if available.returncode:
            raise WorkstationError(f"image is not available locally: {self.image}")
        archive = output_dir / ARCHIVE_NAME
        self._transfer("save", archive, self.image, output="inherit")
        return ImagePackageResult(archive)

    def load(self, tarballs: Sequence[str | Path]) -> None:
        if not tarballs:
            raise WorkstationError("load requires at least one tar file")
        if self._runner is None:
            self._docker()
        for item in tarballs:
            tarball = Path(item)
            self.import_archive(tarball, output="inherit")

    def _transfer(self, action: Literal["save", "load"], path: Path,
                  image: str | None = None, *, output: Literal["inherit", "capture"]) -> str:
        """Inherit retains CLI streams/process errors; capture returns browser job logs."""
        args = [action, "--output" if action == "save" else "--input", str(path)]
        if image is not None:
            args.append(image)
        runner = self._capture_runner if output == "capture" else self._runner
        if runner is not None:
            return runner.run((["image"] if output == "capture" else []) + args,
                              capture=output == "capture")
        result = self._run([self._docker(), *args], check=True, **(
            {"capture_output": True, "text": True} if output == "capture" else {}))
        return (result.stdout or "").strip() if output == "capture" else ""

    def export_archive(self, source: str, destination: Path) -> ImagePackageResult:
        """Resolve a browser selection and save it to a caller-owned destination."""
        if self._inventory is None:
            raise WorkstationError("image inventory is unavailable")
        image = self._inventory.resolve_image(source)
        self._transfer("save", destination, image.id, output="capture")
        return ImagePackageResult(destination, image.display_reference)

    def import_archive(self, path: Path, *, output: Literal["inherit", "capture"] = "capture") -> str:
        if not path.is_file():
            raise WorkstationError(f"tar file does not exist: {path}")
        return self._transfer("load", path, output=output)

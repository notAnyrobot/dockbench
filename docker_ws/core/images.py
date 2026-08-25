"""Docker image archive operations for the workstation image."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from docker_ws.core.defaults import DEFAULT_IMAGE
from docker_ws.core.workstation import WorkstationError


ARCHIVE_NAME = "docker-ws-u22.04-cu12.8.1-v1-desktop.tar"
Run = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ImagePackageResult:
    archive: Path


class WorkstationImages:
    """Own image-transfer policy and Docker interaction behind a small API."""
    def __init__(self, docker_command: str | None = None, image: str | None = None, run: Run = subprocess.run) -> None:
        self.docker_command = docker_command or os.environ.get("ROBOTICS_WS_DOCKER", "docker")
        self.image = image or os.environ.get("ROBOTICS_WS_DESKTOP_IMAGE", DEFAULT_IMAGE)
        self._run = run

    def _docker(self) -> str:
        if shutil.which(self.docker_command) is None:
            raise WorkstationError(f"Docker command not found: {self.docker_command}")
        return self.docker_command

    def package(self, directory: str | Path | None = None) -> ImagePackageResult:
        output_dir = Path(directory or "images")
        output_dir.mkdir(parents=True, exist_ok=True)
        docker = self._docker()
        available = self._run([docker, "image", "inspect", self.image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if available.returncode:
            raise WorkstationError(f"image is not available locally: {self.image}")
        archive = output_dir / ARCHIVE_NAME
        self._run([docker, "save", "--output", str(archive), self.image], check=True)
        return ImagePackageResult(archive)

    def load(self, tarballs: Sequence[str | Path]) -> None:
        if not tarballs:
            raise WorkstationError("load requires at least one tar file")
        docker = self._docker()
        for item in tarballs:
            tarball = Path(item)
            if not tarball.is_file():
                raise WorkstationError(f"tar file does not exist: {tarball}")
            self._run([docker, "load", "--input", str(tarball)], check=True)

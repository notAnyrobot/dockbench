"""Host-local Docker image and NVIDIA GPU discovery.

The inventory is deliberately the only module that knows Docker's discovery
commands.  Workstation lifecycle code consumes the immutable records below.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from typing import Callable, Protocol

from docker_ws.core.errors import WorkstationError


class DockerRunner(Protocol):
    def run(self, args: list[str], *, input: str | None = None, capture: bool = False,
            check: bool = True) -> str: ...


CommandRun = Callable[..., subprocess.CompletedProcess[str]]
DESKTOP_CONTRACT_LABEL = "io.docker-workstation.desktop-contract"


@dataclass(frozen=True)
class LocalImage:
    id: str
    references: tuple[str, ...]
    size: int
    created: str
    architecture: str
    desktop_contract: str | None = None

    @property
    def display_reference(self) -> str:
        return self.references[0] if self.references else self.id

    @property
    def desktop_capable(self) -> bool:
        return self.desktop_contract == "v1"

    def public(self) -> dict[str, object]:
        return {**asdict(self), "references": list(self.references), "display_reference": self.display_reference,
                "desktop_capable": self.desktop_capable}


@dataclass(frozen=True)
class GPU:
    uuid: str
    index: int
    name: str
    memory_total_mib: int

    def public(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class HostInventoryResult:
    images: tuple[LocalImage, ...]
    gpus: tuple[GPU, ...]
    gpu_diagnostic: str | None = None

    def public(self) -> dict[str, object]:
        return {"images": [image.public() for image in self.images], "gpus": [gpu.public() for gpu in self.gpus],
                "gpu_diagnostic": self.gpu_diagnostic}


class HostInventory:
    """Resolve immutable local-image and GPU records without lifecycle policy."""

    def __init__(self, docker: DockerRunner, command_run: CommandRun = subprocess.run) -> None:
        self.docker = docker
        self._command_run = command_run

    def images(self) -> tuple[LocalImage, ...]:
        # This deliberately excludes untagged/dangling layers: users can only
        # select something that has a stable, understandable local reference.
        output = self.docker.run(["image", "ls", "--format", "{{.ID}}\t{{.Repository}}\t{{.Tag}}"], capture=True)
        grouped: dict[str, list[str]] = {}
        for line in output.splitlines():
            fields = line.split("\t")
            if len(fields) != 3 or fields[1] == "<none>" or fields[2] == "<none>":
                continue
            grouped.setdefault(fields[0], []).append(f"{fields[1]}:{fields[2]}")
        images: list[LocalImage] = []
        for short_id, refs in grouped.items():
            raw = self.docker.run(["image", "inspect", "--format", "{{json .}}", short_id], capture=True)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise WorkstationError("Docker returned invalid image metadata") from exc
            labels = data.get("Config", {}).get("Labels") or {}
            images.append(LocalImage(data.get("Id", short_id), tuple(sorted(set(data.get("RepoTags") or refs))),
                int(data.get("Size") or 0), data.get("Created") or "", data.get("Architecture") or "unknown",
                labels.get(DESKTOP_CONTRACT_LABEL)))
        return tuple(sorted(images, key=lambda image: image.display_reference.lower()))

    def resolve_image(self, selection: str) -> LocalImage:
        if not selection:
            raise WorkstationError("an image is required when creating a workstation; use docker-ws images")
        for image in self.images():
            if selection == image.id or selection in image.references or image.id.startswith(selection):
                return image
        raise WorkstationError(f"image is not available locally: {selection}")

    def _nvidia_runtime_available(self) -> tuple[bool, str | None]:
        try:
            runtimes = self.docker.run(["info", "--format", "{{json .Runtimes}}"], capture=True)
        except WorkstationError as exc:
            return False, f"Docker NVIDIA runtime unavailable: {exc}"
        if "nvidia" not in runtimes.lower():
            return False, "Docker does not report an NVIDIA runtime; GPU selection is unavailable."
        return True, None

    def gpus(self) -> tuple[tuple[GPU, ...], str | None]:
        available, diagnostic = self._nvidia_runtime_available()
        if not available:
            return (), diagnostic
        if shutil.which("nvidia-smi") is None:
            return (), "nvidia-smi is unavailable on this host; GPU selection is unavailable."
        try:
            result = self._command_run(["nvidia-smi", "--query-gpu=index,uuid,name,memory.total", "--format=csv,noheader,nounits"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        except OSError as exc:
            return (), f"Unable to inspect NVIDIA GPUs: {exc}"
        if result.returncode:
            return (), "nvidia-smi could not report GPUs; GPU selection is unavailable."
        gpus: list[GPU] = []
        for row in result.stdout.splitlines():
            parts = [part.strip() for part in row.split(",")]
            if len(parts) != 4:
                continue
            try:
                gpus.append(GPU(parts[1], int(parts[0]), parts[2], int(float(parts[3]))))
            except ValueError:
                continue
        return tuple(gpus), None if gpus else "No NVIDIA GPUs were reported by nvidia-smi."

    def inventory(self) -> HostInventoryResult:
        gpus, diagnostic = self.gpus()
        return HostInventoryResult(self.images(), gpus, diagnostic)

    def resolve_gpus(self, selections: tuple[str, ...], all_gpus: bool = False) -> tuple[GPU, ...]:
        gpus, diagnostic = self.gpus()
        if all_gpus:
            if selections:
                raise WorkstationError("--gpu all cannot be combined with individual --gpu selections")
            if not gpus:
                raise WorkstationError(diagnostic or "no GPUs are available")
            return gpus
        if not selections:
            return ()
        if not gpus:
            raise WorkstationError(diagnostic or "no GPUs are available")
        by_uuid = {gpu.uuid: gpu for gpu in gpus}; by_index = {str(gpu.index): gpu for gpu in gpus}
        selected: list[GPU] = []
        for selection in selections:
            gpu = by_uuid.get(selection) or by_index.get(selection)
            if gpu is None:
                raise WorkstationError(f"GPU is not available: {selection}")
            if gpu.uuid in {current.uuid for current in selected}:
                raise WorkstationError(f"GPU selected more than once: {selection}")
            selected.append(gpu)
        return tuple(selected)

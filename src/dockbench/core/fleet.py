"""Managed-container discovery, reservations, and named lifecycle operations."""
from __future__ import annotations

import contextlib
import fcntl
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from dockbench.core.defaults import DEFAULT_IMAGE, data_root_from_value, workspace_root_from_value
from dockbench.core.errors import WorkstationContainerExists, WorkstationError, WorkstationGPUConflict
from dockbench.core.host_inventory import HostInventory
from dockbench.core.workstation import DockerRunner, SubprocessDockerRunner, Workstation, WorkstationConfig, WorkstationStatus, DesktopEndpoint


class FleetManager:
    """Managed-only multi-container lifecycle facade used by Dockbench.

    Default-container lifecycle retains its explicit Workstation interface.
    Named operations and automatic shell selection recognize only managed labels.
    """
    managed_label = "io.github.notanyrobot.dockbench.managed=true"
    _name = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

    def __init__(self, config: WorkstationConfig | None = None, runner: DockerRunner | None = None,
                 inventory: HostInventory | None = None) -> None:
        self.config = config or WorkstationConfig.from_environment()
        self.docker = runner or SubprocessDockerRunner(self.config.docker_command)
        self.host_inventory = inventory or HostInventory(self.docker)

    @contextlib.contextmanager
    def locked(self) -> Iterable[None]:
        self.config.state_root.mkdir(parents=True, exist_ok=True)
        with (self.config.state_root / ".fleet.lock").open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _validate_name(self, name: str) -> str:
        if not self._name.fullmatch(name):
            raise WorkstationError("container names must be 1–63 letters, numbers, dots, underscores, or dashes")
        return name

    def _exists(self, name: str) -> bool:
        try:
            self.docker.run(["container", "inspect", "--format", "{{.State.Status}}", name], capture=True)
            return True
        except WorkstationError:
            return False

    def _managed_names(self) -> tuple[str, ...]:
        try:
            output = self.docker.run(["container", "ls", "-a", "--filter", f"label={self.managed_label}", "--format", "{{.Names}}"], capture=True)
            names = {line.strip() for line in output.splitlines() if self._name.fullmatch(line.strip())}
        except WorkstationError:
            names = set()
        return tuple(sorted(names))

    def _config_for(self, name: str, workspace_root: Path | None = None,
                    data_mounts: tuple[tuple[Path, str], ...] | None = None) -> WorkstationConfig:
        self._validate_name(name)
        if name == self.config.container_name:
            return replace(
                self.config,
                workspace_root=workspace_root or self.config.workspace_root,
                data_mounts=self.config.data_mounts if data_mounts is None else data_mounts,
            )
        return replace(self.config, container_name=name, workspace_root=workspace_root or self.config.workspace_root,
                       data_mounts=self.config.data_mounts if data_mounts is None else data_mounts,
                       state_root=self.config.state_root / "containers" / name,
                       vnc_port=0, dynamic_vnc_port=True)

    def _workstation(self, name: str, workspace_root: Path | None = None,
                     data_mounts: tuple[tuple[Path, str], ...] | None = None) -> Workstation:
        return Workstation(self._config_for(name, workspace_root, data_mounts), self.docker, self.host_inventory)

    def _is_stale(self, status: WorkstationStatus) -> bool:
        if not status.image_id:
            return False
        # The persisted reference records the image selection. A rebuild may
        # leave the old image ID locally present, so the reference must still
        # resolve to the recorded ID.
        if status.image_ref:
            try:
                return self.host_inventory.resolve_image(status.image_ref).id != status.image_id
            except WorkstationError:
                return True
        try: return all(image.id != status.image_id for image in self.host_inventory.images())
        except WorkstationError: return False

    def _status(self, name: str) -> WorkstationStatus:
        status = self._workstation(name).status()
        return replace(status, stale=self._is_stale(status))

    def containers(self) -> tuple[WorkstationStatus, ...]:
        return tuple(self._status(name) for name in self._managed_names())

    def container(self, name: str) -> WorkstationStatus:
        self._validate_name(name)
        if name not in self._managed_names():
            raise WorkstationError("container is not managed by Dockbench")
        return self._status(name)

    def _reserved_gpus(self, excluding: str | None = None) -> dict[str, str]:
        reservations: dict[str, str] = {}
        for status in self.containers():
            if (status.container_name != excluding and status.state == "running"
                    and self._name.fullmatch(status.container_name)):
                reservations.update({gpu: status.container_name for gpu in status.gpu_uuids})
        return reservations

    def _ensure_gpus_available(self, gpus: tuple[str, ...], excluding: str | None = None) -> None:
        reservations = self._reserved_gpus(excluding)
        for gpu in gpus:
            if gpu in reservations:
                raise WorkstationGPUConflict(gpu, reservations[gpu])

    def _remove_failed_created_container(self, name: str) -> None:
        """Remove only a managed container that Docker left in ``Created`` state.

        A failed ``docker run`` or ``docker start`` can leave a named container
        behind before its process starts.  The managed-label check prevents this
        recovery path from removing a user container that happens to share a
        Workbench name.  Persistent state is deliberately left intact.
        """
        try:
            if self._workstation(name)._container_status() != "created":
                return
            if name not in self._managed_names():
                return
            self.docker.run(["rm", name])
        except WorkstationError:
            # Cleanup must never hide the startup failure that prompted it.
            return

    def _start_with_created_cleanup(self, name: str, workspace_root: Path | None = None,
                                    data_mounts: tuple[tuple[Path, str], ...] | None = None,
                                    **kwargs: object) -> WorkstationStatus:
        try:
            return self._workstation(name, workspace_root, data_mounts).start(**kwargs)
        except Exception:
            self._remove_failed_created_container(name)
            raise

    def create(self, name: str, image: str, gpu_uuids: tuple[str, ...] = (), all_gpus: bool = False,
               workspace_root: str | None = None, data_root: str | None = None) -> WorkstationStatus:
        name = self._validate_name(name)
        with self.locked():
            if self._exists(name): raise WorkstationContainerExists(name)
            selected_workspace = workspace_root_from_value(workspace_root) if workspace_root is not None else self.config.workspace_root
            selected_data_mounts = ((data_root_from_value(data_root), "/data/motions"),) if data_root is not None else ()
            selected = self.host_inventory.resolve_gpus(gpu_uuids, all_gpus)
            self._ensure_gpus_available(tuple(gpu.uuid for gpu in selected))
            self._start_with_created_cleanup(name, workspace_root=selected_workspace, data_mounts=selected_data_mounts,
                                             image=image, gpus=tuple(gpu.uuid for gpu in selected), all_gpus=False)
            return self._status(name)

    def start(self, name: str) -> WorkstationStatus:
        with self.locked():
            status = self.container(name)
            self._ensure_gpus_available(status.gpu_uuids, excluding=name)
            self._start_with_created_cleanup(name)
            return self._status(name)

    def stop(self, name: str) -> WorkstationStatus:
        with self.locked():
            self.container(name)
            return self._workstation(name).stop()

    def enter(self, name: str | None = None) -> None:
        """Enter an explicit managed container, the running default, or the sole running container."""
        if name is not None:
            self.container(name)
            self._workstation(name).enter()
            return
        # Configured defaults retain their original identity rules; only
        # explicit and discovered fleet names use managed-name validation.
        default = Workstation(self.config, runner=self.docker, inventory=self.host_inventory)
        if default.status().state == "running":
            default.enter()
            return
        running = tuple(item for item in self.containers() if item.state == "running")
        if len(running) > 1:
            names = ", ".join(item.container_name for item in running)
            raise WorkstationError(
                f"multiple managed containers are running ({names}); "
                "specify one with `dockbench shell CONTAINER`"
            )
        if running:
            self._workstation(running[0].container_name).enter()
        else:
            default.enter()

    def remove(self, name: str) -> None:
        with self.locked():
            self.container(name)
            raw = self._workstation(name)._container_status()
            if raw == "running": self.docker.run(["stop", name])
            if raw: self.docker.run(["rm", name])

    def delete_state(self, name: str) -> None:
        name = self._validate_name(name)
        if self._exists(name): raise WorkstationError("remove the container before deleting its persistent state")
        if name == self.config.container_name: raise WorkstationError("the default Dockbench state cannot be deleted")
        path = self._config_for(name).state_root
        if path.is_dir(): shutil.rmtree(path)

    def orphaned_states(self) -> tuple[str, ...]:
        """Return named persistent-state directories whose container is gone."""
        root = self.config.state_root / "containers"
        if not root.is_dir():
            return ()
        return tuple(sorted(
            entry.name for entry in root.iterdir()
            if entry.is_dir() and self._name.fullmatch(entry.name) and not self._exists(entry.name)
        ))

    def recreate(self, name: str) -> WorkstationStatus:
        with self.locked():
            status = self.container(name)
            if not status.stale: raise WorkstationError("container image is still available; recreation is only needed for stale containers")
            image = status.image_ref or status.image_id
            if not image: raise WorkstationError("stale container has no recorded image reference")
            ws = self._workstation(name)
            raw = ws._container_status()
            if raw == "running": self.docker.run(["stop", name])
            if raw: self.docker.run(["rm", name])
            selected = self.host_inventory.resolve_gpus(status.gpu_uuids, False)
            self._ensure_gpus_available(tuple(gpu.uuid for gpu in selected))
            self._start_with_created_cleanup(name, image=image, gpus=tuple(gpu.uuid for gpu in selected), all_gpus=False)
            return self._status(name)

    def ensure_desktop(self, name: str, password: str | None = None) -> DesktopEndpoint:
        with self.locked():
            status = self.container(name)
            if status.state != "running":
                self._ensure_gpus_available(status.gpu_uuids, excluding=name)
            try:
                return self._workstation(name).ensure_desktop(password)
            except Exception:
                self._remove_failed_created_container(name)
                raise

    def reset_vnc_password(self, name: str, password: str) -> WorkstationStatus:
        with self.locked():
            status = self.container(name)
            if status.state != "running":
                self._ensure_gpus_available(status.gpu_uuids, excluding=name)
            try:
                return self._workstation(name).reset_vnc_password(password)
            except Exception:
                self._remove_failed_created_container(name)
                raise

    def inventory(self) -> dict[str, object]:
        host = self.host_inventory.inventory().public()
        reservations = self._reserved_gpus()
        host["gpus"] = [{**gpu, "reservation": reservations.get(str(gpu["uuid"])), "available": str(gpu["uuid"]) not in reservations} for gpu in host["gpus"]]  # type: ignore[index]
        host["containers"] = [status.public() for status in self.containers()]
        host["default_image"] = self.config.image or DEFAULT_IMAGE
        return host


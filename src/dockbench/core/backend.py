"""Explicit, invocation-scoped construction of Dockbench capabilities.

Recipe and image operations do not validate lifecycle settings or probe Docker.
The lifecycle configuration is resolved once, only when a container operation
needs it, using the same injected runner as all other capabilities.
"""
from __future__ import annotations

import os
import subprocess
from functools import cached_property, partial
from pathlib import Path
from typing import Mapping

from dockbench.core.defaults import DEFAULT_IMAGE, default_workspace_root, default_state_root, default_data_mounts
from dockbench.core.images import WorkstationImages
from dockbench.core.resources import CheckoutResources
from dockbench.core.image_builder import ImageBuilder
from dockbench.core.image_verifier import ImageVerifier
from dockbench.core.host_inventory import HostInventory
from dockbench.core.recipes import RecipeCatalog
from dockbench.core.fleet import FleetManager
from dockbench.core.workstation import DockerRunner, SubprocessDockerRunner, Workstation, WorkstationConfig


class Backend:
    def __init__(self, *, repository_root: Path | None = None,
                 environment: Mapping[str, str] | None = None,
                 runner: DockerRunner | None = None,
                 config: WorkstationConfig | None = None) -> None:
        self.resources = CheckoutResources.discover(repository_root or (config.repository_root if config else None))
        self._environment = dict(os.environ if environment is None else environment)
        self.docker_command = config.docker_command if config else self._environment.get('DOCKBENCH_DOCKER', 'docker')
        self._runner = runner if runner is not None else SubprocessDockerRunner(self.docker_command, environment=self._environment)
        self._archive_runner = runner
        self._config = config
        self._workspace_root = default_workspace_root()
        self._data_mounts = default_data_mounts()
        self._environment.setdefault('DOCKBENCH_STATE_ROOT', str(default_state_root()))

    @property
    def execution_environment(self) -> dict[str, str]:
        """Environment for adapter-owned interactive transports."""
        return dict(self._environment)

    @cached_property
    def config(self) -> WorkstationConfig:
        return self._config if self._config is not None else WorkstationConfig.from_environment(
            self.resources.repository_root, environment=self._environment, runner=self._runner)

    @cached_property
    def inventory(self) -> HostInventory:
        return HostInventory(self._runner)

    @cached_property
    def workstation(self) -> Workstation:
        return Workstation(self.config, runner=self._runner, inventory=self.inventory)

    @cached_property
    def fleet(self) -> FleetManager:
        return FleetManager(self.config, runner=self._runner, inventory=self.inventory)

    @cached_property
    def recipes(self) -> RecipeCatalog:
        return RecipeCatalog.for_repository(self.resources.repository_root)

    @cached_property
    def image_builder(self) -> ImageBuilder:
        return ImageBuilder(self._runner)

    @cached_property
    def image_verifier(self) -> ImageVerifier:
        return ImageVerifier(self._runner)

    @cached_property
    def images(self) -> WorkstationImages:
        # CLI archives inherit both streams and retain CalledProcessError
        # presentation. Other Docker operations deliberately capture stderr.
        # Explicit runner injection still controls every Docker operation.
        return WorkstationImages(
            self.docker_command,
            self._config.image if self._config is not None else self._environment.get('DOCKBENCH_IMAGE', DEFAULT_IMAGE),
            run=partial(subprocess.run, env=self._environment), runner=self._archive_runner,
            capture_runner=self._runner, inventory=self.inventory,
        )

    def host_defaults(self) -> dict[str, str | None]:
        from dockbench.core.defaults import workspace_root_from_value
        if self._config is not None:
            workspace, mounts = self._config.workspace_root, self._config.data_mounts
        else:
            value = self._environment.get('DOCKBENCH_WORKSPACE')
            workspace = workspace_root_from_value(value) if value else self._workspace_root
            mounts = self._data_mounts
        data = next((source for source, destination in mounts if destination == '/data/motions'), None)
        return {'workspace_root': str(workspace) if workspace is not None else None,
                'data_root': str(data) if data is not None else None}

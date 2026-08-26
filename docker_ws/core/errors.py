"""Errors that are safe to surface through the CLI and Workbench."""


class WorkstationError(RuntimeError):
    """An expected, safe-to-display workstation failure."""


class WorkstationRebuildRequired(WorkstationError):
    """Compatibility name for callers handling a changed launch request."""


class WorkstationReplaceRequired(WorkstationRebuildRequired):
    """A managed container must be explicitly replaced to change its launch."""


class WorkstationGPUConflict(WorkstationError):
    """A requested GPU is already allocated to a running managed container."""

    def __init__(self, gpu_uuid: str, owner: str) -> None:
        self.gpu_uuid = gpu_uuid
        self.owner = owner
        super().__init__(f"GPU {gpu_uuid} is reserved by running container {owner}")

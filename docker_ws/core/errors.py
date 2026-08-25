"""Errors that are safe to surface through the CLI and Workbench."""


class WorkstationError(RuntimeError):
    """An expected, safe-to-display workstation failure."""


class WorkstationRebuildRequired(WorkstationError):
    """Compatibility name for callers handling a changed launch request."""


class WorkstationReplaceRequired(WorkstationRebuildRequired):
    """A managed container must be explicitly replaced to change its launch."""

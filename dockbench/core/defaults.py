"""Stable Dockbench defaults for the host workspace root and images."""
from pathlib import Path

from dockbench.core.errors import WorkspaceRootError

DEFAULT_IMAGE = "android-ws:u22.04-cu12.8-v2"


def default_workspace_root(*, home: Path | None = None, data_root: Path = Path("/data")) -> Path | None:
    """Discover ``~/workspace`` locally or ``/data/$USER/workspace`` remotely."""
    user_home = (home or Path.home()).expanduser()
    data_home = data_root / user_home.name
    root = (data_home if data_home.is_dir() else user_home) / "workspace"
    return root.resolve() if root.is_dir() else None


def workspace_root_from_value(value: str) -> Path:
    """Validate an explicit ``DOCKBENCH_WORKSPACE`` directory."""
    if not value:
        raise WorkspaceRootError("DOCKBENCH_WORKSPACE must be a non-empty directory path")
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise WorkspaceRootError(f"workspace root does not exist: {root}")
    return root


def default_state_root(*, home: Path | None = None, data_root: Path = Path("/data")) -> Path:
    """Choose Dockbench's persistent state directory beside the active workspace root."""
    user_home = (home or Path.home()).expanduser()
    data_home = data_root / user_home.name
    return (data_home if data_home.is_dir() else user_home) / ".dockbench"

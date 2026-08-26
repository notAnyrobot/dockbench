"""Stable defaults shared by lifecycle and image-management modules."""

from pathlib import Path

DEFAULT_IMAGE = "android-ws:u22.04-cu12.8-v1"


def default_workspace(*, home: Path | None = None, data_root: Path = Path("/data")) -> Path:
    """Choose the conventional host workspace for local and HPC machines.

    A per-user directory under ``/data`` identifies the HPC/workstation layout;
    all other hosts use the user's home directory. Callers retain responsibility
    for checking that the selected workspace exists.
    """
    user_home = (home or Path.home()).expanduser()
    data_home = data_root / user_home.name
    return data_home / "workspace" if data_home.is_dir() else user_home / "workspace"

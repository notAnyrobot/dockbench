"""Stable Dockbench defaults for host-backed code roots and images."""

import json
import re
from pathlib import Path

from dockbench.core.errors import WorkstationError

DEFAULT_IMAGE = "android-ws:u22.04-cu12.8-v2"
DEFAULT_CODE_ROOT_NAMES = ("android-ws", "GitHub")
_CODE_ROOT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def default_code_roots(*, home: Path | None = None, data_root: Path = Path("/data")) -> dict[str, Path]:
    """Discover conventional source roots on local and HPC hosts.

    A per-user ``/data/$USER`` directory selects the remote-host layout as a
    whole; otherwise discovery uses ``$HOME``. Missing roots are simply omitted
    so callers can report a useful configuration error or supply an explicit map.
    """
    user_home = (home or Path.home()).expanduser()
    data_home = data_root / user_home.name
    base = data_home if data_home.is_dir() else user_home
    roots: dict[str, Path] = {}
    for name in DEFAULT_CODE_ROOT_NAMES:
        root = base / name
        if root.is_dir():
            roots[name] = root.resolve()
    return roots


def default_state_root(*, home: Path | None = None, data_root: Path = Path("/data")) -> Path:
    """Choose Dockbench's persistent state directory beside the active roots."""
    user_home = (home or Path.home()).expanduser()
    data_home = data_root / user_home.name
    return (data_home if data_home.is_dir() else user_home) / ".dockbench"


def code_roots_from_json(value: str) -> dict[str, Path]:
    """Validate the explicit ``DOCKBENCH_CODE_ROOTS`` JSON mapping."""
    try:
        mapping = json.loads(value)
    except json.JSONDecodeError as exc:
        raise WorkstationError("DOCKBENCH_CODE_ROOTS must be a JSON object mapping names to directories") from exc
    if not isinstance(mapping, dict) or not mapping:
        raise WorkstationError("DOCKBENCH_CODE_ROOTS must be a non-empty JSON object mapping names to directories")
    roots: dict[str, Path] = {}
    for name, raw_path in mapping.items():
        if not isinstance(name, str) or name in {".", ".."} or not _CODE_ROOT_NAME.fullmatch(name):
            raise WorkstationError("code-root names must contain only letters, numbers, dots, underscores, and dashes")
        if not isinstance(raw_path, str) or not raw_path:
            raise WorkstationError(f"code root {name!r} must have a non-empty directory path")
        path = Path(raw_path).expanduser().resolve()
        if not path.is_dir():
            raise WorkstationError(f"code root does not exist: {path}")
        if path in roots.values():
            raise WorkstationError(f"code root directory is assigned more than once: {path}")
        roots[name] = path
    return dict(sorted(roots.items()))

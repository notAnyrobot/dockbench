import json
from pathlib import Path

import pytest

from dockbench.core.defaults import code_roots_from_json, default_code_roots, default_state_root
from dockbench.core.errors import WorkstationError


def test_default_code_roots_detect_home_directories(tmp_path):
    home = tmp_path / "home" / "android"
    home.mkdir(parents=True)
    (home / "android-ws").mkdir()
    (home / "GitHub").mkdir()

    assert default_code_roots(home=home, data_root=tmp_path / "data") == {
        "android-ws": home / "android-ws",
        "GitHub": home / "GitHub",
    }


def test_default_code_roots_use_one_remote_layout_without_mixing_home_roots(tmp_path):
    home = tmp_path / "home" / "atom7"
    data_root = tmp_path / "data"
    home.mkdir(parents=True)
    (data_root / "atom7").mkdir(parents=True)
    (home / "GitHub").mkdir()
    (data_root / "atom7" / "android-ws").mkdir()

    assert default_code_roots(home=home, data_root=data_root) == {
        "android-ws": data_root / "atom7" / "android-ws",
    }
    assert default_state_root(home=home, data_root=data_root) == data_root / "atom7" / ".dockbench"


def test_explicit_code_roots_are_json_directories_with_safe_mount_names(tmp_path):
    android = tmp_path / "sources" / "android"
    github = tmp_path / "sources" / "github"
    android.mkdir(parents=True)
    github.mkdir(parents=True)

    roots = code_roots_from_json(json.dumps({"GitHub": str(github), "android-ws": str(android)}))

    assert roots == {"GitHub": github, "android-ws": android}
    with pytest.raises(WorkstationError, match="JSON object"):
        code_roots_from_json("[]")
    with pytest.raises(WorkstationError, match="code-root names"):
        code_roots_from_json(json.dumps({"../unsafe": str(android)}))
    with pytest.raises(WorkstationError, match="code-root names"):
        code_roots_from_json(json.dumps({"..": str(android)}))
    with pytest.raises(WorkstationError, match="does not exist"):
        code_roots_from_json(json.dumps({"android-ws": str(tmp_path / "missing")}))
    with pytest.raises(WorkstationError, match="assigned more than once"):
        code_roots_from_json(json.dumps({"android-ws": str(android), "GitHub": str(android)}))

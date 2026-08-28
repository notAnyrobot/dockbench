from pathlib import Path

import pytest

from dockbench.core.defaults import default_state_root, default_workspace_root, workspace_root_from_value
from dockbench.core.errors import WorkstationError


def test_default_workspace_root_uses_home_on_local_host(tmp_path):
    home = tmp_path / "home" / "android"
    home.mkdir(parents=True)
    (home / "workspace").mkdir()

    assert default_workspace_root(home=home, data_root=tmp_path / "data") == home / "workspace"


def test_default_workspace_root_uses_remote_layout(tmp_path):
    home = tmp_path / "home" / "atom7"
    data_root = tmp_path / "data"
    home.mkdir(parents=True)
    (data_root / "atom7").mkdir(parents=True)
    (home / "workspace").mkdir()
    (data_root / "atom7" / "workspace").mkdir()

    assert default_workspace_root(home=home, data_root=data_root) == data_root / "atom7" / "workspace"
    assert default_state_root(home=home, data_root=data_root) == data_root / "atom7" / ".dockbench"


def test_explicit_workspace_root_must_be_a_directory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert workspace_root_from_value(str(workspace)) == workspace
    with pytest.raises(WorkstationError, match="non-empty"):
        workspace_root_from_value("")
    with pytest.raises(WorkstationError, match="does not exist"):
        workspace_root_from_value(str(tmp_path / "missing"))

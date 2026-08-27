from pathlib import Path

from dockbench.core.defaults import default_workspace


def test_default_workspace_uses_home_workspace_on_local_machine(tmp_path):
    home = tmp_path / "home" / "android"
    home.mkdir(parents=True)

    assert default_workspace(home=home, data_root=tmp_path / "data") == home / "workspace"


def test_default_workspace_uses_per_user_data_workspace_on_hpc(tmp_path):
    home = tmp_path / "home" / "atom7"
    data_root = tmp_path / "data"
    home.mkdir(parents=True)
    (data_root / "atom7").mkdir(parents=True)

    assert default_workspace(home=home, data_root=data_root) == data_root / "atom7" / "workspace"

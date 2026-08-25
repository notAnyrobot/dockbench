from docker_ws.cli import main


def test_workbench_preflight_explains_how_to_build_assets(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main, "WORKBENCH_INDEX", tmp_path / "dist/index.html")
    assert main._workbench() == 1
    output = capsys.readouterr().err
    assert "npm ci --prefix apps/workbench" in output
    assert "npm run --prefix apps/workbench build" in output


def test_service_unit_substitutes_absolute_uv_path(tmp_path, monkeypatch):
    unit_dir = tmp_path / "config"
    calls = []
    monkeypatch.setenv("XDG_CONFIG_HOME", str(unit_dir))
    monkeypatch.setattr(main.shutil, "which", lambda command: "/opt/tools/uv" if command == "uv" else None)
    monkeypatch.setattr(main.subprocess, "run", lambda command, check: calls.append(command))
    assert main._install_service() == 0
    unit = (unit_dir / "systemd/user/docker-ws-workbench.service").read_text()
    assert "ExecStart=/opt/tools/uv run --project" in unit
    assert "__UV_EXECUTABLE__" not in unit
    assert calls == [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "docker-ws-workbench.service"]]


def test_service_install_reports_missing_uv(monkeypatch, capsys):
    monkeypatch.setattr(main.shutil, "which", lambda command: None)
    assert main._install_service() == 1
    assert "uv command not found" in capsys.readouterr().err


def test_start_parser_accepts_image_repeated_gpus_all_replace_and_bare_restart():
    command = main.parser()
    selected = command.parse_args(["start", "--image", "ubuntu:24.04", "--gpu", "0", "--gpu", "GPU-a", "--replace"])
    assert (selected.image, selected.gpu, selected.replace) == ("ubuntu:24.04", ["0", "GPU-a"], True)
    all_gpus = command.parse_args(["start", "--gpu", "all"])
    assert all_gpus.image is None and all_gpus.gpu == ["all"] and all_gpus.replace is False
    cpu_only = command.parse_args(["start", "--gpu", "none"])
    assert cpu_only.gpu == ["none"]
    bare = command.parse_args(["start"])
    assert bare.image is None and bare.gpu is None and bare.replace is False


def test_start_dispatches_all_launch_options(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_workstation", lambda *args: calls.append(args) or 0)
    assert main.main(["start", "--image", "ubuntu:24.04", "--gpu", "0", "--gpu", "all", "--replace"]) == 0
    assert calls == [("start", "ubuntu:24.04", ["0", "all"], True)]

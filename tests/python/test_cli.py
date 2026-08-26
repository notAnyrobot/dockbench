import pytest

from docker_ws.cli import main


def test_workbench_preflight_explains_how_to_build_assets(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main, "WORKBENCH_INDEX", tmp_path / "dist/index.html")
    assert main._workbench() == 1
    output = capsys.readouterr().err
    assert "npm ci --prefix apps/workbench" in output
    assert "npm run --prefix apps/workbench build" in output


def test_legacy_service_install_delegates_to_workbench_deploy(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_deploy_workbench", lambda *args: calls.append(args) or 0)
    assert main.main(["service", "install"]) == 0
    assert calls == [()]


def test_workbench_command_group_preserves_bare_serve_and_dispatches_actions(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_workbench", lambda *args: calls.append(("serve", args)) or 0)
    monkeypatch.setattr(main, "_deploy_workbench", lambda *args: calls.append(("deploy", args)) or 0)
    monkeypatch.setattr(main, "_connect_workbench", lambda *args: calls.append(("connect", args)) or 0)
    monkeypatch.setattr(main, "_workbench_status", lambda *args: calls.append(("status", args)) or 0)

    assert main.main(["workbench"]) == 0
    assert main.main(["workbench", "serve", "--port", "9000", "--config", "/tmp/workbench.json"]) == 0
    assert main.main(["workbench", "deploy", "--port", "9001", "--code-root", "/code",
                      "--state-root", "/state", "--docker-command", "podman"]) == 0
    assert main.main(["workbench", "connect", "hpc", "--local-port", "9002",
                      "--remote-port", "9001", "--no-open"]) == 0
    assert main.main(["workbench", "status"]) == 0
    assert main.main(["workbench", "stop"]) == 0

    assert calls == [
        ("serve", (8787, None)),
        ("serve", (9000, "/tmp/workbench.json")),
        ("deploy", (9001, "/code", "/state", "podman")),
        ("connect", ("hpc", 9002, 9001, False)),
        ("status", ("status",)),
        ("status", ("stop",)),
    ]


def test_workbench_ports_are_validated_by_parser():
    with pytest.raises(SystemExit):
        main.parser().parse_args(["workbench", "connect", "hpc", "--remote-port", "70000"])


def test_start_parser_accepts_image_repeated_gpus_all_replace_and_bare_restart():
    command = main.parser()
    selected = command.parse_args(["container", "start", "--image", "ubuntu:24.04", "--gpu", "0", "--gpu", "GPU-a", "--replace"])
    assert (selected.image, selected.gpu, selected.replace) == ("ubuntu:24.04", ["0", "GPU-a"], True)
    all_gpus = command.parse_args(["container", "start", "--gpu", "all"])
    assert all_gpus.image is None and all_gpus.gpu == ["all"] and all_gpus.replace is False
    cpu_only = command.parse_args(["container", "start", "--gpu", "none"])
    assert cpu_only.gpu == ["none"]
    bare = command.parse_args(["container", "start"])
    assert bare.image is None and bare.gpu is None and bare.replace is False


def test_start_dispatches_all_launch_options(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_workstation", lambda *args: calls.append(args) or 0)
    assert main.main(["container", "start", "--image", "ubuntu:24.04", "--gpu", "0", "--gpu", "all", "--replace"]) == 0
    assert calls == [("start", "ubuntu:24.04", ["0", "all"], True)]


def test_help_image_list_and_container_commands_are_grouped(monkeypatch, capsys):
    assert main.main(["help"]) == 0
    help_output = capsys.readouterr().out
    assert "container" in help_output and "image" in help_output

    calls = []
    monkeypatch.setattr(main, "_host_inventory", lambda *args: calls.append(args) or 0)
    assert main.main(["image", "list", "--json"]) == 0
    assert calls == [("images", True)]

    command = main.parser()
    with pytest.raises(SystemExit):
        command.parse_args(["start"])
    with pytest.raises(SystemExit):
        command.parse_args(["images"])


def test_image_build_recipe_and_verification_options_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_build_recipe", lambda *args, **kwargs: calls.append(("build", args, kwargs)) or 0)
    monkeypatch.setattr(main, "_verify_image", lambda *args: calls.append(("verify", args)) or 0)

    assert main.main(["image", "build", "custom", "--tag", "custom:v2", "--target", "desktop",
                      "--platform", "linux/arm64", "--no-cache"]) == 0
    assert main.main(["image", "verify", "custom:v2"]) == 0
    assert calls == [
        ("build", ("custom",), {"tag": "custom:v2", "target": "desktop",
                                  "platform": "linux/arm64", "no_cache": True}),
        ("verify", ("custom:v2",)),
    ]


def test_recipe_add_and_revise_dispatch_only_explicit_defaults(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_recipe_action", lambda *args, **kwargs: calls.append((args, kwargs)) or 0)

    assert main.main(["image", "recipe", "add", "demo", "Dockerfile", "--tag", "demo:v1"]) == 0
    assert main.main(["image", "recipe", "revise", "demo", "Dockerfile", "--target", "desktop"]) == 0
    assert calls == [
        (("add", "demo", "Dockerfile"), {"tag": "demo:v1", "target": None,
                                           "platform": "linux/amd64"}),
        (("revise", "demo", "Dockerfile"), {"target": "desktop"}),
    ]


@pytest.mark.parametrize(
    ("group", "expected"),
    [("container", "start"), ("image", "list"), ("service", "install")],
)
def test_each_command_group_has_help(group, expected, capsys):
    assert main.main([group, "help"]) == 0
    output = capsys.readouterr().out
    assert f"docker-ws {group}" in output
    assert expected in output

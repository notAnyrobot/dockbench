from types import SimpleNamespace
from pathlib import Path

import pytest

from dockbench.cli import main


def test_bare_command_prints_help_successfully(capsys):
    assert main.main([]) == 0
    output = capsys.readouterr().out
    assert "Manage Dockbench." in output
    assert "deploy" in output and "server" in output and "desktop" in output


@pytest.mark.parametrize("legacy", [
    ["help"], ["container", "start"], ["workbench", "deploy"], ["service", "install"],
    ["gpus"], ["images"], ["image", "list"], ["image", "recipe", "list"],
])
def test_legacy_commands_are_unavailable(legacy):
    with pytest.raises(SystemExit) as exited:
        main.main(legacy)
    assert exited.value.code == 2


def test_server_and_top_level_deploy_connect_serve_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_deploy", lambda *args: calls.append(("deploy", args)) or 0)
    monkeypatch.setattr(main, "_connect", lambda *args: calls.append(("connect", args)) or 0)
    monkeypatch.setattr(main, "_serve", lambda *args: calls.append(("serve", args)) or 0)
    monkeypatch.setattr(main, "_server_status", lambda *args: calls.append(("server", args)) or 0)

    assert main.main(["deploy", "--port", "9001", "--workspace", "/data/atom7/workspace",
                      "--state-root", "/state", "--docker-command", "podman"]) == 0
    assert main.main(["connect", "hpc", "--local-port", "9002", "--remote-port", "9001"]) == 0
    assert main.main(["serve", "--port", "9003", "--config", "/tmp/server.json"]) == 0
    assert main.main(["server", "start"]) == 0
    assert main.main(["server", "status"]) == 0
    assert main.main(["server", "stop"]) == 0
    assert calls == [
        ("deploy", (9001, "/data/atom7/workspace", "/state", "podman")),
        ("connect", ("hpc", 9002, 9001, False)),
        ("serve", (9003, "/tmp/server.json")),
        ("server", ("start",)), ("server", ("status",)), ("server", ("stop",)),
    ]


def test_start_shell_desktop_stop_and_status_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_workstation", lambda *args: calls.append(args) or 0)
    assert main.main(["start", "--image", "ubuntu:24.04", "--gpu", "0", "--gpu", "all", "--replace"]) == 0
    assert main.main(["shell"]) == 0
    assert main.main(["desktop"]) == 0
    assert main.main(["stop"]) == 0
    assert main.main(["status"]) == 0
    assert calls == [
        ("start", "ubuntu:24.04", ["0", "all"], True), ("shell",), ("desktop",), ("stop",), ("status",),
    ]


def test_shell_enters_the_only_running_browser_managed_container(monkeypatch):
    entered = []

    class DefaultWorkstation:
        config = SimpleNamespace(container_name="dockbench")
        docker = object()
        inventory = object()

        def status(self):
            return SimpleNamespace(state="absent")

        def enter(self):
            raise main.WorkstationError("dockbench is not running; use `dockbench start` first")

    class BrowserFleet:
        def __init__(self, config, runner, inventory):
            assert config.container_name == "dockbench"

        def containers(self):
            return (SimpleNamespace(container_name="workstation-8gpu", state="running"),)

        def enter(self, name):
            entered.append(name)

    monkeypatch.setattr(main, "Workstation", DefaultWorkstation)
    monkeypatch.setattr(main, "FleetManager", BrowserFleet)

    assert main.main(["shell"]) == 0
    assert entered == ["workstation-8gpu"]


def test_shell_enters_an_explicit_browser_managed_container(monkeypatch):
    entered = []

    class DefaultWorkstation:
        config = SimpleNamespace(container_name="dockbench")
        docker = object()
        inventory = object()

    class BrowserFleet:
        def __init__(self, config, runner, inventory):
            pass

        def enter(self, name):
            entered.append(name)

    monkeypatch.setattr(main, "Workstation", DefaultWorkstation)
    monkeypatch.setattr(main, "FleetManager", BrowserFleet)

    assert main.main(["shell", "workstation-8gpu"]) == 0
    assert entered == ["workstation-8gpu"]


def test_shell_requires_a_name_when_multiple_managed_containers_are_running(monkeypatch, capsys):
    class DefaultWorkstation:
        config = SimpleNamespace(container_name="dockbench")
        docker = object()
        inventory = object()

        def status(self):
            return SimpleNamespace(state="absent")

    class BrowserFleet:
        def __init__(self, config, runner, inventory):
            pass

        def containers(self):
            return tuple(SimpleNamespace(container_name=name, state="running")
                         for name in ("workstation-4gpu", "workstation-8gpu"))

    monkeypatch.setattr(main, "Workstation", DefaultWorkstation)
    monkeypatch.setattr(main, "FleetManager", BrowserFleet)

    assert main.main(["shell"]) == 1
    assert "specify one with `dockbench shell CONTAINER`" in capsys.readouterr().err


def test_image_operations_dispatch_and_no_recipe_group(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_build_recipe", lambda *args, **kwargs: calls.append(("build", args, kwargs)) or 0)
    monkeypatch.setattr(main, "_verify_image", lambda *args: calls.append(("verify", args)) or 0)
    monkeypatch.setattr(main, "_package_images", lambda *args: calls.append(("archive", args)) or 0)
    monkeypatch.setattr(main, "_workstation", lambda *args: calls.append(("workstation", args)) or 0)
    assert main.main(["image", "build", "custom", "--tag", "custom:v2", "--target", "desktop",
                      "--platform", "linux/arm64", "--no-cache"]) == 0
    assert main.main(["image", "rebuild"]) == 0
    assert main.main(["image", "verify", "custom:v2"]) == 0
    assert main.main(["image", "export", "/tmp/images"]) == 0
    assert main.main(["image", "import", "one.tar", "two.tar"]) == 0
    assert calls == [
        ("build", ("custom",), {"tag": "custom:v2", "target": "desktop", "platform": "linux/arm64", "no_cache": True}),
        ("workstation", ("rebuild",)),
        ("verify", ("custom:v2",)),
        ("archive", ("export", ["/tmp/images"])),
        ("archive", ("import", ["one.tar", "two.tar"])),
    ]


def test_image_build_streams_plain_progress_to_cli(monkeypatch, capsys):
    recipe = SimpleNamespace(id="demo", manifest=SimpleNamespace(revision=2))

    class Builder:
        def __init__(self, docker): pass
        def build(self, selected, **kwargs):
            assert selected is recipe
            kwargs.pop("on_progress")("#7 downloading packages")
            return SimpleNamespace(tag="demo:v2")

    monkeypatch.setattr(main, "_recipe_catalog", lambda: SimpleNamespace(get=lambda _id: recipe))
    monkeypatch.setattr(main, "_docker_runner", lambda: object())
    monkeypatch.setattr(main, "ImageBuilder", Builder)
    assert main._build_recipe("demo") == 0
    assert capsys.readouterr().out.splitlines() == [
        "#7 downloading packages", "demo:v2: image built from demo revision 2",
    ]


def test_server_ports_are_validated_by_parser():
    with pytest.raises(SystemExit):
        main.parser().parse_args(["connect", "hpc", "--remote-port", "70000"])


def test_workspace_parser_preserves_path():
    selected = main.parser().parse_args(["deploy", "--workspace", "/data/atom7/workspace"])
    assert selected.workspace == "/data/atom7/workspace"

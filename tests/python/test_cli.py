from dockbench.core.resources import CheckoutResources
from types import SimpleNamespace
from pathlib import Path

import pytest

from dockbench.cli import main, server, deploy, web
from dockbench.web import server as web_server


def test_bare_command_prints_help_successfully(capsys):
    assert main.main([]) == 0
    output = capsys.readouterr().out
    assert "Manage Dockbench." in output
    assert "deploy" in output and "server" in output and "desktop" in output


@pytest.mark.parametrize("legacy", [
    ["deploy", "--help"], ["serve", "--help"], ["connect", "--help"],
    ["help"], ["container", "start"], ["workbench", "deploy"], ["service", "install"],
    ["gpus"], ["images"], ["image", "list"], ["image", "recipe", "list"],
])
def test_legacy_commands_are_unavailable(legacy):
    with pytest.raises(SystemExit) as exited:
        main.main(legacy)
    assert exited.value.code == 2


def test_server_and_web_preserve_options_and_output(monkeypatch, capsys):
    deployments = []
    class Deployment:
        def __init__(self, options):
            self.options = options
            deployments.append(options)
        def deploy(self):
            return SimpleNamespace(manager="process", url="http://127.0.0.1:9001", log_path="/logs/server")
        def start(self):
            return SimpleNamespace(state="running", manager="process", message="ready", log_path=None, url="http://127.0.0.1:9001")
        status = start
        def stop(self):
            return SimpleNamespace(state="stopped", manager="process", message="stopped", log_path=None, url="http://127.0.0.1:9001")

    tunnels = []
    def tunnel(host, **options):
        options.pop("on_ready")("http://127.0.0.1:9002")
        tunnels.append((host, options))
        return SimpleNamespace(interrupted=True)

    monkeypatch.setattr(deploy, "ServerDeployment", Deployment)
    monkeypatch.setattr(web, "connect", tunnel)
    assert main.main(["server", "deploy", "--port", "9001", "--workspace", "/data/atom7/workspace",
                      "--state-root", "/state", "--docker-command", "podman"]) == 0
    assert main.main(["web", "hpc", "--local-port", "9002", "--port", "9001", "--no-open"]) == 0
    for action in ("start", "status", "stop"):
        assert main.main(["server", action]) == 0
    assert deployments[0].port == 9001
    assert deployments[0].workspace_root == Path("/data/atom7/workspace")
    assert deployments[0].state_root == Path("/state")
    assert deployments[0].docker_command == "podman"
    assert tunnels == [("hpc", {"local_port": 9002, "remote_port": 9001, "open_browser": False})]
    output = capsys.readouterr().out
    assert "Dockbench deployed with process: http://127.0.0.1:9001" in output
    assert "Log: /logs/server" in output
    assert "Press Ctrl+C to close the SSH tunnel." in output
    assert "Dockbench: stopped (process) — stopped" in output


def test_start_shell_desktop_stop_and_status_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_workstation", lambda *args, **kwargs: calls.append(args) or 0)
    assert main.main(["start", "--image", "ubuntu:24.04", "--gpu", "0", "--gpu", "all", "--replace"]) == 0
    assert main.main(["shell"]) == 0
    assert main.main(["desktop"]) == 0
    assert main.main(["stop"]) == 0
    assert main.main(["status"]) == 0
    assert calls == [
        ("start", "ubuntu:24.04", ["0", "all"], True), ("shell",), ("desktop",), ("stop",), ("status",),
    ]


def test_shell_enters_the_only_running_browser_managed_container(tmp_path):
    from test_fleet import Docker, Inventory, config
    from dockbench.core.backend import Backend
    docker = Docker()
    backend = Backend(config=config(tmp_path), runner=docker)
    backend.inventory = Inventory()
    backend.fleet.create("workstation-8gpu", "demo:image")
    assert main.main(["shell"], backend=backend) == 0
    assert any(command[:2] == ["exec", "-it"] and "workstation-8gpu" in command for command in docker.commands)


def test_shell_enters_an_explicit_browser_managed_container(tmp_path):
    from test_fleet import Docker, Inventory, config
    from dockbench.core.backend import Backend
    docker = Docker()
    backend = Backend(config=config(tmp_path), runner=docker)
    backend.inventory = Inventory()
    backend.fleet.create("workstation-8gpu", "demo:image")
    backend.fleet.create("other", "demo:image")
    assert main.main(["shell", "workstation-8gpu"], backend=backend) == 0
    assert any(command[:2] == ["exec", "-it"] and "workstation-8gpu" in command for command in docker.commands)


def test_shell_requires_a_name_when_multiple_managed_containers_are_running(tmp_path, capsys):
    from test_fleet import Docker, Inventory, config
    from dockbench.core.backend import Backend
    docker = Docker()
    backend = Backend(config=config(tmp_path), runner=docker)
    backend.inventory = Inventory()
    backend.fleet.create("workstation-4gpu", "demo:image")
    backend.fleet.create("workstation-8gpu", "demo:image")
    assert main.main(["shell"], backend=backend) == 1
    assert "specify one with `dockbench shell CONTAINER`" in capsys.readouterr().err


def test_image_build_overrides_and_verify_output(capsys):
    calls = []
    recipe = SimpleNamespace(id="custom", manifest=SimpleNamespace(revision=2))
    def build(selected, **kwargs):
        assert selected is recipe
        kwargs.pop("on_progress")("building")
        calls.append(kwargs)
        return SimpleNamespace(tag="custom:v2")
    backend = SimpleNamespace(
        recipes=SimpleNamespace(get=lambda recipe_id: recipe),
        image_builder=SimpleNamespace(build=build),
        image_verifier=SimpleNamespace(verify=lambda image: SimpleNamespace(image=image, checks=("shell",))),
    )
    assert main.main(["image", "build", "custom", "--tag", "custom:v2", "--target", "desktop",
                      "--platform", "linux/arm64", "--no-cache"], backend=backend) == 0
    assert calls == [{"tag": "custom:v2", "target": "desktop", "platform": "linux/arm64", "no_cache": True}]
    assert main.main(["image", "verify", "custom:v2"], backend=backend) == 0
    assert capsys.readouterr().out.splitlines() == [
        "building", "custom:v2: image built from custom revision 2", "custom:v2: verified (shell)",
    ]


def test_cli_rebuild_streams_progress_before_replacement(tmp_path, capsys):
    from test_workstation import config, FakeDocker, FakeInventory
    from dockbench.core.backend import Backend
    c = config(tmp_path, image="test:image")
    class Docker(FakeDocker):
        def run(self, args, **kwargs):
            if args[:2] == ["run", "-d"]:
                assert capsys.readouterr().out == "build progress\ntest:image: image built\n"
            return super().run(args, **kwargs)
    backend = Backend(config=c, runner=Docker(c))
    backend.inventory = FakeInventory()
    backend.recipes.create("android-ws", "FROM scratch", tag="other:tag")
    assert main.main(["image", "rebuild"], backend=backend) == 0


def test_image_build_streams_plain_progress_to_cli(monkeypatch, capsys):
    recipe = SimpleNamespace(id="demo", manifest=SimpleNamespace(revision=2))

    class Builder:
        def __init__(self, docker): pass
        def build(self, selected, **kwargs):
            assert selected is recipe
            kwargs.pop("on_progress")("#7 downloading packages")
            return SimpleNamespace(tag="demo:v2")

    backend = SimpleNamespace(recipes=SimpleNamespace(get=lambda _id: recipe), image_builder=Builder(None))
    assert main.main(["image", "build", "demo"], backend=backend) == 0
    assert capsys.readouterr().out.splitlines() == [
        "#7 downloading packages", "demo:v2: image built from demo revision 2",
    ]


def test_server_ports_are_validated_by_parser():
    with pytest.raises(SystemExit):
        main.parser().parse_args(["web", "hpc", "--port", "70000"])


def test_workspace_parser_preserves_path():
    selected = main.parser().parse_args(["server", "deploy", "--workspace", "/data/atom7/workspace"])
    assert selected.workspace == "/data/atom7/workspace"


def test_foreground_reports_explicit_checkout_build_location(tmp_path, monkeypatch, capsys):
    from dockbench.core.resources import CheckoutResources

    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setattr(deploy, "RESOURCES", CheckoutResources.discover(root))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKBENCH_WORKSPACE", str(tmp_path))

    assert main.main(["server", "start", "--foreground"]) == 1
    error = capsys.readouterr().err
    assert "frontend is not built" in error
    assert str(root / "src/dockbench/web/frontend") in error


def test_foreground_starts_built_checkout_from_other_directory(tmp_path, monkeypatch):
    from dockbench.core.resources import CheckoutResources

    root = tmp_path / "checkout"
    dist = root / "src/dockbench/web/frontend/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("frontend")
    monkeypatch.setattr(deploy, "RESOURCES", CheckoutResources.discover(root))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKBENCH_WORKSPACE", str(tmp_path))
    calls = []
    monkeypatch.setattr(web_server.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert main.main(["server", "start", "--foreground", "--port", "9003"]) == 0
    from fastapi.testclient import TestClient
    assert len(calls) == 1
    assert TestClient(calls[0][0][0]).get('/api/health').json() == {'status': 'ok'}
    assert calls[0][1] == {"host": "127.0.0.1", "port": 9003, "proxy_headers": False}



@pytest.mark.parametrize("entrypoint", ["console", "module"])
def test_installed_checkout_finds_recipes_from_other_directory(tmp_path, entrypoint):
    import os
    import subprocess
    import sys

    from pathlib import Path

    command = ([str(Path(sys.executable).parent / "dockbench")] if entrypoint == "console"
               else [sys.executable, "-m", "dockbench.cli.main"])
    command += ["image", "build", "android-ws"]
    environment = dict(os.environ, DOCKBENCH_DOCKER=str(
        CheckoutResources.discover().repository_root / "tests/helpers/fake-docker"),
        FAKE_DOCKER_LOG=str(tmp_path / "docker.log"),
        FAKE_DOCKER_STATE=str(tmp_path / "docker.state"))
    for cwd in (CheckoutResources.discover().repository_root, tmp_path):
        result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert "image built from android-ws revision 2" in result.stdout
    assert str(CheckoutResources.discover().images / "android-ws") in (tmp_path / "docker.log").read_text()


def test_runtime_config_is_loaded_before_app_settings(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from dockbench.core.resources import CheckoutResources

    root = tmp_path / 'checkout'
    dist = root / 'src/dockbench/web/frontend/dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('frontend')
    workspace = tmp_path / 'saved-workspace'
    workspace.mkdir()
    config = tmp_path / 'server.json'
    config.write_text(json.dumps({'environment': {'DOCKBENCH_WORKSPACE': str(workspace),
                                                 'DOCKBENCH_DOCKER': str(CheckoutResources.discover().repository_root / 'tests/helpers/fake-docker')}}))
    monkeypatch.setenv('DOCKBENCH_WORKSPACE', '/missing/workspace')
    monkeypatch.setenv('DOCKBENCH_DOCKER', '/missing/docker')
    monkeypatch.setattr(deploy, 'RESOURCES', CheckoutResources.discover(root))
    monkeypatch.setenv('FAKE_DOCKER_LOG', str(tmp_path / 'docker.log'))
    monkeypatch.setenv('FAKE_DOCKER_STATE', str(tmp_path / 'docker.state'))
    apps = []
    monkeypatch.setattr(web_server.uvicorn, 'run', lambda app, **kwargs: apps.append(app))
    assert main.main(['server', 'start', '--foreground', '--runtime-config', str(config)]) == 0
    assert TestClient(apps[0]).get('/api/health').json() == {'status': 'ok'}
    # Recipe discovery is independent of lifecycle validation and Docker.
    assert TestClient(apps[0]).get('/api/image-recipes').status_code == 200
    assert TestClient(apps[0]).get('/api/host/inventory').json()['workspace_root'] == str(workspace)


def test_web_module_starts_without_cli_dispatch(monkeypatch, tmp_path):
    import runpy
    import sys
    from fastapi.testclient import TestClient

    monkeypatch.setitem(sys.modules, 'dockbench.cli.main', None)
    monkeypatch.setenv('DOCKBENCH_DOCKER', '/missing/docker')
    calls = []
    monkeypatch.setattr(web_server.uvicorn, 'run', lambda app, **options: calls.append((app, options)))
    # Use a minimal built checkout without depending on the repository build.
    from dockbench.core.resources import CheckoutResources
    root = tmp_path / 'checkout'
    dist = root / 'src/dockbench/web/frontend/dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('frontend')
    monkeypatch.setattr(web_server.CheckoutResources, 'discover', lambda root=None: CheckoutResources(root or tmp_path / 'checkout'))
    with pytest.raises(SystemExit) as exited:
        runpy.run_module('dockbench.web', run_name='__main__')
    assert exited.value.code == 0
    assert calls[0][1] == {'host': '127.0.0.1', 'port': 8787, 'proxy_headers': False}
    assert TestClient(calls[0][0]).get('/api/health').json() == {'status': 'ok'}


@pytest.mark.parametrize('action', ['import', 'export'])
@pytest.mark.parametrize('exit_code', [0, 7])
def test_archive_cli_preserves_command_streams_and_failures(tmp_path, action, exit_code):
    import os
    import subprocess
    import sys

    docker = tmp_path / 'docker'
    docker.write_text('#!/bin/sh\n'
                      'if [ "$1" = image ]; then exit 0; fi\n'
                      'printf "archive output\\n"\n'
                      'printf "archive warning\\n" >&2\n'
                      f'exit {exit_code}\n')
    docker.chmod(0o755)
    archive = tmp_path / 'input.tar'
    archive.touch()
    arguments = ['image', action, str(archive if action == 'import' else tmp_path / 'output')]
    result = subprocess.run([sys.executable, '-m', 'dockbench.cli.main', *arguments],
                            env=dict(os.environ, DOCKBENCH_DOCKER=str(docker),
                                     DOCKBENCH_WORKSPACE='/missing/workspace'),
                            capture_output=True, text=True)
    assert result.returncode == (0 if exit_code == 0 else 1)
    assert result.stdout.startswith('archive output\n')
    assert result.stderr.startswith('archive warning\n')
    if exit_code:
        assert 'returned non-zero exit status 7' in result.stderr
    else:
        assert result.stderr == 'archive warning\n'

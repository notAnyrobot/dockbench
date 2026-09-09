import json
import shutil
import subprocess
import urllib.request

from dockbench.cli.main import main
from dockbench.cli import deploy
from dockbench.core.resources import CheckoutResources


def checkout(tmp_path, monkeypatch):
    resources = CheckoutResources.discover(tmp_path / 'checkout')
    resources.frontend_source.mkdir(parents=True)
    (resources.assets / 'systemd').mkdir(parents=True)
    shutil.copyfile(CheckoutResources.discover().assets / 'systemd/dockbench.service',
                    resources.assets / 'systemd/dockbench.service')
    config_dir = resources.repository_root / 'config'
    config_dir.mkdir()
    workspace = resources.repository_root / 'workspace'
    workspace.mkdir()
    (config_dir / 'dockbench.yaml').write_text('schema_version: 1\nserver: {port: 9123, workspace: ../workspace, docker_command: missing-docker}\n')
    monkeypatch.setattr(deploy, 'RESOURCES', resources)
    monkeypatch.setenv('DOCKBENCH_WORKSPACE', '')
    monkeypatch.delenv('DOCKBENCH_WORKSPACE')
    monkeypatch.setenv('DOCKBENCH_DOCKER', '')
    monkeypatch.delenv('DOCKBENCH_DOCKER')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    return resources


class Healthy:
    status = 200
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return b'{"status":"ok"}'


def test_canonical_deploy_builds_locked_checkout_and_installs_configured_server(tmp_path, monkeypatch, capsys):
    resources = checkout(tmp_path, monkeypatch)
    commands = []
    monkeypatch.setattr(shutil, 'which', lambda name: '/tools/' + name)
    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(subprocess, 'run', run)
    urls = []
    monkeypatch.setattr(urllib.request, 'urlopen', lambda url, **kwargs: urls.append(url) or Healthy())
    monkeypatch.chdir(tmp_path)
    assert main(['server', 'deploy']) == 0
    assert commands[:3] == [['/tools/uv', 'sync', '--frozen'], ['/tools/npm', 'ci'], ['/tools/npm', 'run', 'build']]
    assert ['systemctl', '--user', 'restart', 'dockbench.service'] in commands
    assert urls == ['http://127.0.0.1:9123/api/health']
    snapshot = json.loads((tmp_path / 'xdg/dockbench/server/server.json').read_text())
    assert snapshot['environment']['DOCKBENCH_WORKSPACE'] == str(resources.repository_root / 'workspace')
    assert snapshot['environment']['DOCKBENCH_DOCKER'] == 'missing-docker'
    assert 'http://127.0.0.1:9123' in capsys.readouterr().out


def test_foreground_uses_desired_settings_without_installing_service(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from dockbench.web import server as web_server

    resources = checkout(tmp_path, monkeypatch)
    (resources.frontend_dist / "assets").mkdir(parents=True)
    (resources.frontend_dist / 'index.html').write_text('built')
    monkeypatch.setenv('DOCKBENCH_WORKSPACE', '/missing-env-workspace')
    monkeypatch.setenv('DOCKBENCH_DOCKER', '/missing-env-docker')
    applications = []
    monkeypatch.setattr(web_server.uvicorn, 'run', lambda app, **options: applications.append((app, options)))
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('foreground must not run deployment commands')))
    assert main(['server', 'start', '--foreground', '--port', '9234']) == 0
    app, options = applications[0]
    assert options == {'host': '127.0.0.1', 'port': 9234, 'proxy_headers': False}
    assert TestClient(app).get('/api/health').json() == {'status': 'ok'}
    assert not (tmp_path / 'xdg').exists()
    assert not (tmp_path / 'state').exists()


def test_new_service_launch_uses_foreground_runtime_snapshot_and_ignores_desired_yaml(tmp_path, monkeypatch):
    from dockbench.web import server as web_server
    from fastapi.testclient import TestClient

    resources = checkout(tmp_path, monkeypatch)
    (resources.frontend_dist / 'assets').mkdir(parents=True)
    (resources.frontend_dist / 'index.html').write_text('built')
    (resources.repository_root / 'config/dockbench.yaml').write_text('broken: [')
    snapshot = tmp_path / 'effective.json'
    snapshot.write_text(json.dumps({'environment': {'DOCKBENCH_WORKSPACE': str(resources.repository_root / 'workspace'), 'DOCKBENCH_DOCKER': 'missing-docker'}}))
    applications = []
    monkeypatch.setattr(web_server.uvicorn, 'run', lambda app, **options: applications.append((app, options)))
    assert main(['server', 'start', '--foreground', '--runtime-config', str(snapshot), '--port', '9345']) == 0
    assert applications[0][1]['port'] == 9345
    assert TestClient(applications[0][0]).get('/api/health').status_code == 200
    assert not (tmp_path / 'state').exists()


def test_generated_unit_safely_launches_foreground_using_effective_snapshot(tmp_path, monkeypatch):
    import shlex

    resources = checkout(tmp_path / 'space $dollar %percent', monkeypatch)
    monkeypatch.setattr(shutil, 'which', lambda name: '/tools/' + name)
    monkeypatch.setattr(subprocess, 'run', lambda command, **kwargs: subprocess.CompletedProcess(command, 0, '', ''))
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kwargs: Healthy())
    assert main(['server', 'deploy']) == 0
    unit_path = tmp_path / 'space $dollar %percent/xdg/systemd/user/dockbench.service'
    unit = unit_path.read_text()
    command = next(line.removeprefix('ExecStart=:') for line in unit.splitlines() if line.startswith('ExecStart='))
    tokens = shlex.split(command.replace('%%', '%'))
    assert tokens[:6] == ['/tools/uv', 'run', '--frozen', '--project', str(resources.repository_root), 'dockbench']
    assert tokens[6:12] == ['server', 'start', '--foreground', '--port', '9123', '--runtime-config']
    assert tokens[12].endswith('/xdg/dockbench/server/server.json')
    assert 'ExecStart=:' in unit


def test_process_fallback_launches_canonical_foreground_command(tmp_path, monkeypatch):
    resources = checkout(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, 'which', lambda name: '/tools/' + name)
    def run(command, **kwargs):
        if command[0] == 'systemctl':
            return subprocess.CompletedProcess(command, 1, '', 'Failed to connect to bus')
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(subprocess, 'run', run)
    launches = []
    class Process:
        pid = 999999999
    monkeypatch.setattr(subprocess, 'Popen', lambda command, **kwargs: launches.append((command, kwargs)) or Process())
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kwargs: Healthy())
    assert main(['server', 'deploy']) == 0
    command, options = launches[0]
    assert command[6:12] == ['server', 'start', '--foreground', '--port', '9123', '--runtime-config']
    assert options['env']['DOCKBENCH_WORKSPACE'] == str(resources.repository_root / 'workspace')
    assert options['start_new_session'] is True


def test_legacy_serve_remains_json_compatible_but_aliases_are_hidden(tmp_path, monkeypatch, capsys):
    from dockbench.cli import serve
    from dockbench.web import server as web_server

    resources = checkout(tmp_path, monkeypatch)
    monkeypatch.setattr(serve, 'RESOURCES', resources)
    (resources.frontend_dist / 'assets').mkdir(parents=True)
    (resources.frontend_dist / 'index.html').write_text('built')
    snapshot = tmp_path / 'legacy.json'
    snapshot.write_text(json.dumps({'environment': {'DOCKBENCH_DOCKER': 'legacy-docker'}}))
    apps = []
    monkeypatch.setattr(web_server.uvicorn, 'run', lambda app, **options: apps.append(options))
    assert main([]) == 0
    help_text = capsys.readouterr().out
    assert not any(line.strip().startswith(('deploy ', 'serve ')) for line in help_text.splitlines())
    assert main(['serve', '--config', str(snapshot), '--port', '9456']) == 0
    assert apps[0]['port'] == 9456
    assert 'deprecated' in capsys.readouterr().err


def test_foreground_ctrl_c_finishes_without_traceback_or_service_metadata(tmp_path, monkeypatch):
    from dockbench.web import server as web_server

    resources = checkout(tmp_path, monkeypatch)
    (resources.frontend_dist / 'assets').mkdir(parents=True)
    (resources.frontend_dist / 'index.html').write_text('built')
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(web_server.uvicorn, 'run', interrupt)
    assert main(['server', 'start', '--foreground']) == 0
    assert not (tmp_path / 'state').exists()


def test_invalid_desired_config_fails_before_build_or_metadata_changes(tmp_path, monkeypatch, capsys):
    resources = checkout(tmp_path, monkeypatch)
    (resources.repository_root / 'config/dockbench.yaml').write_text('schema_version: 1\nserver: {port: false}')
    def unexpected(*args, **kwargs):
        raise AssertionError('invalid config must not launch commands')
    monkeypatch.setattr(subprocess, 'run', unexpected)
    monkeypatch.setattr(subprocess, 'Popen', unexpected)
    assert main(['server', 'deploy']) == 1
    assert 'server.port' in capsys.readouterr().err
    assert not (tmp_path / 'state').exists()
    assert not (tmp_path / 'xdg').exists()

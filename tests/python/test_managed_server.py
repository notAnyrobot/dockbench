import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

import pytest

from dockbench.cli.main import main
from test_configured_server import checkout, Healthy


class Host:
    def __init__(self, monkeypatch, manager):
        self.manager = manager
        self.active = False
        self.commands = []
        self.launches = []
        self.processes = {}
        self.signals = []
        self.urls = []
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            if str(path).startswith('/proc/'):
                pid = int(path.parts[2])
                if pid not in self.processes:
                    raise FileNotFoundError
                return f'{pid} (dockbench) S ' + '0 ' * 18 + str(self.processes[pid])
            return original_read(path, *args, **kwargs)
        def kill(pid, sig):
            if pid not in self.processes:
                raise ProcessLookupError
        def killpg(pid, sig):
            self.signals.append(pid)
            self.processes.pop(pid, None)
        monkeypatch.setattr(Path, 'read_text', read)
        monkeypatch.setattr(os, 'kill', kill)
        monkeypatch.setattr(os, 'killpg', killpg)
        monkeypatch.setattr(subprocess, 'run', self.run)
        monkeypatch.setattr(subprocess, 'Popen', self.launch)
        monkeypatch.setattr(shutil, 'which', lambda name: '/tools/' + name)
        monkeypatch.setattr(urllib.request, 'urlopen', lambda url, **kwargs: self.urls.append(url) or Healthy())

    def run(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == 'systemctl':
            if self.manager == 'process':
                return subprocess.CompletedProcess(command, 1, '', 'Failed to connect to bus')
            action = command[2]
            if action in ('start', 'restart'):
                self.active = True
            if action == 'stop':
                self.active = False
            if action == 'is-active':
                return subprocess.CompletedProcess(command, 0 if self.active else 3, 'active' if self.active else 'inactive', '')
        return subprocess.CompletedProcess(command, 0, '', '')

    def launch(self, command, **kwargs):
        self.launches.append((command, kwargs))
        pid = 900000 + len(self.launches)
        self.processes[pid] = 12345
        return type('Process', (), {'pid': pid, 'wait': lambda self: 0})()


def test_undeployed_start_explains_deploy_without_building(tmp_path, monkeypatch, capsys):
    checkout(tmp_path, monkeypatch)
    host = Host(monkeypatch, 'process')
    assert main(['server', 'start']) == 1
    assert 'dockbench server deploy' in capsys.readouterr().err
    assert not host.launches
    assert all(command[0] == 'systemctl' for command in host.commands)


def test_process_stop_start_applies_yaml_without_builds(tmp_path, monkeypatch, capsys):
    resources = checkout(tmp_path, monkeypatch)
    host = Host(monkeypatch, 'process')
    assert main(['server', 'deploy']) == 0
    assert main(['server', 'stop']) == 0
    snapshot = tmp_path / 'state/dockbench/server/service.json'
    assert json.loads(snapshot.read_text())['manager'] == 'process'
    assert 'pid' not in json.loads(snapshot.read_text())
    (resources.repository_root / 'config/dockbench.yaml').write_text('schema_version: 1\nserver: {port: 9234, docker_command: podman}\n')
    host.commands.clear()
    assert main(['server', 'start']) == 0
    assert len(host.launches) == 2
    assert host.launches[-1][0][11] == '9234'
    assert host.launches[-1][1]['env']['DOCKBENCH_DOCKER'] == 'podman'
    assert host.urls[-1] == 'http://127.0.0.1:9234/api/health'
    assert all(command[0] == 'systemctl' for command in host.commands)
    assert 'http://127.0.0.1:9234' in capsys.readouterr().out


def test_systemd_stop_start_refreshes_unit_and_reports_actual_endpoint(tmp_path, monkeypatch, capsys):
    resources = checkout(tmp_path, monkeypatch)
    host = Host(monkeypatch, 'systemd')
    assert main(['server', 'deploy']) == 0
    assert main(['server', 'stop']) == 0
    (resources.repository_root / 'config/dockbench.yaml').write_text('schema_version: 1\nserver: {port: 9345}\n')
    host.commands.clear()
    assert main(['server', 'start', '--docker-command', 'podman']) == 0
    assert '--port 9345' in (tmp_path / 'xdg/systemd/user/dockbench.service').read_text()
    assert ['systemctl', '--user', 'start', 'dockbench.service'] in host.commands
    assert all(command[0] == 'systemctl' and 'restart' not in command for command in host.commands)
    assert host.urls[-1] == 'http://127.0.0.1:9345/api/health'
    assert main(['server', 'status']) == 0
    assert 'http://127.0.0.1:9345' in capsys.readouterr().out


@pytest.mark.parametrize('manager', ['process', 'systemd'])
def test_repeated_start_keeps_live_settings_and_explains_stop_start(tmp_path, monkeypatch, capsys, manager):
    resources = checkout(tmp_path, monkeypatch)
    host = Host(monkeypatch, manager)
    assert main(['server', 'deploy']) == 0
    capsys.readouterr()
    host.commands.clear()
    (resources.repository_root / 'config/dockbench.yaml').write_text('schema_version: 1\nserver: {port: 9999, docker_command: podman}\n')
    assert main(['server', 'start']) == 0
    output = capsys.readouterr().out
    assert 'http://127.0.0.1:9123' in output
    assert 'stop/start' in output
    assert not any(command[2] in ('start', 'restart') for command in host.commands)
    assert len(host.launches) == (1 if manager == 'process' else 0)
    assert json.loads((tmp_path / 'xdg/dockbench/server/server.json').read_text())['port'] == 9123


@pytest.mark.parametrize('manager', ['process', 'systemd'])
def test_status_and_stop_ignore_malformed_yaml_and_keep_actual_url(tmp_path, monkeypatch, capsys, manager):
    resources = checkout(tmp_path, monkeypatch)
    host = Host(monkeypatch, manager)
    assert main(['server', 'deploy']) == 0
    capsys.readouterr()
    (resources.repository_root / 'config/dockbench.yaml').write_text('broken: [')
    assert main(['server', 'status']) == 0
    assert 'http://127.0.0.1:9123' in capsys.readouterr().out
    assert main(['server', 'stop']) == 0
    assert 'http://127.0.0.1:9123' in capsys.readouterr().out
    assert not host.processes and not host.active


def test_reused_pid_is_not_stopped_and_can_resume_installed_process(tmp_path, monkeypatch):
    checkout(tmp_path, monkeypatch)
    host = Host(monkeypatch, 'process')
    assert main(['server', 'deploy']) == 0
    host.processes[900001] = 98765
    assert main(['server', 'stop']) == 0
    assert host.signals == []
    assert main(['server', 'start']) == 0
    assert host.processes[900001] == 98765
    assert host.processes[900002] == 12345


@pytest.mark.parametrize('manager', ['process', 'systemd'])
def test_failed_start_cleans_up_owned_launch_and_can_retry(tmp_path, monkeypatch, capsys, manager):
    from dockbench.core.server_deployment import DeploymentOptions, ServerDeployment, DeploymentError
    resources = checkout(tmp_path, monkeypatch)
    host = Host(monkeypatch, manager)
    assert main(['server', 'deploy']) == 0
    assert main(['server', 'stop']) == 0
    operation = ServerDeployment(DeploymentOptions(resources.repository_root, port=9123,
        workspace_root=resources.repository_root / 'workspace', health_timeout_seconds=0))
    with pytest.raises(DeploymentError, match='did not become healthy'):
        operation.start()
    assert not host.processes and not host.active
    assert operation.status().state == 'stopped'
    assert main(['server', 'start']) == 0


def test_failed_process_metadata_write_reaps_new_owned_child(tmp_path, monkeypatch):
    from dockbench.core.server_deployment import DeploymentOptions, ServerDeployment
    resources = checkout(tmp_path, monkeypatch)
    host = Host(monkeypatch, 'process')
    assert main(['server', 'deploy']) == 0
    assert main(['server', 'stop']) == 0
    original_replace = os.replace
    def replace(source, destination):
        if str(destination).endswith('server.pid'):
            raise OSError('disk full')
        return original_replace(source, destination)
    monkeypatch.setattr(os, 'replace', replace)
    operation = ServerDeployment(DeploymentOptions(resources.repository_root,
        workspace_root=resources.repository_root / 'workspace'))
    with pytest.raises(OSError, match='disk full'):
        operation.start()
    assert not host.processes


@pytest.mark.parametrize('manager', ['process', 'systemd'])
def test_managed_launch_disables_implicit_dependency_builds(tmp_path, monkeypatch, manager):
    checkout(tmp_path, monkeypatch)
    host = Host(monkeypatch, manager)
    assert main(['server', 'deploy']) == 0
    command = host.launches[0][0] if manager == 'process' else (tmp_path / 'xdg/systemd/user/dockbench.service').read_text()
    assert '--no-sync' in command


def test_exited_child_cannot_claim_another_health_endpoint(tmp_path, monkeypatch, capsys):
    checkout(tmp_path, monkeypatch)
    host = Host(monkeypatch, 'process')
    assert main(['server', 'deploy']) == 0
    assert main(['server', 'stop']) == 0
    def response(*args, **kwargs):
        host.processes.clear()
        return Healthy()
    monkeypatch.setattr(urllib.request, 'urlopen', response)
    assert main(['server', 'start']) == 1
    assert 'exited' in capsys.readouterr().err


def test_stop_does_not_kill_replacement_pid_during_shutdown(tmp_path, monkeypatch):
    import signal
    import time
    checkout(tmp_path, monkeypatch)
    host = Host(monkeypatch, 'process')
    assert main(['server', 'deploy']) == 0
    signals = []
    def killpg(pid, sig):
        signals.append(sig)
        host.processes[pid] = 88888
    clock = [0]
    monkeypatch.setattr(os, 'killpg', killpg)
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert main(['server', 'stop']) == 0
    assert signals == [signal.SIGTERM]


def test_failed_start_reaps_its_owned_child(tmp_path, monkeypatch):
    import sys
    from dockbench.core.server_deployment import DeploymentOptions, ServerDeployment, DeploymentError
    resources = checkout(tmp_path, monkeypatch)
    original_popen, original_read = subprocess.Popen, Path.read_text
    original_kill, original_killpg = os.kill, os.killpg
    host = Host(monkeypatch, 'process')
    assert main(['server', 'deploy']) == 0
    assert main(['server', 'stop']) == 0
    monkeypatch.setattr(Path, 'read_text', original_read)
    monkeypatch.setattr(os, 'kill', original_kill)
    monkeypatch.setattr(os, 'killpg', original_killpg)
    children = []
    def launch(command, **kwargs):
        child = original_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', launch)
    operation = ServerDeployment(DeploymentOptions(resources.repository_root,
        workspace_root=resources.repository_root / 'workspace', health_timeout_seconds=0))
    try:
        with pytest.raises(DeploymentError):
            operation.start()
        with pytest.raises(ChildProcessError):
            os.waitpid(children[0].pid, os.WNOHANG)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()


def test_systemd_start_command_failure_stops_partially_started_service(tmp_path, monkeypatch, capsys):
    checkout(tmp_path, monkeypatch)
    host = Host(monkeypatch, 'systemd')
    assert main(['server', 'deploy']) == 0
    assert main(['server', 'stop']) == 0
    def run(command, **kwargs):
        result = host.run(command, **kwargs)
        if command[:3] == ['systemctl', '--user', 'start']:
            return subprocess.CompletedProcess(command, 1, 'start job failed', '')
        return result
    monkeypatch.setattr(subprocess, 'run', run)
    assert main(['server', 'start']) == 1
    assert not host.active
    assert 'start job failed' in capsys.readouterr().err


def test_new_owned_child_is_cleaned_when_identity_was_unavailable(tmp_path, monkeypatch, capsys):
    checkout(tmp_path, monkeypatch)
    host = Host(monkeypatch, 'process')
    assert main(['server', 'deploy']) == 0
    assert main(['server', 'stop']) == 0
    original_read = Path.read_text
    def read(path, *args, **kwargs):
        if str(path).startswith('/proc/'):
            raise FileNotFoundError
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    assert main(['server', 'start']) == 1
    assert not host.processes
    assert 'exited' in capsys.readouterr().err

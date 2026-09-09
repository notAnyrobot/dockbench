"""Web access through the CLI, controlling only host I/O."""
import json

from dockbench.cli.main import main
from dockbench.core import server_connection as connection


def ready_host(monkeypatch):
    events = []

    class Response:
        status = 200
        def read(self):
            return b'{"status":"ok"}'
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False

    class Opener:
        def open(self, url, timeout):
            events.append(('health', url))
            return Response()

    def forbidden(*args, **kwargs):
        raise AssertionError('Opening a local UI must not launch processes')

    monkeypatch.setattr(connection.urllib.request, 'build_opener', lambda *args: Opener())
    monkeypatch.setattr(connection.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(connection.subprocess, 'run', forbidden)
    monkeypatch.setattr(connection.webbrowser, 'open', lambda url: events.append(('browser', url)) or True)
    return events


def test_web_opens_existing_local_server_by_default(monkeypatch, capsys, tmp_path):
    events = ready_host(monkeypatch)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    assert main(['web']) == 0
    assert events == [('health', 'http://127.0.0.1:8787/api/health'), ('browser', 'http://127.0.0.1:8787')]
    assert 'Dockbench: http://127.0.0.1:8787' in capsys.readouterr().out


def test_local_web_resolves_yaml_and_explicit_port_and_browser(monkeypatch, tmp_path, capsys):
    events = ready_host(monkeypatch)
    config = tmp_path / 'host.yaml'
    config.write_text('schema_version: 1\nserver:\n  port: 9101\nweb:\n  remote_port: 9102\n  open_browser: false\n')
    assert main(['web', '--config', str(config)]) == 0
    assert events == [('health', 'http://127.0.0.1:9101/api/health')]
    events.clear()
    assert main(['web', '--config', str(config), '--port', '9201', '--open-browser']) == 0
    assert events == [('health', 'http://127.0.0.1:9201/api/health'), ('browser', 'http://127.0.0.1:9201')]


def test_local_web_uses_only_saved_port_for_this_checkout(monkeypatch, tmp_path):
    from dockbench.core.resources import CheckoutResources
    events = ready_host(monkeypatch)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    path = tmp_path / 'dockbench/server/server.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'repository_root': str(CheckoutResources.discover().repository_root), 'port': 9301, 'environment': {}}))
    assert main(['web', '--no-open']) == 0
    assert events == [('health', 'http://127.0.0.1:9301/api/health')]


def ssh_host(monkeypatch):
    import socket
    events = ready_host(monkeypatch)
    commands = []

    class Process:
        terminated = False
        def poll(self):
            return None
        def terminate(self):
            self.terminated = True
        def wait(self, timeout=None):
            return 0

    class Listener:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False

    process = Process()
    monkeypatch.setattr(connection.shutil, 'which', lambda _: '/usr/bin/ssh')
    monkeypatch.setattr(connection.subprocess, 'Popen', lambda cmd: commands.append(cmd) or process)
    monkeypatch.setattr(socket, 'create_connection', lambda *args, **kwargs: Listener())
    monkeypatch.setattr(connection.time, 'sleep', lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    return events, commands, process


def test_explicit_remote_uses_remote_settings_without_local_workspace(monkeypatch, tmp_path, capsys):
    events, commands, process = ssh_host(monkeypatch)
    config = tmp_path / 'remote.yaml'
    config.write_text('schema_version: 1\nserver:\n  port: 9101\n  workspace: missing-directory\nweb:\n  remote_port: 9401\n  local_port: 19401\n')
    assert main(['web', 'gpu', '--config', str(config)]) == 0
    assert '127.0.0.1:19401:127.0.0.1:9401' in commands[0]
    assert commands[0][-1] == 'gpu' and commands[0][1] == '-N'
    assert events == [('health', 'http://127.0.0.1:19401/api/health'), ('browser', 'http://127.0.0.1:19401')]
    assert process.terminated
    assert 'Press Ctrl+C' in capsys.readouterr().out


def test_remote_browser_exception_keeps_announced_tunnel_until_interrupt(monkeypatch, capsys):
    events, commands, process = ssh_host(monkeypatch)
    waited = []

    def browser(url):
        assert url in capsys.readouterr().out
        raise RuntimeError('browser unavailable')

    def interrupt(_):
        waited.append(not process.terminated)
        raise KeyboardInterrupt()

    monkeypatch.setattr(connection.webbrowser, 'open', browser)
    monkeypatch.setattr(connection.time, 'sleep', interrupt)
    assert main(['web', 'gpu', '--local-port', '19402']) == 0
    assert waited == [True]
    assert process.terminated


def test_local_browser_exception_leaves_manual_url_and_success(monkeypatch, capsys):
    ready_host(monkeypatch)
    monkeypatch.setattr(connection.webbrowser, 'open', lambda _: (_ for _ in ()).throw(RuntimeError('no browser')))
    assert main(['web']) == 0
    assert 'Dockbench: http://127.0.0.1:8787' in capsys.readouterr().out


def test_web_supports_explicit_no_browser_for_migrated_scripts(monkeypatch, capsys):
    events, commands, process = ssh_host(monkeypatch)
    assert main([]) == 0
    assert '\n    connect ' not in capsys.readouterr().out
    assert main(['web', 'gpu', '--local-port', '19403', '--port', '9403', '--no-open']) == 0
    output = capsys.readouterr()
    assert not output.err
    assert events == [('health', 'http://127.0.0.1:19403/api/health')]
    assert '127.0.0.1:19403:127.0.0.1:9403' in commands[0]


def test_unreachable_local_server_reports_actionable_failure_without_browser(monkeypatch, capsys):
    import urllib.error
    events = ready_host(monkeypatch)
    class Unreachable:
        def open(self, *args, **kwargs):
            raise urllib.error.URLError('connection refused')
    monkeypatch.setattr(connection.urllib.request, 'build_opener', lambda *args: Unreachable())
    assert main(['web', '--port', '9404']) == 1
    output = capsys.readouterr()
    assert 'http://127.0.0.1:9404' in output.err and 'dockbench server status' in output.err
    assert events == []


def test_remote_flags_override_yaml_and_false_browser_keeps_tunnel(monkeypatch, tmp_path):
    events, commands, process = ssh_host(monkeypatch)
    config = tmp_path / 'remote.yaml'
    config.write_text('schema_version: 1\nweb:\n  remote_port: 9405\n  local_port: 19405\n  open_browser: false\n')
    monkeypatch.setattr(connection.webbrowser, 'open', lambda url: False)
    assert main(['web', 'gpu', '--config', str(config), '--port', '9406', '--local-port', '19406', '--open-browser']) == 0
    assert '127.0.0.1:19406:127.0.0.1:9406' in commands[0]
    assert process.terminated


def test_null_local_port_chooses_a_free_port_when_default_busy(monkeypatch, tmp_path):
    import socket
    events, commands, process = ssh_host(monkeypatch)
    config = tmp_path / 'remote.yaml'
    config.write_text('schema_version: 1\nweb:\n  local_port: null\n')
    real_socket = socket.socket
    class Probe:
        def __init__(self, *args):
            self.socket = real_socket(*args)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            self.socket.close()
        def bind(self, address):
            if address[1] == 8787:
                raise OSError('occupied')
            self.socket.bind(address)
        def getsockname(self):
            return self.socket.getsockname()
    monkeypatch.setattr(socket, 'socket', Probe)
    assert main(['web', 'gpu', '--config', str(config), '--no-open']) == 0
    forward = commands[0][-2]
    assert forward.startswith('127.0.0.1:') and forward.endswith(':127.0.0.1:8787')
    assert not forward.startswith('127.0.0.1:8787:')
    assert process.terminated

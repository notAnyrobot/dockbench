import subprocess
from io import BytesIO

import pytest

from dockbench.core.errors import WorkstationError
from dockbench.core import server_connection as connection


class FakeProcess:
    def __init__(self, polls=None, returncode=0):
        self.polls = list(polls) if polls is not None else []
        self.last_poll = None
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.wait_calls = []

    def poll(self):
        if self.polls:
            self.last_poll = self.polls.pop(0)
        return self.last_poll

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.returncode


def ready_connection(monkeypatch, process=None, *, available=True, health=True):
    process = process or FakeProcess()
    commands = []
    monkeypatch.setattr(connection.shutil, "which", lambda name: "/usr/bin/ssh" if name == "ssh" else None)
    monkeypatch.setattr(connection, "_is_port_available", lambda port: available)
    monkeypatch.setattr(connection.subprocess, "Popen", lambda command: commands.append(command) or process)
    monkeypatch.setattr(connection, "_tunnel_is_listening", lambda port: True)
    monkeypatch.setattr(connection, "_health_ready", lambda url: health)
    return process, commands


def test_connect_uses_loopback_ssh_argv_without_opening_browser(monkeypatch):
    process, commands = ready_connection(monkeypatch)
    browser_urls = []
    monkeypatch.setattr(connection.webbrowser, "open", lambda url: browser_urls.append(url) or True)
    monkeypatch.setattr(connection.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))

    result = connection.connect("hpc-login", remote_port=8899)

    assert result.url == "http://127.0.0.1:8787"
    assert not result.browser_opened and result.interrupted
    assert browser_urls == []
    assert commands == [[
        "/usr/bin/ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
        "-L", "127.0.0.1:8787:127.0.0.1:8899", "hpc-login",
    ]]
    assert process.terminated and process.wait_calls == [5]


def test_connect_can_open_browser_after_announcing_ready_url(monkeypatch):
    process, _ = ready_connection(monkeypatch)
    events = []
    monkeypatch.setattr(connection.webbrowser, "open", lambda url: events.append(("browser", url)) or True)
    monkeypatch.setattr(connection.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))

    result = connection.connect("hpc", open_browser=True, on_ready=lambda url: events.append(("ready", url)))

    assert events == [("ready", result.url), ("browser", result.url)]


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, b'{"status":"ok"}', True),
        (200, b'{"status":"starting"}', False),
        (200, b"not-json", False),
        (503, b'{"status":"ok"}', False),
    ],
)
def test_health_requires_expected_json_payload(monkeypatch, status, body, expected):
    class Response:
        def __init__(self):
            self.status = status
            self.payload = BytesIO(body)

        def read(self):
            return self.payload.read()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class Opener:
        def open(self, url, timeout):
            return Response()

    monkeypatch.setattr(connection.urllib.request, "build_opener", lambda *handlers: Opener())
    assert connection._health_ready("http://127.0.0.1:8787") is expected


def test_loopback_health_check_disables_environment_proxy_discovery(monkeypatch):
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
            assert url == "http://127.0.0.1:8787/api/health"
            return Response()

    proxy_arguments = []
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setattr(
        connection.urllib.request,
        "ProxyHandler",
        lambda proxies: proxy_arguments.append(proxies) or object(),
    )
    monkeypatch.setattr(connection.urllib.request, "build_opener", lambda *handlers: Opener())

    assert connection._health_ready("http://127.0.0.1:8787")
    assert proxy_arguments == [{}]


def test_default_busy_port_uses_ephemeral_port(monkeypatch):
    process, commands = ready_connection(monkeypatch, available=False)
    monkeypatch.setattr(connection, "_choose_local_port", lambda requested: 43001)
    monkeypatch.setattr(connection.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))

    result = connection.connect("gpu", open_browser=False)

    assert result.local_port == 43001 and not result.browser_opened
    assert "127.0.0.1:43001:127.0.0.1:8787" in commands[0]
    assert process.terminated


def test_explicit_busy_local_port_is_rejected(monkeypatch):
    monkeypatch.setattr(connection, "_is_port_available", lambda port: False)
    with pytest.raises(WorkstationError, match="already in use"):
        connection.connect("gpu", local_port=8787)


def test_missing_ssh_is_actionable(monkeypatch):
    monkeypatch.setattr(connection, "_is_port_available", lambda port: True)
    monkeypatch.setattr(connection.shutil, "which", lambda name: None)
    with pytest.raises(WorkstationError, match="SSH command not found"):
        connection.connect("gpu")


def test_early_ssh_exit_is_reported_and_not_reterminated(monkeypatch):
    process, _ = ready_connection(monkeypatch, FakeProcess([23], returncode=23))
    with pytest.raises(WorkstationError, match="exit code 23"):
        connection.connect("gpu")
    assert not process.terminated


def test_health_timeout_starts_after_interactive_ssh_authentication(monkeypatch):
    process, _ = ready_connection(monkeypatch, health=False)
    tunnel_checks = iter([False, True])
    health_requests = []
    now = 0.0

    def monotonic():
        return now

    def sleep(_):
        nonlocal now
        now += connection.HEALTH_TIMEOUT_SECONDS + 1

    monkeypatch.setattr(connection, "_tunnel_is_listening", lambda port: next(tunnel_checks))
    monkeypatch.setattr(connection, "_health_ready", lambda url: health_requests.append(url) or False)
    monkeypatch.setattr(connection.time, "monotonic", monotonic)
    monkeypatch.setattr(connection.time, "sleep", sleep)

    with pytest.raises(WorkstationError, match="did not answer through the local forward") as exc_info:
        connection.connect("gpu", open_browser=False)
    assert "SSH tunnel to gpu is up" not in str(exc_info.value)
    assert "dockbench server status" in str(exc_info.value)
    assert health_requests == ["http://127.0.0.1:8787"]
    assert process.terminated and process.wait_calls == [5]


def test_connect_keeps_health_budget_after_authentication_then_cleans_up(monkeypatch):
    process, _ = ready_connection(monkeypatch, health=True)
    tunnel_checks = iter([False, True])
    now = 0.0
    sleep_count = 0

    def monotonic():
        return now

    def sleep(_):
        nonlocal now, sleep_count
        sleep_count += 1
        now += connection.HEALTH_TIMEOUT_SECONDS + 1
        if sleep_count == 2:
            raise KeyboardInterrupt()

    monkeypatch.setattr(connection, "_tunnel_is_listening", lambda port: next(tunnel_checks))
    monkeypatch.setattr(connection.time, "monotonic", monotonic)
    monkeypatch.setattr(connection.time, "sleep", sleep)

    result = connection.connect("gpu", open_browser=False)

    assert result.interrupted
    assert process.terminated and process.wait_calls == [5]


def test_ssh_exit_while_waiting_for_interactive_authentication_is_accurate(monkeypatch):
    process, _ = ready_connection(monkeypatch, FakeProcess([23], returncode=23))
    monkeypatch.setattr(connection, "_tunnel_is_listening", lambda port: False)

    with pytest.raises(WorkstationError, match="exited before it became usable \\(exit code 23\\)"):
        connection.connect("gpu", open_browser=False)

    assert not process.terminated and process.wait_calls == []


def test_unexpected_exit_after_readiness_is_reported(monkeypatch):
    process, _ = ready_connection(monkeypatch, FakeProcess([None, None, 7], returncode=7))
    with pytest.raises(WorkstationError, match=r"exited \(exit code 7\)"):
        connection.connect("gpu", open_browser=False)
    assert not process.terminated


@pytest.mark.parametrize("kwargs", [
    {"ssh_host": ""}, {"ssh_host": "bad\nhost"}, {"ssh_host": "gpu", "remote_port": 0},
    {"ssh_host": "-oProxyCommand=bad"}, {"ssh_host": "gpu", "local_port": True},
    {"ssh_host": "gpu", "open_browser": "yes"}, {"ssh_host": "gpu", "on_ready": "not-a-callback"},
])
def test_connect_validates_public_inputs(kwargs):
    with pytest.raises(WorkstationError):
        connection.connect(**kwargs)


def test_terminate_kills_after_grace_timeout():
    class StubbornProcess(FakeProcess):
        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if len(self.wait_calls) == 1:
                raise subprocess.TimeoutExpired("ssh", timeout)
            return 0

    process = StubbornProcess()
    connection._terminate(process)
    assert process.terminated and process.killed and process.wait_calls == [5, 5]

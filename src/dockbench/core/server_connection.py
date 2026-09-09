"""Loopback SSH tunnel lifecycle for a remote Dockbench server.

The module deliberately knows nothing about Docker or the Dockbench build.  A
local client only needs OpenSSH and an SSH configuration that can reach the
remote host; the server continues to bind to the remote loopback interface.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
import json
from typing import Callable

from dockbench.core.errors import WorkstationError


DEFAULT_SERVER_PORT = 8787
HEALTH_PATH = "/api/health"
HEALTH_TIMEOUT_SECONDS = 20.0
HEALTH_POLL_INTERVAL_SECONDS = 0.2
HEALTH_REQUEST_TIMEOUT_SECONDS = 1.0
SSH_SERVER_ALIVE_INTERVAL_SECONDS = 30
SSH_SERVER_ALIVE_COUNT_MAX = 3


@dataclass(frozen=True)
class ServerConnectionResult:
    """Details of a tunnel that reached Dockbench and then ended cleanly."""

    ssh_host: str
    local_port: int
    remote_port: int
    url: str
    browser_opened: bool
    interrupted: bool


def _validate_port(name: str, port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise WorkstationError(f"{name} must be an integer between 1 and 65535")
    return port


def _validate_ssh_host(ssh_host: str) -> str:
    if not isinstance(ssh_host, str):
        raise WorkstationError("SSH host must be a non-empty SSH host or configured alias")
    host = ssh_host.strip()
    if not host or host.startswith("-") or any(character in host for character in "\r\n\x00"):
        raise WorkstationError("SSH host must be a non-empty SSH host or configured alias")
    return host


def _is_port_available(port: int) -> bool:
    """Check loopback availability without retaining the port across ssh startup."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _choose_local_port(requested_port: int | None) -> int:
    if requested_port is not None:
        port = _validate_port("local port", requested_port)
        if not _is_port_available(port):
            raise WorkstationError(f"Local port {port} is already in use; choose another --local-port")
        return port
    if _is_port_available(DEFAULT_SERVER_PORT):
        return DEFAULT_SERVER_PORT
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])
    except OSError as exc:
        raise WorkstationError("Could not select a free local port for Dockbench") from exc


def _ssh_command(ssh_command: str, ssh_host: str, local_port: int, remote_port: int) -> list[str]:
    return [
        ssh_command,
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        f"ServerAliveInterval={SSH_SERVER_ALIVE_INTERVAL_SECONDS}",
        "-o",
        f"ServerAliveCountMax={SSH_SERVER_ALIVE_COUNT_MAX}",
        "-L",
        f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
        ssh_host,
    ]


def _health_ready(url: str) -> bool:
    # ``url`` is the local end of an SSH tunnel.  Explicitly disable proxy
    # discovery: urllib otherwise honors HTTP(S)_PROXY, which can send a
    # loopback health check to a corporate proxy instead of the tunnel.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url + HEALTH_PATH, timeout=HEALTH_REQUEST_TIMEOUT_SECONDS) as response:
            if not 200 <= response.status < 300:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            return isinstance(payload, dict) and payload.get("status") == "ok"
    except (json.JSONDecodeError, UnicodeDecodeError, urllib.error.URLError, OSError, TimeoutError):
        return False


def _tunnel_is_listening(port: int) -> bool:
    """Return whether SSH has installed the local forwarding listener.

    OpenSSH creates local forwarding listeners only after authentication and
    session setup.  Waiting for that listener keeps the Dockbench readiness
    timeout from running while the user is at an interactive password prompt.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=HEALTH_REQUEST_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _terminate(process: subprocess.Popen[object]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _unavailable_error(ssh_host: str, remote_port: int) -> WorkstationError:
    return WorkstationError(
        f"Dockbench did not answer through the local forward to {ssh_host} remote port {remote_port}. "
        "Verify 'dockbench server status' (or, if needed, 'dockbench deploy') on the remote host "
        "and SSH forwarding, then retry."
    )


def connect(
    ssh_host: str,
    *,
    local_port: int | None = None,
    remote_port: int = DEFAULT_SERVER_PORT,
    open_browser: bool = False,
    on_ready: Callable[[str], None] | None = None,
) -> ServerConnectionResult:
    """Open a foreground SSH tunnel to a remote Dockbench.

    ``local_port=None`` deliberately means "prefer 8787, otherwise choose a
    free port".  Passing an integer is an explicit choice and fails when that
    port is occupied.  The function returns only after Ctrl+C; a terminated SSH
    process is an error because it would otherwise silently leave a stale UI.
    """
    host = _validate_ssh_host(ssh_host)
    remote = _validate_port("remote port", remote_port)
    if not isinstance(open_browser, bool):
        raise WorkstationError("open_browser must be a boolean")
    if on_ready is not None and not callable(on_ready):
        raise WorkstationError("on_ready must be callable")
    local = _choose_local_port(local_port)
    ssh = shutil.which("ssh")
    if ssh is None:
        raise WorkstationError("SSH command not found: ssh")
    command = _ssh_command(ssh, host, local, remote)
    url = f"http://127.0.0.1:{local}"
    try:
        process: subprocess.Popen[object] = subprocess.Popen(command)
    except OSError as exc:
        raise WorkstationError(f"Could not start SSH tunnel to {host}: {exc}") from exc

    browser_opened = False
    try:
        # Do not put a deadline around interactive SSH authentication.  Until
        # OpenSSH has installed its local listener, a Dockbench health timeout
        # would misleadingly report that a tunnel exists when it does not.
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                raise WorkstationError(f"SSH tunnel to {host} exited before it became usable (exit code {exit_code})")
            if _tunnel_is_listening(local):
                break
            time.sleep(HEALTH_POLL_INTERVAL_SECONDS)

        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            exit_code = process.poll()
            if exit_code is not None:
                raise WorkstationError(f"SSH tunnel to {host} exited before Dockbench became ready (exit code {exit_code})")
            if _health_ready(url):
                if on_ready is not None:
                    on_ready(url)
                if open_browser:
                    browser_opened = bool(webbrowser.open(url))
                break
            time.sleep(HEALTH_POLL_INTERVAL_SECONDS)
        else:
            raise _unavailable_error(host, remote)

        while process.poll() is None:
            time.sleep(HEALTH_POLL_INTERVAL_SECONDS)
        raise WorkstationError(f"SSH tunnel to {host} exited (exit code {process.returncode})")
    except KeyboardInterrupt:
        return ServerConnectionResult(host, local, remote, url, browser_opened, True)
    finally:
        _terminate(process)

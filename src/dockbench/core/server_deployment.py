"""Deploy the loopback Dockbench server without requiring root privileges.

The module deliberately owns only the host-side process lifecycle.  It does
not start Docker containers, and the server stays loopback-only; clients reach
it through their own SSH tunnel.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping

from dockbench.core.resources import CheckoutResources
from dockbench.core.defaults import default_workspace_root, workspace_root_from_value
from dockbench.core.errors import WorkstationError


class DeploymentError(WorkstationError):
    """A safe, actionable failure while installing or operating Dockbench."""


_RUNTIME_ENVIRONMENT = frozenset({
    "DOCKBENCH_WORKSPACE", "DOCKBENCH_STATE_ROOT", "DOCKBENCH_DOCKER",
    "DOCKBENCH_VNC_PORT", "DOCKBENCH_SHM_SIZE",
    "DOCKBENCH_HOST_UID", "DOCKBENCH_HOST_GID", "DOCKBENCH_HOST_USER",
    "DOCKBENCH_IMAGE", "DOCKBENCH_CONTAINER", "DOCKBENCH_VNC_VIEWER",
})
_SYSTEMD_UNAVAILABLE = ("failed to connect to bus", "no medium found", "not been booted")


@dataclass(frozen=True)
class DeploymentOptions:
    """Inputs for a remote Dockbench installation.

    ``config_home`` and ``state_home`` are primarily useful to test and to
    embed this API.  Normal CLI callers leave them unset for XDG defaults.
    """

    repository_root: Path
    port: int = 8787
    workspace_root: Path | None = None
    state_root: Path | None = None
    docker_command: str | None = None
    config_home: Path | None = None
    state_home: Path | None = None
    health_timeout_seconds: float = 30.0
    runtime_environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DeploymentResult:
    manager: str
    url: str
    config_path: Path
    environment_path: Path
    log_path: Path | None = None
    unit_path: Path | None = None
    pid: int | None = None


@dataclass(frozen=True)
class ServerStatus:
    manager: str | None
    state: str
    message: str
    url: str
    pid: int | None = None
    log_path: Path | None = None


def _xdg_path(variable: str, fallback: str) -> Path:
    return Path(os.environ.get(variable, str(Path.home() / fallback))).expanduser()


def load_runtime_config(path: Path) -> dict[str, str]:
    """Load a deployment's safe environment snapshot.

    Invalid files fail closed.  In particular, this never permits a config
    file to inject arbitrary environment variables into the server process.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentError(f"cannot read Dockbench runtime config: {path}") from exc
    values = raw.get("environment") if isinstance(raw, dict) else None
    if not isinstance(values, dict):
        raise DeploymentError(f"invalid Dockbench runtime config: {path}")
    result: dict[str, str] = {}
    for key, value in values.items():
        if key in _RUNTIME_ENVIRONMENT and isinstance(value, str) and "\x00" not in value:
            result[key] = value
    return result


class ServerDeployment:
    """A small deployment facade designed for CLI integration and testing."""

    def __init__(self, options: DeploymentOptions) -> None:
        root = options.repository_root.expanduser().resolve()
        if not root.is_dir():
            raise DeploymentError(f"Dockbench repository does not exist: {root}")
        if not 1 <= options.port <= 65535:
            raise DeploymentError("Dockbench port must be between 1 and 65535")
        self.options = DeploymentOptions(
            repository_root=root, port=options.port, workspace_root=options.workspace_root,
            state_root=options.state_root, docker_command=options.docker_command,
            config_home=options.config_home, state_home=options.state_home,
            health_timeout_seconds=options.health_timeout_seconds, runtime_environment=options.runtime_environment,
        )
        self.config_dir = (options.config_home or _xdg_path("XDG_CONFIG_HOME", ".config")) / "dockbench" / "server"
        self.state_dir = (options.state_home or _xdg_path("XDG_STATE_HOME", ".local/state")) / "dockbench" / "server"
        self.config_path = self.config_dir / "server.json"
        self.environment_path = self.config_dir / "server.env"
        self.metadata_path = self.state_dir / "service.json"
        self.pid_path = self.state_dir / "server.pid"
        self.log_path = self.state_dir / "server.log"
        self.unit_path = (options.config_home or _xdg_path("XDG_CONFIG_HOME", ".config")) / "systemd" / "user" / "dockbench.service"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.options.port}"

    def runtime_environment(self) -> dict[str, str]:
        environment = {key: value for key, value in self.options.runtime_environment.items()
                       if key in _RUNTIME_ENVIRONMENT and isinstance(value, str) and "\x00" not in value}
        environment.update({key: os.environ[key] for key in _RUNTIME_ENVIRONMENT if key in os.environ})
        configured_root = environment.get("DOCKBENCH_WORKSPACE")
        try:
            if self.options.workspace_root is not None:
                workspace_root = workspace_root_from_value(str(self.options.workspace_root))
            elif configured_root:
                workspace_root = workspace_root_from_value(configured_root)
            else:
                workspace_root = default_workspace_root()
        except WorkstationError as exc:
            raise DeploymentError(f"{exc}; create it or pass --workspace PATH") from exc
        if workspace_root is None:
            raise DeploymentError("workspace root not found; create /data/$USER/workspace or pass --workspace PATH")
        environment["DOCKBENCH_WORKSPACE"] = str(workspace_root)
        if self.options.state_root is not None:
            environment["DOCKBENCH_STATE_ROOT"] = str(self.options.state_root.expanduser())
        if self.options.docker_command is not None:
            environment["DOCKBENCH_DOCKER"] = self.options.docker_command
        # VNC credentials are accepted only from the live process environment;
        # deployment configuration must never persist them.
        environment.pop("DOCKBENCH_VNC_PASSWORD", None)
        return environment

    @staticmethod
    def _mkdir_private(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    @staticmethod
    def _write_private(path: Path, value: str) -> None:
        temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            path.chmod(0o600)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)

    def _write_runtime_config(self, environment: Mapping[str, str] | None = None) -> dict[str, str]:
        self._mkdir_private(self.config_dir)
        self._mkdir_private(self.state_dir)
        values = dict(environment) if environment is not None else self.runtime_environment()
        config = {
            "schema_version": 1,
            "repository_root": str(self.options.repository_root),
            "port": self.options.port,
            "environment": values,
        }
        self._write_private(self.config_path, json.dumps(config, sort_keys=True) + "\n")
        # EnvironmentFile uses shell-like quoting.  Values in this application
        # are paths/commands; JSON quoting safely handles whitespace and quotes.
        self._write_private(self.environment_path, "".join(f"{key}={json.dumps(value)}\n" for key, value in sorted(values.items())))
        return values

    def _require_build_tools(self) -> tuple[str, str]:
        uv, node, npm = shutil.which("uv"), shutil.which("node"), shutil.which("npm")
        missing = [name for name, value in (("uv", uv), ("node", node), ("npm", npm)) if value is None]
        if missing:
            raise DeploymentError(f"required command not found: {', '.join(missing)}")
        assert uv is not None and node is not None and npm is not None
        return uv, npm

    @staticmethod
    def _command(args: list[str], *, cwd: Path) -> None:
        try:
            result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        except OSError as exc:
            raise DeploymentError(f"cannot run {' '.join(args[:2])}: {exc}") from exc
        if result.returncode:
            detail = (result.stdout or "").strip()
            raise DeploymentError(f"command failed ({result.returncode}): {' '.join(args)}{': ' + detail if detail else ''}")

    def _build(self, uv: str, npm: str) -> None:
        app = CheckoutResources.discover(self.options.repository_root).frontend_source
        if not app.is_dir():
            raise DeploymentError(f"Dockbench frontend directory does not exist: {app}")
        self._command([uv, "sync", "--frozen"], cwd=self.options.repository_root)
        self._command([npm, "ci"], cwd=app)
        self._command([npm, "run", "build"], cwd=app)

    @staticmethod
    def _systemd_probe() -> str:
        """Return available/unavailable; raise for a broken detected manager."""
        try:
            result = subprocess.run(["systemctl", "--user", "show-environment"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        except FileNotFoundError:
            return "unavailable"
        except OSError as exc:
            raise DeploymentError(f"cannot probe user systemd: {exc}") from exc
        if result.returncode == 0:
            return "available"
        detail = f"{result.stdout}\n{result.stderr}".lower()
        if any(marker in detail for marker in _SYSTEMD_UNAVAILABLE):
            return "unavailable"
        raise DeploymentError(f"user systemd is available but unhealthy: {(result.stderr or result.stdout).strip()}")

    def _install_systemd(self, uv: str, *, restart: bool = True) -> DeploymentResult:
        template_path = CheckoutResources.discover(self.options.repository_root).assets / "systemd" / "dockbench.service"
        try:
            template = template_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DeploymentError(f"cannot read Dockbench service template: {template_path}") from exc
        substitutions = {
            "__SERVER_WORKING_DIRECTORY__": str(self.options.repository_root),
            "__SERVER_ROOT__": str(self.options.repository_root),
            "__UV_EXECUTABLE__": uv,
            "__SERVER_CONFIG__": str(self.config_path),
            "__SERVER_ENV_FILE__": str(self.environment_path),
            "__SERVER_PORT__": str(self.options.port),
        }
        for old, new in substitutions.items():
            # Path directives consume the entire value literally, unlike ExecStart
            # arguments. Quote only command arguments; escape specifiers in both.
            if old in {"__SERVER_WORKING_DIRECTORY__", "__SERVER_ENV_FILE__"}:
                if any(character in new for character in "\x00\r\n"):
                    raise DeploymentError("systemd paths cannot contain NUL or line breaks")
            elif old != "__SERVER_PORT__":
                new = json.dumps(new)
            template = template.replace(old, new.replace("%", "%%"))
        self._mkdir_private(self.unit_path.parent)
        self._write_private(self.unit_path, template)
        for command in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "dockbench.service"],
            ["systemctl", "--user", "restart" if restart else "start", "dockbench.service"],
        ):
            self._command(command, cwd=self.options.repository_root)
        return DeploymentResult("systemd", self.url, self.config_path, self.environment_path, unit_path=self.unit_path)

    def _fallback_command(self, uv: str) -> list[str]:
        return [uv, "run", "--frozen", "--no-sync", "--project", str(self.options.repository_root), "dockbench", "server", "start", "--foreground", "--port", str(self.options.port), "--runtime-config", str(self.config_path)]

    def _install_fallback(self, uv: str, environment: Mapping[str, str]) -> DeploymentResult:
        self._stop_managed_fallback()
        self._mkdir_private(self.state_dir)
        command = self._fallback_command(uv)
        runtime_env = os.environ.copy()
        runtime_env.update(environment)
        try:
            log = self.log_path.open("a", encoding="utf-8")
            process = subprocess.Popen(command, cwd=self.options.repository_root, env=runtime_env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            raise DeploymentError(f"cannot start managed Dockbench process: {exc}") from exc
        finally:
            if "log" in locals():
                log.close()
        self._owned_process = process
        metadata = self._installation_metadata("process", process.pid, command)
        try:
            self._write_private(self.metadata_path, json.dumps(metadata, sort_keys=True) + "\n")
            self._write_private(self.pid_path, f"{process.pid}\n")
        except BaseException:
            self._stop_process(process.pid, expected=metadata.get("process_identity"))
            process.wait()
            raise
        return DeploymentResult("process", self.url, self.config_path, self.environment_path, log_path=self.log_path, pid=process.pid)

    def _wait_for_health(self, result: DeploymentResult) -> None:
        deadline = time.monotonic() + self.options.health_timeout_seconds
        last_error = "not ready"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{self.url}/api/health", timeout=1) as response:
                    payload = json.loads(response.read())
                    if response.status == 200 and payload == {"status": "ok"}:
                        return
                    last_error = f"health endpoint returned an unexpected response ({response.status})"
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                last_error = str(exc.reason if isinstance(exc, urllib.error.URLError) else exc)
            time.sleep(0.2)
        diagnostics = f"See journalctl --user -u dockbench.service" if result.manager == "systemd" else f"See {self.log_path}"
        raise DeploymentError(f"Dockbench did not become healthy at {self.url}: {last_error}. {diagnostics}")

    def deploy(self) -> DeploymentResult:
        environment = self.runtime_environment()
        uv, npm = self._require_build_tools()
        self._build(uv, npm)
        self._write_runtime_config(environment)
        manager = self._systemd_probe()
        if manager == "available":
            # A new systemd unit and an old fallback must never own the same
            # loopback port.  Stop only a process whose saved identity matches.
            self._stop_managed_fallback()
            result = self._install_systemd(uv)
        else:
            result = self._install_fallback(uv, environment)
        try:
            self._wait_for_health(result)
        except DeploymentError:
            if result.manager == "process" and result.pid is not None:
                self._stop_managed_fallback()
            raise
        if result.manager == "systemd":
            self._write_private(self.metadata_path, json.dumps(self._installation_metadata("systemd"), sort_keys=True) + "\n")
        return result

    def _read_metadata(self) -> dict[str, object] | None:
        try:
            value = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _process_identity(pid: int) -> int | None:
        """Linux proc start time used to make PID reuse harmless."""
        try:
            # ``starttime`` is field 22.  Split after the final ')' because a
            # process name is allowed to contain whitespace and parentheses.
            _, _, tail = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rpartition(")")
            values = tail.split()
            return int(values[19])  # state is index 0; starttime is field 22
        except (OSError, ValueError, IndexError):
            return None

    def _pid_matches(self, pid: int, expected: int | None) -> bool:
        return expected is not None and self._pid_alive(pid) and self._process_identity(pid) == expected

    def _installation_metadata(self, manager: str, pid: int | None = None, command: list[str] | None = None) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": 1, "manager": manager, "repository_root": str(self.options.repository_root),
            "port": self.options.port, "url": self.url, "config_path": str(self.config_path),
            "log_path": str(self.log_path),
        }
        if pid is not None:
            result.update({"pid": pid, "process_identity": self._process_identity(pid), "command": command or []})
        return result

    def _stop_process(self, pid: int, *, expected: int | None = None) -> None:
        if expected is not None and not self._pid_matches(pid, expected):
            return
        if expected is None and not self._pid_alive(pid):
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise DeploymentError(f"cannot stop managed Dockbench process {pid}: {exc}") from exc
        deadline = time.monotonic() + 5
        while self._pid_alive(pid) and (expected is None or self._pid_matches(pid, expected)) and time.monotonic() < deadline:
            time.sleep(0.1)
        if self._pid_alive(pid) and (expected is None or self._pid_matches(pid, expected)):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _stop_managed_fallback(self) -> None:
        metadata = self._read_metadata()
        if not metadata or metadata.get("manager") != "process":
            return
        pid, identity = metadata.get("pid"), metadata.get("process_identity")
        if isinstance(pid, int):
            child = getattr(self, "_owned_process", None)
            owned = child is not None and child.pid == pid
            if not isinstance(identity, int) and not owned:
                raise DeploymentError(
                    f"cannot safely stop managed Dockbench PID {pid}: its process identity is missing; "
                    "stop it manually before redeploying"
                )
            self._stop_process(pid, expected=identity if isinstance(identity, int) else None)
            if owned:
                child.wait()
        metadata.pop("pid", None)
        metadata.pop("process_identity", None)
        self._write_private(self.metadata_path, json.dumps(metadata, sort_keys=True) + "\n")
        self.pid_path.unlink(missing_ok=True)

    @staticmethod
    def _saved_url(metadata: Mapping[str, object], fallback: str) -> str:
        value = metadata.get("url")
        return value if isinstance(value, str) and value.startswith("http://127.0.0.1:") else fallback

    def status(self) -> ServerStatus:
        metadata = self._read_metadata()
        url = self._saved_url(metadata or {}, self.url)
        if metadata and metadata.get("manager") == "process":
            pid = metadata.get("pid")
            identity = metadata.get("process_identity")
            if isinstance(pid, int) and self._pid_matches(pid, identity if isinstance(identity, int) else None):
                return ServerStatus("process", "running", "managed fallback process is running", url, pid, self.log_path)
            return ServerStatus("process", "stopped", "managed fallback process is not running", url, pid if isinstance(pid, int) else None, self.log_path)
        if self._systemd_probe() == "unavailable":
            return ServerStatus(None, "absent", "user systemd is unavailable and no managed fallback exists", url)
        try:
            result = subprocess.run(["systemctl", "--user", "is-active", "dockbench.service"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        except OSError as exc:
            raise DeploymentError(f"cannot inspect Dockbench service: {exc}") from exc
        state = (result.stdout or "unknown").strip()
        return ServerStatus("systemd", "running" if state == "active" else "stopped", state or (result.stderr or "unknown").strip(), url)

    def start(self) -> ServerStatus:
        """Resume an installed server using these resolved desired options."""
        metadata = self._read_metadata()
        manager = metadata.get("manager") if metadata else None
        if manager not in {"process", "systemd"}:
            if not self.unit_path.is_file():
                raise DeploymentError("Dockbench is not deployed; run `dockbench server deploy` first")
            manager = "systemd"
        current = self.status()
        if current.state == "running":
            return replace(current, message=current.message + "; already running; stop/start applies desired settings")
        environment = self.runtime_environment()
        uv = shutil.which("uv")
        if uv is None:
            raise DeploymentError("required command not found: uv")
        self._write_runtime_config(environment)
        try:
            if manager == "process":
                result = self._install_fallback(uv, environment)
            else:
                result = self._install_systemd(uv, restart=False)
                self._write_private(self.metadata_path, json.dumps(self._installation_metadata("systemd")) + "\n")
            self._wait_for_health(result)
            current = self.status()
            if current.state != "running":
                raise DeploymentError(f"Dockbench exited before readiness at {self.url}; see {self.log_path} or journalctl --user -u dockbench.service")
        except (OSError, DeploymentError, KeyboardInterrupt):
            self.stop()
            raise
        return current

    def stop(self) -> ServerStatus:
        metadata = self._read_metadata()
        if metadata and metadata.get("manager") == "process":
            pid = metadata.get("pid")
            self._stop_managed_fallback()
            return ServerStatus("process", "stopped", "managed fallback process stopped", self._saved_url(metadata, self.url), pid if isinstance(pid, int) else None, self.log_path)
        if self._systemd_probe() == "unavailable":
            return ServerStatus(None, "absent", "no Dockbench service is running", self.url)
        self._command(["systemctl", "--user", "stop", "dockbench.service"], cwd=self.options.repository_root)
        return ServerStatus("systemd", "stopped", "user service stopped", self._saved_url(metadata or {}, self.url))

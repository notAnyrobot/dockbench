"""Typed desired host settings, independent of installed server identity."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path

import yaml

from dockbench.core.resources import CheckoutResources
from dockbench.core.errors import WorkstationError
from dockbench.core.server_connection import DEFAULT_SERVER_PORT
from dockbench.core.server_deployment import DeploymentOptions, load_runtime_config


@dataclass(frozen=True)
class ServerSettings:
    port: int | None = None
    workspace: Path | None = None
    state_root: Path | None = None
    docker_command: str | None = None


@dataclass(frozen=True)
class WebSettings:
    remote_port: int | None = None
    local_port: int | None = None
    open_browser: bool | None = None
    local_port_supplied: bool = False


@dataclass(frozen=True)
class HostConfig:
    path: Path
    server: ServerSettings = field(default_factory=ServerSettings)
    web: WebSettings = field(default_factory=WebSettings)


class _UniqueSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        for index, key in enumerate(keys):
            if key in keys[:index]:
                raise WorkstationError(f'duplicate host configuration field: {key}')
        return super().construct_mapping(node, deep=deep)


def _mapping(value: object, allowed: set[str], field: str) -> dict:
    if not isinstance(value, dict):
        raise WorkstationError(f'{field} must be a mapping')
    unknown = value.keys() - allowed
    if unknown:
        raise WorkstationError(f'{field}: unknown fields: {", ".join(map(str, unknown))}')
    return value


def _port(value: object, field: str) -> None:
    if type(value) is not int or not 1 <= value <= 65535:
        raise WorkstationError(f'{field} must be an integer between 1 and 65535')


def _text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or any(char in value for char in ('\x00', '\n', '\r')):
        raise WorkstationError(f'{field} must be a non-empty single-line string')


def load_host_config(config: str | Path | None = None, *,
                     resources: CheckoutResources | None = None) -> HostConfig:
    resources = resources or CheckoutResources.discover()
    path = Path(config).expanduser().resolve() if config is not None else resources.repository_root / 'config/dockbench.yaml'
    try:
        raw = yaml.load(path.read_text(encoding='utf-8'), Loader=_UniqueSafeLoader)
    except FileNotFoundError as exc:
        if config is None:
            return HostConfig(path)
        raise WorkstationError(f'cannot read host configuration: {path}') from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise WorkstationError(f'cannot read host configuration: {path}: {exc}') from exc
    raw = _mapping(raw, {'schema_version', 'server', 'web'}, str(path))
    if type(raw.get('schema_version')) is not int or raw['schema_version'] != 1:
        raise WorkstationError(f'{path}: schema_version must be 1')
    server = _mapping(raw.get('server', {}), {'port', 'workspace', 'state_root', 'docker_command'}, 'server')
    for key, value in server.items():
        (_port if key == 'port' else _text)(value, f'server.{key}')
    for key in ('workspace', 'state_root'):
        if key in server:
            value = Path(server[key]).expanduser()
            server[key] = (path.parent / value).resolve()
    web = _mapping(raw.get('web', {}), {'remote_port', 'local_port', 'open_browser'}, 'web')
    for key, value in web.items():
        if key == 'open_browser':
            if type(value) is not bool:
                raise WorkstationError('web.open_browser must be a boolean')
        elif key != 'local_port' or value is not None:
            _port(value, f'web.{key}')
    return HostConfig(path, ServerSettings(**server), WebSettings(**web, local_port_supplied='local_port' in web))


def saved_server_options(resources: CheckoutResources, *, config_home: Path | None = None) -> tuple[int | None, dict[str, str]]:
    """Read only a compatible checkout's effective snapshot; missing/old state is optional."""
    home = config_home or Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))).expanduser()
    path = home / 'dockbench/server/server.json'
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict) or raw.get('repository_root') != str(resources.repository_root):
            return None, {}
        port = raw.get('port')
        _port(port, 'saved server port')
        return port, load_runtime_config(path)
    except (OSError, ValueError, WorkstationError):
        return None, {}


def resolve_server_options(config: str | Path | None = None, *,
                           resources: CheckoutResources | None = None,
                           overrides: ServerSettings | None = None,
                           config_home: Path | None = None,
                           state_home: Path | None = None) -> DeploymentOptions:
    """Resolve desired settings without writing config, building, or managing a process."""
    resources = resources or CheckoutResources.discover()
    desired = load_host_config(config, resources=resources).server
    overrides = overrides or ServerSettings()
    saved_port, saved = saved_server_options(resources, config_home=config_home)

    def select(key: str, environment: str | None = None):
        for settings in (overrides, desired):
            value = getattr(settings, key)
            if value is not None:
                return value
        if environment:
            if key == 'workspace':
                # An empty legacy workspace value means discovery, never cwd.
                return os.environ.get(environment) or saved.get(environment) or None
            return os.environ.get(environment, saved.get(environment))
        return saved_port if saved_port is not None else DEFAULT_SERVER_PORT

    workspace = select('workspace', 'DOCKBENCH_WORKSPACE')
    state_root = select('state_root', 'DOCKBENCH_STATE_ROOT')
    return DeploymentOptions(
        repository_root=resources.repository_root,
        port=select('port'),
        workspace_root=Path(workspace).expanduser().resolve() if workspace is not None else None,
        state_root=Path(state_root).expanduser().resolve() if state_root is not None else None,
        docker_command=select('docker_command', 'DOCKBENCH_DOCKER'),
        config_home=config_home, state_home=state_home, runtime_environment=saved,
    )

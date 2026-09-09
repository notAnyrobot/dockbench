from pathlib import Path

from dockbench.core.host_config import load_host_config
from dockbench.core.resources import CheckoutResources


def test_checkout_yaml_resolves_paths_independently_of_caller(tmp_path, monkeypatch):
    checkout = tmp_path / 'checkout'
    config_dir = checkout / 'config'
    config_dir.mkdir(parents=True)
    (config_dir / 'dockbench.yaml').write_text('''schema_version: 1
server:
  port: 9012
  workspace: ../projects
  state_root: ~/dockbench-state
  docker_command: podman
web:
  remote_port: 9034
  local_port: null
  open_browser: false
''')
    monkeypatch.chdir(tmp_path)
    config = load_host_config(resources=CheckoutResources.discover(checkout))
    assert config.server.port == 9012
    assert config.server.workspace == checkout / 'projects'
    assert config.server.state_root == Path.home() / 'dockbench-state'
    assert config.server.docker_command == 'podman'
    assert config.web.remote_port == 9034
    assert config.web.local_port is None
    assert config.web.open_browser is False


def test_absent_default_is_optional_but_explicit_missing_file_is_an_error(tmp_path):
    import pytest
    from dockbench.core.errors import WorkstationError

    resources = CheckoutResources.discover(tmp_path)
    assert load_host_config(resources=resources).server.port is None
    with pytest.raises(WorkstationError, match='cannot read.*missing.yaml'):
        load_host_config(tmp_path / 'missing.yaml', resources=resources)


import pytest
from dockbench.core.errors import WorkstationError


@pytest.mark.parametrize('document, message', [
    ('', 'mapping'),
    ('[]', 'mapping'),
    ('server: {}', 'schema_version'),
    ('schema_version: true', 'schema_version'),
    ('schema_version: 2', 'schema_version'),
    ('schema_version: 1\ncredentials: secret', 'unknown'),
    ('schema_version: 1\nserver: null', 'server'),
    ('schema_version: 1\nserver: {port: true}', 'server.port'),
    ('schema_version: 1\nserver: {port: "9000"}', 'server.port'),
    ('schema_version: 1\nserver: {port: 65536}', 'server.port'),
    ('schema_version: 1\nserver: {workspace: null}', 'server.workspace'),
    ('schema_version: 1\nserver: {state_root: ""}', 'server.state_root'),
    ('schema_version: 1\nserver: {docker_command: []}', 'server.docker_command'),
    ('schema_version: 1\nweb: {open_browser: "false"}', 'web.open_browser'),
    ('schema_version: 1\nweb: {remote_port: null}', 'web.remote_port'),
    ('schema_version: 1\nweb: {host: example.org}', 'unknown'),
    ('schema_version: 1\nserver: [', 'cannot read'),
    ('!!python/object/apply:os.system ["false"]', 'cannot read'),
    ('schema_version: 1\nserver: {port: 9000, port: 9001}', 'duplicate'),
])
def test_invalid_yaml_is_rejected_with_actionable_field_errors(tmp_path, document, message):
    path = tmp_path / 'settings.yaml'
    path.write_text(document)
    with pytest.raises(WorkstationError, match=message):
        load_host_config(path)


def test_server_settings_resolve_flags_yaml_environment_saved_then_defaults(tmp_path, monkeypatch):
    import json
    from dockbench.core.host_config import ServerSettings, resolve_server_options

    resources = CheckoutResources.discover(tmp_path)
    saved_dir = tmp_path / 'xdg/dockbench/server'
    saved_dir.mkdir(parents=True)
    (saved_dir / 'server.json').write_text(json.dumps({
        'repository_root': str(tmp_path), 'port': 8901,
        'environment': {'DOCKBENCH_WORKSPACE': str(tmp_path), 'DOCKBENCH_DOCKER': 'saved-docker',
                        'DOCKBENCH_STATE_ROOT': str(tmp_path / 'saved-state'), 'DOCKBENCH_SHM_SIZE': '8g'},
    }))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    for key in ('DOCKBENCH_WORKSPACE', 'DOCKBENCH_STATE_ROOT', 'DOCKBENCH_DOCKER'):
        monkeypatch.delenv(key, raising=False)
    saved = resolve_server_options(resources=resources)
    assert (saved.port, saved.workspace_root, saved.docker_command) == (8901, tmp_path, 'saved-docker')
    assert saved.runtime_environment['DOCKBENCH_SHM_SIZE'] == '8g'
    monkeypatch.setenv('DOCKBENCH_DOCKER', 'env-docker')
    assert resolve_server_options(resources=resources).docker_command == 'env-docker'
    config = tmp_path / 'custom.yaml'
    config.write_text('schema_version: 1\nserver: {port: 8902, docker_command: yaml-docker, state_root: yaml-state}')
    configured = resolve_server_options(config=config, resources=resources)
    assert (configured.port, configured.docker_command, configured.state_root) == (8902, 'yaml-docker', tmp_path / 'yaml-state')
    monkeypatch.chdir(tmp_path)
    explicit = resolve_server_options(config=config, resources=resources,
        overrides=ServerSettings(port=8903, docker_command='cli-docker', state_root=Path('cli-state')))
    assert (explicit.port, explicit.docker_command, explicit.state_root) == (8903, 'cli-docker', tmp_path / 'cli-state')


def test_other_checkout_snapshot_is_not_used_and_optional_web_port_retains_presence(tmp_path, monkeypatch):
    import json
    from dockbench.core.host_config import resolve_server_options

    directory = tmp_path / 'xdg/dockbench/server'
    directory.mkdir(parents=True)
    (directory / 'server.json').write_text(json.dumps({'repository_root': '/other/checkout', 'port': 9999,
        'environment': {'DOCKBENCH_WORKSPACE': '/other/workspace', 'DOCKBENCH_SHM_SIZE': '8g'}}))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    monkeypatch.delenv('DOCKBENCH_WORKSPACE', raising=False)
    resources = CheckoutResources.discover(tmp_path)
    options = resolve_server_options(resources=resources)
    assert options.port == 8787
    assert options.workspace_root is None
    assert options.runtime_environment == {}
    assert load_host_config(resources=resources).web.local_port_supplied is False
    path = tmp_path / 'config.yaml'
    path.write_text('schema_version: 1\nweb: {local_port: null}')
    assert load_host_config(path).web.local_port_supplied is True


@pytest.mark.parametrize('live_workspace,saved_workspace', [('', None), ('', ''), (None, ''), ('', 'saved-workspace')])
def test_empty_legacy_workspace_uses_saved_or_default_from_foreign_directory(tmp_path, monkeypatch, live_workspace, saved_workspace):
    import json
    from dockbench.core.host_config import resolve_server_options
    from dockbench.core.server_deployment import ServerDeployment

    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    caller = tmp_path / 'foreign-caller'
    caller.mkdir()
    home = tmp_path / 'dockbench-test-home'
    default_workspace = home / 'workspace'
    default_workspace.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(caller)
    if live_workspace is None:
        monkeypatch.delenv('DOCKBENCH_WORKSPACE', raising=False)
    else:
        monkeypatch.setenv('DOCKBENCH_WORKSPACE', live_workspace)
    config_home = tmp_path / 'xdg'
    expected_workspace = default_workspace
    if saved_workspace is not None:
        saved_value = ''
        if saved_workspace:
            expected_workspace = tmp_path / saved_workspace
            expected_workspace.mkdir()
            saved_value = str(expected_workspace)
        snapshot = config_home / 'dockbench/server/server.json'
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(json.dumps({'repository_root': str(checkout), 'port': 8787,
                                       'environment': {'DOCKBENCH_WORKSPACE': saved_value}}))
    options = resolve_server_options(resources=CheckoutResources.discover(checkout), config_home=config_home)
    environment = ServerDeployment(options).runtime_environment()
    assert environment['DOCKBENCH_WORKSPACE'] == str(expected_workspace)

from fastapi.testclient import TestClient

from dockbench.core.backend import Backend
from dockbench.web.app import create_app


def test_recipe_and_health_do_not_require_workspace_or_docker(tmp_path):
    class Docker:
        def run(self, *args, **kwargs):
            raise AssertionError('Docker was contacted')

    backend = Backend(environment={'DOCKBENCH_WORKSPACE': str(tmp_path / 'missing')}, runner=Docker())
    assert backend.recipes.get('android-ws').id == 'android-ws'
    assert TestClient(create_app(backend=backend)).get('/api/health').json() == {'status': 'ok'}


def test_lifecycle_uses_injected_runner_and_snapshotted_settings(tmp_path, monkeypatch):
    calls = []
    class Docker:
        def run(self, args, **kwargs):
            calls.append(args)
            return '["name=rootless"]' if args[0] == 'info' else ''

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    environment = {'DOCKBENCH_WORKSPACE': str(workspace), 'DOCKBENCH_STATE_ROOT': str(tmp_path / 'state'),
                   'DOCKBENCH_DOCKER': 'missing-injected-docker', 'DOCKBENCH_CONTAINER': 'original'}
    backend = Backend(environment=environment, runner=Docker())
    environment['DOCKBENCH_CONTAINER'] = 'changed'
    assert calls == []
    assert backend.workstation.status().container_name == 'original'
    assert backend.config.docker_mode == 'rootless'
    assert backend.config.state_root == tmp_path / 'state'
    assert len([call for call in calls if call[0] == 'info']) == 1
    backend.fleet.containers()
    assert len([call for call in calls if call[0] == 'info']) == 1

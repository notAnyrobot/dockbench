import pytest
from pathlib import Path
import json

from dockbench.core.backend import Backend


class ArchiveDocker:
    def run(self, args, **kwargs):
        if args[:2] == ['image', 'ls']:
            return 'sha256:one\trepo/image\ttag'
        if args[:2] == ['image', 'inspect']:
            return json.dumps({'Id': 'sha256:one', 'RepoTags': ['repo/image:tag'], 'Config': {}})
        if 'save' in args:
            Path(args[args.index('--output') + 1]).write_bytes(b'archive')
        if 'load' in args:
            assert Path(args[args.index('--input') + 1]).read_bytes() == b'archive'
            return 'Loaded image: repo/image:tag'
        return ''


def test_backend_archives_resolve_selected_image_and_capture_import(tmp_path):
    backend = Backend(runner=ArchiveDocker())
    archive = tmp_path / 'chosen.tar'
    result = backend.images.export_archive('sha256:one', archive)
    assert result.archive.read_bytes() == b'archive'
    assert result.reference == 'repo/image:tag'
    assert backend.images.import_archive(archive) == 'Loaded image: repo/image:tag'


def test_http_archives_download_upload_and_cleanup(tmp_path, monkeypatch):
    import tempfile
    import time
    from fastapi.testclient import TestClient
    from dockbench.web.app import create_app

    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    with TestClient(create_app(backend=Backend(runner=ArchiveDocker()))) as client:
        response = client.get('/api/images/sha256:one/package')
        assert response.status_code == 200
        assert response.content == b'archive'
        assert response.headers['content-type'] == 'application/x-tar'
        assert 'repo_image_tag.tar' in response.headers['content-disposition']
        assert not list(tmp_path.glob('dockbench-image-*'))
        client.get('/api/image-recipes')
        headers = {'x-csrf-token': client.cookies['dockbench_csrf'], 'origin': 'http://testserver'}
        assert client.post('/api/images/load', content=b'').status_code == 403
        assert client.post('/api/images/load', content=b'', headers=headers).status_code == 422
        response = client.post('/api/images/load', content=iter([b'arch', b'ive']), headers=headers)
        assert response.status_code == 200
        job_id = response.json()['id']
        for _ in range(100):
            job = client.get(f'/api/image-jobs/{job_id}').json()
            if job['state'] in ('completed', 'failed'):
                break
            time.sleep(.01)
        assert job['state'] == 'completed'
        assert 'Loaded image: repo/image:tag' in job['logs']
        assert not list(tmp_path.glob('dockbench-image-*'))


def test_http_archive_failures_are_redacted_and_remove_temporary_files(tmp_path, monkeypatch):
    import tempfile
    import time
    from fastapi.testclient import TestClient
    from dockbench.core.errors import DockerCommandError
    from dockbench.web.app import create_app

    class BrokenDocker(ArchiveDocker):
        def run(self, args, **kwargs):
            if 'load' in args or 'save' in args:
                super().run(args, **kwargs)
                raise DockerCommandError('transfer failed token=private-value')
            return super().run(args, **kwargs)

    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    with TestClient(create_app(backend=Backend(runner=BrokenDocker()))) as client:
        response = client.get('/api/images/sha256:one/package')
        assert response.status_code == 503
        assert 'private-value' not in response.text
        assert not list(tmp_path.glob('dockbench-image-*'))
        client.get('/api/image-recipes')
        headers = {'x-csrf-token': client.cookies['dockbench_csrf'], 'origin': 'http://testserver'}
        response = client.post('/api/images/load', content=b'archive', headers=headers)
        for _ in range(100):
            job = client.get('/api/image-jobs/' + response.json()['id']).json()
            if job['state'] == 'failed':
                break
            time.sleep(.01)
        assert job['state'] == 'failed'
        assert job['code'] == 'docker_error'
        assert 'private-value' not in str(job)
        assert not list(tmp_path.glob('dockbench-image-*'))


@pytest.mark.asyncio
async def test_cancelled_download_waits_for_docker_before_removing_archive(tmp_path, monkeypatch):
    import asyncio
    import tempfile
    import threading
    import httpx
    from dockbench.web.app import create_app

    entered, release = threading.Event(), threading.Event()
    observed = []

    class BlockingDocker(ArchiveDocker):
        def run(self, args, **kwargs):
            if 'save' in args:
                path = Path(args[args.index('--output') + 1])
                entered.set()
                assert release.wait(5)
                observed.append(path.exists())
            return super().run(args, **kwargs)

    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(backend=Backend(runner=BlockingDocker()))), base_url='http://testserver') as client:
        request = asyncio.create_task(client.get('/api/images/sha256:one/package'))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            request.cancel()
            await asyncio.sleep(.02)
            assert list(tmp_path.glob('dockbench-image-*'))
        finally:
            release.set()
        import pytest
        with pytest.raises(asyncio.CancelledError):
            await request
    assert observed == [True]
    assert not list(tmp_path.glob('dockbench-image-*'))


def test_shutdown_cleans_queued_upload_without_loading_it(tmp_path, monkeypatch):
    import tempfile
    import threading
    from fastapi.testclient import TestClient
    from dockbench.web.app import create_app

    started, release = threading.Event(), threading.Event()
    loaded = []

    class BlockingDocker(ArchiveDocker):
        def run(self, args, **kwargs):
            if 'load' in args:
                path = Path(args[args.index('--input') + 1])
                loaded.append(path)
                started.set()
                assert release.wait(5)
                assert path.exists()
            return super().run(args, **kwargs)

    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    with TestClient(create_app(backend=Backend(runner=BlockingDocker()))) as client:
        client.get('/api/image-recipes')
        headers = {'x-csrf-token': client.cookies['dockbench_csrf'], 'origin': 'http://testserver'}
        first = client.post('/api/images/load', content=b'archive', headers=headers)
        assert first.status_code == 200
        assert started.wait(5)
        second = client.post('/api/images/load', content=b'archive', headers=headers)
        assert second.json()['state'] == 'queued'
        # Release the active Docker operation after application shutdown begins.
        timer = threading.Timer(.1, release.set)
        timer.start()
    timer.join()
    assert len(loaded) == 1
    assert not list(tmp_path.glob('dockbench-image-*'))

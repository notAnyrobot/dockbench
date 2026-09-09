"""Recipe HTTP adaptation, progress, and job outcomes."""
import json
import time

import pytest

from fastapi.testclient import TestClient

from dockbench.web.app import create_app
from web_fakes import FakeWorkstation, FakeFleet
from dockbench.core.errors import DockerCommandError
from dockbench.core.recipes import RecipeError
from dockbench.web.security import _redact_image_log


class FakeManifest:
    def __init__(self, recipe_id="android-ws", revision=1, tag="android-ws:test", target="desktop", platform="linux/amd64"):
        self.id, self.revision, self.dockerfile = recipe_id, revision, f"Dockerfile.{recipe_id}-v{revision}"
        self.tag, self.target, self.platform = tag, target, platform


class FakeRecipe:
    def __init__(self, *args, **kwargs): self.manifest = FakeManifest(*args, **kwargs)


class FakeRecipes:
    def __init__(self): self.items = {"android-ws": FakeRecipe()}; self.created = None; self.revised = None
    def list(self): return tuple(self.items.values())
    def get(self, recipe_id): return self.items[recipe_id]
    def create(self, recipe_id, dockerfile, **kwargs):
        if recipe_id in self.items: raise RecipeError(f"recipe already exists: {recipe_id}")
        self.created = (recipe_id, dockerfile, kwargs); item = FakeRecipe(recipe_id, **kwargs); self.items[recipe_id] = item; return item
    def revise(self, recipe_id, dockerfile, **kwargs):
        self.revised = (recipe_id, dockerfile, kwargs); old = self.items[recipe_id].manifest; item = FakeRecipe(recipe_id, old.revision + 1, kwargs.get("tag", old.tag), kwargs.get("target", old.target), kwargs.get("platform", old.platform)); self.items[recipe_id] = item; return item


class FakeImageBuilder:
    def __init__(self): self.calls = []
    def build(self, recipe, *, on_progress=None, **kwargs):
        self.calls.append((recipe, kwargs))
        if on_progress is not None:
            on_progress("#7 downloading token=private-package")
            on_progress("#7 42% complete")
        return "build completed"


class FakeImageVerifier:
    def __init__(self): self.calls = []
    def verify(self, image): self.calls.append(image); return "verified"


def test_image_job_log_redaction_removes_secret_values():
    log = _redact_image_log("build token=private password:also-private normal output")
    assert "private" not in log
    assert "token=[REDACTED]" in log
    assert "password:[REDACTED]" in log


def test_recipe_api_requires_csrf_and_creates_revisions_without_starting_builds():
    recipes = FakeRecipes()
    builder = FakeImageBuilder()
    client = TestClient(create_app(FakeWorkstation(), fleet=FakeFleet(), recipes=recipes, image_builder=builder, image_verifier=FakeImageVerifier()))
    listed = client.get("/api/image-recipes")
    assert listed.status_code == 200
    assert listed.json()["recipes"] == [{"id": "android-ws", "revision": 1, "dockerfile": "Dockerfile.android-ws-v1", "tag": "android-ws:test", "target": "desktop", "platform": "linux/amd64"}]
    payload = {"id": "custom-ws", "dockerfile": "FROM ubuntu:24.04\n", "tag": "custom:one", "target": None, "platform": "linux/amd64"}
    assert client.post("/api/image-recipes", json=payload).status_code == 403
    headers = {"origin": "http://testserver", "x-csrf-token": listed.json()["csrf_token"]}
    created = client.post("/api/image-recipes", json=payload, headers=headers)
    assert created.status_code == 200
    assert recipes.created == ("custom-ws", "FROM ubuntu:24.04\n", {"tag": "custom:one", "target": None, "platform": "linux/amd64"})
    assert builder.calls == []
    revised = client.post("/api/image-recipes/custom-ws/revisions", json={"dockerfile": "FROM ubuntu:24.04\nRUN true\n", "target": "desktop"}, headers=headers)
    assert revised.status_code == 200
    assert recipes.revised == ("custom-ws", "FROM ubuntu:24.04\nRUN true\n", {"target": "desktop"})


def test_build_and_verify_are_csrf_protected_serialized_image_jobs():
    recipes, builder, verifier = FakeRecipes(), FakeImageBuilder(), FakeImageVerifier()
    app = create_app(FakeWorkstation(), fleet=FakeFleet(), recipes=recipes,
                     image_builder=builder, image_verifier=verifier)
    with TestClient(app) as client:
        def wait_for_job(job_id):
            for _ in range(100):
                response = client.get(f"/api/image-jobs/{job_id}")
                if response.json()["state"] in {"completed", "failed"}:
                    return response
                time.sleep(0.001)
            pytest.fail(f"image job did not complete: {job_id}")

        token = client.get("/api/image-recipes").json()["csrf_token"]
        headers = {"origin": "http://testserver", "x-csrf-token": token}
        assert client.post("/api/images/build", json={}).status_code == 403
        build = client.post("/api/images/build", json={"recipe_id": "android-ws", "tag": "custom:two", "target": None, "no_cache": True}, headers=headers)
        assert build.status_code == 200
        build_job = wait_for_job(build.json()["id"])
        assert build_job.json()["state"] == "completed"
        assert "#7 downloading token=[REDACTED]" in build_job.json()["logs"]
        assert "#7 42% complete" in build_job.json()["logs"]
        assert builder.calls and builder.calls[0][1] == {"tag": "custom:two", "target": None, "no_cache": True}
        verify = client.post("/api/images/sha256:one/verify", headers=headers)
        assert verify.status_code == 200
        verify_job = wait_for_job(verify.json()["id"])
        assert verify_job.json()["state"] == "completed"
        assert verifier.calls == ["sha256:one"]


def test_failed_image_build_reports_sanitized_progress_and_actionable_error():
    class FailingImageBuilder:
        def build(self, recipe, *, on_progress, **kwargs):
            on_progress("#2 pulling token=private-registry-token")
            raise DockerCommandError("Error response from daemon: registry timeout token=private-registry-token")

    app = create_app(FakeWorkstation(), fleet=FakeFleet(), recipes=FakeRecipes(),
                     image_builder=FailingImageBuilder(), image_verifier=FakeImageVerifier())
    with TestClient(app) as client:
        token = client.get("/api/image-recipes").json()["csrf_token"]
        started = client.post("/api/images/build", json={"recipe_id": "android-ws"},
                              headers={"origin": "http://testserver", "x-csrf-token": token})
        for _ in range(100):
            job = client.get(f"/api/image-jobs/{started.json()['id']}").json()
            if job["state"] == "failed":
                break
            time.sleep(0.001)
        else:
            pytest.fail("image build did not fail")

        assert "#2 pulling token=[REDACTED]" in job["logs"]
        assert job["code"] == "docker_error"
        assert "registry timeout" in job["message"]
        assert "private-registry-token" not in json.dumps(job)

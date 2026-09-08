"""HTTP recipe catalog and build/verification job adaptation."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import Cookie, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from dockbench.core.backend import Backend
from dockbench.web.image_jobs import ImageJobs
from dockbench.web.security import safe_error, _require_csrf, _issue_csrf


class ImageBuildRequest(BaseModel):
    recipe_id: str = Field(default="android-ws", min_length=1, max_length=96,
                           pattern=r"[a-z0-9]+(?:-[a-z0-9]+)*")
    tag: str | None = Field(default=None, min_length=1, max_length=512)
    target: str | None = Field(default=None, min_length=1, max_length=128)
    platform: str | None = Field(default=None, min_length=1, max_length=128)
    no_cache: bool = False


class RecipeCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=96, pattern=r"[a-z0-9]+(?:-[a-z0-9]+)*")
    dockerfile: str = Field(min_length=1, max_length=1_048_576)
    tag: str = Field(min_length=1, max_length=512)
    target: str | None = Field(default=None, max_length=128)
    platform: str = Field(default="linux/amd64", min_length=1, max_length=128)


class RecipeReviseRequest(BaseModel):
    dockerfile: str = Field(min_length=1, max_length=1_048_576)
    tag: str | None = Field(default=None, max_length=512)
    target: str | None = Field(default=None, max_length=128)
    platform: str | None = Field(default=None, max_length=128)


def register_recipe_routes(app: FastAPI, backend: Backend, jobs: ImageJobs, *,
                           recipes: Any | None = None, image_builder: Any | None = None,
                           image_verifier: Any | None = None) -> None:
    def recipe_catalog() -> Any:
        return recipes if recipes is not None else backend.recipes

    def recipe_builder() -> Any:
        return image_builder if image_builder is not None else backend.image_builder

    def image_verifier_capability() -> Any:
        return image_verifier if image_verifier is not None else backend.image_verifier

    def recipe_public(recipe: Any) -> dict[str, Any]:
        manifest = getattr(recipe, "manifest", recipe)
        return {
            "id": getattr(manifest, "id"),
            "revision": getattr(manifest, "revision"),
            "dockerfile": getattr(recipe, "dockerfile", getattr(manifest, "dockerfile", None)),
            "tag": getattr(manifest, "tag"),
            "target": getattr(manifest, "target", None),
            "platform": getattr(manifest, "platform", None),
        }

    @app.get("/api/image-recipes")
    async def image_recipes(response: Response, dockbench_csrf: str | None = Cookie(default=None)):
        token = _issue_csrf(response, dockbench_csrf)
        try:
            items = await run_in_threadpool(recipe_catalog().list)
            return {"recipes": [recipe_public(item) for item in items], "csrf_token": token}
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/image-recipes")
    async def create_image_recipe(body: RecipeCreateRequest, request: Request,
                                  dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            recipe = await run_in_threadpool(
                recipe_catalog().create, body.id, body.dockerfile,
                tag=body.tag, target=body.target, platform=body.platform,
            )
            return recipe_public(recipe)
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/image-recipes/{recipe_id}/revisions")
    async def revise_image_recipe(recipe_id: str, body: RecipeReviseRequest, request: Request,
                                  dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            # Only pass requested defaults: omitted fields retain their prior
            # manifest values while an explicit null target clears the target.
            defaults = {field: getattr(body, field) for field in ("tag", "target", "platform")
                        if field in body.model_fields_set}
            recipe = await run_in_threadpool(recipe_catalog().revise, recipe_id, body.dockerfile, **defaults)
            return recipe_public(recipe)
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/images/build")
    async def build_image(body: ImageBuildRequest, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            recipe = await run_in_threadpool(recipe_catalog().get, body.recipe_id)

            overrides = {field: getattr(body, field) for field in ("tag", "target", "platform")
                         if field in body.model_fields_set}

            def build(report: Callable[[str], None]) -> Any:
                return recipe_builder().build(recipe, no_cache=body.no_cache,
                                              on_progress=report, **overrides)

            job = jobs.start("no-cache build" if body.no_cache else "build", build)
            return {"id": job.id, "kind": job.kind, "state": job.state}
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/images/{image_id}/verify")
    async def verify_image(image_id: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            job = jobs.start("verify", lambda _report: image_verifier_capability().verify(image_id))
            return {"id": job.id, "kind": job.kind, "state": job.state}
        except Exception as exc:
            return safe_error(exc)


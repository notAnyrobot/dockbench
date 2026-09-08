"""Same-origin FastAPI host for the desktop-first Dockbench browser app."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dockbench.web.containers import register_container_routes
from dockbench.core.backend import Backend
from dockbench.core.workstation import Workstation

from dockbench.web.image_jobs import ImageJobs
from dockbench.web.archives import register_archive_routes
from dockbench.web.recipes import register_recipe_routes

from dockbench.web.security import install_security

from dockbench.web.access import register_access_routes


def create_app(workstation: Workstation | None = None, fleet: Any | None = None,
               recipes: Any | None = None, image_builder: Any | None = None,
               image_verifier: Any | None = None, *, repository_root: Path | None = None,
               backend: Backend | None = None) -> FastAPI:
    backend = backend if backend is not None else Backend(repository_root=repository_root)
    resources = backend.resources
    app = FastAPI(title="Dockbench", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.workstation = workstation
    app.state.fleet = fleet
    jobs = ImageJobs(app)
    app.state.recipes = recipes
    app.state.image_builder = image_builder
    app.state.image_verifier = image_verifier

    install_security(app)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        """Report that the HTTP server is ready without contacting Docker.

        Deployment and SSH-tunnel clients use this endpoint as their readiness
        probe.  It deliberately avoids the workstation and fleet services so a
        reachable Dockbench server can still report ready while Docker is stopped or
        temporarily unavailable.
        """
        return {"status": "ok"}

    def ws() -> Workstation:
        return workstation if workstation is not None else backend.workstation

    def managed_fleet() -> Any:
        return fleet if fleet is not None else backend.fleet

    close_desktop_sockets = register_access_routes(app, ws, managed_fleet, backend)

    register_container_routes(app, ws, managed_fleet, backend.host_defaults, close_desktop_sockets)

    register_recipe_routes(app, backend, jobs, recipes=recipes, image_builder=image_builder,
                           image_verifier=image_verifier)

    register_archive_routes(app, backend, jobs)

    if resources.frontend_dist.is_dir():
        app.mount("/assets", StaticFiles(directory=resources.frontend_dist / "assets"), name="assets")

        @app.get("/{path:path}")
        async def frontend(path: str):
            candidate = resources.frontend_dist / path
            return FileResponse(candidate if path and candidate.is_file() else resources.frontend_dist / "index.html")
    else:
        @app.get("/")
        async def unavailable_frontend():
            return JSONResponse(status_code=503, content={"code": "frontend_not_built", "message": "Build the Dockbench frontend with npm run build."})
    return app

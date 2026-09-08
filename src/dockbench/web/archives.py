"""HTTP archive transport and ownership of temporary upload/download files."""
import asyncio
from anyio import CancelScope
from pathlib import Path
import tempfile

from fastapi import Cookie, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from dockbench.core.backend import Backend
from dockbench.web.image_jobs import ImageJobs
from dockbench.web.security import _require_csrf, safe_error


class ArchiveResponse(FileResponse):
    """Release the download even when response transmission is interrupted."""
    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            Path(self.path).unlink(missing_ok=True)


def register_archive_routes(app: FastAPI, backend: Backend, jobs: ImageJobs) -> None:
    @app.post("/api/images/load")
    async def load_image(request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        temporary = tempfile.NamedTemporaryFile(prefix="dockbench-image-", suffix=".tar", delete=False)
        path = Path(temporary.name)
        scheduled = False
        try:
            async for chunk in request.stream():
                temporary.write(chunk)
            temporary.close()
            if path.stat().st_size == 0:
                raise HTTPException(422, "Image archive is empty")
            job = jobs.start("load", lambda _report: backend.images.import_archive(path),
                             cleanup=lambda: path.unlink(missing_ok=True))
            scheduled = True
            return {"id": job.id, "kind": job.kind, "state": job.state}
        except HTTPException:
            raise
        except Exception as exc:
            return safe_error(exc)
        finally:
            temporary.close()
            if not scheduled:
                path.unlink(missing_ok=True)

    @app.get("/api/images/{image_id}/package")
    async def package_image(image_id: str):
        temporary = tempfile.NamedTemporaryFile(prefix="dockbench-image-", suffix=".tar", delete=False)
        path = Path(temporary.name)
        temporary.close()
        transferred = False
        try:
            work = asyncio.create_task(run_in_threadpool(backend.images.export_archive, image_id, path))
            try:
                result = await asyncio.shield(work)
            except asyncio.CancelledError:
                # A cancelled request cannot stop Docker's thread. Wait before
                # deleting its destination, including a partially written file.
                try:
                    with CancelScope(shield=True):
                        await asyncio.shield(work)
                finally:
                    raise
            filename = f"{result.reference.replace('/', '_').replace(':', '_')}.tar"
            response = ArchiveResponse(path, media_type="application/x-tar", filename=filename)
            transferred = True
            return response
        except Exception as exc:
            return safe_error(exc)
        finally:
            if not transferred:
                path.unlink(missing_ok=True)

"""Serialized image jobs and their bounded, redacted browser progress."""
import asyncio
from anyio import CancelScope
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from dockbench.core.errors import DockerCommandError
from dockbench.web.security import _redact_image_log, _safe_docker_error

LOG = logging.getLogger(__name__)
MAX_IMAGE_JOB_LOG_LINES = 2000


@dataclass
class ImageJob:
    id: str
    kind: str
    state: str = "running"
    message: str = ""
    created_at: float = 0
    logs: list[str] = field(default_factory=list)
    code: str | None = None



class ImageJobs:
    def __init__(self, app: FastAPI) -> None:
        self._jobs: dict[str, ImageJob] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        previous_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application: FastAPI):
            async with previous_lifespan(application) as state:
                try:
                    yield state
                finally:
                    await self.close()

        app.router.lifespan_context = lifespan

        @app.get("/api/image-jobs/{job_id}")
        async def image_job(job_id: str):
            job = self._jobs.get(job_id)
            if job is None:
                raise HTTPException(404, "Image job not found")
            return {"id": job.id, "kind": job.kind, "state": job.state, "message": job.message,
                    "code": job.code, "created_at": job.created_at, "logs": job.logs}


    async def close(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def start(self, kind: str, operation: Callable[[Callable[[str], None]], Any], *, cleanup: Callable[[], None] | None = None) -> ImageJob:
        queued = self._lock.locked()
        job = ImageJob(uuid.uuid4().hex, kind, state="queued" if queued else "running",
                       message="Waiting for another image operation." if queued else "Starting image operation.",
                       created_at=time.time(), logs=["queued" if queued else "starting"])
        self._jobs[job.id] = job

        def report(value: str) -> None:
            lines = _redact_image_log(value).splitlines() or [""]
            job.logs.extend(lines)
            if len(job.logs) > MAX_IMAGE_JOB_LOG_LINES:
                del job.logs[:-MAX_IMAGE_JOB_LOG_LINES]

        started = False

        def execute() -> Any:
            nonlocal started
            started = True
            try:
                return operation(report)
            finally:
                if cleanup is not None:
                    cleanup()

        async def run() -> None:
            async with self._lock:
                job.state = "running"
                job.message = "Image operation is running."
                report("running")
                try:
                    work = asyncio.create_task(run_in_threadpool(execute))
                    try:
                        output = await asyncio.shield(work)
                    except asyncio.CancelledError:
                        # Keep the serialization lock until Docker and cleanup
                        # finish; cancelling asyncio cannot stop a worker thread.
                        try:
                            with CancelScope(shield=True):
                                await asyncio.shield(work)
                        finally:
                            raise
                except Exception as exc:
                    LOG.warning("image job failed id=%s kind=%s error=%s", job.id, kind, type(exc).__name__)
                    job.state = "failed"
                    if isinstance(exc, DockerCommandError):
                        job.code = "docker_error"
                        job.message = _safe_docker_error(str(exc))
                        report(job.message)
                    else:
                        job.message = "Image operation failed. Check Dockbench logs and try again."
                    report("failed")
                else:
                    if isinstance(output, str) and output:
                        report(output)
                    job.state = "completed"
                    job.message = "Image operation completed."
                    report("completed")

        coroutine = run()
        try:
            task = asyncio.create_task(coroutine)
        except BaseException:
            coroutine.close()
            self._jobs.pop(job.id, None)
            raise
        self._tasks.add(task)
        def finished(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            if not started and cleanup is not None:
                cleanup()

        task.add_done_callback(finished)
        return job


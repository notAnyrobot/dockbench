"""HTTP contracts for managed-container lifecycle and host inventory."""
from __future__ import annotations

from typing import Any, Callable, Awaitable
from fastapi import Cookie, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from dockbench.core.fleet import FleetManager
from dockbench.core.workstation import Workstation
from dockbench.web.security import safe_error, _require_csrf, _issue_csrf


class StartRequest(BaseModel):
    image: str | None = Field(default=None, min_length=1, max_length=512)
    gpu_uuids: list[str] = Field(default_factory=list, max_length=64)
    all_gpus: bool | None = None
    replace: bool = False


class ContainerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=96, pattern=r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")
    image: str = Field(min_length=1, max_length=512)
    gpu_uuids: list[str] = Field(default_factory=list, max_length=64)
    all_gpus: bool = False
    workspace_root: str | None = Field(default=None, min_length=1, max_length=4096)
    data_root: str | None = Field(default=None, min_length=1, max_length=4096)


def container_public(status: Any) -> dict[str, Any]:
    result = status.public() if hasattr(status, "public") else dict(status)
    result["name"] = result.get("container_name", "")
    return result


def register_container_routes(
    app: FastAPI, ws: Callable[[], Workstation], managed_fleet: Callable[[], FleetManager],
    host_defaults: Callable[[], dict[str, str | None]], _close_desktop_sockets: Callable[..., Awaitable[None]],
) -> None:
    async def _fleet_call(method: str, *args: Any, **kwargs: Any) -> Any:
        return await run_in_threadpool(getattr(managed_fleet(), method), *args, **kwargs)

    @app.get("/api/workstation")
    async def workstation_status(response: Response, dockbench_csrf: str | None = Cookie(default=None)):
        token = _issue_csrf(response, dockbench_csrf)
        try:
            return {**ws().status().public(), "csrf_token": token}
        except Exception as exc:
            return safe_error(exc)

    @app.get("/api/host/inventory")
    async def host_inventory():
        try:
            return {
                **await inventory_with_reservations(),
                "default_all_gpus": True,
                **host_defaults(),
            }
        except Exception as exc:
            return safe_error(exc)

    async def inventory_with_reservations() -> dict[str, Any]:
        result = await _fleet_call("inventory")
        # FleetManager owns reservation decisions.  The UI calls its display
        # field `owner`; retain `reservation` for external consumers that used
        # the core representation directly.
        result["gpus"] = [{**gpu, "owner": gpu.get("reservation")} for gpu in result["gpus"]]
        return result

    @app.get("/api/containers")
    async def containers(response: Response, dockbench_csrf: str | None = Cookie(default=None)):
        token = _issue_csrf(response, dockbench_csrf)
        try:
            items = await _fleet_call("containers")
            return {"containers": [container_public(item) for item in items], "csrf_token": token}
        except Exception as exc:
            return safe_error(exc)

    @app.get("/api/containers/{name}")
    async def container(name: str):
        try:
            return container_public(await _fleet_call("container", name))
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers")
    async def create_container(body: ContainerCreateRequest, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            status = await _fleet_call(
                "create", body.name, body.image, tuple(body.gpu_uuids), body.all_gpus,
                body.workspace_root, body.data_root,
            )
            return container_public(status)
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers/{name}/start")
    async def start_container(name: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            return container_public(await _fleet_call("start", name))
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers/{name}/stop")
    async def stop_container(name: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            status = await _fleet_call("stop", name)
            await _close_desktop_sockets(name)
            return container_public(status)
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers/{name}/remove")
    async def remove_container(name: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            await _fleet_call("remove", name)
            await _close_desktop_sockets(name)
            return {"removed": True, "name": name}
        except Exception as exc:
            return safe_error(exc)

    @app.delete("/api/containers/{name}")
    async def remove_container_delete(name: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        return await remove_container(name, request, dockbench_csrf)

    @app.delete("/api/containers/{name}/state")
    async def delete_container_state(name: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            await _fleet_call("delete_state", name)
            return {"state_deleted": True, "name": name}
        except Exception as exc:
            return safe_error(exc)

    @app.get("/api/container-states")
    async def container_states():
        try:
            names = await _fleet_call("orphaned_states")
            return {"container_states": [{"name": name} for name in names]}
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/containers/{name}/recreate")
    async def recreate_container(name: str, request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            status = await _fleet_call("recreate", name)
            await _close_desktop_sockets(name)
            return container_public(status)
        except Exception as exc:
            return safe_error(exc)

    @app.get("/api/images")
    async def images():
        try:
            data = await inventory_with_reservations()
            containers = await _fleet_call("containers")
            dependents: dict[str, list[str]] = {}
            stale: dict[str, list[str]] = {}
            for item in containers:
                image_id = item.image_id
                if image_id:
                    dependents.setdefault(image_id, []).append(item.container_name)
                    if getattr(item, "stale", False): stale.setdefault(image_id, []).append(item.container_name)
            data["images"] = [{**image, "dependent_containers": dependents.get(str(image["id"]), []), "stale_dependents": stale.get(str(image["id"]), [])} for image in data["images"]]
            return {"images": data["images"]}
        except Exception as exc:
            return safe_error(exc)

    @app.get("/api/gpus")
    async def gpus():
        try:
            data = await inventory_with_reservations()
            return {"gpus": data["gpus"], "gpu_diagnostic": data["gpu_diagnostic"]}
        except Exception as exc:
            return safe_error(exc)

    @app.post("/api/workstation/start")
    async def start_workstation(request: Request, body: StartRequest | None = None, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            if body is None:
                return (await run_in_threadpool(ws().start)).public()
            return (await run_in_threadpool(ws().start, body.image, tuple(body.gpu_uuids), body.all_gpus, body.replace)).public()
        except Exception as exc: return safe_error(exc)

    @app.post("/api/workstation/stop")
    async def stop_workstation(request: Request, dockbench_csrf: str | None = Cookie(default=None)):
        _require_csrf(request, dockbench_csrf)
        try:
            result = await run_in_threadpool(ws().stop)
            await _close_desktop_sockets()
            return result.public()
        except Exception as exc: return safe_error(exc)


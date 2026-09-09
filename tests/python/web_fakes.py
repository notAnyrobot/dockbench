"""Controlled container services shared by HTTP and WebSocket tests."""
from dockbench.core.workstation import WorkstationStatus


class FakeWorkstation:
    reset_password: str | None = None
    def status(self): return WorkstationStatus("stopped", False, "test", "dockbench", "/workspace")
    def start(self, *args):
        self.start_args = args
        return WorkstationStatus("running", False, "test", "dockbench", "/workspace")
    def stop(self): return WorkstationStatus("stopped", False, "test", "dockbench", "/workspace")
    def reset_vnc_password(self, password):
        self.reset_password = password
        return WorkstationStatus("running", True, "test", "dockbench", "/workspace")


class FakeFleet:
    def __init__(self):
        self.created = None
        self.config = type("Config", (), {"workspace_root": "/data/atom7/workspace", "data_mounts": (("/data/share/motion_datasets", "/data/motions"),)})()
        self.items = [WorkstationStatus("running", False, "desktop", "alpha", "/workspace", image_id="sha256:one", image_ref="desktop:latest", gpu_uuids=("GPU-a",), desktop_capable=True)]

    def containers(self): return tuple(self.items)
    def container(self, name): return next(item for item in self.items if item.container_name == name)
    def create(self, name, image, gpu_uuids, all_gpus, workspace_root=None, data_root=None):
        self.created = (name, image, gpu_uuids, all_gpus, workspace_root, data_root)
        return WorkstationStatus("running", False, image, name, "/workspace", image_id="sha256:new", image_ref=image, gpu_uuids=gpu_uuids)
    def start(self, name): return self.container(name)
    def stop(self, name): return WorkstationStatus("stopped", False, "desktop", name, "/workspace")
    def remove(self, name): self.removed = name
    def delete_state(self, name): self.deleted_state = name
    def recreate(self, name): return self.container(name)
    def orphaned_states(self): return ("removed-alpha",)
    def inventory(self):
        return {"images": [{"id": "sha256:one", "display_reference": "desktop:latest", "desktop_capable": True}], "gpus": [{"uuid": "GPU-a", "index": 0, "name": "GPU", "memory_total_mib": 100, "reservation": "alpha", "available": False}], "gpu_diagnostic": None, "default_image": "desktop:latest", "containers": []}

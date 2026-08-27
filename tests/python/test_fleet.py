from pathlib import Path

import pytest

from dockbench.core.errors import WorkstationError
from dockbench.core.host_inventory import GPU, LocalImage
from dockbench.core.workstation import FleetManager, WorkstationConfig


class Inventory:
    def __init__(self):
        self.image = LocalImage("sha256:image", ("demo:image",), 1, "now", "amd64", "v1")
        self.gpus = (GPU("GPU-a", 0, "A", 100), GPU("GPU-b", 1, "B", 100))

    def images(self): return (self.image,)
    def inventory(self):
        class Result:
            def public(_): return {"images": [self.image.public()], "gpus": [gpu.public() for gpu in self.gpus]}
        return Result()
    def resolve_image(self, value):
        if value in {"demo:image", "sha256:image"}: return self.image
        raise WorkstationError("missing image")
    def resolve_gpus(self, values, all_gpus=False):
        if all_gpus: return self.gpus
        result = tuple(next((gpu for gpu in self.gpus if value in {gpu.uuid, str(gpu.index)}), None) for value in values)
        if None in result or len({gpu.uuid for gpu in result}) != len(result): raise WorkstationError("bad GPU")
        return result


class Docker:
    def __init__(self): self.containers = {}; self.commands = []
    def run(self, args, *, input=None, capture=False, check=True):
        self.commands.append(args)
        if args[:2] == ["container", "ls"]:
            filter_value = args[args.index("--filter") + 1]
            if filter_value != "label=io.github.notanyrobot.dockbench.managed=true":
                raise WorkstationError(f"invalid filter: {filter_value}")
            return "\n".join(sorted(name for name, item in self.containers.items() if item["managed"]))
        if args[:2] == ["container", "inspect"]:
            name = args[-1]
            if name not in self.containers: raise WorkstationError("missing")
            item = self.containers[name]
            command = " ".join(args)
            if "io.github.notanyrobot.dockbench.launch-spec" in command: return item["spec"]
            if "{{.Image}}" in command: return "sha256:image"
            if "HostPort" in command: return "49123"
            return item["state"]
        if args[:2] == ["run", "--rm"]: return ""
        if args[:2] == ["run", "-d"]:
            name = args[args.index("--name") + 1]
            spec = next(value.split("=", 1)[1] for value in args if value.startswith("io.github.notanyrobot.dockbench.launch-spec="))
            self.containers[name] = {"state": "running", "spec": spec, "managed": "io.github.notanyrobot.dockbench.managed=true" in args}
            return "id"
        if args[0] == "start": self.containers[args[1]]["state"] = "running"; return ""
        if args[0] == "stop": self.containers[args[1]]["state"] = "exited"; return ""
        if args[0] == "rm": del self.containers[args[1]]; return ""
        if args[0] == "exec": return ""
        return ""


def config(tmp_path: Path):
    android = tmp_path / "android-ws"; github = tmp_path / "GitHub"
    android.mkdir(); github.mkdir()
    return WorkstationConfig(tmp_path, "docker", {"android-ws": android, "GitHub": github}, tmp_path / ".dockbench", "1g", 1, 1, "user", "demo:image", "dockbench", 5901, "vncviewer", "rootful", 1, 1)


def test_fleet_ignores_old_containers_and_labels(tmp_path):
    configuration = config(tmp_path)
    docker = Docker(); docker.containers["other"] = {"state": "running", "spec": "", "managed": False}
    docker.containers["docker-ws"] = {"state": "exited", "spec": "", "managed": False}
    fleet = FleetManager(configuration, docker, Inventory())
    assert [item.container_name for item in fleet.containers()] == []


def test_stale_when_recorded_reference_now_resolves_to_a_new_image_id(tmp_path):
    class RebuiltInventory(Inventory):
        def __init__(self):
            super().__init__()
            self.image = LocalImage("sha256:old", ("demo:old",), 1, "now", "amd64", "v1")
            self.current = LocalImage("sha256:new", ("demo:image",), 1, "now", "amd64", "v1")

        def images(self): return (self.image, self.current)
        def resolve_image(self, value):
            if value == "demo:image": return self.current
            return super().resolve_image(value)

    docker = Docker()
    spec = '{"desktop_contract":"v1","gpu_uuids":[],"image_id":"sha256:old","image_ref":"demo:image"}'
    docker.containers["one"] = {"state": "running", "spec": spec, "managed": True}
    assert FleetManager(config(tmp_path), docker, RebuiltInventory()).container("one").stale is True


def test_fleet_reserves_only_running_gpus_and_uses_dynamic_vnc_port(tmp_path):
    docker = Docker(); fleet = FleetManager(config(tmp_path), docker, Inventory())
    first = fleet.create("one", "demo:image", ("GPU-a",))
    assert first.container_name == "one"
    run = next(command for command in docker.commands if command[:2] == ["run", "-d"])
    assert "127.0.0.1::5901" in run
    with pytest.raises(WorkstationError, match="reserved by running container one"):
        fleet.create("two", "demo:image", ("GPU-a",))
    fleet.stop("one")
    fleet.create("two", "demo:image", ("GPU-a",))
    assert fleet.ensure_desktop("two", "password").port == 49123


def test_newly_created_named_container_is_discovered_by_docker_label(tmp_path):
    docker = Docker(); fleet = FleetManager(config(tmp_path), docker, Inventory())

    fleet.create("workstation-cpu-only", "demo:image")

    assert [item.container_name for item in fleet.containers()] == ["workstation-cpu-only"]
    listing = next(command for command in docker.commands if command[:2] == ["container", "ls"])
    assert listing[listing.index("--filter") + 1] == "label=io.github.notanyrobot.dockbench.managed=true"


def test_fleet_removes_managed_created_container_after_failed_startup(tmp_path):
    class CreatedOnFailureDocker(Docker):
        def run(self, args, **kwargs):
            if args[:2] == ["run", "-d"]:
                name = args[args.index("--name") + 1]
                spec = next(value.split("=", 1)[1] for value in args if value.startswith("io.github.notanyrobot.dockbench.launch-spec="))
                self.commands.append(args)
                self.containers[name] = {"state": "created", "spec": spec, "managed": "io.github.notanyrobot.dockbench.managed=true" in args}
                raise WorkstationError("failed to start")
            return super().run(args, **kwargs)

    docker = CreatedOnFailureDocker()
    fleet = FleetManager(config(tmp_path), docker, Inventory())

    with pytest.raises(WorkstationError, match="failed to start"):
        fleet.create("one", "demo:image")

    assert "one" not in docker.containers
    assert ["rm", "one"] in docker.commands


def test_remove_preserves_named_state_and_state_delete_requires_absence(tmp_path):
    docker = Docker(); fleet = FleetManager(config(tmp_path), docker, Inventory())
    fleet.create("one", "demo:image")
    path = fleet._config_for("one").state_root; path.mkdir(parents=True, exist_ok=True); (path / "marker").touch()
    fleet.remove("one")
    assert (path / "marker").exists()
    fleet.delete_state("one")
    assert not path.exists()


def test_fleet_inventory_method_is_not_shadowed_by_host_inventory(tmp_path):
    fleet = FleetManager(config(tmp_path), Docker(), Inventory())

    result = fleet.inventory()

    assert result["images"][0]["display_reference"] == "demo:image"
    assert [gpu["uuid"] for gpu in result["gpus"]] == ["GPU-a", "GPU-b"]


def test_fleet_never_adopts_the_former_default_container_name(tmp_path):
    docker = Docker()
    docker.containers["docker-ws"] = {"state": "running", "spec": "", "managed": False}

    with pytest.raises(WorkstationError, match="not managed"):
        FleetManager(config(tmp_path), docker, Inventory()).container("docker-ws")

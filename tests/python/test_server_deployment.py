import json
import subprocess
from pathlib import Path

import pytest

from dockbench.core.server_deployment import (
    DeploymentError,
    DeploymentOptions,
    ServerDeployment,
    ServerStatus,
    load_runtime_config,
)


def _deployment(tmp_path: Path) -> ServerDeployment:
    root = tmp_path / "repo"
    (root / "apps/workbench").mkdir(parents=True)
    code_root = tmp_path / "code"
    code_root.mkdir()
    (root / "assets/systemd").mkdir(parents=True)
    (root / "assets/systemd/dockbench.service").write_text(
        "ExecStart=__UV_EXECUTABLE__ run --project __SERVER_ROOT__ dockbench serve --port __SERVER_PORT__ --config __SERVER_CONFIG__\nEnvironmentFile=__SERVER_ENV_FILE__\n"
    )
    return ServerDeployment(DeploymentOptions(
        root,
        code_roots=(("repo", code_root),),
        config_home=tmp_path / "config",
        state_home=tmp_path / "state",
    ))


def test_runtime_config_is_allowlisted_and_persists_named_code_roots(tmp_path, monkeypatch):
    deployment = _deployment(tmp_path)
    root = tmp_path / "GitHub"
    root.mkdir()
    deployment = ServerDeployment(DeploymentOptions(
        deployment.options.repository_root,
        code_roots=(("GitHub", root),),
        config_home=tmp_path / "config",
        state_home=tmp_path / "state",
    ))
    monkeypatch.setenv("DOCKBENCH_WORKSPACE", "/cluster/workspace")
    monkeypatch.setenv("DOCKBENCH_VNC_PASSWORD", "do-not-save")
    monkeypatch.setenv("UNRELATED", "also-do-not-save")
    values = deployment._write_runtime_config()

    persisted = json.loads(deployment.config_path.read_text())
    assert values == {"DOCKBENCH_CODE_ROOTS": json.dumps({"GitHub": str(root)})}
    assert persisted["environment"] == values
    assert "DOCKBENCH_VNC_PASSWORD" not in deployment.environment_path.read_text()
    assert "UNRELATED" not in deployment.config_path.read_text()
    assert load_runtime_config(deployment.config_path) == values
    assert deployment.config_path.stat().st_mode & 0o777 == 0o600


def test_runtime_config_rejects_legacy_environment_names(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"environment": {"ROBOTICS_WS_CODE_ROOT": "/legacy/Code"}}))

    assert load_runtime_config(config) == {}


def test_new_deployment_ignores_legacy_and_workspace_environment(tmp_path, monkeypatch):
    deployment = _deployment(tmp_path)
    monkeypatch.setenv("ROBOTICS_WS_CODE_ROOT", "/legacy/Code")
    monkeypatch.setenv("DOCKBENCH_WORKSPACE", "/workspace")

    assert deployment._snapshot_environment() == {
        "DOCKBENCH_CODE_ROOTS": json.dumps({"repo": str(tmp_path / "code")}),
    }


def test_deployment_uses_code_roots_from_environment_when_cli_does_not_override(tmp_path, monkeypatch):
    deployment = _deployment(tmp_path)
    github = tmp_path / "GitHub"
    github.mkdir()
    deployment = ServerDeployment(DeploymentOptions(
        deployment.options.repository_root,
        config_home=tmp_path / "config",
        state_home=tmp_path / "state",
    ))
    monkeypatch.setenv("DOCKBENCH_CODE_ROOTS", json.dumps({"GitHub": str(github)}))

    assert deployment._snapshot_environment() == {
        "DOCKBENCH_CODE_ROOTS": json.dumps({"GitHub": str(github)}),
    }


def test_runtime_config_rejects_arbitrary_environment_keys(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"environment": {"PATH": "/evil", "DOCKBENCH_DOCKER": "docker"}}))
    assert load_runtime_config(config) == {"DOCKBENCH_DOCKER": "docker"}
    config.write_text("[]")
    with pytest.raises(DeploymentError, match="invalid Dockbench runtime config"):
        load_runtime_config(config)


def test_systemd_probe_falls_back_only_when_user_bus_is_unavailable(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "Failed to connect to bus: No medium found"))
    assert ServerDeployment._systemd_probe() == "unavailable"
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "unit manager corrupted"))
    with pytest.raises(DeploymentError, match="available but unhealthy"):
        ServerDeployment._systemd_probe()


def test_deploy_builds_before_install_and_systemd_unit_uses_serve(tmp_path, monkeypatch):
    deployment = _deployment(tmp_path)
    commands = []

    monkeypatch.setattr(deployment, "_require_build_tools", lambda: ("/bin/uv", "/bin/npm"))
    monkeypatch.setattr(deployment, "_command", lambda args, *, cwd: commands.append((args, cwd)))
    monkeypatch.setattr(deployment, "_systemd_probe", lambda: "available")
    monkeypatch.setattr(deployment, "_wait_for_health", lambda result: None)

    result = deployment.deploy()

    assert commands[:3] == [
        (["/bin/uv", "sync", "--frozen"], deployment.options.repository_root),
        (["/bin/npm", "ci"], deployment.options.repository_root / "apps/workbench"),
        (["/bin/npm", "run", "build"], deployment.options.repository_root / "apps/workbench"),
    ]
    unit = result.unit_path.read_text()
    assert "dockbench serve --port 8787 --config" in unit
    assert "__SERVER_" not in unit
    assert result.manager == "systemd"


def test_deploy_rejects_missing_code_root_before_build(tmp_path, monkeypatch):
    deployment = _deployment(tmp_path)
    deployment = ServerDeployment(DeploymentOptions(
        deployment.options.repository_root,
        code_roots=(("GitHub", tmp_path / "missing"),),
        config_home=tmp_path / "config",
        state_home=tmp_path / "state",
    ))
    monkeypatch.setattr(deployment, "_require_build_tools", lambda: pytest.fail("build preflight should not run"))

    with pytest.raises(DeploymentError, match=r"code root 'GitHub' does not exist: .*--code-root NAME=PATH"):
        deployment.deploy()


def test_systemd_unit_treats_uv_sigterm_exit_as_clean():
    unit = (Path(__file__).parents[2] / "assets/systemd/dockbench.service").read_text()
    assert "SuccessExitStatus=143" in unit


def test_start_starts_an_existing_systemd_deployment_without_rebuilding(tmp_path, monkeypatch):
    deployment = _deployment(tmp_path)
    deployment._mkdir_private(deployment.unit_path.parent)
    deployment.unit_path.write_text("[Service]\n")
    commands = []
    expected = ServerStatus("systemd", "running", "active", deployment.url)

    monkeypatch.setattr(deployment, "_systemd_probe", lambda: "available")
    monkeypatch.setattr(deployment, "_command", lambda args, *, cwd: commands.append((args, cwd)))
    monkeypatch.setattr(deployment, "status", lambda: expected)

    assert deployment.start() == expected
    assert commands == [
        (["systemctl", "--user", "start", "dockbench.service"], deployment.options.repository_root)
    ]


def test_start_requires_an_existing_systemd_deployment(tmp_path, monkeypatch):
    deployment = _deployment(tmp_path)
    monkeypatch.setattr(deployment, "_systemd_probe", lambda: "available")

    with pytest.raises(DeploymentError, match="dockbench deploy"):
        deployment.start()


def test_fallback_replaces_old_process_persists_metadata_and_status(tmp_path, monkeypatch):
    deployment = _deployment(tmp_path)
    deployment._write_runtime_config()
    stopped = []
    monkeypatch.setattr(deployment, "_pid_alive", lambda pid: pid == 42)
    monkeypatch.setattr(deployment, "_process_identity", lambda pid: 123 if pid == 42 else None)
    monkeypatch.setattr(deployment, "_stop_process", lambda pid, **kwargs: stopped.append(pid))

    class Process:
        pid = 42

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    result = deployment._install_fallback("/bin/uv", {})
    assert result.manager == "process" and result.pid == 42
    assert json.loads(deployment.metadata_path.read_text())["command"][-5:] == ["serve", "--port", "8787", "--config", str(deployment.config_path)]
    status = deployment.status()
    assert status.manager == "process" and status.state == "running"
    deployment.stop()
    assert stopped == [42]


def test_health_timeout_includes_relevant_diagnostic(tmp_path, monkeypatch):
    deployment = _deployment(tmp_path)
    deployment = ServerDeployment(DeploymentOptions(deployment.options.repository_root, config_home=tmp_path / "config", state_home=tmp_path / "state", health_timeout_seconds=0))
    with pytest.raises(DeploymentError, match="journalctl"):
        deployment._wait_for_health(type("Result", (), {"manager": "systemd"})())


def test_status_uses_saved_custom_port_after_a_new_cli_invocation(tmp_path, monkeypatch):
    deployed = _deployment(tmp_path)
    deployed = ServerDeployment(DeploymentOptions(deployed.options.repository_root, port=9123, config_home=tmp_path / "config", state_home=tmp_path / "state"))
    deployed._mkdir_private(deployed.state_dir)
    deployed._write_private(deployed.metadata_path, json.dumps(deployed._installation_metadata("systemd")))
    later = ServerDeployment(DeploymentOptions(deployed.options.repository_root, config_home=tmp_path / "config", state_home=tmp_path / "state"))
    monkeypatch.setattr(later, "_systemd_probe", lambda: "available")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "active\n", ""))
    assert later.status().url == "http://127.0.0.1:9123"


def test_fallback_health_failure_stops_process_and_removes_metadata(tmp_path, monkeypatch):
    deployment = _deployment(tmp_path)
    deployment._mkdir_private(deployment.state_dir)
    metadata = deployment._installation_metadata("process", 77)
    metadata["process_identity"] = 2
    deployment._write_private(deployment.metadata_path, json.dumps(metadata))
    stopped = []
    monkeypatch.setattr(deployment, "_require_build_tools", lambda: ("/bin/uv", "/bin/npm"))
    monkeypatch.setattr(deployment, "_build", lambda *args: None)
    monkeypatch.setattr(deployment, "_systemd_probe", lambda: "unavailable")
    monkeypatch.setattr(deployment, "_install_fallback", lambda *args: type("Result", (), {"manager": "process", "pid": 77})())
    monkeypatch.setattr(deployment, "_process_identity", lambda pid: 2)
    monkeypatch.setattr(deployment, "_stop_process", lambda pid, **kwargs: stopped.append((pid, kwargs["expected"])))
    monkeypatch.setattr(deployment, "_wait_for_health", lambda result: (_ for _ in ()).throw(DeploymentError("not healthy")))
    with pytest.raises(DeploymentError, match="not healthy"):
        deployment.deploy()
    assert stopped == [(77, 2)]
    assert not deployment.metadata_path.exists()

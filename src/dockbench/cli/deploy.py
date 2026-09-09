"""CLI deploy command ownership."""
import argparse
from pathlib import Path

from dockbench.cli.common import fail as _fail, port as _port
from dockbench.core.server_connection import DEFAULT_SERVER_PORT
from dockbench.core.errors import WorkstationError

from dockbench.core.resources import CheckoutResources
from dockbench.core.server_deployment import DeploymentOptions, ServerDeployment

RESOURCES = CheckoutResources.discover()

def _deployment(port: int = DEFAULT_SERVER_PORT, workspace_root: str | None = None,
                state_root: str | None = None, docker_command: str | None = None) -> ServerDeployment:
    return ServerDeployment(DeploymentOptions(
        repository_root=RESOURCES.repository_root, port=port,
        workspace_root=Path(workspace_root).expanduser() if workspace_root else None,
        state_root=Path(state_root).expanduser() if state_root else None,
        docker_command=docker_command,
    ))

def _deploy(port: int = DEFAULT_SERVER_PORT, workspace_root: str | None = None,
            state_root: str | None = None, docker_command: str | None = None) -> int:
    try:
        print("Building and deploying Dockbench…")
        result = _deployment(port, workspace_root, state_root, docker_command).deploy()
        print(f"Dockbench deployed with {result.manager}: {result.url}")
        if result.log_path:
            print(f"Log: {result.log_path}")
        elif result.manager == "systemd":
            print("Logs: journalctl --user -u dockbench.service")
        return 0
    except (OSError, WorkstationError) as exc:
        return _fail(str(exc))

def run(args: argparse.Namespace) -> int:
    return _deploy(args.port, args.workspace, args.state_root, args.docker_command)

def register(actions) -> None:
    deploy = actions.add_parser("deploy", help="Build and deploy Dockbench on this Docker host.")
    deploy.add_argument("--port", type=_port, default=DEFAULT_SERVER_PORT)
    deploy.add_argument("--workspace", metavar="PATH",
                        help="Host workspace root mounted at /workspace (default: /data/$USER/workspace on remote hosts).")
    deploy.add_argument("--state-root", help="Host directory for persistent Dockbench state.")
    deploy.add_argument("--docker-command", help="Docker-compatible command used on the remote host.")
    deploy.set_defaults(handler=run)

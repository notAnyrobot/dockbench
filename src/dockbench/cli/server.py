"""CLI server command ownership."""
import argparse
from pathlib import Path

from dockbench.cli.common import fail as _fail, port as _port
from dockbench.core.server_connection import DEFAULT_SERVER_PORT
from dockbench.core.errors import WorkstationError

from dockbench.cli.deploy import _deployment
from dockbench.cli import deploy
from dockbench.core.host_config import ServerSettings, resolve_server_options
from dockbench.web.server import serve

def _server_status(action: str) -> int:
    try:
        status = getattr(_deployment(), action)()
        print(f"Dockbench: {status.state} ({status.manager or 'unmanaged'}) — {status.message}")
        if status.log_path:
            print(f"Log: {status.log_path}")
        elif status.manager == "systemd":
            print("Logs: journalctl --user -u dockbench.service")
        return 0 if status.state in {"running", "stopped", "absent"} else 1
    except (OSError, WorkstationError) as exc:
        return _fail(str(exc))

def run(args: argparse.Namespace) -> int:
    if args.server_action == "start" and args.foreground:
        try:
            if args.runtime_config is not None:
                if args.config is not None or any(value is not None for value in (args.workspace, args.state_root, args.docker_command)):
                    raise WorkstationError("--runtime-config cannot be combined with desired host settings")
                serve(args.port if args.port is not None else DEFAULT_SERVER_PORT, args.runtime_config, resources=deploy.RESOURCES)
                return 0
            options = resolve_server_options(args.config, resources=deploy.RESOURCES, overrides=ServerSettings(
                port=args.port, workspace=Path(args.workspace) if args.workspace is not None else None,
                state_root=Path(args.state_root) if args.state_root is not None else None,
                docker_command=args.docker_command))
            environment = deploy.ServerDeployment(options).runtime_environment()
            serve(options.port, resources=deploy.RESOURCES, environment=environment)
            return 0
        except KeyboardInterrupt:
            return 0
        except (OSError, WorkstationError) as exc:
            return _fail(str(exc))
    if args.server_action == "start" and args.runtime_config is not None:
        return _fail("--runtime-config requires --foreground")
    return _server_status(args.server_action)

def register(actions) -> None:
    server = actions.add_parser("server", help="Build, deploy, and manage the Dockbench server.")
    server_actions = server.add_subparsers(dest="server_action", required=True, metavar="ACTION")
    for action, description in {
        "start": "Start the deployed Dockbench server without rebuilding.",
        "status": "Show deployed Dockbench server status.",
        "stop": "Stop the deployed Dockbench server.",
    }.items():
        action_parser = server_actions.add_parser(action, help=description)
        if action == "start":
            action_parser.add_argument("--foreground", action="store_true", help="Serve this built checkout in the current terminal.")
            deploy.add_configuration_arguments(action_parser)
            action_parser.add_argument("--runtime-config", help=argparse.SUPPRESS)
    server.set_defaults(handler=run)
    deploy.register(server_actions, hidden=False)

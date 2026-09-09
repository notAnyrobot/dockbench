"""CLI server command ownership."""
import argparse
from pathlib import Path

from dockbench.cli.common import fail as _fail, port as _port
from dockbench.core.server_connection import DEFAULT_SERVER_PORT
from dockbench.core.errors import WorkstationError

from dockbench.cli.deploy import _deployment

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
    return _server_status(args.server_action)

def register(actions) -> None:
    server = actions.add_parser("server", help="Manage an already deployed Dockbench server.")
    server_actions = server.add_subparsers(dest="server_action", required=True, metavar="ACTION")
    for action, description in {
        "start": "Start the deployed Dockbench server without rebuilding.",
        "status": "Show deployed Dockbench server status.",
        "stop": "Stop the deployed Dockbench server.",
    }.items():
        server_actions.add_parser(action, help=description)
    server.set_defaults(handler=run)

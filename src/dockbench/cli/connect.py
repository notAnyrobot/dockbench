"""CLI connect command ownership."""
import argparse
import sys

from dockbench.cli.common import fail as _fail, port as _port
from dockbench.core.server_connection import DEFAULT_SERVER_PORT
from dockbench.core.errors import WorkstationError

from dockbench.core.server_connection import connect

def _connect(ssh_host: str, local_port: int | None, remote_port: int, open_browser: bool) -> int:
    try:
        result = connect(
            ssh_host, local_port=local_port, remote_port=remote_port, open_browser=open_browser,
            on_ready=lambda url: print(f"Dockbench: {url}\nPress Ctrl+C to close the SSH tunnel."),
        )
        return 0 if result.interrupted else 1
    except (OSError, WorkstationError) as exc:
        return _fail(str(exc))

def run(args: argparse.Namespace) -> int:
    print("`dockbench connect` is deprecated; use `dockbench web`. Browser opening now defaults on.", file=sys.stderr)
    return _connect(args.ssh_host, args.local_port, args.remote_port, args.open_browser)

def register(actions) -> None:
    connect_command = actions.add_parser("connect")
    connect_command.add_argument("ssh_host", help="SSH host, user@host, or configured SSH alias.")
    connect_command.add_argument("--local-port", type=_port, default=None)
    connect_command.add_argument("--remote-port", type=_port, default=DEFAULT_SERVER_PORT)
    browser = connect_command.add_mutually_exclusive_group()
    browser.add_argument("--open-browser", action="store_true")
    browser.add_argument("--no-open", dest="open_browser", action="store_false", help=argparse.SUPPRESS)
    connect_command.set_defaults(handler=run)

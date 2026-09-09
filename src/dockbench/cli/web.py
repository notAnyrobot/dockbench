"""Open an existing Dockbench server without changing its lifecycle."""
import argparse

from dockbench.cli.common import fail, port
from dockbench.core.errors import WorkstationError
from dockbench.core.server_connection import DEFAULT_SERVER_PORT, connect, open_local
from dockbench.core.host_config import load_host_config, saved_server_options
from dockbench.core.resources import CheckoutResources


def run(args: argparse.Namespace) -> int:
    try:
        resources = CheckoutResources.discover()
        config = load_host_config(args.config, resources=resources)
        browser = args.open_browser if args.open_browser is not None else config.web.open_browser
        browser = browser if browser is not None else True
        if args.ssh_host is not None:
            result = connect(
                args.ssh_host, remote_port=args.port or config.web.remote_port or DEFAULT_SERVER_PORT,
                local_port=args.local_port if args.local_port is not None else config.web.local_port,
                open_browser=browser,
                on_ready=lambda url: print(f"Dockbench: {url}\nPress Ctrl+C to close the SSH tunnel."),
            )
            return 0 if result.interrupted else 1
        saved_port, _ = saved_server_options(resources)
        selected_port = args.port or config.server.port or saved_port or DEFAULT_SERVER_PORT
        open_local(port=selected_port, open_browser=browser, on_ready=lambda url: print(f"Dockbench: {url}"))
        return 0
    except (OSError, WorkstationError) as exc:
        return fail(str(exc))


def register(actions) -> None:
    command = actions.add_parser('web', help='Open an existing local or remote Dockbench server.')
    command.add_argument('ssh_host', nargs='?', help='Explicit SSH host or alias; omitted means local.')
    command.add_argument('--local-port', type=port, help='Local SSH forwarding port (default: automatic).')
    command.add_argument('--config', help='Host YAML configuration file.')
    command.add_argument('--port', type=port, help='Destination server port.')
    browser = command.add_mutually_exclusive_group()
    browser.add_argument('--open-browser', action='store_true', default=None)
    browser.add_argument('--no-open', dest='open_browser', action='store_false')
    command.set_defaults(handler=run)

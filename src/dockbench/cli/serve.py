"""CLI serve command ownership."""
import argparse
from pathlib import Path

from dockbench.cli.common import fail as _fail, port as _port
from dockbench.core.server_connection import DEFAULT_SERVER_PORT
from dockbench.core.errors import WorkstationError

from dockbench.core.resources import CheckoutResources
from dockbench.web.server import serve

RESOURCES = CheckoutResources.discover()

def _serve(port: int = DEFAULT_SERVER_PORT, config: str | Path | None = None) -> int:
    try:
        serve(port, config, resources=RESOURCES)
        return 0
    except (OSError, WorkstationError) as exc:
        return _fail(str(exc))

def run(args: argparse.Namespace) -> int:
    return _serve(args.port, args.config)

def register(actions) -> None:
    serve = actions.add_parser("serve", help="Serve Dockbench on loopback.")
    serve.add_argument("--port", type=_port, default=DEFAULT_SERVER_PORT)
    serve.add_argument("--config", help="Deployment runtime configuration file.")
    serve.set_defaults(handler=run)

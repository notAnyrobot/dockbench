"""Start the loopback browser server after loading deployment configuration."""
from pathlib import Path
import os
from typing import Mapping

import uvicorn

from dockbench.core.resources import CheckoutResources
from dockbench.core.server_connection import DEFAULT_SERVER_PORT
from dockbench.core.server_deployment import load_runtime_config
from dockbench.core.errors import WorkstationError


def serve(port: int = DEFAULT_SERVER_PORT, config: str | Path | None = None, *,
          resources: CheckoutResources | None = None,
          environment: Mapping[str, str] | None = None) -> None:
    resources = resources if resources is not None else CheckoutResources.discover()
    if not (resources.frontend_dist / 'index.html').is_file():
        raise WorkstationError('Dockbench frontend is not built. Run `dockbench server deploy` or:\n'
                               f'  npm ci --prefix {resources.frontend_source}\n'
                               f'  npm run --prefix {resources.frontend_source} build')
    if config is not None:
        os.environ.update(load_runtime_config(Path(config).expanduser()))
    if environment is not None:
        os.environ.update(environment)
    from dockbench.web.app import create_app
    uvicorn.run(create_app(repository_root=resources.repository_root), host='127.0.0.1',
                port=port, proxy_headers=False)

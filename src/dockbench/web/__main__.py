"""Run Dockbench's browser server with its default invocation settings."""
import sys

from dockbench.core.errors import WorkstationError
from dockbench.web.server import serve


def main() -> int:
    try:
        serve()
        return 0
    except (OSError, WorkstationError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

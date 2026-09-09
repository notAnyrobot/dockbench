# Architecture and contributor checks

Dockbench runs from a repository checkout. Its installed Python package is
`src/dockbench`; Dockerfiles and recipe build contexts remain in `assets/images`.
The browser client and its npm tooling live in `src/dockbench/web/frontend`.
A wheel without the accompanying checkout assets is not a deployment target.

## Ownership

- `core/backend.py` constructs a fixed set of capabilities with explicit resource,
  configuration, and Docker-runner inputs. It snapshots the invocation environment
  and resolves container configuration lazily, so readiness and recipe operations
  do not require Docker availability or a valid workspace root.
- `core/resources.py` is the authority for checkout, recipe, and frontend locations.
  Explicit roots support deployment and tests; default discovery follows the
  installed editable source rather than the caller's working directory.
- `core/recipes.py`, `image_builder.py`, `image_verifier.py`, and `images.py` own
  recipe validation, builds, verification, and image archives. Default-container
  rebuild uses the same catalog and builder, retaining its existing effective
  tag, desktop target, platform, and build-before-replacement behavior.
- `core/workstation.py`, `fleet.py`, and `host_inventory.py` own container lifecycle,
  selection, mounts, persisted state, managed labels, and GPU reservations.
  Configured-default and named-container selection retain their distinct policies.
- `core/access.py` prepares shell execution and desktop access, including user
  identity and password provisioning for VNC. CLI and browser terminals share root
  shell preparation, `/workspace`, and the Bash-or-shell fallback; desktop access
  retains the configured host user and persistent home.
- `core/server_deployment.py` and `server_connection.py` own checkout deployment,
  readiness, saved configuration, process/systemd lifecycle, and SSH connections.
- `cli` adapts argparse requests into operations and owns output, exit status,
  interactive command execution, and the native desktop viewer.
- `web/app.py` composes the HTTP application. Route modules adapt requests;
  `image_jobs.py` owns serialized jobs and temporary-upload cleanup; `sessions.py`
  owns expiring single-use tokens; `access.py` and `terminal.py` own socket and
  PTY transport lifetime. `web/server.py` starts the server for both CLI `serve`
  and `python -m dockbench.web`.

Shared modules import neither adapter, and web does not import CLI. Backend
operations return results, expected errors, or progress; adapters translate these
into command output or HTTP responses. Add a module when it hides operational
complexity or owns a coherent responsibility. Avoid pass-through layers and
interfaces that make every caller coordinate configuration and Docker details.

Resource ownership includes failure paths: image jobs retain worker ownership
until completion before deleting uploads; terminal shutdown adopts a cancelled
launch and terminates, kills if necessary, and reaps its child; desktop forwarding
closes its TCP stream and removes scoped socket registrations.

## Development and validation

Install the checkout and frontend dependencies, then run the complete checks:

```bash
uv sync --frozen --group dev
npm --prefix src/dockbench/web/frontend ci
uv run --frozen --group dev python -m pytest
bash tests/check-context.sh
npm --prefix src/dockbench/web/frontend test
npm --prefix src/dockbench/web/frontend run build
```

The repository check includes shell syntax, fake-Docker CLI and archive workflows,
bootstrap checks, and source/asset layout checks. Python tests cover shared
operations, CLI output, HTTP/WebSocket behavior, deployment, SSH cleanup, and
forbidden import directions. Tests use temporary state and controlled external
commands; they do not need a live Docker daemon or modify managed containers.

After building, run `/path/to/dockbench/.venv/bin/dockbench serve --port 9878`
from another directory to exercise editable installation and resource discovery.
`GET /api/health` should return `{"status":"ok"}` even when Docker is unavailable,
and `/` should serve the built browser client. Choose an unused port and stop only
the server started for the check.

Test behavior through CLI invocations, the application factory, and shared
operation interfaces. Lifecycle ordering matters when it protects state or
cleanup; incidental helper calls and file decomposition are not contracts.
Handwritten files should normally stay below 500 physical lines. Split by
responsibility, and explain cohesive exceptions rather than adding a line-count
CI gate or compressing code.

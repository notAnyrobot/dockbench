# Migrating to Dockbench 2.0

Dockbench 2.0 uses the unified `src/dockbench` package, shared backend operations,
root CLI and browser shells, desktop clipboard controls, and host YAML settings.
The maintainer reports successful testing of the unified server commands and
configuration on both a local laptop and a remote HPC host on September 9, 2026.

## Command changes

The compatibility aliases are removed in 2.0. Update scripts and custom launchers:

| Previous command | Dockbench 2.0 |
| --- | --- |
| `dockbench deploy` | `dockbench server deploy` |
| `dockbench serve` | `dockbench server start --foreground` |
| `dockbench connect HOST` | `dockbench web HOST` |
| `connect --remote-port PORT` | `web --port PORT` |

`web` opens the browser by default. Add `--no-open` to preserve the previous
browser opt-in behavior. `server start`, `server status`, and `server stop` manage
the installed server. Top-level `start`, `status`, and `stop` manage containers.

## Upgrade each server host

Stop the Dockbench server before updating; managed containers and their workspace
and state mounts remain intact. Browser terminal and desktop connections will
disconnect while the server is stopped. Stop manually launched foreground servers
with Ctrl+C in their terminal.

```bash
uv run dockbench server stop
git pull --ff-only
uv sync --frozen
uv run dockbench server deploy
uv run dockbench server status
```

Run these from the checkout after the 2.0 changes have merged into `main`.
`server deploy` rebuilds the browser assets and regenerates the installed launch
command. This is required for older units that still invoke `dockbench serve`.
Resolve any local Git conflicts without discarding customized Dockerfiles.

Existing host YAML, compatible saved settings, Docker images, managed containers,
workspace mounts, and persistent state are retained. No image rebuild or container
recreation is required. To customize host settings, use `config/dockbench.yaml`
based on `config/dockbench.example.yaml`, or pass `--config PATH` to deployment.
An old `serve --config` JSON snapshot is not host YAML: retain that generated file
as deployment state and use YAML for user-managed settings. Custom launchers that
must use an effective JSON snapshot can invoke `server start --foreground
--runtime-config PATH`.

## Connect and validate

```bash
uv run dockbench web                  # existing local server
uv run dockbench web YOUR_SSH_ALIAS   # existing HPC server through SSH
```

Check container inventory, root shell access, desktop access, and clipboard
transfer. An occupied local tunnel port can be overridden with `web --local-port`;
the destination server port is selected with `--port` or host YAML.

The Python package and private frontend package use version `2.0.0`. The release
tag is `v2.0.0` once the migration PR is reviewed, merged, and published.

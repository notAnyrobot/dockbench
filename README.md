# Dockbench

Dockbench is a browser workbench and companion CLI for building managed Docker
workstation images and running GPU-enabled development containers on local or
remote Docker hosts. The installable Python package lives in `src/dockbench`; the browser
client lives in `src/dockbench/web/frontend`.

The bundled `assets/images/android-ws` recipe provides a CUDA `core` target and
a `desktop` target with XFCE, TigerVNC, and Firefox. Projects, environments,
data, and credentials remain on host mounts; this repository deliberately
contains no project checkout or credential material.

Recipe revision 2 pulls its CUDA base directly from NVIDIA NGC
(`nvcr.io/nvidia/cuda`) and uses BuildKit's bundled Dockerfile frontend, so the
recipe does not depend on Docker Hub. Other build steps still require their
documented upstream package and source hosts.

## Build and run

Install the checkout and build the browser client before serving:

```bash
uv sync --frozen
npm --prefix src/dockbench/web/frontend ci
npm --prefix src/dockbench/web/frontend run build
```

Run the repository and application checks:

```bash
bash tests/check-context.sh
uv run --frozen --group dev python -m pytest
npm --prefix src/dockbench/web/frontend test
```

See [architecture and contributor checks](docs/architecture.md) for module ownership,
cleanup responsibilities, and the complete validation workflow.

The editable installation uses `src/dockbench` directly. From another directory,
use `/path/to/dockbench/.venv/bin/dockbench` or
`uv run --project /path/to/dockbench dockbench`; repository assets and frontend
builds are resolved from that checkout.

On a native Ubuntu `linux/amd64` host with NVIDIA Container Toolkit, build and
verify the desktop image:

```bash
uv run dockbench image build
uv run dockbench image verify android-ws:u22.04-cu12.8-v2
```

Use `--no-cache` for a cache-free build. `image rebuild` builds the selected
recipe, replaces the default managed container, and starts it again:

```bash
uv run dockbench image rebuild
```

The replacement retains the workspace root and `.dockbench` state, but
discards changes made only in the old container filesystem.

Use Docker directly for general inventory:

```bash
docker image ls
nvidia-smi
```

Manage the default `dockbench` container directly from the top-level CLI:

```bash
uv run dockbench start
uv run dockbench start --image ubuntu:24.04 --gpu none
uv run dockbench start --image YOUR_IMAGE --gpu 0 --gpu GPU-UUID
uv run dockbench shell
uv run dockbench shell workstation-8gpu
uv run dockbench desktop
uv run dockbench status
uv run dockbench stop
```

The default launch uses `android-ws:u22.04-cu12.8-v2` and all reported GPUs.
Use `--gpu none` for CPU-only operation, or repeat `--gpu` to select a subset.
A running managed container is immutable: changing image or GPU selection
requires `--replace`, which retains the workspace mount and `/state` but discards its
container filesystem. Containers run as root, so files created in a workspace root
may become root-owned on the host.

`dockbench shell CONTAINER` enters a managed container as root, matching browser
terminals. Both start in `/workspace`, using Bash when available or `/bin/sh`
otherwise. Shell entry does not provision or select the host user. The VNC desktop
keeps its configured host user and persistent `/state/home`; this shell policy does
not change desktop identity or settings.
Without a name, `shell` uses the configured default container when it is running,
or the sole running managed container. If several are running, specify the name.

Dockbench presents one host workspace root through the `/workspace` mount. The default is
`~/workspace` locally and `/data/$USER/workspace` on remote hosts with a
per-user `/data/$USER` directory. Thus the standard roots `~/workspace` and
`/data/atom7/workspace` have the same in-container path.

Override the detected root with `DOCKBENCH_WORKSPACE`. For a remote deployment,
use `--workspace PATH`:

```bash
export DOCKBENCH_WORKSPACE="$HOME/workspace"
uv run dockbench start
uv run dockbench server start --foregroundr deploy --workspace /data/$USER/workspace
```

The browser's **Create container** dialog uses that normalized root by default.
Enable **Use a custom workspace root** to mount another existing host directory
at `/workspace` for that container, or use **Reset to default** to restore the
normalized root.

When `/data/share/motion_datasets` exists on the host, the browser offers it as
the default optional data root. Enable **Mount data** to mount that directory—or
another existing host directory—at `/data/motions` for the new container. The
leaf mount keeps the rest of the image's `/data` directory available.

`DOCKBENCH_VNC_PASSWORD` is used only while provisioning a VNC password; never
store it in this repository. The desktop image advertises desktop contract
`v1`; shell-only images can still be used with `dockbench shell`.

Image archives use the standard Docker format:

```bash
uv run dockbench image export ./image-archives
uv run dockbench image import ./image-archives/android-ws.tar
```

## Image recipes

Recipes are repository-owned files under `assets/images/<recipe-id>/`. Dockbench
does not provide recipe-management commands in its CLI. To add or update one,
edit the repository directly:

```text
assets/images/<recipe-id>/
├── recipe.json
└── Dockerfile.<recipe-id>-v<revision>
```

`recipe.json` declares the active revision, Dockerfile name, default tag,
target, and platform. Recipe IDs use lowercase kebab-case. A revision is a new
versioned Dockerfile plus an updated manifest; previous versioned Dockerfiles
remain in the directory. The directory is the Docker build context, so maintain
any companion build files there as well. Build a repository recipe with:

```bash
uv run dockbench image build <recipe-id>
```

The browser app retains its optional recipe create/revise workflow for a
writable checkout, but direct file editing is the canonical repository workflow.
`RecipeCatalog` remains the internal validator used by both the CLI and browser
build flows.

## Browser workbench on a remote host

On a new Linux HPC or workstation host, clone this repository and bootstrap the
user-scoped tooling:

```bash
./scripts/bootstrap.sh
```

When needed, bootstrap installs pinned `uv` and `nvm` releases without `sudo`,
then uses `nvm` to install Node 22 and npm. It does not install Docker; Docker
daemon policy is host-specific. See [`dependencies.txt`](dependencies.txt) for
required host commands.

Deploy the loopback-only Dockbench server on the Docker host:

```bash
uv run dockbench server start --foregroundr deploy
```

Deployment installs locked Python and frontend dependencies, builds the browser
client, starts the server, and waits for its health check. It does not build an
image or recreate a container. Manage an already deployed server with:

```bash
uv run dockbench server start --foregroundr start
uv run dockbench server start --foregroundr status
uv run dockbench server start --foregroundr stop
```

For foreground development or direct local use, build the frontend first, then run
in the current terminal without deploying a service (Ctrl+C stops it):

```bash
uv run dockbench server start --foreground
```

Host settings can be kept in the checkout:

```bash
cp config/dockbench.example.yaml config/dockbench.yaml
# Edit config/dockbench.yaml for this host, then:
uv run dockbench server deploy
uv run dockbench server start --foreground --config /path/to/host.yaml --port 9878
```

`config/dockbench.yaml` is gitignored and user-managed; Dockbench never creates or
overwrites it. The default is found from the installed checkout, even when the
command runs in another directory. Explicit `--config` paths must exist. The
[schema example](config/dockbench.example.yaml) has version 1 and optional
`server.port`, `server.workspace`, `server.state_root`, `server.docker_command`,
`web.remote_port`, `web.local_port`, and `web.open_browser` fields. Unknown fields,
invalid types, and null values except `web.local_port` are rejected. A null local
port means automatic tunnel-port selection; browser opening defaults to enabled
for the canonical web command.

Server settings use explicit flags, then YAML, applicable `DOCKBENCH_WORKSPACE`,
`DOCKBENCH_STATE_ROOT`, and `DOCKBENCH_DOCKER` environment values, a compatible
saved deployment from this checkout, and existing defaults. YAML paths are
relative to the YAML directory; CLI paths are relative to the invoking directory.
Both expand `~`. Workspace validation happens before deployment changes. Host
settings do not alter image recipes or other container command defaults.

The desired YAML remains separate from the effective JSON runtime snapshot under
`$XDG_CONFIG_HOME/dockbench/server` (default `~/.config/dockbench/server`) and
installed metadata under `$XDG_STATE_HOME/dockbench/server` (default
`~/.local/state/dockbench/server`). Only existing allowlisted environment settings
are saved; credentials do not belong in YAML or the runtime snapshot. New service
launch definitions use `server start --foreground` with the effective snapshot.
The hidden deprecated `deploy` and `serve` aliases remain available with migration
guidance on stderr. Legacy `serve --config` still means the old JSON runtime file,
while canonical `--config` selects host YAML.

Open an existing local server, or supply an explicit SSH host for remote access:

```bash
uv run dockbench web
uv run dockbench web --port 9878 --no-open
uv run dockbench web USER@HPC_HOST
uv run dockbench web research-hpc --local-port 9878 --port 8787
uv run dockbench web research-hpc --config ~/dockbench-client.yaml
```

`web` verifies readiness, prints the loopback URL, and opens your browser by
default. `--no-open` disables browser opening; `--open-browser` overrides the YAML
preference. It never starts, deploys, or rebuilds a server. Without an SSH host,
access is always local: the destination port comes from `--port`, YAML
`server.port`, a compatible saved deployment, then 8787. With an explicit SSH
host it comes from `--port`, YAML `web.remote_port`, then 8787. Remote access does
not require the configured local workspace directory to exist.

Remote forwarding uses `--local-port`, then YAML `web.local_port`; an omitted or
null setting selects 8787 when free, otherwise a free local port. An explicitly
occupied port fails with guidance. Keep the tunnel command running while using
Dockbench and press Ctrl+C to close it. Interactive SSH authentication retains
its normal prompts. The server and tunnel bind only to `127.0.0.1`.

If browser launch fails, open the printed URL manually; the remote tunnel stays
available until interrupted. The hidden deprecated `connect` alias retains
`--remote-port` and its opt-in `--open-browser` default. Migrate scripts to
`web HOST --port PORT --no-open` to retain that browser behavior.

### Rootless Docker with NVIDIA GPUs

Rootless Docker requires one additional NVIDIA Container Toolkit setup. The
Docker user configures the runtime for their own daemon, then restarts it:

```bash
nvidia-ctk runtime configure \
  --runtime=docker \
  --config="$HOME/.config/docker/daemon.json"
systemctl --user restart docker
```

An administrator must configure NVIDIA Container Toolkit not to modify cgroup
device rules, because a rootless daemon cannot perform those operations:

```bash
sudo nvidia-ctk config \
  --set nvidia-container-cli.no-cgroups \
  --in-place
```

Coordinate this host-global setting with the host administrator. Verify GPU
injection before deploying workloads:

```bash
docker run --rm --gpus all nvcr.io/nvidia/cuda:12.8.1-base-ubuntu22.04 nvidia-smi
```

See NVIDIA's [rootless Docker installation instructions](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).

Historical UX and planning inputs remain in
[`docs/workbench/archive/`](docs/workbench/archive/).

## Provenance

The runtime and historical design inputs were imported from
[`notAnyrobot/awesome-robotics`](https://github.com/notAnyrobot/awesome-robotics)
commits `4765bef`, `060758f`, and `0b25b71`.

### Browser desktop clipboard

Open a desktop and connect with its existing VNC password, then select
**Clipboard** in the toolbar. Paste host text into **Outgoing text** and click
**Send to desktop**. This sets the remote clipboard only; choose Paste inside
the remote application yourself. Sending never presses keys or executes commands.

Copy text within the remote desktop to populate **Incoming remote text**, then
click **Copy to host**. If browser clipboard access is unavailable or denied,
select the incoming text and copy it manually. Text stays in memory and clears
when the connection ends or changes; sending and host copying are disabled while
disconnected. Dockbench does not continuously read or write your host clipboard.
No image rebuild, container recreation, or desktop configuration change is needed.

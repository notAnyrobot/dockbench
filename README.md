# Dockbench

Dockbench is a browser workbench and companion CLI for building managed Docker
workstation images and running GPU-enabled development containers on local or
remote Docker hosts. The installable Python package is `dockbench`; the browser
client lives in `apps/workbench`.

The bundled `assets/images/android-ws` recipe provides a CUDA `core` target and
a `desktop` target with XFCE, TigerVNC, and Firefox. Projects, environments,
data, and credentials remain on host mounts; this repository deliberately
contains no project checkout or credential material.

Recipe revision 2 pulls its CUDA base directly from NVIDIA NGC
(`nvcr.io/nvidia/cuda`) and uses BuildKit's bundled Dockerfile frontend, so the
recipe does not depend on Docker Hub. Other build steps still require their
documented upstream package and source hosts.

## Build and run

Run the repository checks:

```bash
bash tests/check-context.sh
```

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

Dockbench presents one host workspace root through the `/workspace` mount. The default is
`~/workspace` locally and `/data/$USER/workspace` on remote hosts with a
per-user `/data/$USER` directory. Thus the standard roots `~/workspace` and
`/data/atom7/workspace` have the same in-container path.

Override the detected root with `DOCKBENCH_WORKSPACE`. For a remote deployment,
use `--workspace PATH`:

```bash
export DOCKBENCH_WORKSPACE="$HOME/workspace"
uv run dockbench start
uv run dockbench deploy --workspace /data/$USER/workspace
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
uv run dockbench deploy
```

Deployment installs locked Python and frontend dependencies, builds the browser
client, starts the server, and waits for its health check. It does not build an
image or recreate a container. Manage an already deployed server with:

```bash
uv run dockbench server start
uv run dockbench server status
uv run dockbench server stop
```

For foreground development or direct local use, run:

```bash
uv run dockbench serve
```

On a local machine, create an SSH tunnel to the remote browser server:

```bash
uv run dockbench connect USER@HPC_HOST
uv run dockbench connect research-hpc --local-port 9878 --remote-port 8787
uv run dockbench connect research-hpc --open-browser
```

Open the printed `127.0.0.1` URL and keep the tunnel command running while
using Dockbench. The server binds only to `127.0.0.1` on the remote host; it is
not exposed to the network.

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

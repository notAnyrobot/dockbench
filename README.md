# Docker Workstation

Managed Docker image recipes and a generic local-image workstation launcher.
The bundled recipe at `assets/images/android-ws` has a CUDA `core` target and a
`desktop` target that adds XFCE, TigerVNC, and Firefox. Host lifecycle code is
the installable `docker_ws` Python package; `apps/workbench` is the browser UI.
Projects, environments, data, and credentials remain on host mounts; this
repository deliberately contains no project checkout or credential material.

The bundled desktop image is optional: `docker-ws` launches any tagged local
image that provides `/bin/sh` and `sleep`. The managed container name and
hostname default to `docker-ws`. Existing `ROBOTICS_WS_*` configuration
variables and `.robotics-ws` state paths remain supported.

## Build and verify

Run the inexpensive structural checks from the repository root:

```bash
bash tests/check-context.sh
```

On a native Ubuntu `linux/amd64` host with NVIDIA Container Toolkit, build the
desktop image. Docker reuses unchanged layers and rebuilds layers affected by
Dockerfile edits:

```bash
uv run docker-ws image build
```

The same recipe can be built directly without `docker-ws`:

```bash
docker buildx build --platform linux/amd64 \
  --file assets/images/android-ws/Dockerfile.android-ws-v1 \
  --target desktop --load --tag android-ws:u22.04-cu12.8-v1 \
  assets/images/android-ws
```

Use `--no-cache` for a cache-free build. Verification is explicit and checks
the generic shell contract plus the desktop-v1 contract when the image
advertises it:

```bash
uv run docker-ws image build --no-cache
uv run docker-ws image verify android-ws:u22.04-cu12.8-v1
```

`image build` leaves an existing container untouched. To build the image and
replace the existing container so it immediately uses that image, run
`uv run docker-ws image rebuild`. The replacement container keeps the host-mounted
`/workspace` projects and `.robotics-ws` state, but changes made only in the old
container filesystem are discarded.

Inspect tagged local images and GPUs, then start a persistent workstation:

```bash
uv run docker-ws image list
uv run docker-ws image recipe list
uv run docker-ws gpus
uv run docker-ws container start
uv run docker-ws container start --image ubuntu:24.04 --gpu none
uv run docker-ws container start --image YOUR_IMAGE --gpu 0 --gpu GPU-UUID
uv run docker-ws container enter
```

The default launch uses `android-ws:u22.04-cu12.8-v1` and all reported
GPUs. Use `--gpu none` for CPU-only operation, or repeat `--gpu` to select a
specific subset. A running managed container is immutable: changing its image or GPUs
requires `--replace`, which retains `/workspace` and `/state` but discards
changes made only in the old container filesystem. Containers are explicitly
created as root, so files created in `/workspace` may become root-owned on the host.
Stopping a container terminates all processes inside it. Starting it again
resumes that same container and writable filesystem; it does not create a fresh
container. Use remove/create or an explicit replacement when a fresh container
is required.

By default, `docker-ws` mounts the host's `~/Code` at `/workspace` and preserves
state at `.robotics-ws`. Set `ROBOTICS_WS_CODE_ROOT` to mount a different host
directory. `start` does not configure or run VNC, so it works for shell-only use
without a VNC password. `vnc` provisions a password when needed, starts VNC,
and opens the viewer. Run
`uv run docker-ws --help` for all operations. Set `ROBOTICS_WS_VNC_PASSWORD` only in the environment
when provisioning a password—never store it in this repository.

The bundled image advertises desktop contract `v1`, enabling VNC and Workbench
desktop controls. VNC configuration and startup are managed by `docker-ws` core
code rather than an image-provided helper. Shell-only images never have VNC installed or started; use
`docker-ws container enter` instead. The generic launcher runs root to avoid requiring
user-management tools in arbitrary images.

```bash
sudo apt update
sudo apt install PACKAGE
sudo -i
```

Packages installed interactively modify only the current container. Add
permanent packages as a new revision of the relevant image recipe and rebuild.

### Image recipes

Each recipe is stored at `assets/images/RECIPE_ID`. Its `recipe.json` records
the current revision, Dockerfile name, default tag, target, and platform. A
Dockerfile revision is named `Dockerfile.RECIPE_ID-vREVISION`; previous
revisions remain in the directory when a new one is added.

Register and build a Dockerfile from the CLI:

```bash
uv run docker-ws image recipe add my-workstation ./Dockerfile \
  --tag my-workstation:v1 --target desktop --platform linux/amd64
uv run docker-ws image build my-workstation
uv run docker-ws image recipe revise my-workstation ./Dockerfile.v2 \
  --tag my-workstation:v2
```

Recipe IDs are lowercase kebab-case. Adding an existing ID is rejected; use
`recipe revise` to add the next version. The recipe directory is the Docker
build context. Workbench uploads one UTF-8 Dockerfile per revision in this
version, so companion context files must be maintained directly in the recipe
directory. Recipe changes made through Workbench are uncommitted files in this
repository and require the checkout to be writable.

To transfer built images, use `uv run docker-ws image package [DIRECTORY]` or
`uv run docker-ws image load TARFILE [TARFILE ...]`.

## Workbench

Workbench is a desktop-first, single-user browser companion for the same
`docker-ws` fleet. It lists managed containers, local images, and available
GPUs, manages Dockerfile recipe revisions, builds and explicitly verifies
images, and can create containers from a selected image and GPU allocation. Its
inspector and lower Activity/Root Bash dock are resizable; layout and Activity
history persist in the browser.

### Remote host deployment

On the HPC or workstation that runs Docker, clone this repository and run one
command from its checkout:

```bash
uv run docker-ws workbench deploy
```

Deploy requires `uv`, Node/npm, Docker, and a writable repository checkout. It
installs the locked Python and frontend dependencies, builds the browser client,
starts a loopback-only server, and waits for its health check. Image builds stay
explicit: deploy does not build `android-ws` or recreate containers.

`workbench deploy` prefers a user-level systemd service and is safe to repeat
after updating the checkout. If user systemd is unavailable, it runs a managed
detached process instead. The fallback may be terminated by HPC systems when
your login session ends; user systemd is the durable option. Use
`uv run docker-ws workbench status` to inspect the service/process and
`uv run docker-ws workbench stop` to stop it. Deployment configuration is kept
under the XDG configuration directory and its logs are reported by the status
and deployment commands. `ROBOTICS_WS_VNC_PASSWORD` is intentionally never
persisted there. On systems using user systemd, enable linger when the service
must survive logout:

```bash
loginctl enable-linger "$USER"
```

The Workbench server always binds to `127.0.0.1` on the remote host; it is not
exposed to the network and no public authentication surface is added.

### Local browser connection

On the local machine, where Docker is not required, use the same CLI to create
the SSH tunnel, wait for Workbench readiness, and open the browser:

```bash
uv run docker-ws workbench connect USER@HPC_HOST
```

The local browser opens a `127.0.0.1` URL through the foreground SSH tunnel.
Keep this command running while using Workbench; press Ctrl+C to close the
tunnel. `connect` uses your normal SSH configuration, so aliases, identity
files, nonstandard ports, and bastion hosts via `ProxyJump` work unchanged. For
example:

```bash
uv run docker-ws workbench connect research-hpc
uv run docker-ws workbench connect research-hpc --local-port 9878 --remote-port 8787 --no-open
```

When the default local port is occupied, `connect` selects a free local port;
an explicitly requested busy `--local-port` fails instead. `--no-open` prints
the local URL without launching a browser. `workbench serve` remains available
for foreground development or direct use on the Docker host. The legacy
`service install` command remains a compatibility alias for deployment.

### Remote acceptance flow

1. On the remote Docker host, run `uv run docker-ws workbench deploy`.
2. On the local machine, run `uv run docker-ws workbench connect USER@HPC_HOST`.
3. In Workbench, build and explicitly verify `android-ws`.
4. Create a managed remote container from the verified image, then open its
   Root Bash terminal or desktop from the same local browser session.

The VNC password is sent directly to noVNC for the current connection, is not
saved by the browser or Workbench, and is never returned by its API. The first
connection provisions the password if the container has none. Historical UX
and planning inputs remain in [`docs/workbench/archive/`](docs/workbench/archive/).

## Provenance

The runtime and historical design inputs were imported from
[`notAnyrobot/awesome-robotics`](https://github.com/notAnyrobot/awesome-robotics)
commits `4765bef`, `060758f`, and `0b25b71`.

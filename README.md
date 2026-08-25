# Docker Workstation

One Linux `amd64` Docker build context for GPU-enabled robotics development.
The Dockerfile at `assets/docker/Dockerfile` has a CUDA `core` target and a
`desktop` target that adds XFCE, TigerVNC, and Firefox. Host lifecycle code is
the installable `docker_ws` Python package; `apps/workbench` is the browser UI.
Projects, environments, data, and credentials remain on host mounts; this
repository deliberately contains no project checkout or credential material.

The default image is `docker-ws:u22.04-cu12.8.1-v1-desktop` and the default
container name and hostname are both `docker-ws`. Existing `ROBOTICS_WS_*`
configuration variables and `.robotics-ws` state paths remain supported.

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

`image build` leaves an existing container untouched. To build the image and
replace the existing container so it immediately uses that image, run
`uv run docker-ws image rebuild`. The replacement container keeps the host-mounted
`/workspace` projects and `.robotics-ws` state, but changes made only in the old
container filesystem are discarded.

Start a persistent workstation with the repository-local CLI:

```bash
uv run docker-ws start
uv run docker-ws enter
uv run docker-ws vnc
```

By default, `docker-ws` mounts the host's `~/Code` at `/workspace` and preserves
state at `.robotics-ws`. Set `ROBOTICS_WS_CODE_ROOT` to mount a different host
directory. `start` does not configure or run VNC, so it works for shell-only use
without a VNC password. `vnc` provisions a password when needed, starts VNC,
and opens the viewer. Run
`uv run docker-ws --help` for all operations. Set `ROBOTICS_WS_VNC_PASSWORD` only in the environment
when provisioning a password—never store it in this repository.

The shell and XFCE desktop use the host-mapped user rather than running every
graphical process as root. That user has unrestricted passwordless `sudo`
inside the container, so administrative commands work directly:

```bash
sudo apt update
sudo apt install PACKAGE
sudo -i
```

Packages installed interactively modify only the current container. Add
permanent packages to `assets/docker/Dockerfile` and rebuild the image.

To transfer built images, use `uv run docker-ws image package [DIRECTORY]` or
`uv run docker-ws image load TARFILE [TARFILE ...]`.

## Workbench

Workbench is a desktop-first, single-user browser companion for the same
`docker-ws` workstation. It can show status, start or stop the workstation,
and open its noVNC desktop. Image build/rebuild, shell access, image transfer,
and other Docker actions intentionally remain CLI-only.

Install its Python dependencies and build the browser client once:

```bash
uv sync --group dev
cd apps/workbench && npm ci && npm run build && cd ../..
uv run docker-ws workbench
```

The server always binds to the host loopback interface (`127.0.0.1:8787`). On
the host, open <http://127.0.0.1:8787>. From another computer, create an SSH
tunnel and then open the same URL locally:

```bash
ssh -L 8787:127.0.0.1:8787 USER@WORKSTATION_HOST
```

To run Workbench as a user-level systemd service, use
`uv run docker-ws service install`. It does not need root privileges. For it to
remain running after logout, enable linger for the account with
`loginctl enable-linger "$USER"`.

The VNC password is sent directly to noVNC for the current connection, is not
saved by the browser or Workbench, and is never returned by its API. The first
connection provisions the password if the container has none. Historical UX
and planning inputs remain in [`docs/workbench/archive/`](docs/workbench/archive/).

## Provenance

The runtime and historical design inputs were imported from
[`notAnyrobot/awesome-robotics`](https://github.com/notAnyrobot/awesome-robotics)
commits `4765bef`, `060758f`, and `0b25b71`.

# Docker Workstation

One Linux `amd64` Docker build context for GPU-enabled robotics development.
The `core` target is a CUDA base image and `desktop` adds XFCE and TigerVNC.
Projects, environments, data, and credentials remain on host mounts; this
repository deliberately contains no project checkout or credential material.

The runtime contract is intentionally stable: existing `ROBOTICS_WS_*`
variables, `robotics-ws:*` image tags, managed container names, labels, and
`.robotics-ws` state paths continue unchanged.

## Build and verify

Run the inexpensive structural checks from the repository root:

```bash
bash tests/check-context.sh
```

On a native Ubuntu `linux/amd64` host with NVIDIA Container Toolkit, build the
two targets:

```bash
docker buildx build --platform linux/amd64 --target core --load \
  -t robotics-ws:u22.04-cu12.8.1-v1-headless .
docker buildx build --platform linux/amd64 --target desktop --load \
  -t robotics-ws:u22.04-cu12.8.1-v1-desktop .
```

Start a persistent workstation container with `./bin/launch-headless --start`
or `./bin/launch-desktop --start`. The launchers mount `/Code` and preserve
state at `.robotics-ws`; see `--help` for their operations. Set
`ROBOTICS_WS_VNC_PASSWORD` only in the environment when provisioning a desktop
password—never store it in this repository.

## Future Workbench

Workbench is a future product, intentionally neither designed nor implemented
in this repository today. Historical UX and planning inputs live in
[`docs/workbench/archive/`](docs/workbench/archive/); they are explicitly
non-authoritative and must be reconsidered in a clean future design.

## Provenance

The runtime and historical design inputs were imported from
[`notAnyrobot/awesome-robotics`](https://github.com/notAnyrobot/awesome-robotics)
commits `4765bef`, `060758f`, and `0b25b71`.

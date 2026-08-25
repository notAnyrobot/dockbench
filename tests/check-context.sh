#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

for file in .dockerignore pyproject.toml assets/docker/Dockerfile \
  assets/docker/bin/start-vnc assets/docker/bin/verify-image \
  assets/systemd/docker-ws-workbench.service docker_ws/cli/main.py \
  docker_ws/core/workstation.py docker_ws/core/images.py docker_ws/web/app.py apps/workbench/package.json \
  tests/test-workstation.sh tests/test-cli.sh tests/test-package-images.sh; do
  test -f "$file" || { echo "missing: $file" >&2; exit 1; }
done
for obsolete in Dockerfile cli core web workbench systemd; do
  test ! -e "$obsolete" || { echo "obsolete path remains: $obsolete" >&2; exit 1; }
done

grep -F 'FROM ${CUDA_IMAGE} AS core' assets/docker/Dockerfile >/dev/null
grep -F 'FROM core AS desktop' assets/docker/Dockerfile >/dev/null
grep -F 'COPY assets/docker/bin/verify-image /usr/local/bin/verify-image' assets/docker/Dockerfile >/dev/null
grep -F 'COPY assets/docker/bin/start-vnc /usr/local/bin/start-vnc' assets/docker/Dockerfile >/dev/null
grep -F 'WORKDIR /workspace' assets/docker/Dockerfile >/dev/null
grep -F 'assets/docker/Dockerfile' docker_ws/core/workstation.py >/dev/null
grep -F 'dst=/workspace' docker_ws/core/workstation.py >/dev/null
grep -F 'apps" / "workbench" / "dist' docker_ws/web/app.py >/dev/null
grep -F 'docker-ws workbench' assets/systemd/docker-ws-workbench.service >/dev/null
grep -F '__UV_EXECUTABLE__' assets/systemd/docker-ws-workbench.service >/dev/null

bash -n assets/docker/bin/start-vnc assets/docker/bin/verify-image \
  tests/helpers/fake-docker tests/helpers/fake-vncviewer tests/test-workstation.sh \
  tests/test-cli.sh tests/test-package-images.sh
python3 -m py_compile docker_ws/cli/main.py docker_ws/core/workstation.py docker_ws/core/images.py docker_ws/web/app.py
bash tests/test-workstation.sh
bash tests/test-cli.sh
bash tests/test-package-images.sh

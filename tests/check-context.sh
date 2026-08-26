#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

for file in .dockerignore pyproject.toml dependencies.txt scripts/bootstrap.sh \
  assets/images/android-ws/recipe.json \
  assets/images/android-ws/Dockerfile.android-ws-v1 \
  assets/images/android-ws/Dockerfile.android-ws-v2 \
  assets/systemd/docker-ws-workbench.service docker_ws/cli/main.py \
  docker_ws/core/workstation.py docker_ws/core/images.py docker_ws/core/recipes.py \
  docker_ws/core/image_builder.py docker_ws/core/image_verifier.py \
  docker_ws/core/workbench_deployment.py docker_ws/core/workbench_connection.py \
  docker_ws/web/app.py apps/workbench/package.json tests/python/test_recipes.py \
  tests/python/test_workbench_deployment.py tests/python/test_workbench_connection.py \
  tests/helpers/fake-bootstrap-command tests/test-bootstrap.sh \
  tests/test-workstation.sh tests/test-cli.sh tests/test-package-images.sh; do
  test -f "$file" || { echo "missing: $file" >&2; exit 1; }
done
for obsolete in Dockerfile cli core web workbench systemd; do
  test ! -e "$obsolete" || { echo "obsolete path remains: $obsolete" >&2; exit 1; }
done

test ! -e assets/docker || { echo 'obsolete asset path remains: assets/docker' >&2; exit 1; }
grep -F 'ARG CUDA_IMAGE=nvcr.io/nvidia/cuda:12.8.1-devel-ubuntu22.04' assets/images/android-ws/Dockerfile.android-ws-v2 >/dev/null
! grep -F 'docker.io/' assets/images/android-ws/Dockerfile.android-ws-v2 >/dev/null
grep -F 'FROM ${CUDA_IMAGE} AS core' assets/images/android-ws/Dockerfile.android-ws-v2 >/dev/null
grep -F 'FROM core AS desktop' assets/images/android-ws/Dockerfile.android-ws-v2 >/dev/null
! grep -F 'verify-image' assets/images/android-ws/Dockerfile.android-ws-v2 >/dev/null
! grep -F 'start-vnc' assets/images/android-ws/Dockerfile.android-ws-v2 >/dev/null
grep -F 'WORKDIR /workspace' assets/images/android-ws/Dockerfile.android-ws-v2 >/dev/null
grep -F 'Dockerfile.android-ws-v2' docker_ws/core/workstation.py >/dev/null
grep -F 'dst=/workspace' docker_ws/core/workstation.py >/dev/null
grep -F 'ROBOTICS_WS_WORKSPACE' docker_ws/core/workstation.py >/dev/null
grep -F -- '--workspace' docker_ws/cli/main.py >/dev/null
! grep -F -- '--code-root' docker_ws/cli/main.py >/dev/null
grep -F 'apps" / "workbench" / "dist' docker_ws/web/app.py >/dev/null
grep -F 'docker-ws workbench' assets/systemd/docker-ws-workbench.service >/dev/null
grep -F '__UV_EXECUTABLE__' assets/systemd/docker-ws-workbench.service >/dev/null
grep -F 'workbench serve' assets/systemd/docker-ws-workbench.service >/dev/null
grep -F '__WORKBENCH_CONFIG__' assets/systemd/docker-ws-workbench.service >/dev/null
grep -F '__WORKBENCH_PORT__' assets/systemd/docker-ws-workbench.service >/dev/null
grep -F 'run --frozen' assets/systemd/docker-ws-workbench.service >/dev/null
grep -F 'SuccessExitStatus=143' assets/systemd/docker-ws-workbench.service >/dev/null

bash -n scripts/bootstrap.sh tests/helpers/fake-bootstrap-command tests/helpers/fake-docker \
  tests/helpers/fake-vncviewer tests/test-bootstrap.sh tests/test-workstation.sh \
  tests/test-cli.sh tests/test-package-images.sh
python3 -m py_compile docker_ws/cli/main.py docker_ws/core/workstation.py docker_ws/core/images.py \
  docker_ws/core/recipes.py docker_ws/core/image_builder.py docker_ws/core/image_verifier.py \
  docker_ws/core/workbench_deployment.py docker_ws/core/workbench_connection.py docker_ws/web/app.py
bash tests/test-workstation.sh
bash tests/test-cli.sh
bash tests/test-package-images.sh
bash tests/test-bootstrap.sh

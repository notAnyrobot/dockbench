#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

for file in .dockerignore CONTEXT.md pyproject.toml dependencies.txt scripts/bootstrap.sh \
  assets/images/android-ws/recipe.json \
  assets/images/android-ws/Dockerfile.android-ws-v1 \
  assets/images/android-ws/Dockerfile.android-ws-v2 \
  assets/systemd/dockbench.service dockbench/cli/main.py \
  dockbench/core/workstation.py dockbench/core/images.py dockbench/core/recipes.py \
  dockbench/core/image_builder.py dockbench/core/image_verifier.py \
  dockbench/core/server_deployment.py dockbench/core/server_connection.py \
  dockbench/web/app.py apps/workbench/package.json tests/python/test_recipes.py \
  tests/python/test_server_deployment.py tests/python/test_server_connection.py \
  tests/helpers/fake-bootstrap-command tests/test-bootstrap.sh \
  tests/test-workstation.sh tests/test-cli.sh tests/test-package-images.sh; do
  test -f "$file" || { echo "missing: $file" >&2; exit 1; }
done
for obsolete in Dockerfile cli core web workbench systemd docker_ws assets/systemd/docker-ws-workbench.service; do
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
grep -F 'Dockerfile.android-ws-v2' dockbench/core/workstation.py >/dev/null
grep -F 'dst=/workspace/' dockbench/core/workstation.py >/dev/null
grep -F 'DOCKBENCH_CODE_ROOTS' dockbench/core/workstation.py >/dev/null
grep -F -- '--code-root' dockbench/cli/main.py >/dev/null
! grep -F -- '--workspace' dockbench/cli/main.py >/dev/null
grep -F 'apps" / "workbench" / "dist' dockbench/web/app.py >/dev/null
grep -F 'dockbench serve' assets/systemd/dockbench.service >/dev/null
grep -F '__UV_EXECUTABLE__' assets/systemd/dockbench.service >/dev/null
grep -F '__SERVER_CONFIG__' assets/systemd/dockbench.service >/dev/null
grep -F '__SERVER_PORT__' assets/systemd/dockbench.service >/dev/null
grep -F 'run --frozen' assets/systemd/dockbench.service >/dev/null
grep -F 'SuccessExitStatus=143' assets/systemd/dockbench.service >/dev/null
! grep -Eq 'add_parser\("(images|gpus|recipe|container|workbench|service)"' dockbench/cli/main.py

bash -n scripts/bootstrap.sh tests/helpers/fake-bootstrap-command tests/helpers/fake-docker \
  tests/helpers/fake-vncviewer tests/test-bootstrap.sh tests/test-workstation.sh \
  tests/test-cli.sh tests/test-package-images.sh
python3 -m py_compile dockbench/cli/main.py dockbench/core/workstation.py dockbench/core/images.py \
  dockbench/core/recipes.py dockbench/core/image_builder.py dockbench/core/image_verifier.py \
  dockbench/core/server_deployment.py dockbench/core/server_connection.py dockbench/web/app.py
bash tests/test-workstation.sh
bash tests/test-cli.sh
bash tests/test-package-images.sh
bash tests/test-bootstrap.sh

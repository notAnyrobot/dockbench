#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

for file in Dockerfile .dockerignore bin/launch-desktop bin/launch-headless bin/package-images bin/start-vnc bin/verify-image; do
  test -f "$file" || { echo "missing: $file" >&2; exit 1; }
done

grep -F 'FROM ${CUDA_IMAGE} AS core' Dockerfile >/dev/null
grep -F 'FROM core AS desktop' Dockerfile >/dev/null
grep -F 'nvidia/cuda:12.8.1-devel-ubuntu22.04' Dockerfile >/dev/null
grep -F 'ghcr.io/astral-sh/uv:${UV_VERSION}' Dockerfile >/dev/null
grep -F 'UV_VERSION=0.12.3' Dockerfile >/dev/null
grep -F 'MINIFORGE_VERSION=26.3.2-2' Dockerfile >/dev/null
grep -F 'huggingface-hub==${HF_HUB_VERSION}' Dockerfile >/dev/null
grep -F 'tigervnc-tools' Dockerfile >/dev/null

if grep -Eq 'COPY .*ProtoMotions|COPY .*mjlab|uv sync|pip install .*torch' Dockerfile; then
  echo 'project dependency found in base image' >&2
  exit 1
fi

bash -n bin/launch-desktop bin/launch-headless bin/package-images bin/start-vnc bin/verify-image \
  tests/test-launch-desktop.sh tests/test-launch-headless.sh
bash tests/test-launch-desktop.sh
bash tests/test-launch-headless.sh

temporary_home="$(mktemp -d)"
trap 'rm -rf "$temporary_home"' EXIT

if HOME="$temporary_home" bin/start-vnc >"$temporary_home/output" 2>&1; then
  echo 'start-vnc accepted a missing password' >&2
  exit 1
fi

grep -F "VNC password missing; run: vncpasswd ${temporary_home}/.vnc/passwd" \
  "$temporary_home/output" >/dev/null

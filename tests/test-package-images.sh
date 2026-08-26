#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT
fake_docker="$PWD/tests/helpers/fake-docker"
output_dir="$temporary_dir/images"

FAKE_DOCKER_LOG="$temporary_dir/docker.log" FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
  ROBOTICS_WS_DOCKER="$fake_docker" uv run --no-sync docker-ws image package "$output_dir" >"$temporary_dir/output"
test -f "$output_dir/android-ws-u22.04-cu12.8-v1.tar"
grep -F 'Created:' "$temporary_dir/output" >/dev/null

second_tarball="$temporary_dir/second-image.tar"
cp "$output_dir/android-ws-u22.04-cu12.8-v1.tar" "$second_tarball"
FAKE_DOCKER_LOG="$temporary_dir/docker.log" FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
  ROBOTICS_WS_DOCKER="$fake_docker" uv run --no-sync docker-ws image load "$output_dir/android-ws-u22.04-cu12.8-v1.tar" "$second_tarball"
test "$(grep -Fc '<|load|--input|' "$temporary_dir/docker.log")" = 2

#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT

uv run --no-sync docker-ws --help >"$temporary_dir/help"
grep -F 'Manage the Docker Workstation.' "$temporary_dir/help" >/dev/null
grep -F 'image' "$temporary_dir/help" >/dev/null
grep -F 'workbench' "$temporary_dir/help" >/dev/null

uv run --no-sync docker-ws workbench help >"$temporary_dir/workbench-help"
grep -F 'deploy' "$temporary_dir/workbench-help" >/dev/null
grep -F 'connect' "$temporary_dir/workbench-help" >/dev/null
grep -F 'serve' "$temporary_dir/workbench-help" >/dev/null
grep -F 'start' "$temporary_dir/workbench-help" >/dev/null

uv run --no-sync docker-ws image recipe list >"$temporary_dir/recipes"
grep -F $'android-ws\tv1\tandroid-ws:u22.04-cu12.8-v1' "$temporary_dir/recipes" >/dev/null

FAKE_DOCKER_LOG="$temporary_dir/docker.log" FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
  ROBOTICS_WS_DOCKER="$PWD/tests/helpers/fake-docker" \
  uv run --no-sync docker-ws image verify docker-ws:test-desktop >"$temporary_dir/verify"
grep -Fx 'docker-ws:test-desktop: verified (shell, desktop-v1)' "$temporary_dir/verify" >/dev/null

for invocation in '' 'unknown' 'start extra' 'image' 'service'; do
  if uv run --no-sync docker-ws $invocation >"$temporary_dir/output" 2>&1; then
    echo "accepted invalid invocation: $invocation" >&2
    exit 1
  else
    status=$?
  fi
  test "$status" = 2
done

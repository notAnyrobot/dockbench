#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT

uv run --no-sync dockbench --help >"$temporary_dir/help"
grep -F 'Manage Dockbench.' "$temporary_dir/help" >/dev/null
grep -F 'image' "$temporary_dir/help" >/dev/null
grep -F 'server' "$temporary_dir/help" >/dev/null

uv run --no-sync dockbench server --help >"$temporary_dir/server-help"
grep -F 'start' "$temporary_dir/server-help" >/dev/null
grep -F 'status' "$temporary_dir/server-help" >/dev/null
grep -F 'stop' "$temporary_dir/server-help" >/dev/null

uv run --no-sync dockbench deploy --help >"$temporary_dir/deploy-help"
grep -F -- '--workspace' "$temporary_dir/deploy-help" >/dev/null
! grep -F -- '--code-root' "$temporary_dir/deploy-help" >/dev/null

FAKE_DOCKER_LOG="$temporary_dir/docker.log" FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
  DOCKBENCH_DOCKER="$PWD/tests/helpers/fake-docker" \
  uv run --no-sync dockbench image verify dockbench:test-desktop >"$temporary_dir/verify"
grep -Fx 'dockbench:test-desktop: verified (shell, desktop-v1)' "$temporary_dir/verify" >/dev/null

uv run --no-sync dockbench >"$temporary_dir/bare"
grep -F 'Manage Dockbench.' "$temporary_dir/bare" >/dev/null

for invocation in 'unknown' 'start extra' 'image' 'container' 'workbench' 'service' 'gpus' 'images'; do
  if uv run --no-sync dockbench $invocation >"$temporary_dir/output" 2>&1; then
    echo "accepted invalid invocation: $invocation" >&2
    exit 1
  else
    status=$?
  fi
  test "$status" = 2
done

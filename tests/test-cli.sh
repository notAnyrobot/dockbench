#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT

uv run --no-sync docker-ws --help >"$temporary_dir/help"
grep -F 'Manage the Docker Workstation.' "$temporary_dir/help" >/dev/null
grep -F 'image' "$temporary_dir/help" >/dev/null
grep -F 'workbench' "$temporary_dir/help" >/dev/null

for invocation in '' 'unknown' 'start extra' 'image' 'service'; do
  if uv run --no-sync docker-ws $invocation >"$temporary_dir/output" 2>&1; then
    echo "accepted invalid invocation: $invocation" >&2
    exit 1
  else
    status=$?
  fi
  test "$status" = 2
done

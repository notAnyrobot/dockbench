#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT

fake_docker="$PWD/tests/helpers/fake-docker"
fake_vncviewer="$PWD/tests/helpers/fake-vncviewer"
android_root="$temporary_dir/android-ws"
github_root="$temporary_dir/GitHub"
state_root="$temporary_dir/.dockbench"
vnc_password_file="$state_root/home/.vnc/passwd"
mkdir -p "$android_root" "$github_root"

run_ws() {
  FAKE_DOCKER_LOG="$temporary_dir/docker.log" FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
  FAKE_LAUNCH_SPEC="$temporary_dir/launch-spec" FAKE_VNC_PASSWORD="$vnc_password_file" \
  FAKE_VNC_RUNNING="$temporary_dir/vnc-running" DOCKBENCH_DOCKER="$fake_docker" \
  DOCKBENCH_CODE_ROOTS="{\"android-ws\":\"$android_root\",\"GitHub\":\"$github_root\"}" DOCKBENCH_STATE_ROOT="$state_root" \
  DOCKBENCH_SHM_SIZE=8g DOCKBENCH_HOST_UID=1234 DOCKBENCH_HOST_GID=5678 \
  DOCKBENCH_HOST_USER=test-user uv run --no-sync dockbench "$@"
}

: >"$temporary_dir/docker.log"
run_ws image build >"$temporary_dir/output"
grep -Fx 'android-ws:u22.04-cu12.8-v2: image built from android-ws revision 2' "$temporary_dir/output" >/dev/null
grep -F -- '<|buildx|build|--progress=plain|--platform|linux/amd64|--file|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '/assets/images/android-ws/Dockerfile.android-ws-v2|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '|--target|desktop|--load|--tag|android-ws:u22.04-cu12.8-v2|' "$temporary_dir/docker.log" >/dev/null

: >"$temporary_dir/docker.log"
run_ws status >"$temporary_dir/output"
grep -Fx 'dockbench: absent' "$temporary_dir/output" >/dev/null

: >"$temporary_dir/docker.log"
DOCKBENCH_IMAGE=dockbench:test-desktop DOCKBENCH_CONTAINER=test-container run_ws start --gpu none >"$temporary_dir/output"
grep -Fx 'test-container: running' "$temporary_dir/output" >/dev/null
grep -F -- '|--name|test-container|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '|--user|root|' "$temporary_dir/docker.log" >/dev/null
grep -F -- "src=$android_root,dst=/workspace/android-ws" "$temporary_dir/docker.log" >/dev/null
grep -F -- "src=$github_root,dst=/workspace/GitHub" "$temporary_dir/docker.log" >/dev/null
grep -F -- "src=$state_root,dst=/state" "$temporary_dir/docker.log" >/dev/null
! test -e "$vnc_password_file"

: >"$temporary_dir/docker.log"
DOCKBENCH_IMAGE=dockbench:test-desktop DOCKBENCH_CONTAINER=test-container run_ws shell
grep -F '<|exec|-it|--user|1234:5678|--workdir|/workspace|' "$temporary_dir/docker.log" >/dev/null

DOCKBENCH_IMAGE=dockbench:test-desktop DOCKBENCH_CONTAINER=test-container \
  DOCKBENCH_VNC_PASSWORD=test-password DOCKBENCH_VNC_VIEWER="$fake_vncviewer" run_ws desktop
test -e "$vnc_password_file"
test -e "$temporary_dir/vnc-running"

DOCKBENCH_CONTAINER=test-container run_ws stop >"$temporary_dir/output"
grep -Fx 'test-container: stopped' "$temporary_dir/output" >/dev/null
DOCKBENCH_CONTAINER=test-container run_ws status >"$temporary_dir/output"
grep -Fx 'test-container: stopped' "$temporary_dir/output" >/dev/null

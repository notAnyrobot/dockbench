#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT

fake_docker="$PWD/tests/helpers/fake-docker"
fake_vncviewer="$PWD/tests/helpers/fake-vncviewer"
code_root="$temporary_dir/Code"
state_root="$temporary_dir/.robotics-ws"
vnc_password_file="$state_root/home/.vnc/passwd"
mkdir -p "$code_root"
launch_config="$code_root|$state_root|8g|1234|5678|rootful|5901"

run_ws() {
  FAKE_DOCKER_LOG="$temporary_dir/docker.log" FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
  FAKE_LAUNCH_CONFIG="$launch_config" FAKE_VNC_PASSWORD="$vnc_password_file" \
  FAKE_VNC_RUNNING="$temporary_dir/vnc-running" ROBOTICS_WS_DOCKER="$fake_docker" \
  ROBOTICS_WS_CODE_ROOT="$code_root" ROBOTICS_WS_STATE_ROOT="$state_root" \
  ROBOTICS_WS_SHM_SIZE=8g ROBOTICS_WS_HOST_UID=1234 ROBOTICS_WS_HOST_GID=5678 \
  ROBOTICS_WS_HOST_USER=test-user uv run --no-sync docker-ws "$@"
}

: >"$temporary_dir/docker.log"
run_ws image build >"$temporary_dir/output"
grep -Fx 'android-ws:u22.04-cu12.8-v1: image built from android-ws revision 1' "$temporary_dir/output" >/dev/null
grep -F -- '<|buildx|build|--platform|linux/amd64|--file|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '/assets/images/android-ws/Dockerfile.android-ws-v1|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '|--target|desktop|--load|--tag|android-ws:u22.04-cu12.8-v1|' "$temporary_dir/docker.log" >/dev/null

: >"$temporary_dir/docker.log"
run_ws container status >"$temporary_dir/output"
grep -Fx 'docker-ws: absent' "$temporary_dir/output" >/dev/null

: >"$temporary_dir/docker.log"
ROBOTICS_WS_DESKTOP_IMAGE=docker-ws:test-desktop ROBOTICS_WS_DESKTOP_NAME=test-workstation run_ws container start --gpu none >"$temporary_dir/output"
grep -Fx 'test-workstation: running' "$temporary_dir/output" >/dev/null
grep -F -- '|--name|test-workstation|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '|--user|root|' "$temporary_dir/docker.log" >/dev/null
grep -F -- "src=$code_root,dst=/workspace" "$temporary_dir/docker.log" >/dev/null
grep -F -- "src=$state_root,dst=/state" "$temporary_dir/docker.log" >/dev/null
! test -e "$vnc_password_file"

: >"$temporary_dir/docker.log"
ROBOTICS_WS_DESKTOP_IMAGE=docker-ws:test-desktop ROBOTICS_WS_DESKTOP_NAME=test-workstation run_ws container enter
grep -F '<|exec|-it|--user|1234:5678|--workdir|/workspace|' "$temporary_dir/docker.log" >/dev/null

ROBOTICS_WS_DESKTOP_IMAGE=docker-ws:test-desktop ROBOTICS_WS_DESKTOP_NAME=test-workstation \
  ROBOTICS_WS_VNC_PASSWORD=test-password ROBOTICS_WS_VNCVIEWER="$fake_vncviewer" run_ws container vnc
test -e "$vnc_password_file"
test -e "$temporary_dir/vnc-running"

ROBOTICS_WS_DESKTOP_NAME=test-workstation run_ws container stop >"$temporary_dir/output"
grep -Fx 'test-workstation: stopped' "$temporary_dir/output" >/dev/null
ROBOTICS_WS_DESKTOP_NAME=test-workstation run_ws container status >"$temporary_dir/output"
grep -Fx 'test-workstation: stopped' "$temporary_dir/output" >/dev/null

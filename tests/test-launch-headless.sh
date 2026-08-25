#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT

fake_docker="$temporary_dir/docker"
cat >"$fake_docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf '<' >>"$FAKE_DOCKER_LOG"
printf '|%s' "$@" >>"$FAKE_DOCKER_LOG"
printf '|>\n' >>"$FAKE_DOCKER_LOG"

case "${1:-} ${2:-}" in
  "info --format")
    if test "${FAKE_ROOTLESS:-0}" = 1; then
      printf '%s\n' '["name=rootless"]'
    else
      printf '%s\n' '[]'
    fi
    ;;
  "container inspect")
    test -f "$FAKE_DOCKER_STATE" || exit 1
    if [[ "$*" == *'{{.Image}}'* ]]; then
      printf '%s\n' "${FAKE_CONTAINER_IMAGE_ID:-sha256:test-image}"
    elif [[ "$*" == *'robotics-ws.launch-config'* ]]; then
      printf '%s\n' "${FAKE_LAUNCH_CONFIG:-}"
    else
      cat "$FAKE_DOCKER_STATE"
    fi
    ;;
  "image inspect")
    printf '%s\n' "${FAKE_IMAGE_ID:-sha256:test-image}"
    ;;
  "run -d")
    printf '%s\n' 'running' >"$FAKE_DOCKER_STATE"
    printf '%s\n' 'test-container-id'
    ;;
  "start test-headless")
    printf '%s\n' 'running' >"$FAKE_DOCKER_STATE"
    printf '%s\n' 'test-headless'
    ;;
  "stop test-headless")
    printf '%s\n' 'exited' >"$FAKE_DOCKER_STATE"
    printf '%s\n' 'test-headless'
    ;;
  "exec -i")
    cat >/dev/null
    ;;
  "exec -it")
    ;;
  *)
    printf 'unexpected fake docker command: %s\n' "$*" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$fake_docker"

code_root="$temporary_dir/Code"
state_root="$temporary_dir/.robotics-ws"
mkdir -p "$code_root"
export ROBOTICS_WS_SHM_SIZE='8g'
export ROBOTICS_WS_HOST_UID='1234'
export ROBOTICS_WS_HOST_GID='5678'
export ROBOTICS_WS_HOST_USER='test-user'

FAKE_DOCKER_LOG="$temporary_dir/docker.log" \
FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
ROBOTICS_WS_DOCKER="$fake_docker" \
ROBOTICS_WS_CODE_ROOT="$code_root" \
ROBOTICS_WS_STATE_ROOT="$state_root" \
ROBOTICS_WS_HEADLESS_IMAGE='robotics-ws:test-headless' \
ROBOTICS_WS_HEADLESS_NAME='test-headless' \
  bin/launch-headless --start >"$temporary_dir/output"

test "$(cat "$temporary_dir/docker.state")" = running
grep -F 'test-headless: created and running' "$temporary_dir/output" >/dev/null
grep -F -- '|--name|test-headless|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '|--hostname|test-headless|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '|--gpus|all|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '|--shm-size|8g|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '|--restart|unless-stopped|' "$temporary_dir/docker.log" >/dev/null
grep -F -- "src=$code_root,dst=/Code" "$temporary_dir/docker.log" >/dev/null
grep -F -- "src=$state_root,dst=/state" "$temporary_dir/docker.log" >/dev/null
grep -F 'robotics-ws:test-headless' "$temporary_dir/docker.log" >/dev/null

launch_config="$code_root|$state_root|8g|1234|5678|rootful"
export FAKE_LAUNCH_CONFIG="$launch_config"

printf '%s\n' 'exited' >"$temporary_dir/docker.state"
: >"$temporary_dir/docker.log"

FAKE_DOCKER_LOG="$temporary_dir/docker.log" \
FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
ROBOTICS_WS_DOCKER="$fake_docker" \
ROBOTICS_WS_CODE_ROOT="$code_root" \
ROBOTICS_WS_STATE_ROOT="$state_root" \
ROBOTICS_WS_HEADLESS_IMAGE='robotics-ws:test-headless' \
ROBOTICS_WS_HEADLESS_NAME='test-headless' \
  bin/launch-headless --start >"$temporary_dir/output"

test "$(cat "$temporary_dir/docker.state")" = running
grep -F 'test-headless: started' "$temporary_dir/output" >/dev/null
grep -F '<|start|test-headless|>' "$temporary_dir/docker.log" >/dev/null
if grep -F '<|run|-d|' "$temporary_dir/docker.log" >/dev/null; then
  echo '--start recreated an existing stopped container' >&2
  exit 1
fi

: >"$temporary_dir/docker.log"

FAKE_DOCKER_LOG="$temporary_dir/docker.log" \
FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
ROBOTICS_WS_DOCKER="$fake_docker" \
ROBOTICS_WS_CODE_ROOT="$code_root" \
ROBOTICS_WS_STATE_ROOT="$state_root" \
ROBOTICS_WS_HEADLESS_NAME='test-headless' \
ROBOTICS_WS_HOST_UID='1234' \
ROBOTICS_WS_HOST_GID='5678' \
ROBOTICS_WS_HOST_USER='test-user' \
  bin/launch-headless --enter

grep -F '<|exec|-it|--user|1234:5678|--workdir|/Code|' \
  "$temporary_dir/docker.log" >/dev/null
grep -F '|--env|HOME=/state/home|' "$temporary_dir/docker.log" >/dev/null
grep -F '|--env|USER=test-user|' "$temporary_dir/docker.log" >/dev/null
grep -F '|test-headless|/bin/bash|--rcfile|/state/.robotics-ws-bashrc|>' \
  "$temporary_dir/docker.log" >/dev/null
test "$(cat "$temporary_dir/docker.state")" = running

FAKE_DOCKER_LOG="$temporary_dir/docker.log" \
FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
ROBOTICS_WS_DOCKER="$fake_docker" \
ROBOTICS_WS_CODE_ROOT="$code_root" \
ROBOTICS_WS_STATE_ROOT="$state_root" \
ROBOTICS_WS_HEADLESS_NAME='test-headless' \
  bin/launch-headless --stop >"$temporary_dir/output"

test "$(cat "$temporary_dir/docker.state")" = exited
grep -F 'test-headless: stopped' "$temporary_dir/output" >/dev/null

FAKE_DOCKER_LOG="$temporary_dir/docker.log" \
FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
ROBOTICS_WS_DOCKER="$fake_docker" \
ROBOTICS_WS_CODE_ROOT="$code_root" \
ROBOTICS_WS_STATE_ROOT="$state_root" \
ROBOTICS_WS_HEADLESS_NAME='test-headless' \
  bin/launch-headless --status >"$temporary_dir/output"
grep -Fx 'test-headless: stopped' "$temporary_dir/output" >/dev/null

rm "$temporary_dir/docker.state"
FAKE_DOCKER_LOG="$temporary_dir/docker.log" \
FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
ROBOTICS_WS_DOCKER="$fake_docker" \
ROBOTICS_WS_CODE_ROOT="$code_root" \
ROBOTICS_WS_STATE_ROOT="$state_root" \
ROBOTICS_WS_HEADLESS_NAME='test-headless' \
  bin/launch-headless --status >"$temporary_dir/output"
grep -Fx 'test-headless: absent' "$temporary_dir/output" >/dev/null

if ROBOTICS_WS_DOCKER="$fake_docker" bin/launch-headless --start --enter \
    >"$temporary_dir/output" 2>&1; then
  echo 'launcher accepted conflicting commands' >&2
  exit 1
fi
grep -F 'exactly one command flag is required' "$temporary_dir/output" >/dev/null

printf '%s\n' 'running' >"$temporary_dir/docker.state"
if FAKE_DOCKER_LOG="$temporary_dir/docker.log" \
    FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
    FAKE_CONTAINER_IMAGE_ID='sha256:old-image' \
    FAKE_IMAGE_ID='sha256:new-image' \
    ROBOTICS_WS_DOCKER="$fake_docker" \
    ROBOTICS_WS_CODE_ROOT="$code_root" \
    ROBOTICS_WS_STATE_ROOT="$state_root" \
    ROBOTICS_WS_HEADLESS_IMAGE='robotics-ws:test-headless' \
    ROBOTICS_WS_HEADLESS_NAME='test-headless' \
    bin/launch-headless --start >"$temporary_dir/output" 2>&1; then
  echo 'launcher accepted a container created from a stale image' >&2
  exit 1
fi
grep -F 'remove and recreate it' "$temporary_dir/output" >/dev/null

rootless_state="$temporary_dir/rootless.state"
rootless_log="$temporary_dir/rootless.log"
printf '%s\n' 'running' >"$rootless_state"
FAKE_ROOTLESS=1 \
FAKE_DOCKER_LOG="$rootless_log" \
FAKE_DOCKER_STATE="$rootless_state" \
FAKE_LAUNCH_CONFIG="$code_root|$state_root|8g|1234|5678|rootless" \
ROBOTICS_WS_DOCKER="$fake_docker" \
ROBOTICS_WS_CODE_ROOT="$code_root" \
ROBOTICS_WS_STATE_ROOT="$state_root" \
ROBOTICS_WS_HEADLESS_IMAGE='robotics-ws:test-headless' \
ROBOTICS_WS_HEADLESS_NAME='test-headless' \
  bin/launch-headless --enter

grep -F '<|exec|-it|--user|0:0|--workdir|/Code|' "$rootless_log" >/dev/null

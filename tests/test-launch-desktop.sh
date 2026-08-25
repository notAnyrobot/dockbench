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
    printf '%s\n' '[]'
    ;;
  "container inspect")
    test -f "$FAKE_DOCKER_STATE" || exit 1
    if [[ "$*" == *'{{.Image}}'* ]]; then
      printf '%s\n' 'sha256:test-image'
    elif [[ "$*" == *'robotics-ws.launch-config'* ]]; then
      printf '%s\n' "$FAKE_LAUNCH_CONFIG"
    else
      cat "$FAKE_DOCKER_STATE"
    fi
    ;;
  "image inspect")
    printf '%s\n' 'sha256:test-image'
    ;;
  "run -d")
    printf '%s\n' 'running' >"$FAKE_DOCKER_STATE"
    printf '%s\n' 'test-container-id'
    ;;
  "start test-desktop")
    printf '%s\n' 'running' >"$FAKE_DOCKER_STATE"
    ;;
  "stop test-desktop")
    printf '%s\n' 'exited' >"$FAKE_DOCKER_STATE"
    ;;
  "exec -i")
    cat >/dev/null
    ;;
  "exec -it")
    if [[ "$*" == *'vncpasswd'* ]]; then
      : >"$FAKE_VNC_PASSWORD"
    fi
    ;;
  "exec -d")
    ;;
  "exec --user")
    if [[ "$*" == *'test -s /state/home/.vnc/passwd'* ]] && test ! -e "$FAKE_VNC_PASSWORD"; then
      exit 1
    fi
    if [[ "$*" == *'vncserver -list'* ]] && test ! -e "$FAKE_VNC_RUNNING"; then
      exit 1
    fi
    if [[ "$*" == *'vncserver -list'* ]] && test "${FAKE_VNC_SESSION:-live}" = stale; then
      [[ "$*" == *'grep -Fv stale'* ]] && exit 1
    fi
    ;;
  *)
    printf 'unexpected fake docker command: %s\n' "$*" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$fake_docker"

fake_vncviewer="$temporary_dir/vncviewer"
cat >"$fake_vncviewer" <<'EOF'
#!/usr/bin/env bash
printf '<vncviewer|%s|>\n' "$*" >>"$FAKE_DOCKER_LOG"
EOF
chmod +x "$fake_vncviewer"

code_root="$temporary_dir/Code"
state_root="$temporary_dir/.robotics-ws"
mkdir -p "$code_root"

export ROBOTICS_WS_HOST_UID='1234'
export ROBOTICS_WS_HOST_GID='5678'
export ROBOTICS_WS_HOST_USER='test-user'
launch_config="$code_root|$state_root|8g|1234|5678|rootful|5901"

FAKE_DOCKER_LOG="$temporary_dir/docker.log" \
FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
FAKE_LAUNCH_CONFIG="$launch_config" \
FAKE_VNC_PASSWORD="$temporary_dir/vnc-password" \
FAKE_VNC_RUNNING="$temporary_dir/vnc-running" \
ROBOTICS_WS_DOCKER="$fake_docker" \
ROBOTICS_WS_CODE_ROOT="$code_root" \
ROBOTICS_WS_STATE_ROOT="$state_root" \
ROBOTICS_WS_DESKTOP_IMAGE='robotics-ws:test-desktop' \
ROBOTICS_WS_DESKTOP_NAME='test-desktop' \
ROBOTICS_WS_SHM_SIZE='8g' \
ROBOTICS_WS_VNC_PASSWORD='test-password' \
  bin/launch-desktop --start >"$temporary_dir/output"

test "$(cat "$temporary_dir/docker.state")" = running
grep -F 'test-desktop: created and VNC started' "$temporary_dir/output" >/dev/null
grep -F -- '|--name|test-desktop|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '|--hostname|test-desktop|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '|--gpus|all|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '|--shm-size|8g|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '|--restart|unless-stopped|' "$temporary_dir/docker.log" >/dev/null
grep -F -- '|-p|127.0.0.1:5901:5901|' "$temporary_dir/docker.log" >/dev/null
grep -F -- "src=$code_root,dst=/Code" "$temporary_dir/docker.log" >/dev/null
grep -F -- "src=$state_root,dst=/state" "$temporary_dir/docker.log" >/dev/null
grep -F '|start-vnc|>' "$temporary_dir/docker.log" >/dev/null
grep -F '|exec|-i|--user|1234:5678|test-desktop|/bin/bash|-c|vncpasswd -f > /state/home/.vnc/passwd && chmod 600 /state/home/.vnc/passwd|>' "$temporary_dir/docker.log" >/dev/null

printf '%s\n' 'running' >"$temporary_dir/docker.state"
FAKE_DOCKER_LOG="$temporary_dir/docker.log" \
FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
FAKE_LAUNCH_CONFIG="$launch_config" \
FAKE_VNC_PASSWORD="$temporary_dir/vnc-password" \
FAKE_VNC_RUNNING="$temporary_dir/vnc-running" \
ROBOTICS_WS_DOCKER="$fake_docker" \
ROBOTICS_WS_CODE_ROOT="$code_root" \
ROBOTICS_WS_STATE_ROOT="$state_root" \
ROBOTICS_WS_DESKTOP_IMAGE='robotics-ws:test-desktop' \
ROBOTICS_WS_DESKTOP_NAME='test-desktop' \
ROBOTICS_WS_SHM_SIZE='8g' \
  bin/launch-desktop --enter
grep -F '<|exec|-it|--user|1234:5678|--workdir|/Code|' "$temporary_dir/docker.log" >/dev/null
grep -F '|--rcfile|/state/.robotics-ws-bashrc|>' "$temporary_dir/docker.log" >/dev/null

FAKE_DOCKER_LOG="$temporary_dir/docker.log" \
FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
FAKE_LAUNCH_CONFIG="$launch_config" \
FAKE_VNC_PASSWORD="$temporary_dir/vnc-password" \
FAKE_VNC_RUNNING="$temporary_dir/vnc-running" \
FAKE_VNCVIEWER="$fake_vncviewer" \
ROBOTICS_WS_DOCKER="$fake_docker" \
ROBOTICS_WS_CODE_ROOT="$code_root" \
ROBOTICS_WS_STATE_ROOT="$state_root" \
ROBOTICS_WS_DESKTOP_NAME='test-desktop' \
ROBOTICS_WS_VNCVIEWER="$fake_vncviewer" \
  bin/launch-desktop --vnc
grep -F '<vncviewer|127.0.0.1:5901|>' "$temporary_dir/docker.log" >/dev/null

: >"$temporary_dir/vnc-password"
: >"$temporary_dir/vnc-running"
: >"$temporary_dir/docker.log"
FAKE_DOCKER_LOG="$temporary_dir/docker.log" \
FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
FAKE_LAUNCH_CONFIG="$launch_config" \
FAKE_VNC_PASSWORD="$temporary_dir/vnc-password" \
FAKE_VNC_RUNNING="$temporary_dir/vnc-running" \
FAKE_VNC_SESSION='stale' \
ROBOTICS_WS_DOCKER="$fake_docker" \
ROBOTICS_WS_CODE_ROOT="$code_root" \
ROBOTICS_WS_STATE_ROOT="$state_root" \
ROBOTICS_WS_DESKTOP_IMAGE='robotics-ws:test-desktop' \
ROBOTICS_WS_DESKTOP_NAME='test-desktop' \
ROBOTICS_WS_SHM_SIZE='8g' \
  bin/launch-desktop --start >"$temporary_dir/output"

if ! grep -F '|start-vnc|>' "$temporary_dir/docker.log" >/dev/null; then
  echo '--start did not replace a stale VNC session' >&2
  exit 1
fi

FAKE_DOCKER_LOG="$temporary_dir/docker.log" \
FAKE_DOCKER_STATE="$temporary_dir/docker.state" \
FAKE_LAUNCH_CONFIG="$launch_config" \
ROBOTICS_WS_DOCKER="$fake_docker" \
ROBOTICS_WS_CODE_ROOT="$code_root" \
ROBOTICS_WS_STATE_ROOT="$state_root" \
ROBOTICS_WS_DESKTOP_NAME='test-desktop' \
  bin/launch-desktop --stop >"$temporary_dir/output"
test "$(cat "$temporary_dir/docker.state")" = exited
grep -Fx 'test-desktop: stopped' "$temporary_dir/output" >/dev/null

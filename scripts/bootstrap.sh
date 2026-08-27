#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd -- "$script_dir/.." && pwd)"
dependency_file="$repository_root/dependencies.txt"

uv_version="${DOCKBENCH_UV_VERSION:-0.12.6}"
nvm_version="${DOCKBENCH_NVM_VERSION:-v0.40.6}"
node_version="${DOCKBENCH_NODE_VERSION:-22}"
profile_file="${DOCKBENCH_PROFILE:-$HOME/.bashrc}"

temporary_dir=""
cleanup() {
  if [[ -n "$temporary_dir" && -d "$temporary_dir" ]]; then
    rm -rf -- "$temporary_dir"
  fi
}
trap cleanup EXIT

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_bootstrap_commands() {
  [[ -f "$dependency_file" ]] || fail "dependency inventory not found: $dependency_file"
  local command scope requirement purpose
  local missing=()
  while IFS='|' read -r command scope requirement purpose; do
    [[ -z "$command" || "$command" == \#* ]] && continue
    if [[ "$scope" == "bootstrap" && "$requirement" == "required" ]] && ! command -v "$command" >/dev/null 2>&1; then
      missing+=("$command")
    fi
  done < "$dependency_file"
  ((${#missing[@]} == 0)) || fail "install the required host commands first: ${missing[*]}"
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    printf 'uv already installed: %s\n' "$(uv --version)"
    return
  fi
  printf 'Installing uv %s...\n' "$uv_version"
  curl -LsSf "https://astral.sh/uv/$uv_version/install.sh" -o "$temporary_dir/uv-install.sh"
  UV_INSTALL_DIR="$HOME/.local/bin" sh "$temporary_dir/uv-install.sh"
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || fail "uv installed but is not available on PATH; add $HOME/.local/bin"
}

set_nvm_dir() {
  if [[ -n "${NVM_DIR:-}" ]]; then
    return
  fi
  if [[ -n "${XDG_CONFIG_HOME:-}" ]]; then
    NVM_DIR="$XDG_CONFIG_HOME/nvm"
  else
    NVM_DIR="$HOME/.nvm"
  fi
  export NVM_DIR
}

load_nvm() {
  set_nvm_dir
  if [[ -s "$NVM_DIR/nvm.sh" ]]; then
    # nvm is intentionally a shell function, not an executable.
    # shellcheck source=/dev/null
    . "$NVM_DIR/nvm.sh"
  fi
}

install_node() {
  load_nvm
  if ! command -v nvm >/dev/null 2>&1; then
    printf 'Installing nvm %s...\n' "$nvm_version"
    curl -LsSf "https://raw.githubusercontent.com/nvm-sh/nvm/$nvm_version/install.sh" -o "$temporary_dir/nvm-install.sh"
    PROFILE="$profile_file" NVM_DIR="$NVM_DIR" bash "$temporary_dir/nvm-install.sh"
    load_nvm
  else
    printf 'nvm already installed: %s\n' "$(nvm --version)"
  fi
  command -v nvm >/dev/null 2>&1 || fail "nvm installation did not provide the nvm shell function"

  nvm install "$node_version"
  nvm alias default "$node_version" >/dev/null
  nvm use "$node_version" >/dev/null
  command -v node >/dev/null 2>&1 || fail "Node.js installation failed"
  command -v npm >/dev/null 2>&1 || fail "npm installation failed"
}

check_external_host_tools() {
  if ! command -v docker >/dev/null 2>&1; then
    fail "Docker is required on the remote host but is not installed; choose the host's approved rootful or rootless Docker setup"
  fi
  if ! docker version >/dev/null 2>&1; then
    local current_user="${USER:-$(id -un)}"
    fail "Docker is installed but unavailable to $current_user; start Docker or configure access to its socket"
  fi
}

main() {
  [[ "$(uname -s)" == "Linux" ]] || fail "this bootstrap currently supports Linux hosts"
  require_bootstrap_commands
  temporary_dir="$(mktemp -d)"
  install_uv
  install_node
  check_external_host_tools

  printf '\nDockbench prerequisites are ready.\n'
  printf '  %s\n' "$(uv --version)" "node $(node --version)" "npm $(npm --version)" "docker available"
  printf '\nDeploy Dockbench with:\n  uv run dockbench deploy\n'
  printf 'Open a new shell (or source %s) if uv or nvm is not yet on its PATH.\n' "$profile_file"
}

main "$@"

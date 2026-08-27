#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT
fake_home="$temporary_dir/home"
fake_bin="$temporary_dir/bin"
mkdir -p "$fake_home/.nvm" "$fake_bin"

for command in curl git docker uv node npm; do
  cp tests/helpers/fake-bootstrap-command "$fake_bin/$command"
done

cat > "$fake_home/.nvm/nvm.sh" <<'EOF'
nvm() {
  printf '%s\n' "$*" >> "$FAKE_NVM_LOG"
  case "$1" in
    --version) printf '0.40.6\n' ;;
    install|alias|use) return 0 ;;
    *) return 1 ;;
  esac
}
EOF

FAKE_NVM_LOG="$temporary_dir/nvm.log" HOME="$fake_home" PATH="$fake_bin:/usr/bin:/bin" \
  bash scripts/bootstrap.sh > "$temporary_dir/output"

grep -F 'uv already installed: uv 0.12.6' "$temporary_dir/output" >/dev/null
grep -F 'nvm already installed: 0.40.6' "$temporary_dir/output" >/dev/null
grep -F 'Dockbench prerequisites are ready.' "$temporary_dir/output" >/dev/null
grep -F 'uv run dockbench deploy' "$temporary_dir/output" >/dev/null
grep -Fx 'install 22' "$temporary_dir/nvm.log" >/dev/null
grep -Fx 'alias default 22' "$temporary_dir/nvm.log" >/dev/null
grep -Fx 'use 22' "$temporary_dir/nvm.log" >/dev/null

if FAKE_DOCKER_UNAVAILABLE=1 FAKE_NVM_LOG="$temporary_dir/nvm-unavailable.log" \
  HOME="$fake_home" PATH="$fake_bin:/usr/bin:/bin" \
  bash scripts/bootstrap.sh > "$temporary_dir/unavailable-output" 2>&1; then
  echo 'bootstrap accepted an unavailable Docker daemon' >&2
  exit 1
fi
grep -F 'Docker is installed but unavailable' "$temporary_dir/unavailable-output" >/dev/null

#!/usr/bin/env bash
# Offline tests for the ServeGuard playbooks: no cloud, no cluster, no login.
#   tests/serveguard/run.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
cd "$repo"
# The test inventory has no vault; do not require .vault_pass.
export ANSIBLE_CONFIG="$here/ansible.cfg"
export ANSIBLE_ROLES_PATH="$repo/roles"
log="${TMPDIR:-/tmp}/serveguard-tests.log"
run() {
  if ansible-playbook -i "$here/inventory" "$here/$1" -e versions_file="$repo/versions.yml" "${@:2}" >"$log" 2>&1; then
    echo "PASS $1: $(grep -oE 'ok=[0-9]+ +changed=[0-9]+ +unreachable=0 +failed=0' "$log" | head -1)$(grep -oE '[0-9]+ decision cases passed' "$log" | sed 's/^/, /')"
  else
    echo "FAIL $1 (full log: $log)"; grep -E -A30 'fatal:|ERROR' "$log" | head -60; exit 1
  fi
}
run test_checks.yml
run test_preflight.yml
run test_identity.yml
run test_stories.yml -e serveguard_repo_dir="$here/stub" -e serveguard_log_dir="${TMPDIR:-/tmp}/serveguard-test-logs"
echo "== syntax-check"
# With the repository's own ansible.cfg and inventory, as a user runs them.
for p in playbooks/*.yml; do
  ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook --syntax-check -e @scenarios/serveguard/cpu.yml "$p" >"$log" 2>&1 \
    || { echo "FAIL syntax-check $p"; cat "$log"; exit 1; }
done
echo "all playbooks pass --syntax-check"
